"""Telegram control plane for the Douyin Cloud Run video pipeline.

The service uses only Python's standard library. Series state is stored in GCS,
so /next continues from the exact source timestamp even when the user's PC is
off or the Cloud Run instance is restarted.
"""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


DOUYIN_HOSTS = ("douyin.com", "iesdouyin.com")
URL_TOKEN_RE = re.compile(r"!?https?://[^\s<>\"']+", re.IGNORECASE)
COMMAND_RE = re.compile(r"^/([a-zA-Z0-9_]+)(?:@\w+)?\s*([\s\S]*)$")
TRAILING_PUNCTUATION_RE = re.compile(r"[.,;!?，。；！？)\]}]+$")
SERIES_MIN_SECONDS = 600
SERIES_MAX_SECONDS = 900
SERIES_CHUNK_SECONDS = 60
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: dict[str, Any] = {"value": "", "expires_at": 0.0}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _series_publish_at(part_number: int) -> str:
    """Return the configured UTC publish time for a series Part."""
    base_part_raw = _env("SERIES_SCHEDULE_BASE_PART")
    base_date_raw = _env("SERIES_SCHEDULE_BASE_DATE")
    times_raw = _env("SERIES_SCHEDULE_TIMES")
    offset_raw = _env("SERIES_SCHEDULE_TIMEZONE_OFFSET", "+07:00")
    if not base_part_raw or not base_date_raw or not times_raw:
        return ""
    try:
        base_part = int(base_part_raw)
        base_date = date.fromisoformat(base_date_raw)
        slots: list[tuple[int, int]] = []
        for raw_slot in re.split(r"[,;]", times_raw):
            hour_raw, minute_raw = raw_slot.strip().split(":", 1)
            hour, minute = int(hour_raw), int(minute_raw)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError("invalid time slot")
            slots.append((hour, minute))
        offset_match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", offset_raw)
        if not slots or not offset_match:
            raise ValueError("invalid timezone offset")
        offset_minutes = int(offset_match.group(2)) * 60 + int(offset_match.group(3))
        if offset_minutes > 14 * 60:
            raise ValueError("timezone offset out of range")
        if offset_match.group(1) == "-":
            offset_minutes *= -1
    except (TypeError, ValueError) as exc:
        raise ValueError("Cấu hình lịch đăng series không hợp lệ") from exc

    slot_index = int(part_number) - base_part
    if slot_index < 0:
        return ""
    slot_day = base_date + timedelta(days=slot_index // len(slots))
    hour, minute = slots[slot_index % len(slots)]
    local_tz = timezone(timedelta(minutes=offset_minutes))
    local_publish_at = datetime(
        slot_day.year,
        slot_day.month,
        slot_day.day,
        hour,
        minute,
        tzinfo=local_tz,
    )
    return (
        local_publish_at.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _series_auto_until_part(chat_id: int | str) -> int:
    """Return the final Part for the server-side series queue, if enabled."""
    target_chat = _env("SERIES_AUTO_CHAT_ID")
    raw_until = _env("SERIES_AUTO_UNTIL_PART")
    if not target_chat or str(chat_id) != target_chat or not raw_until:
        return 0
    try:
        return max(0, int(raw_until))
    except ValueError as exc:
        raise ValueError("SERIES_AUTO_UNTIL_PART không hợp lệ") from exc


def validate_douyin_url(raw_url: str) -> str:
    cleaned = TRAILING_PUNCTUATION_RE.sub("", raw_url.lstrip("!"))
    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"URL không đúng định dạng: {raw_url}")

    hostname = parsed.hostname.lower().removeprefix("www.")
    if not any(hostname == root or hostname.endswith(f".{root}") for root in DOUYIN_HOSTS):
        raise ValueError(f"Link không thuộc Douyin: {raw_url}")

    path = parsed.path or "/"
    valid_path = (
        (hostname == "v.douyin.com" and re.fullmatch(r"/[A-Za-z0-9_-]+/?", path))
        or re.fullmatch(r"/video/\d+/?", path)
        or re.fullmatch(r"/share/video/\d+/?", path)
        or re.fullmatch(r"/note/\d+/?", path)
    )
    if not valid_path:
        raise ValueError(f"Link Douyin không đúng định dạng video: {raw_url}")
    return cleaned


def help_text() -> str:
    return "\n".join(
        [
            "🤖 <b>Bot tạo video từ Douyin</b>",
            "",
            "<b>/part</b> &lt;link&gt; — Tạo và đăng private Part 1, kèm thumbnail tự động",
            "<b>/next</b> — Tạo Part kế tiếp, đăng private và gắn thumbnail tự động",
            "<b>/long</b> &lt;link1&gt; [link2 ...] — Luồng Long cũ và upload YouTube",
            "<b>/help</b> — Hiện hướng dẫn này",
            "",
            "📌 Mỗi Part nối đúng điểm dừng trước và dừng ở cao trào trong khoảng 10–15 phút.",
        ]
    )


def _request_id(chat_id: Any, message_id: Any, prefix: str) -> str:
    return f"{prefix}-{chat_id}-{message_id}-{int(time.time() * 1000)}"


def parse_message(message: dict[str, Any]) -> dict[str, Any]:
    raw_text = str(message.get("text") or message.get("caption") or "").strip()
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    if chat_id is None:
        raise ValueError("Telegram update không có chat_id")

    command = ""
    rest = raw_text
    match = COMMAND_RE.match(raw_text)
    if match:
        command = match.group(1).lower()
        rest = match.group(2) or ""

    if command == "help":
        return {"route": "help", "chat_id": chat_id, "text": help_text()}
    if command == "next":
        return {
            "route": "series_next",
            "chat_id": chat_id,
            "id": _request_id(chat_id, message_id, "series-next"),
        }

    force_refresh = bool(re.search(r"(^|\s)--refresh(\s|$)", rest, re.IGNORECASE))
    rest = re.sub(r"(^|\s)--refresh(\s|$)", " ", rest, flags=re.IGNORECASE)
    tokens = URL_TOKEN_RE.findall(rest)
    if not tokens:
        raise ValueError("Không tìm thấy link Douyin")

    if command == "part":
        if len(tokens) != 1:
            raise ValueError("Lệnh /part chỉ nhận đúng một link Douyin")
        return {
            "route": "series_start",
            "chat_id": chat_id,
            "id": _request_id(chat_id, message_id, "series-part-001"),
            "douyin_url": validate_douyin_url(tokens[0]),
        }

    request_id = _request_id(chat_id, message_id, "telegram")
    if command == "long":
        if len(tokens) > 30:
            raise ValueError("Lệnh /long chỉ nhận tối đa 30 link")
        videos: list[dict[str, Any]] = []
        errors: list[str] = []
        for token in tokens:
            refresh_one = token.startswith("!")
            try:
                item: dict[str, Any] = {"douyin_url": validate_douyin_url(token)}
                if refresh_one:
                    item["force_refresh"] = True
                videos.append(item)
            except ValueError as exc:
                errors.append(str(exc))
        if not videos:
            raise ValueError("Không có link hợp lệ. " + "; ".join(errors))
        return {
            "route": "long",
            "chat_id": chat_id,
            "id": request_id,
            "videos": videos,
            "force_refresh": force_refresh,
            "skipped": errors,
        }

    return {
        "route": "short",
        "chat_id": chat_id,
        "id": request_id,
        "douyin_url": validate_douyin_url(tokens[0]),
    }


def _json_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"HTTP {exc.code} từ {url}: {detail}") from exc
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def _metadata_json(path: str) -> dict[str, Any]:
    url = f"http://metadata.google.internal/computeMetadata/v1/{path.lstrip('/')}"
    request = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _access_token() -> str:
    now = time.time()
    with _TOKEN_LOCK:
        if _TOKEN_CACHE["value"] and now < float(_TOKEN_CACHE["expires_at"]) - 60:
            return str(_TOKEN_CACHE["value"])
        data = _metadata_json("instance/service-accounts/default/token")
        value = str(data.get("access_token") or "")
        if not value:
            raise RuntimeError("Không lấy được access token của Cloud Run")
        _TOKEN_CACHE.update(
            value=value,
            expires_at=now + int(data.get("expires_in") or 300),
        )
        return value


def _identity_token(audience: str) -> str:
    query = urllib.parse.urlencode({"audience": audience, "format": "full"})
    url = (
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        f"service-accounts/default/identity?{query}"
    )
    request = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read().decode("utf-8")


def _state_object(chat_id: int | str) -> str:
    prefix = _env("FLOW_STATE_PREFIX", "telegram-series-state").strip("/")
    safe_chat = re.sub(r"[^0-9A-Za-z_-]", "_", str(chat_id))
    return f"{prefix}/{safe_chat}.json"


def _telegram_update_receipt_object(update_id: int) -> str:
    prefix = _env("FLOW_STATE_PREFIX", "telegram-series-state").strip("/")
    return f"{prefix}/_telegram-update-receipts/{update_id}.json"


def _claim_telegram_update(update: dict[str, Any]) -> bool:
    """Atomically claim a Telegram update so retries cannot run it twice."""
    raw_update_id = update.get("update_id")
    if raw_update_id is None:
        return True
    try:
        update_id = int(raw_update_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Telegram update_id không hợp lệ") from exc
    if update_id < 0:
        raise ValueError("Telegram update_id không hợp lệ")

    bucket = _env("FLOW_STATE_BUCKET")
    if not bucket:
        raise RuntimeError("Thiếu FLOW_STATE_BUCKET")
    object_name = _telegram_update_receipt_object(update_id)
    query = urllib.parse.urlencode(
        {
            "uploadType": "media",
            "name": object_name,
            "ifGenerationMatch": "0",
        }
    )
    url = f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o?{query}"
    body = json.dumps(
        {"update_id": update_id, "claimed_at": int(time.time())},
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == HTTPStatus.PRECONDITION_FAILED:
            return False
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(
            f"Không lưu được Telegram update receipt: HTTP {exc.code}: {detail}"
        ) from exc


def load_series_state(chat_id: int | str) -> dict[str, Any] | None:
    bucket = _env("FLOW_STATE_BUCKET")
    if not bucket:
        raise RuntimeError("Thiếu FLOW_STATE_BUCKET")
    object_name = urllib.parse.quote(_state_object(chat_id), safe="")
    url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{object_name}?alt=media"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {_access_token()}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == HTTPStatus.NOT_FOUND:
            return None
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Không đọc được trạng thái series: HTTP {exc.code}: {detail}") from exc


def save_series_state(state: dict[str, Any]) -> None:
    bucket = _env("FLOW_STATE_BUCKET")
    if not bucket:
        raise RuntimeError("Thiếu FLOW_STATE_BUCKET")
    object_name = urllib.parse.quote(_state_object(state["chat_id"]), safe="")
    url = (
        f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o"
        f"?uploadType=media&name={object_name}"
    )
    body = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Không lưu được trạng thái series: HTTP {exc.code}: {detail}") from exc


def telegram_send(chat_id: int | str, text: str, *, parse_mode: str = "HTML") -> None:
    token = _env("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Thiếu TELEGRAM_BOT_TOKEN")
    _json_request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        method="POST",
        payload={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        },
        timeout=15,
    )


def _callback_url() -> str:
    base = _env("PUBLIC_BASE_URL").rstrip("/")
    token = _env("CALLBACK_TOKEN")
    if not base or not token:
        raise RuntimeError("Thiếu PUBLIC_BASE_URL hoặc CALLBACK_TOKEN")
    return f"{base}/callbacks/job?{urllib.parse.urlencode({'token': token})}"


def start_long_job(parsed: dict[str, Any]) -> dict[str, Any]:
    runner_url = _env("LONG_RUNNER_URL").rstrip("/")
    api_key = _env("LONG_RUNNER_API_KEY")
    if not runner_url or not api_key:
        raise RuntimeError("Thiếu cấu hình long-job-runner")
    audience = _env("LONG_RUNNER_AUDIENCE", runner_url)
    payload: dict[str, Any] = {
        "videos": parsed["videos"],
        "force_refresh": bool(parsed.get("force_refresh")),
        "youtube_upload_enabled": bool(parsed.get("youtube_upload_enabled", True)),
        "youtube_privacy_status": "private",
        "youtube_category_id": "24",
        "youtube_made_for_kids": False,
        "id": parsed["id"],
        "chat_id": str(parsed["chat_id"]),
        "callback_enabled": True,
        "callback_url": _callback_url(),
    }
    for key in (
        "source_start_seconds",
        "source_max_seconds",
        "cliffhanger_enabled",
        "cliffhanger_min_seconds",
        "cliffhanger_max_seconds",
        "chunk_seconds",
        "min_chunk_seconds",
        "max_chunk_seconds",
        "auto_remove_ads",
        "auto_ad_min_confidence",
        "auto_ad_max_passes",
        "series_part_number",
        "thumbnail_generate_enabled",
        "thumbnail_required",
        "thumbnail_reference_uri",
        "thumbnail_headline",
        "thumbnail_model",
        "thumbnail_force_refresh",
        "youtube_publish_at",
    ):
        if key in parsed:
            payload[key] = parsed[key]
    return _json_request(
        f"{runner_url}/run",
        method="POST",
        payload=payload,
        headers={
            "X-API-Key": api_key,
            "Authorization": f"Bearer {_identity_token(audience)}",
        },
        timeout=30,
    )


def _series_job(
    parsed: dict[str, Any],
    *,
    url: str,
    start: float,
    part_number: int,
) -> dict[str, Any]:
    state_bucket = _env("FLOW_STATE_BUCKET")
    reference_uri = _env("THUMBNAIL_REFERENCE_URI")
    if not reference_uri and state_bucket:
        reference_uri = (
            f"gs://{state_bucket}/long/_assets/ha-nhan-thumbnail-reference.png"
        )
    if not reference_uri:
        raise ValueError("Thiếu THUMBNAIL_REFERENCE_URI")
    publish_at = _series_publish_at(part_number)
    return start_long_job(
        {
            **parsed,
            "videos": [{"douyin_url": url}],
            "force_refresh": False,
            "youtube_upload_enabled": True,
            "series_part_number": int(part_number),
            "thumbnail_generate_enabled": True,
            "thumbnail_required": True,
            "thumbnail_reference_uri": reference_uri,
            "thumbnail_headline": "XUYÊN KHÔNG ĐẠI ĐƯỜNG",
            **({"youtube_publish_at": publish_at} if publish_at else {}),
            "source_start_seconds": float(start),
            "source_max_seconds": SERIES_MAX_SECONDS,
            "cliffhanger_enabled": True,
            "cliffhanger_min_seconds": SERIES_MIN_SECONDS,
            "cliffhanger_max_seconds": SERIES_MAX_SECONDS,
            "chunk_seconds": SERIES_CHUNK_SECONDS,
            "min_chunk_seconds": SERIES_CHUNK_SECONDS,
            "max_chunk_seconds": SERIES_CHUNK_SECONDS,
            "auto_remove_ads": True,
            "auto_ad_min_confidence": 0.85,
            "auto_ad_max_passes": 2,
        }
    )


def start_series(parsed: dict[str, Any], *, next_part: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    chat_id = parsed["chat_id"]
    current = load_series_state(chat_id)
    if next_part:
        if not current:
            raise ValueError("Chưa có series. Hãy dùng /part <link> để tạo Part 1 trước")
        if current.get("status") in {"processing", "auto_queued"}:
            raise ValueError("Part hiện tại vẫn đang xử lý, chưa thể chạy /next")
        source_duration = float(current.get("source_duration") or 0.0)
        start = float(current.get("next_start_seconds") or 0.0)
        if source_duration and start >= source_duration - 0.5:
            raise ValueError("Đã xử lý hết video nguồn")
        url = str(current.get("douyin_url") or "")
        current_part_number = int(current.get("part_number") or 1)
        part_number = (
            current_part_number
            if current.get("status") == "failed"
            else current_part_number + 1
        )
        history = list(current.get("history") or [])
    else:
        if current and current.get("status") == "processing":
            raise ValueError("Một Part đang xử lý. Hãy chờ hoàn tất trước khi tạo series mới")
        url = str(parsed["douyin_url"])
        start = 0.0
        part_number = 1
        history = []

    state = {
        "chat_id": chat_id,
        "douyin_url": url,
        "part_number": part_number,
        "next_start_seconds": start,
        "source_duration": float((current or {}).get("source_duration") or 0.0),
        "status": "processing",
        "active_batch_id": parsed["id"],
        "history": history,
        "updated_at": int(time.time()),
    }
    save_series_state(state)
    try:
        response = _series_job(
            parsed,
            url=url,
            start=start,
            part_number=part_number,
        )
    except Exception as exc:
        state.update(status="failed", error=str(exc), updated_at=int(time.time()))
        save_series_state(state)
        raise
    state["active_batch_id"] = str(response.get("batch_id") or parsed["id"])
    state["runner_operation"] = str(response.get("operation") or "")
    save_series_state(state)
    return state, response


def _escape(value: Any, default: str = "N/A") -> str:
    return html.escape(str(value or default))


def _clock(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    return f"{total // 60:02d}:{total % 60:02d}"


def _handle_series_callback(body: dict[str, Any], state: dict[str, Any]) -> bool:
    batch_id = str(body.get("batch_id") or "")
    if batch_id != str(state.get("active_batch_id") or ""):
        return False
    chat_id = state["chat_id"]
    event = str(body.get("event") or "")
    if event in {"long.batch.completed", "long.youtube.completed"} and body.get("ok"):
        source_parts = body.get("source_parts") or []
        if not source_parts:
            raise ValueError("Callback Part không có source_parts")
        source = source_parts[0]
        start = float(source.get("source_start") or state.get("next_start_seconds") or 0.0)
        end = float(source.get("source_processed_end") or 0.0)
        duration = float(source.get("source_duration") or 0.0)
        if end <= start:
            raise ValueError("Điểm kết thúc Part không hợp lệ")
        part_number = int(state.get("part_number") or 1)
        history = list(state.get("history") or [])
        history.append(
            {
                "part_number": part_number,
                "source_start": start,
                "source_end": end,
                "output_uri": body.get("output_uri"),
                "download_url": body.get("download_url"),
                "batch_id": batch_id,
                "cliffhanger": source.get("cliffhanger") or {},
                "youtube_video_id": body.get("youtube_video_id"),
                "youtube_url": body.get("youtube_url"),
                "youtube_title": body.get("youtube_title"),
                "thumbnail_uri": body.get("thumbnail_uri"),
            }
        )
        complete = bool(duration and end >= duration - 0.5)
        state.update(
            status="complete" if complete else "ready",
            next_start_seconds=end,
            source_duration=duration,
            history=history,
            active_batch_id="",
            updated_at=int(time.time()),
        )
        # Persist the completed boundary before starting the following Part so
        # start_series reads the exact source timestamp and cannot be rejected
        # as a still-processing Part.
        save_series_state(state)

        auto_until = _series_auto_until_part(chat_id)
        auto_next = bool(
            not complete and auto_until and part_number < auto_until
        )
        auto_started_part = 0
        auto_error = ""
        if auto_next:
            next_part = part_number + 1
            auto_request = {
                "route": "series_next",
                "chat_id": chat_id,
                "id": _request_id(chat_id, f"auto-{next_part}", "series-next"),
            }
            try:
                auto_state, _ = start_series(auto_request, next_part=True)
                auto_started_part = int(auto_state.get("part_number") or next_part)
            except Exception as exc:  # noqa: BLE001
                auto_error = str(exc)

        cliffhanger = source.get("cliffhanger") or {}
        message = [
            f"✅ <b>Part {part_number} đã hoàn tất</b>",
            "",
            f"⏱ <b>Đoạn nguồn:</b> {_clock(start)} → {_clock(end)}",
        ]
        if body.get("youtube_url"):
            message.extend(
                [
                    f"🎬 <b>Tiêu đề:</b> {_escape(body.get('youtube_title') or body.get('title_vi'))}",
                    f"📺 <b>YouTube private:</b> {_escape(body.get('youtube_url'))}",
                    "🖼 <b>Thumbnail:</b> Đã tạo và gắn tự động",
                ]
            )
        else:
            message.append(
                f"🎬 <b>File:</b> {_escape(body.get('download_url') or body.get('output_uri'))}"
            )
        if cliffhanger.get("reason_vi"):
            message.append(f"🔥 <b>Điểm dừng:</b> {_escape(cliffhanger.get('reason_vi'))}")
        message.append("")
        if complete:
            message.extend(
                [
                    "🏁 <b>Đã xử lý hết video nguồn.</b>",
                    "📦 Bước tiếp theo: gửi <b>/final</b> để ghép toàn bộ các Part thành một video Full.",
                ]
            )
        elif auto_started_part:
            message.append(
                f"🤖 <b>Đã tự động bắt đầu Part {auto_started_part}</b> theo hàng đợi."
            )
        elif auto_error:
            message.extend(
                [
                    "⚠️ Không thể tự khởi chạy Part tiếp theo.",
                    f"Chi tiết: {_escape(auto_error)}",
                    "Bạn có thể gửi <b>/next</b> để thử lại.",
                ]
            )
        else:
            message.append(
                "Gửi <b>/next</b> để làm Part tiếp theo từ đúng điểm này."
            )
        telegram_send(chat_id, "\n".join(message))
        return True

    if event == "long.batch.failed" or not body.get("ok", True):
        state.update(
            status="failed",
            error=str(body.get("error") or "Xử lý Part thất bại"),
            active_batch_id="",
            updated_at=int(time.time()),
        )
        save_series_state(state)
        telegram_send(
            chat_id,
            f"❌ <b>Part {int(state.get('part_number') or 1)} thất bại</b>\n\n"
            f"⚠️ {_escape(state.get('error'))}",
        )
        return True
    return False


def handle_callback(body: dict[str, Any]) -> None:
    chat_id = body.get("chat_id")
    if chat_id is None:
        raise ValueError("Callback không có chat_id")
    state = load_series_state(chat_id)
    if state and _handle_series_callback(body, state):
        return

    event = body.get("event")
    if event == "long.youtube.completed" and body.get("youtube_url"):
        telegram_send(
            chat_id,
            "\n".join(
                [
                    "🎉 <b>Đăng video Long thành công!</b>",
                    "",
                    f"🎬 <b>Tiêu đề:</b> {_escape(body.get('youtube_title') or body.get('title_vi'))}",
                    f"📺 <b>YouTube:</b> {_escape(body.get('youtube_url'))}",
                ]
            ),
        )
        return
    if event == "long.batch.completed" and body.get("ok"):
        telegram_send(
            chat_id,
            f"✅ <b>Xử lý Long hoàn tất</b>\n\n🎬 {_escape(body.get('download_url') or body.get('output_uri'))}",
        )
        return
    error = body.get("error") or body.get("message") or f"Sự kiện không thành công: {event or 'unknown'}"
    telegram_send(
        chat_id,
        f"❌ <b>Xử lý video thất bại</b>\n\n⚠️ {_escape(error)}",
    )


def required_config_missing() -> list[str]:
    return [
        name
        for name in (
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_WEBHOOK_SECRET",
            "CALLBACK_TOKEN",
            "PUBLIC_BASE_URL",
            "LONG_RUNNER_URL",
            "LONG_RUNNER_API_KEY",
            "FLOW_STATE_BUCKET",
        )
        if not _env(name)
    ]


class FlowHandler(BaseHTTPRequestHandler):
    server_version = "DouyinFlow/2.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[flow] {self.address_string()} {fmt % args}", flush=True)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _read_json(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > 1_000_000:
            raise ValueError("Request body không hợp lệ")
        return json.loads(self.rfile.read(size).decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path in {"/health", "/healthz"}:
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if path == "/health/readiness":
            missing = required_config_missing()
            self._send_json(
                HTTPStatus.OK if not missing else HTTPStatus.SERVICE_UNAVAILABLE,
                {"ready": not missing, "missing": missing},
            )
            return
        self._send_json(HTTPStatus.OK, {"service": "douyin-flow", "health": "/health"})

    def do_POST(self) -> None:  # noqa: N802
        parsed_url = urllib.parse.urlsplit(self.path)
        try:
            if parsed_url.path == "/telegram/webhook":
                self._handle_telegram()
                return
            if parsed_url.path == "/callbacks/job":
                supplied = urllib.parse.parse_qs(parsed_url.query).get("token", [""])[0]
                if not secrets.compare_digest(supplied, _env("CALLBACK_TOKEN")):
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False})
                    return
                handle_callback(self._read_json())
                self._send_json(HTTPStatus.OK, {"ok": True})
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
        except (ValueError, RuntimeError, urllib.error.URLError, json.JSONDecodeError) as exc:
            print(f"[flow] request failed: {type(exc).__name__}: {exc}", flush=True)
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)[:1000]})

    def _handle_telegram(self) -> None:
        expected = _env("TELEGRAM_WEBHOOK_SECRET")
        supplied = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not expected or not secrets.compare_digest(supplied, expected):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False})
            return
        update = self._read_json()
        if not _claim_telegram_update(update):
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "ignored": True, "reason": "duplicate_update"},
            )
            return
        message = update.get("message") or update.get("channel_post")
        if not isinstance(message, dict):
            self._send_json(HTTPStatus.OK, {"ok": True, "ignored": True})
            return
        try:
            parsed = parse_message(message)
            route = parsed["route"]
            if route == "help":
                telegram_send(parsed["chat_id"], parsed["text"])
            elif route in {"series_start", "series_next"}:
                state, _ = start_series(parsed, next_part=route == "series_next")
                telegram_send(
                    parsed["chat_id"],
                    "\n".join(
                        [
                            f"📥 <b>Đã nhận Part {state['part_number']}</b>",
                            "",
                            f"⏱ <b>Bắt đầu từ:</b> {_clock(float(state['next_start_seconds']))}",
                            "🔥 Hệ thống sẽ chọn cao trào mạnh nhất trong khoảng 10–15 phút để dừng.",
                            "Bot sẽ gửi file khi hoàn tất.",
                        ]
                    ),
                )
            elif route == "long":
                start_long_job(parsed)
                telegram_send(
                    parsed["chat_id"],
                    f"📥 <b>Đã nhận lệnh Long</b>\n\n🔢 <b>Số link:</b> {len(parsed['videos'])}",
                )
            else:
                telegram_send(
                    parsed["chat_id"],
                    "⚠️ Phần Short hiện không hoạt động. Hãy dùng <b>/part &lt;link&gt;</b>.",
                )
            self._send_json(HTTPStatus.OK, {"ok": True})
        except Exception as exc:
            chat_id = (message.get("chat") or {}).get("id")
            if chat_id is not None:
                try:
                    telegram_send(chat_id, f"❌ <b>Lệnh không hợp lệ</b>\n\n⚠️ {_escape(exc)}")
                except Exception as send_exc:
                    print(f"[flow] unable to report Telegram error: {send_exc}", flush=True)
            # Telegram retries every non-2xx webhook response. Application-level
            # errors (for example a second /next while a Part is processing)
            # are already reported to the chat, so acknowledge the update once
            # instead of creating an endless retry/error loop.
            log_error = str(exc).encode("ascii", errors="backslashreplace").decode("ascii")
            print(
                f"[flow] Telegram command rejected: {type(exc).__name__}: {log_error}",
                flush=True,
            )
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "handled": False, "error": str(exc)[:1000]},
            )


def main() -> None:
    port = int(_env("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), FlowHandler)
    print(f"[flow] listening on {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
