from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from urllib.parse import urlencode

import google.auth
from flask import Flask, Response, jsonify, request, stream_with_context
from google.auth.transport.requests import AuthorizedSession, Request
from google.cloud import storage

import douyin_channel


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} environment variable is required")
    return value


PROJECT_ID = _required_env("GCP_PROJECT_ID")
REGION = os.environ.get("RUN_REGION", "asia-southeast1")
JOB_NAME = os.environ.get("JOB_NAME", "short-maker")
OUTPUT_PREFIX = os.environ.get(
    "OUTPUT_PREFIX", "gs://YOUR_GCP_PROJECT-shorts/out/"
).rstrip("/") + "/"
API_KEY = os.environ.get("API_KEY", "")
DOWNLOAD_SECRET = os.environ.get("DOWNLOAD_SECRET", "")
DOWNLOAD_TTL_SECONDS = int(os.environ.get("DOWNLOAD_TTL_SECONDS", "86400"))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "3"))
WAIT_TIMEOUT_SECONDS = int(os.environ.get("WAIT_TIMEOUT_SECONDS", "900"))
SERVICE_BASE_URL = os.environ.get("SERVICE_BASE_URL", "").rstrip("/")
# Nơi lưu trạng thái "đã xử lý" cho từng kênh (chống đăng trùng).
CHANNEL_STATE_PREFIX = os.environ.get(
    "CHANNEL_STATE_PREFIX", "gs://YOUR_GCP_PROJECT-shorts/state/channels/"
).rstrip("/") + "/"
# Cookie Douyin (tùy chọn) nếu trang share yêu cầu đăng nhập.
DOUYIN_COOKIE = os.environ.get("DOUYIN_COOKIE", "")
# Số aweme_id tối đa giữ trong mỗi file state.
CHANNEL_STATE_MAX = int(os.environ.get("CHANNEL_STATE_MAX", "2000"))

credentials, _ = google.auth.default(
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)
auth_session = AuthorizedSession(credentials)
storage_client = storage.Client(project=PROJECT_ID, credentials=credentials)
app = Flask(__name__)


def _error(message: str, status: int):
    return jsonify({"ok": False, "error": message}), status


def _check_api_key() -> bool:
    supplied = request.headers.get("X-API-Key") or request.args.get("api_key", "")
    return bool(API_KEY) and hmac.compare_digest(supplied, API_KEY)


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _parse_gs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith(OUTPUT_PREFIX):
        raise ValueError("output_uri must stay inside the configured output prefix")
    rest = uri[5:]
    bucket, sep, name = rest.partition("/")
    if not sep or not bucket or not name:
        raise ValueError("invalid gs:// output URI")
    return bucket, name


def _default_output_uri(run_id: str) -> str:
    safe_id = "".join(c for c in run_id if c.isalnum() or c in "-_")[:80]
    if not safe_id:
        safe_id = uuid.uuid4().hex
    return f"{OUTPUT_PREFIX}{safe_id}/final.mp4"


def _signature(uri: str, expires: int) -> str:
    payload = f"{uri}\n{expires}".encode()
    digest = hmac.new(DOWNLOAD_SECRET.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _download_url(uri: str) -> str:
    expires = int(time.time()) + DOWNLOAD_TTL_SECONDS
    query = urlencode(
        {"uri": uri, "expires": expires, "sig": _signature(uri, expires)}
    )
    base_url = SERVICE_BASE_URL or request.host_url.rstrip("/")
    return f"{base_url}/download?{query}"


def _load_metadata(output_uri: str) -> dict:
    meta_uri = output_uri.rsplit(".", 1)[0] + ".json"
    bucket_name, blob_name = _parse_gs_uri(meta_uri)
    blob = storage_client.bucket(bucket_name).blob(blob_name)
    if not blob.exists():
        return {}
    try:
        return json.loads(blob.download_as_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


PUBLIC_YOUTUBE_HASHTAGS = (
    "#truyentranhreview",
    "#tutien",
    "#truongsinh",
    "#tomtatphim",
    "#manhua",
    "#huyenhuyen",
    "#douyin",
    "#china",
    "#hanhan",
)


def _public_youtube_hashtag_text() -> str:
    return " ".join(PUBLIC_YOUTUBE_HASHTAGS)


def _youtube_metadata(metadata: dict) -> dict:
    title = str(metadata.get("title_vi") or "YouTube Short").strip()
    description = str(metadata.get("description_vi") or "").strip()
    hashtags = [
        str(tag).strip().lstrip("#")
        for tag in (metadata.get("hashtags") or [])
        if str(tag).strip()
    ]
    hashtag_text = _public_youtube_hashtag_text()
    youtube_description = "\n\n".join(
        part for part in (description, hashtag_text, "#shorts") if part
    )
    return {
        "title_vi": title,
        "description_vi": description,
        "hashtags": hashtags,
        "youtube_title": title[:100],
        "youtube_description": youtube_description[:5000],
    }


def _run_job(
    douyin_url: str,
    output_uri: str,
    extra_env: dict | None = None,
) -> str:
    endpoint = (
        f"https://run.googleapis.com/v2/projects/{PROJECT_ID}/locations/"
        f"{REGION}/jobs/{JOB_NAME}:run"
    )
    env = [
        {"name": "DOUYIN_URL", "value": douyin_url},
        {"name": "OUTPUT_URI", "value": output_uri},
    ]
    for key, value in (extra_env or {}).items():
        if value:
            env.append({"name": key, "value": str(value)})
    body = {
        "overrides": {
            "containerOverrides": [{"env": env}],
            "taskCount": 1,
        }
    }
    response = auth_session.post(endpoint, json=body, timeout=60)
    response.raise_for_status()
    operation_name = response.json().get("name")
    if not operation_name:
        raise RuntimeError("Cloud Run jobs.run returned no operation name")
    return operation_name


def _wait_operation(operation_name: str) -> dict:
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    endpoint = f"https://run.googleapis.com/v2/{operation_name}"
    while time.monotonic() < deadline:
        response = auth_session.get(endpoint, timeout=30)
        response.raise_for_status()
        operation = response.json()
        if operation.get("done"):
            if operation.get("error"):
                message = operation["error"].get("message", str(operation["error"]))
                raise RuntimeError(message)
            return operation
        time.sleep(POLL_SECONDS)
    raise TimeoutError(
        f"Job is still running after {WAIT_TIMEOUT_SECONDS} seconds"
    )


@app.get("/health")
def health():
    return jsonify({"ok": True, "job": JOB_NAME, "region": REGION})


@app.route("/run", methods=["GET", "POST"])
def run_short():
    if not _check_api_key():
        return _error("unauthorized", 401)

    payload = request.get_json(silent=True) or {}
    douyin_url = str(
        payload.get("douyin_url") or request.args.get("douyin_url") or ""
    ).strip()
    if not douyin_url:
        return _error("douyin_url is required", 400)

    run_id = str(
        payload.get("id") or request.args.get("id") or uuid.uuid4().hex
    ).strip()
    output_uri = str(
        payload.get("output_uri")
        or request.args.get("output_uri")
        or _default_output_uri(run_id)
    ).strip()
    callback_url = str(
        payload.get("callback_url") or request.args.get("callback_url") or ""
    ).strip()
    callback_enabled = _as_bool(
        payload.get("callback_enabled", request.args.get("callback_enabled"))
    )
    # chat_id: truyền xuyên suốt để callback biết gửi Telegram về đâu.
    # Tách riêng với run_id (run_id quyết định đường dẫn output, không nên trùng).
    chat_id = str(
        payload.get("chat_id") or request.args.get("chat_id") or ""
    ).strip()

    # Async: nếu n8n cung cấp callback_url → fire job rồi trả 202 ngay.
    # Job tự POST kết quả về callback_url khi xong/fail (xem short_job.py).
    if callback_enabled and callback_url:
        try:
            _parse_gs_uri(output_uri)
            download_url = _download_url(output_uri)
            operation_name = _run_job(
                douyin_url,
                output_uri,
                extra_env={
                    "CALLBACK_ENABLED": "true",
                    "RUN_ID": run_id,
                    "CHAT_ID": chat_id,
                    "CALLBACK_URL": callback_url,
                    "DOWNLOAD_URL": download_url,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return _error(str(exc), 500)
        return (
            jsonify(
                {
                    "callback_enabled": callback_enabled,
                    "ok": True,
                    "status": "queued",
                    "run_id": run_id,
                    "chat_id": chat_id,
                    "operation": operation_name,
                    "output_uri": output_uri,
                    "download_url": download_url,
                    "expires_in": DOWNLOAD_TTL_SECONDS,
                }
            ),
            202,
        )

    try:
        _parse_gs_uri(output_uri)
        operation_name = _run_job(douyin_url, output_uri)
        operation = _wait_operation(operation_name)
        bucket_name, blob_name = _parse_gs_uri(output_uri)
        blob = storage_client.bucket(bucket_name).blob(blob_name)
        if not blob.exists():
            raise RuntimeError("Job completed but final.mp4 was not found")
    except TimeoutError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "status": "running",
                    "error": str(exc),
                    "output_uri": output_uri,
                }
            ),
            202,
        )
    except Exception as exc:  # noqa: BLE001
        return _error(str(exc), 500)

    execution_name = (
        operation.get("response", {}).get("name")
        or operation.get("metadata", {}).get("name")
        or ""
    )
    youtube = _youtube_metadata(_load_metadata(output_uri))
    return jsonify(
        {
            "ok": True,
            "status": "completed",
            "execution": execution_name,
            "output_uri": output_uri,
            "download_url": _download_url(output_uri),
            "expires_in": DOWNLOAD_TTL_SECONDS,
            **youtube,
        }
    )


@app.get("/download")
def download():
    uri = request.args.get("uri", "")
    expires_raw = request.args.get("expires", "")
    supplied_signature = request.args.get("sig", "")
    try:
        expires = int(expires_raw)
        if expires < int(time.time()):
            return _error("download link expired", 410)
        if not DOWNLOAD_SECRET or not hmac.compare_digest(
            supplied_signature, _signature(uri, expires)
        ):
            return _error("invalid download signature", 403)
        bucket_name, blob_name = _parse_gs_uri(uri)
        blob = storage_client.bucket(bucket_name).blob(blob_name)
        if not blob.exists():
            return _error("file not found", 404)
        stream = blob.open("rb")
    except Exception as exc:  # noqa: BLE001
        return _error(str(exc), 400)

    return Response(
        stream_with_context(stream),
        mimetype="video/mp4",
        headers={
            "Content-Disposition": 'attachment; filename="final.mp4"',
            "Cache-Control": "private, no-store",
        },
    )


def _parse_any_gs_uri(uri: str) -> tuple[str, str]:
    """Tách gs://bucket/name, không ràng buộc OUTPUT_PREFIX (dùng cho state)."""
    if not uri.startswith("gs://"):
        raise ValueError("state_uri must be a gs:// URI")
    rest = uri[5:]
    bucket, sep, name = rest.partition("/")
    if not sep or not bucket or not name:
        raise ValueError("invalid gs:// state URI")
    return bucket, name


def _channel_state_uri(channel_url: str, channel_key: str = "") -> str:
    """Đường dẫn file state cho 1 kênh. Ưu tiên channel_key ổn định (vd sec_uid)."""
    key = channel_key.strip() or hashlib.sha1(
        channel_url.strip().encode("utf-8")
    ).hexdigest()[:16]
    safe = "".join(c for c in key if c.isalnum() or c in "-_")[:80] or "default"
    return f"{CHANNEL_STATE_PREFIX}{safe}.json"


def _load_processed(state_uri: str) -> dict:
    bucket_name, blob_name = _parse_any_gs_uri(state_uri)
    blob = storage_client.bucket(bucket_name).blob(blob_name)
    if not blob.exists():
        return {"processed": [], "updated_at": 0}
    try:
        data = json.loads(blob.download_as_text(encoding="utf-8"))
        if not isinstance(data.get("processed"), list):
            data["processed"] = []
        return data
    except Exception:  # noqa: BLE001
        return {"processed": [], "updated_at": 0}


def _save_processed(state_uri: str, processed: list[str]) -> None:
    bucket_name, blob_name = _parse_any_gs_uri(state_uri)
    blob = storage_client.bucket(bucket_name).blob(blob_name)
    # Giữ N id mới nhất (cuối danh sách) để file không phình mãi.
    trimmed = processed[-CHANNEL_STATE_MAX:]
    blob.upload_from_string(
        json.dumps(
            {"processed": trimmed, "updated_at": int(time.time())},
            ensure_ascii=False,
        ),
        content_type="application/json",
    )


@app.route("/channel/new-videos", methods=["GET", "POST"])
def channel_new_videos():
    """Liệt kê video MỚI (chưa xử lý) của 1 kênh Douyin cho Schedule của n8n.

    Body/query: channel_url (bắt buộc), limit (mặc định 5), channel_key (tùy chọn),
    state_uri (tùy chọn), mark (mặc định false — đánh dấu đã xử lý ngay khi trả).
    """
    if not _check_api_key():
        return _error("unauthorized", 401)

    payload = request.get_json(silent=True) or {}
    channel_url = str(
        payload.get("channel_url") or request.args.get("channel_url") or ""
    ).strip()
    if not channel_url:
        return _error("channel_url is required", 400)
    try:
        limit = int(payload.get("limit") or request.args.get("limit") or 5)
    except (TypeError, ValueError):
        limit = 5
    channel_key = str(
        payload.get("channel_key") or request.args.get("channel_key") or ""
    ).strip()
    state_uri = str(
        payload.get("state_uri") or request.args.get("state_uri") or ""
    ).strip() or _channel_state_uri(channel_url, channel_key)
    mark_now = _as_bool(payload.get("mark", request.args.get("mark")))

    try:
        _parse_any_gs_uri(state_uri)
        videos = douyin_channel.list_user_videos(
            channel_url, limit=limit, cookie=DOUYIN_COOKIE or None
        )
    except douyin_channel.DouyinChannelError as exc:
        return _error(str(exc), 502)
    except Exception as exc:  # noqa: BLE001
        return _error(str(exc), 500)

    state = _load_processed(state_uri)
    seen = set(state.get("processed") or [])
    new_videos = [v for v in videos if v.aweme_id not in seen]

    if mark_now and new_videos:
        merged = list(state.get("processed") or []) + [
            v.aweme_id for v in new_videos
        ]
        _save_processed(state_uri, merged)

    return jsonify(
        {
            "ok": True,
            "state_uri": state_uri,
            "scanned": len(videos),
            "new_count": len(new_videos),
            "videos": [
                {**v.to_dict(), "douyin_url": v.share_url} for v in new_videos
            ],
        }
    )


@app.route("/channel/mark", methods=["POST"])
def channel_mark():
    """Đánh dấu 1 hoặc nhiều aweme_id là ĐÃ XỬ LÝ (gọi sau khi upload thành công)."""
    if not _check_api_key():
        return _error("unauthorized", 401)

    payload = request.get_json(silent=True) or {}
    channel_url = str(payload.get("channel_url") or "").strip()
    channel_key = str(payload.get("channel_key") or "").strip()
    state_uri = str(payload.get("state_uri") or "").strip() or (
        _channel_state_uri(channel_url, channel_key) if (channel_url or channel_key) else ""
    )
    if not state_uri:
        return _error("state_uri or channel_url/channel_key is required", 400)

    raw_ids = payload.get("aweme_id") or payload.get("aweme_ids") or []
    if isinstance(raw_ids, (str, int)):
        raw_ids = [raw_ids]
    ids = [str(i).strip() for i in raw_ids if str(i).strip()]
    if not ids:
        return _error("aweme_id(s) is required", 400)

    try:
        _parse_any_gs_uri(state_uri)
        state = _load_processed(state_uri)
        seen = set(state.get("processed") or [])
        merged = list(state.get("processed") or [])
        added = []
        for i in ids:
            if i not in seen:
                merged.append(i)
                seen.add(i)
                added.append(i)
        _save_processed(state_uri, merged)
    except Exception as exc:  # noqa: BLE001
        return _error(str(exc), 500)

    return jsonify(
        {"ok": True, "state_uri": state_uri, "added": added, "total": len(merged)}
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
