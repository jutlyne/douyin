from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

import google.auth
import requests
from google.auth.transport.requests import AuthorizedSession

from container_long.gemini_long import detect_ad_spans, detect_video_ad_spans
from container_short.steps import gcsio


PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
RUN_REGION = os.environ.get("RUN_REGION", "asia-southeast1")
WORKER_JOB_NAME = os.environ.get("WORKER_JOB_NAME", "long-worker")
ASSEMBLER_JOB_NAME = os.environ.get(
    "ASSEMBLER_JOB_NAME", "long-assembler"
)
YOUTUBE_UPLOADER_JOB_NAME = os.environ.get(
    "YOUTUBE_UPLOADER_JOB_NAME", "long-youtube-uploader"
)
THUMBNAIL_GENERATOR_JOB_NAME = os.environ.get(
    "THUMBNAIL_GENERATOR_JOB_NAME", "long-thumbnail-generator"
)
REVIEW_JOB_NAME = os.environ.get("REVIEW_JOB_NAME", "long-review")
# Gemini ad detection (text-only over the final subtitles). flash-lite is only
# served on Vertex `global`, so detection uses VERTEX_REGION, not RUN_REGION.
VERTEX_REGION = os.environ.get("VERTEX_REGION", "global")
GEMINI_AD_MODEL = os.environ.get(
    "GEMINI_AD_MODEL",
    os.environ.get("GEMINI_MODEL", "gemini-2.5-pro"),
)
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "10"))
WAIT_TIMEOUT_SECONDS = int(os.environ.get("WAIT_TIMEOUT_SECONDS", "43200"))
WORKER_FANOUT = max(1, int(os.environ.get("WORKER_FANOUT", "2")))
WORKER_SOURCE_MAX_ATTEMPTS = max(
    1,
    int(os.environ.get("WORKER_SOURCE_MAX_ATTEMPTS", "2")),
)
LONG_SOURCE_CACHE_VERSION = (
    os.environ.get("LONG_SOURCE_CACHE_VERSION", "v4").strip() or "v4"
)
# Legacy download_url for the n8n download/upload path.
DOWNLOAD_SECRET = os.environ.get("DOWNLOAD_SECRET", "")
DOWNLOAD_TTL_SECONDS = int(os.environ.get("DOWNLOAD_TTL_SECONDS", "43200"))
SERVICE_BASE_URL = os.environ.get("SERVICE_BASE_URL", "").rstrip("/")

credentials, _ = google.auth.default(
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)
auth_session = AuthorizedSession(credentials)


def _require_project_id() -> str:
    if not PROJECT_ID:
        raise ValueError("GCP_PROJECT_ID environment variable is required")
    return PROJECT_ID


class BatchSourceFailure(RuntimeError):
    def __init__(self, payload: dict):
        self.payload = payload
        super().__init__(str(payload.get("error") or "long batch failed"))


class TransientOperationReadError(RuntimeError):
    pass


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _callback(payload: dict, *, enabled: bool, url: str) -> None:
    if not enabled or not url:
        return
    import requests

    # The authoritative result is always written to status.json before this is
    # called, so a down/scaled-to-zero n8n must never fail the job — just log.
    try:
        response = requests.post(url, json=payload, timeout=30)
        print(
            f"[long-coordinator] callback HTTP {response.status_code}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[long-coordinator] callback failed (result saved in status.json): "
            f"{exc}",
            flush=True,
        )


def _signature(uri: str, expires: int) -> str:
    payload = f"{uri}\n{expires}".encode()
    digest = hmac.new(DOWNLOAD_SECRET.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _download_url(uri: str) -> str:
    """Signed HMAC download URL for the legacy n8n path."""
    if not DOWNLOAD_SECRET or not SERVICE_BASE_URL:
        return ""
    expires = int(time.time()) + DOWNLOAD_TTL_SECONDS
    query = urlencode(
        {"uri": uri, "expires": expires, "sig": _signature(uri, expires)}
    )
    return f"{SERVICE_BASE_URL}/download?{query}"


def _browser_url(uri: str) -> str:
    """Browser-viewable GCS URL: streams + seeks inline (needs Google login).

    Used for the review preview so a reviewer can scrub a 1h video in the
    browser instead of downloading the whole MP4.
    """
    if not uri.startswith("gs://"):
        return ""
    return "https://storage.cloud.google.com/" + uri[len("gs://"):]


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


def _youtube_fields(metadata: dict) -> dict:
    """Build YouTube title/description fields from final-long.json."""
    title = str(metadata.get("title_vi") or "Video tong hop").strip()
    description = str(metadata.get("description_vi") or "").strip()
    hashtags = [
        str(tag).strip().lstrip("#")
        for tag in (metadata.get("hashtags") or [])
        if str(tag).strip()
    ]
    chapters = [str(c).strip() for c in (metadata.get("chapters") or []) if str(c).strip()]
    chapter_block = "\n".join(chapters)
    hashtag_text = _public_youtube_hashtag_text()
    youtube_description = "\n\n".join(
        part for part in (description, chapter_block, hashtag_text) if part
    )
    return {
        "title_vi": title,
        "description_vi": description,
        "hashtags": hashtags,
        "youtube_title": title[:100],
        "youtube_description": youtube_description[:5000],
    }


def _series_youtube_fields(fields: dict, request_data: dict) -> dict:
    """Append a stable Part marker without exceeding YouTube's title limit."""
    raw_part = request_data.get("series_part_number")
    if raw_part in (None, ""):
        return fields
    try:
        part_number = max(1, int(float(raw_part)))
    except (TypeError, ValueError):
        return fields
    suffix = f" | Phần {part_number}"
    title = str(fields.get("youtube_title") or fields.get("title_vi") or "")
    fields = dict(fields)
    fields["youtube_title"] = f"{title[: 100 - len(suffix)].rstrip()}{suffix}"
    fields["series_part_number"] = part_number
    return fields


def _request_env_value(
    request_data: dict,
    request_key: str,
    env_key: str,
    default: str = "",
) -> str:
    value = request_data.get(request_key)
    if value is None or value == "":
        value = os.environ.get(env_key, default)
    return str(value).strip()


def _youtube_upload_enabled(request_data: dict) -> bool:
    if "youtube_upload_enabled" in request_data:
        return _as_bool(request_data.get("youtube_upload_enabled"))
    return _as_bool(os.environ.get("YOUTUBE_UPLOAD_ENABLED", "false"))


def _thumbnail_generation_enabled(request_data: dict) -> bool:
    if "thumbnail_generate_enabled" in request_data:
        return _as_bool(request_data.get("thumbnail_generate_enabled"))
    return _as_bool(os.environ.get("THUMBNAIL_GENERATE_ENABLED", "false"))


def _thumbnail_required(request_data: dict) -> bool:
    if "thumbnail_required" in request_data:
        return _as_bool(request_data.get("thumbnail_required"))
    return _as_bool(os.environ.get("THUMBNAIL_REQUIRED", "false"))


def _review_enabled(request_data: dict) -> bool:
    """Whether to gate upload for human ad-review instead of auto-uploading."""
    if "review_mode" in request_data:
        return _as_bool(request_data.get("review_mode"))
    return _as_bool(os.environ.get("LONG_REVIEW_MODE", "false"))


def _auto_remove_ads_enabled(request_data: dict) -> bool:
    if "auto_remove_ads" in request_data:
        return _as_bool(request_data.get("auto_remove_ads"))
    return _as_bool(os.environ.get("LONG_AUTO_REMOVE_ADS", "false"))


def _auto_ad_min_confidence(request_data: dict) -> float:
    raw = request_data.get(
        "auto_ad_min_confidence",
        os.environ.get("LONG_AUTO_AD_MIN_CONFIDENCE", "0.85"),
    )
    return max(0.5, min(1.0, float(raw)))


def _auto_ad_max_passes(request_data: dict) -> int:
    raw = request_data.get(
        "auto_ad_max_passes",
        os.environ.get("LONG_AUTO_AD_MAX_PASSES", "2"),
    )
    return max(1, min(3, int(raw)))


def _hms(seconds: float) -> str:
    total = max(0, int(float(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _merge_ad_candidates(spans: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for item in sorted(spans, key=lambda span: float(span["start"])):
        current = {
            **item,
            "start": max(0.0, float(item["start"])),
            "end": float(item["end"]),
            "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0.0))),
            "sources": sorted(set(item.get("sources") or [])),
        }
        if current["end"] <= current["start"]:
            continue
        if merged and current["start"] <= merged[-1]["end"] + 1.0:
            previous = merged[-1]
            previous["end"] = max(previous["end"], current["end"])
            previous["confidence"] = max(
                previous["confidence"], current["confidence"]
            )
            previous["sources"] = sorted(
                set(previous["sources"] + current["sources"])
            )
            reasons = [
                reason
                for reason in (
                    str(previous.get("reason_vi") or "").strip(),
                    str(current.get("reason_vi") or "").strip(),
                )
                if reason
            ]
            previous["reason_vi"] = " | ".join(dict.fromkeys(reasons))
        else:
            merged.append(current)
    return merged


def _detect_ad_candidates(
    metadata: dict,
    *,
    video_uri: str = "",
) -> tuple[list[dict], str]:
    """Detect promotions from both subtitles and the complete audiovisual file.

    Returns ``(candidates, error)``; ``error`` is a short diagnostic string when
    detection could not run (surfaced in status.json as ``ad_detection_error``).
    """
    subtitles = metadata.get("subtitles") or []
    spans: list[dict] = []
    errors: list[str] = []
    if subtitles:
        try:
            spans.extend(
                {
                    **span,
                    "sources": ["subtitles"],
                }
                for span in detect_ad_spans(
                    subtitles=subtitles,
                    project_id=_require_project_id(),
                    region=VERTEX_REGION,
                    model=GEMINI_AD_MODEL,
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"subtitles {type(exc).__name__}: {exc}")
    else:
        errors.append("metadata has no subtitles")
    if video_uri:
        try:
            spans.extend(
                {
                    **span,
                    "sources": ["audiovisual"],
                }
                for span in detect_video_ad_spans(
                    video_uri=video_uri,
                    project_id=_require_project_id(),
                    region=VERTEX_REGION,
                    model=GEMINI_AD_MODEL,
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"audiovisual {type(exc).__name__}: {exc}")
    else:
        errors.append("video_uri is missing")
    if errors:
        error = "; ".join(errors)
        print(f"[long-coordinator] ad detection warning: {error}", flush=True)
    else:
        error = ""
    candidates = [
        {
            **span,
            "start_hms": _hms(span["start"]),
            "end_hms": _hms(span["end"]),
        }
        for span in _merge_ad_candidates(spans)
    ]
    return candidates, error


def _run_review_job(*, output_prefix: str, spans: list[list[float]]) -> str:
    return _run_job(
        REVIEW_JOB_NAME,
        env={
            "OUTPUT_PREFIX": output_prefix,
            "CUT_SPANS": json.dumps(spans),
        },
    )


def _youtube_upload_env(
    *,
    request_data: dict,
    batch_id: str,
    chat_id: str,
    final_uri: str,
    metadata_uri: str,
    upload_result_uri: str,
    youtube_fields: dict,
    thumbnail_uri: str = "",
) -> dict[str, str]:
    tags = youtube_fields.get("hashtags") or []
    env = {
        "VIDEO_URI": final_uri,
        "METADATA_URI": metadata_uri,
        "UPLOAD_RESULT_URI": upload_result_uri,
        "BATCH_ID": batch_id,
        "CHAT_ID": chat_id,
        "YOUTUBE_TITLE": youtube_fields.get("youtube_title", ""),
        "YOUTUBE_DESCRIPTION": youtube_fields.get(
            "youtube_description", ""
        ),
        "YOUTUBE_PRIVACY_STATUS": _request_env_value(
            request_data,
            "youtube_privacy_status",
            "YOUTUBE_PRIVACY_STATUS",
            "private",
        ),
        "YOUTUBE_PUBLISH_AT": _request_env_value(
            request_data,
            "youtube_publish_at",
            "YOUTUBE_PUBLISH_AT",
        ),
        "YOUTUBE_CATEGORY_ID": _request_env_value(
            request_data,
            "youtube_category_id",
            "YOUTUBE_CATEGORY_ID",
            "24",
        ),
        "YOUTUBE_MADE_FOR_KIDS": _request_env_value(
            request_data,
            "youtube_made_for_kids",
            "YOUTUBE_MADE_FOR_KIDS",
            "false",
        ),
        "YOUTUBE_NOTIFY_SUBSCRIBERS": _request_env_value(
            request_data,
            "youtube_notify_subscribers",
            "YOUTUBE_NOTIFY_SUBSCRIBERS",
            "false",
        ),
        "YOUTUBE_TAGS": ",".join(str(tag).lstrip("#") for tag in tags),
        "YOUTUBE_THUMBNAIL_URI": thumbnail_uri,
    }
    return {
        key: str(value)
        for key, value in env.items()
        if value is not None and str(value) != ""
    }


def _run_thumbnail_generation(
    *,
    request_data: dict,
    batch_id: str,
    metadata_uri: str,
    thumbnail_uri: str,
    result_uri: str,
) -> dict:
    reference_uri = _request_env_value(
        request_data,
        "thumbnail_reference_uri",
        "THUMBNAIL_REFERENCE_URI",
    )
    if not reference_uri:
        raise ValueError("thumbnail_reference_uri is required")
    env = {
        "BATCH_ID": batch_id,
        "METADATA_URI": metadata_uri,
        "REFERENCE_IMAGE_URI": reference_uri,
        "THUMBNAIL_URI": thumbnail_uri,
        "THUMBNAIL_RESULT_URI": result_uri,
        "PART_NUMBER": _request_env_value(
            request_data,
            "series_part_number",
            "THUMBNAIL_PART_NUMBER",
            "1",
        ),
        "THUMBNAIL_HEADLINE": _request_env_value(
            request_data,
            "thumbnail_headline",
            "THUMBNAIL_HEADLINE",
            "XUYÊN KHÔNG ĐẠI ĐƯỜNG",
        ),
        "THUMBNAIL_FORCE_REFRESH": _request_env_value(
            request_data,
            "thumbnail_force_refresh",
            "THUMBNAIL_FORCE_REFRESH",
            "false",
        ),
    }
    model = _request_env_value(
        request_data,
        "thumbnail_model",
        "THUMBNAIL_MODEL",
    )
    if model:
        env["THUMBNAIL_MODEL"] = model
    operation = _run_job(THUMBNAIL_GENERATOR_JOB_NAME, env=env)
    _wait_operation(operation)
    result = _load_json(result_uri)
    if not result.get("ok"):
        raise RuntimeError(
            str(result.get("error") or "Thumbnail generation failed")
        )
    result["operation"] = operation
    result["result_uri"] = result_uri
    return result


def _run_youtube_upload(
    *,
    request_data: dict,
    batch_id: str,
    chat_id: str,
    final_uri: str,
    metadata_uri: str,
    upload_result_uri: str,
    youtube_fields: dict,
    thumbnail_uri: str = "",
) -> dict:
    operation = _run_job(
        YOUTUBE_UPLOADER_JOB_NAME,
        env=_youtube_upload_env(
            request_data=request_data,
            batch_id=batch_id,
            chat_id=chat_id,
            final_uri=final_uri,
            metadata_uri=metadata_uri,
            upload_result_uri=upload_result_uri,
            youtube_fields=youtube_fields,
            thumbnail_uri=thumbnail_uri,
        ),
    )
    _wait_operation(operation)
    upload_result = _load_json(upload_result_uri)
    if not upload_result.get("ok"):
        raise RuntimeError(
            str(upload_result.get("error") or "YouTube upload failed")
        )
    upload_result["operation"] = operation
    upload_result["result_uri"] = upload_result_uri
    return upload_result

def _run_job(
    job_name: str,
    *,
    env: dict[str, str],
    task_count: int = 1,
) -> str:
    endpoint = (
        f"https://run.googleapis.com/v2/projects/{_require_project_id()}/locations/"
        f"{RUN_REGION}/jobs/{job_name}:run"
    )
    body = {
        "overrides": {
            "containerOverrides": [{
                "env": [
                    {"name": key, "value": str(value)}
                    for key, value in env.items()
                    if value is not None
                ]
            }],
            "taskCount": task_count,
        }
    }
    response = auth_session.post(endpoint, json=body, timeout=60)
    response.raise_for_status()
    operation_name = str(response.json().get("name") or "")
    if not operation_name:
        raise RuntimeError(f"{job_name}: jobs.run returned no operation name")
    return operation_name


def _is_transient_http_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if int(status_code or 0) in {429, 500, 502, 503, 504}:
        return True
    return isinstance(
        exc,
        (requests.exceptions.ConnectionError, requests.exceptions.Timeout),
    )


def _auto_remove_ads(
    *,
    request_data: dict,
    output_prefix: str,
    video_uri: str,
    metadata_uri: str,
    metadata: dict,
) -> tuple[dict, dict]:
    """Audit, cut high-confidence ads, and re-audit before completion."""
    threshold = _auto_ad_min_confidence(request_data)
    max_passes = _auto_ad_max_passes(request_data)
    audit: dict = {
        "enabled": True,
        "min_confidence": threshold,
        "max_passes": max_passes,
        "passes": [],
        "applied_cuts": [],
    }
    current = metadata
    for pass_number in range(1, max_passes + 1):
        candidates, detect_error = _detect_ad_candidates(
            current,
            video_uri=video_uri,
        )
        if detect_error:
            raise RuntimeError(f"automatic ad audit incomplete: {detect_error}")
        approved = [
            item
            for item in candidates
            if float(item.get("confidence") or 0.0) >= threshold
        ]
        audit["passes"].append({
            "pass": pass_number,
            "candidates": candidates,
            "approved_cuts": approved,
        })
        if not approved:
            audit["clean"] = not candidates
            audit["remaining_low_confidence"] = candidates
            return current, audit
        spans = [
            [float(item["start"]), float(item["end"])]
            for item in approved
        ]
        print(
            f"[long-coordinator] auto-removing ads pass {pass_number}: {spans}",
            flush=True,
        )
        _wait_operation(_run_review_job(output_prefix=output_prefix, spans=spans))
        audit["applied_cuts"].extend(spans)
        current = _load_json(metadata_uri)

    remaining, detect_error = _detect_ad_candidates(
        current,
        video_uri=video_uri,
    )
    if detect_error:
        raise RuntimeError(f"automatic ad re-audit incomplete: {detect_error}")
    remaining_high = [
        item
        for item in remaining
        if float(item.get("confidence") or 0.0) >= threshold
    ]
    if remaining_high:
        raise RuntimeError(
            "automatic ad removal reached pass limit with ads remaining"
        )
    audit["clean"] = not remaining
    audit["remaining_low_confidence"] = remaining
    return current, audit


def _read_operation(operation_name: str) -> dict:
    endpoint = f"https://run.googleapis.com/v2/{operation_name}"
    try:
        response = auth_session.get(endpoint, timeout=30)
        response.raise_for_status()
    except Exception as exc:
        if _is_transient_http_error(exc):
            raise TransientOperationReadError(str(exc)) from exc
        raise
    return response.json()


def _operation_error_message(operation: dict) -> str:
    error = operation.get("error")
    if not error:
        return ""
    return str(error.get("message") or error)


def _raise_operation_error(operation: dict, *, label: str) -> None:
    message = _operation_error_message(operation)
    if message:
        raise RuntimeError(f"{label}: {message}")


def _wait_operation(operation_name: str) -> dict:
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            operation = _read_operation(operation_name)
        except TransientOperationReadError as exc:
            print(
                f"[long-coordinator] transient operation poll error "
                f"{operation_name}: {exc}",
                flush=True,
            )
            time.sleep(POLL_SECONDS)
            continue
        if operation.get("done"):
            _raise_operation_error(operation, label=operation_name)
            return operation
        time.sleep(POLL_SECONDS)
    raise TimeoutError(
        f"Cloud Run operation exceeded {WAIT_TIMEOUT_SECONDS} seconds"
    )


def _load_json(uri: str) -> dict:
    work = tempfile.mkdtemp(prefix="long_coordinator_read_")
    path = os.path.join(work, "data.json")
    gcsio.download(uri, path)
    with open(path, encoding="utf-8-sig") as handle:
        return json.load(handle)


def _upload_json(value: dict, uri: str) -> None:
    work = tempfile.mkdtemp(prefix="long_coordinator_write_")
    path = os.path.join(work, "data.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    gcsio.upload(path, uri, content_type="application/json")


def _normalize_url(value: str) -> str:
    raw = str(value or "").strip()
    parts = urlsplit(raw)
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    path = "/" + parts.path.strip("/")
    if path != "/":
        path += "/"
    return urlunsplit((scheme, netloc, path, "", ""))


def _source_cache_key(normalized_url: str, processing_profile: str = "") -> str:
    payload = f"{normalized_url}\n{processing_profile}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


def _output_root_from_prefix(output_prefix: str) -> str:
    return output_prefix.rstrip("/").rsplit("/", 1)[0]


def _source_artifact_base(prefix: str, index: int) -> str:
    return f"{prefix.rstrip('/')}/processed/{index:03d}/final"


def _source_cache_base(output_prefix: str, cache_key: str) -> str:
    root = _output_root_from_prefix(output_prefix)
    return (
        f"{root}/_source-cache/{LONG_SOURCE_CACHE_VERSION}/{cache_key}/final"
    )


def _raw_source_uri(output_prefix: str, normalized_url: str) -> str:
    root = _output_root_from_prefix(output_prefix)
    raw_key = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:32]
    return f"{root}/_raw-source-cache/v1/{raw_key}/source.mp4"


def _source_artifact_uris(base: str) -> dict[str, str]:
    return {
        "output_uri": f"{base}.mp4",
        "metadata_uri": f"{base}.json",
        "subtitle_uri": f"{base}.srt",
    }


def _source_artifacts_exist(base: str) -> bool:
    uris = _source_artifact_uris(base)
    return all(gcsio.exists(uri) for uri in uris.values())


def _copy_source_artifacts(src_base: str, dst_base: str) -> None:
    src = _source_artifact_uris(src_base)
    dst = _source_artifact_uris(dst_base)
    gcsio.copy(src["output_uri"], dst["output_uri"])
    gcsio.copy(src["subtitle_uri"], dst["subtitle_uri"])
    metadata = _load_json(src["metadata_uri"])
    metadata.update(dst)
    _upload_json(metadata, dst["metadata_uri"])


def _build_manifest(
    *,
    batch_id: str,
    output_prefix: str,
    count: int,
) -> dict:
    items: list[dict] = []
    for index in range(count):
        base = f"{output_prefix}/processed/{index:03d}/final"
        metadata_uri = f"{base}.json"
        metadata = _load_json(metadata_uri)
        items.append({
            "index": index,
            "output_uri": f"{base}.mp4",
            "metadata_uri": metadata_uri,
            "subtitle_uri": f"{base}.srt",
            "title_vi": str(metadata.get("title_vi") or ""),
            "duration": float(metadata.get("duration") or 0.0),
        })
    return {
        "batch_id": batch_id,
        "expected_count": count,
        "items": items,
    }


def _video_url(item, *, index: int) -> str:
    if isinstance(item, str):
        url = item.strip()
    elif isinstance(item, dict):
        url = str(item.get("douyin_url") or "").strip()
    else:
        raise ValueError(f"videos[{index}] must be a string or object")
    if not url:
        raise ValueError(f"videos[{index}] has an empty douyin_url")
    return url


def _worker_output_uri(work_prefix: str, index: int) -> str:
    return f"{_source_artifact_base(work_prefix, index)}.mp4"


def _source_force_refresh(item, *, force_refresh_all: bool) -> bool:
    return force_refresh_all or (
        isinstance(item, dict) and _as_bool(item.get("force_refresh"))
    )


def _source_record(
    source: dict,
    *,
    status: str,
    attempts: int,
    operation: str = "",
    error: str = "",
    cache_error: str = "",
) -> dict:
    record = {
        "index": source["index"],
        "douyin_url": source["douyin_url"],
        "normalized_url": source["normalized_url"],
        "force_refresh": source["force_refresh"],
        "cache_key": source["cache_key"],
        "processing_profile": source.get("processing_profile", ""),
        "raw_source_uri": source["raw_source_uri"],
        "output_uri": source["output_uri"],
        "metadata_uri": source["metadata_uri"],
        "subtitle_uri": source["subtitle_uri"],
        "cache_output_uri": source["cache_output_uri"],
        "status": status,
        "attempts": attempts,
    }
    if operation:
        record["operation"] = operation
    if error:
        record["error"] = error
    if cache_error:
        record["cache_error"] = cache_error
    return record


def _build_source(
    *,
    index: int,
    item,
    output_prefix: str,
    work_prefix: str,
    force_refresh_all: bool,
    processing_profile: str = "",
) -> dict:
    url = _video_url(item, index=index)
    normalized_url = _normalize_url(url)
    cache_key = _source_cache_key(normalized_url, processing_profile)
    output_base = _source_artifact_base(work_prefix, index)
    cache_base = _source_cache_base(output_prefix, cache_key)
    uris = _source_artifact_uris(output_base)
    cache_uris = _source_artifact_uris(cache_base)
    return {
        "index": index,
        "item": item,
        "douyin_url": url,
        "normalized_url": normalized_url,
        "force_refresh": _source_force_refresh(
            item,
            force_refresh_all=force_refresh_all,
        ),
        "cache_key": cache_key,
        "processing_profile": processing_profile,
        "raw_source_uri": _raw_source_uri(output_prefix, normalized_url),
        "output_base": output_base,
        "cache_base": cache_base,
        "output_uri": uris["output_uri"],
        "metadata_uri": uris["metadata_uri"],
        "subtitle_uri": uris["subtitle_uri"],
        "cache_output_uri": cache_uris["output_uri"],
        "cache_metadata_uri": cache_uris["metadata_uri"],
        "cache_subtitle_uri": cache_uris["subtitle_uri"],
    }


def _run_worker_sources(
    *,
    videos: list,
    batch_id: str,
    output_prefix: str,
    work_prefix: str,
    force_refresh_all: bool,
    worker_env: dict[str, str] | None = None,
) -> dict:
    worker_env = dict(worker_env or {})
    processing_profile = (
        json.dumps(
            worker_env,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        if worker_env
        else ""
    )
    sources = [
        _build_source(
            index=index,
            item=item,
            output_prefix=output_prefix,
            work_prefix=work_prefix,
            force_refresh_all=force_refresh_all,
            processing_profile=processing_profile,
        )
        for index, item in enumerate(videos)
    ]
    pending: list[tuple[dict, int]] = []
    active: list[tuple[dict, int, str]] = []
    completed_sources: list[dict] = []
    cached_sources: list[dict] = []
    failed_sources: list[dict] = []
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS

    for source in sources:
        try:
            cache_hit = (
                not source["force_refresh"]
                and _source_artifacts_exist(source["cache_base"])
            )
        except Exception as exc:  # noqa: BLE001
            cache_hit = False
            print(
                f"[long-coordinator] cache check failed source "
                f"{source['index']:03d}; rerunning worker: {exc}",
                flush=True,
            )
        if cache_hit:
            try:
                _copy_source_artifacts(source["cache_base"], source["output_base"])
                cached_sources.append(
                    _source_record(source, status="cached", attempts=0)
                )
                print(
                    f"[long-coordinator] cache hit source "
                    f"{source['index']:03d}: {source['cache_output_uri']}",
                    flush=True,
                )
                continue
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[long-coordinator] cache copy failed source "
                    f"{source['index']:03d}; rerunning worker: {exc}",
                    flush=True,
                )
        pending.append((source, 1))

    def fail_attempt(
        *,
        source: dict,
        attempt: int,
        error: str,
        operation: str = "",
    ) -> None:
        print(
            f"[long-coordinator] failed source {source['index']:03d} "
            f"attempt {attempt}/{WORKER_SOURCE_MAX_ATTEMPTS}: {error}",
            flush=True,
        )
        if attempt < WORKER_SOURCE_MAX_ATTEMPTS:
            pending.append((source, attempt + 1))
            print(
                f"[long-coordinator] queued retry for source "
                f"{source['index']:03d} "
                f"attempt {attempt + 1}/{WORKER_SOURCE_MAX_ATTEMPTS}",
                flush=True,
            )
        else:
            failed_sources.append(
                _source_record(
                    source,
                    status="failed",
                    attempts=attempt,
                    operation=operation,
                    error=error,
                )
            )

    def start_next() -> None:
        source, attempt = pending.pop(0)
        try:
            operation = _run_job(
                WORKER_JOB_NAME,
                env={
                    "DOUYIN_URL": source["douyin_url"],
                    "OUTPUT_URI": source["output_uri"],
                    "SOURCE_INDEX": str(source["index"]),
                    "SOURCE_ATTEMPT": str(attempt),
                    "BATCH_ID": batch_id,
                    "CALLBACK_ENABLED": "false",
                    **worker_env,
                    "LONG_RAW_SOURCE_URI": source["raw_source_uri"],
                },
            )
        except Exception as exc:  # noqa: BLE001
            fail_attempt(source=source, attempt=attempt, error=str(exc))
            return
        print(
            f"[long-coordinator] started source {source['index']:03d} "
            f"attempt {attempt}/{WORKER_SOURCE_MAX_ATTEMPTS}: {operation}",
            flush=True,
        )
        active.append((source, attempt, operation))

    while pending and len(active) < WORKER_FANOUT:
        start_next()

    while active:
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Cloud Run workers exceeded {WAIT_TIMEOUT_SECONDS} seconds"
            )
        next_active: list[tuple[dict, int, str]] = []
        completed = False
        for source, attempt, operation_name in active:
            try:
                operation = _read_operation(operation_name)
            except TransientOperationReadError as exc:
                print(
                    f"[long-coordinator] transient operation poll error "
                    f"source {source['index']:03d} attempt "
                    f"{attempt}/{WORKER_SOURCE_MAX_ATTEMPTS}: {exc}",
                    flush=True,
                )
                next_active.append((source, attempt, operation_name))
                continue
            if not operation.get("done"):
                next_active.append((source, attempt, operation_name))
                continue
            error = _operation_error_message(operation)
            if error:
                completed = True
                fail_attempt(
                    source=source,
                    attempt=attempt,
                    operation=operation_name,
                    error=error,
                )
                continue
            completed = True
            if not _source_artifacts_exist(source["output_base"]):
                fail_attempt(
                    source=source,
                    attempt=attempt,
                    operation=operation_name,
                    error="worker completed but source artifacts are missing",
                )
                continue
            cache_error = ""
            try:
                _copy_source_artifacts(source["output_base"], source["cache_base"])
            except Exception as exc:  # noqa: BLE001
                cache_error = str(exc)
                print(
                    f"[long-coordinator] cache write failed source "
                    f"{source['index']:03d}: {cache_error}",
                    flush=True,
                )
            completed_sources.append(
                _source_record(
                    source,
                    status="completed",
                    attempts=attempt,
                    operation=operation_name,
                    cache_error=cache_error,
                )
            )
            print(
                f"[long-coordinator] completed source {source['index']:03d} "
                f"attempt {attempt}/{WORKER_SOURCE_MAX_ATTEMPTS}",
                flush=True,
            )
        active = next_active
        while pending and len(active) < WORKER_FANOUT:
            start_next()
        if active and not completed:
            time.sleep(POLL_SECONDS)
    return {
        "completed_sources": sorted(
            completed_sources,
            key=lambda item: int(item["index"]),
        ),
        "cached_sources": sorted(
            cached_sources,
            key=lambda item: int(item["index"]),
        ),
        "failed_sources": sorted(
            failed_sources,
            key=lambda item: int(item["index"]),
        ),
    }


def _run_full() -> dict:
    request_uri = os.environ.get("REQUEST_URI", "").strip()
    output_prefix = os.environ.get("OUTPUT_PREFIX", "").strip().rstrip("/")
    work_prefix = (
        os.environ.get("WORK_PREFIX", "").strip().rstrip("/")
        or output_prefix
    )
    if not request_uri or not output_prefix:
        raise ValueError("REQUEST_URI and OUTPUT_PREFIX are required")

    request_data = _load_json(request_uri)
    videos = request_data.get("videos") or []
    if not 1 <= len(videos) <= 30:
        raise ValueError("videos must contain between 1 and 30 items")
    batch_id = str(
        request_data.get("batch_id") or os.environ.get("BATCH_ID") or ""
    ).strip()
    if not batch_id:
        raise ValueError("batch_id is required")
    callback_enabled = _as_bool(request_data.get("callback_enabled"))
    callback_url = str(request_data.get("callback_url") or "").strip()
    chat_id = str(request_data.get("chat_id") or "").strip()
    force_refresh_all = _as_bool(request_data.get("force_refresh"))
    status_uri = f"{output_prefix}/status.json"
    worker_env: dict[str, str] = {}
    for request_key, env_key in (
        ("source_start_seconds", "LONG_SOURCE_START_SECONDS"),
        ("source_max_seconds", "LONG_SOURCE_MAX_SECONDS"),
        ("cliffhanger_enabled", "LONG_CLIFFHANGER_ENABLED"),
        ("cliffhanger_min_seconds", "LONG_CLIFFHANGER_MIN_SECONDS"),
        ("cliffhanger_max_seconds", "LONG_CLIFFHANGER_MAX_SECONDS"),
        ("chunk_seconds", "LONG_CHUNK_SECONDS"),
        ("min_chunk_seconds", "LONG_MIN_CHUNK_SECONDS"),
        ("max_chunk_seconds", "LONG_MAX_CHUNK_SECONDS"),
    ):
        if request_key in request_data:
            worker_env[env_key] = str(request_data[request_key])

    source_state = _run_worker_sources(
        videos=videos,
        batch_id=batch_id,
        output_prefix=output_prefix,
        work_prefix=work_prefix,
        force_refresh_all=force_refresh_all,
        worker_env=worker_env,
    )
    if source_state["failed_sources"]:
        failure_payload = {
            "event": "long.batch.failed",
            "ok": False,
            "status": "failed",
            "batch_id": batch_id,
            "chat_id": chat_id,
            "request_uri": request_uri,
            "status_uri": status_uri,
            "completed_sources": source_state["completed_sources"],
            "cached_sources": source_state["cached_sources"],
            "failed_sources": source_state["failed_sources"],
            "error": "one or more source videos failed",
        }
        _upload_json(failure_payload, status_uri)
        raise BatchSourceFailure(failure_payload)

    manifest = _build_manifest(
        batch_id=batch_id,
        output_prefix=work_prefix,
        count=len(videos),
    )
    manifest_uri = f"{output_prefix}/manifest.json"
    _upload_json(manifest, manifest_uri)
    final_uri = f"{output_prefix}/final/final-long.mp4"
    assembler_operation = _run_job(
        ASSEMBLER_JOB_NAME,
        env={
            "MANIFEST_URI": manifest_uri,
            "OUTPUT_URI": final_uri,
            "BATCH_ID": batch_id,
            "CALLBACK_ENABLED": "false",
        },
    )
    _wait_operation(assembler_operation)
    metadata_uri = f"{output_prefix}/final/final-long.json"
    # Load metadata so callback/upload has title and description fields.
    try:
        long_meta = _load_json(metadata_uri)
    except Exception:  # noqa: BLE001 - metadata is optional here
        long_meta = {}
    ad_audit: dict = {}
    if _auto_remove_ads_enabled(request_data):
        long_meta, ad_audit = _auto_remove_ads(
            request_data=request_data,
            output_prefix=output_prefix,
            video_uri=final_uri,
            metadata_uri=metadata_uri,
            metadata=long_meta,
        )
    youtube_fields = _series_youtube_fields(
        _youtube_fields(long_meta),
        request_data,
    )
    youtube_upload_enabled = _youtube_upload_enabled(request_data)
    result = {
        "event": "long.batch.completed",
        "ok": True,
        "status": "completed",
        "batch_id": batch_id,
        "chat_id": chat_id,
        "manifest_uri": manifest_uri,
        "output_uri": final_uri,
        "metadata_uri": metadata_uri,
        "chapters_uri": f"{output_prefix}/final/final-long.chapters.txt",
        "subtitle_uri": f"{output_prefix}/final/final-long.srt",
        "status_uri": status_uri,
        "completed_sources": source_state["completed_sources"],
        "cached_sources": source_state["cached_sources"],
        "failed_sources": [],
        "source_parts": long_meta.get("source_parts") or [],
        "youtube_upload_enabled": youtube_upload_enabled,
        "auto_ad_audit": ad_audit,
        **youtube_fields,
    }
    # Human-in-the-loop ad review: when enabled, do NOT upload. Suggest ad spans
    # and wait for the operator to confirm cuts / upload over Telegram.
    if youtube_upload_enabled and _review_enabled(request_data):
        candidates, detect_error = _detect_ad_candidates(
            long_meta,
            video_uri=final_uri,
        )
        review_result = {
            **result,
            "event": "long.review.required",
            "status": "pending_review",
            "ad_candidates": candidates,
            "watch_url": _browser_url(final_uri),
            "download_url": _download_url(final_uri),
        }
        if detect_error:
            review_result["ad_detection_error"] = detect_error
        _upload_json(review_result, status_uri)
        _callback(review_result, enabled=callback_enabled, url=callback_url)
        return review_result
    thumbnail_uri = ""
    thumbnail_result: dict = {}
    if youtube_upload_enabled and _thumbnail_generation_enabled(request_data):
        thumbnail_uri = f"{output_prefix}/final/youtube-thumbnail.jpg"
        thumbnail_result_uri = (
            f"{output_prefix}/final/youtube-thumbnail-generation.json"
        )
        try:
            thumbnail_result = _run_thumbnail_generation(
                request_data=request_data,
                batch_id=batch_id,
                metadata_uri=metadata_uri,
                thumbnail_uri=thumbnail_uri,
                result_uri=thumbnail_result_uri,
            )
        except Exception as exc:  # noqa: BLE001
            if _thumbnail_required(request_data):
                failure_payload = {
                    **result,
                    "event": "long.thumbnail.failed",
                    "ok": False,
                    "status": "thumbnail_failed",
                    "thumbnail_uri": thumbnail_uri,
                    "thumbnail_result_uri": thumbnail_result_uri,
                    "error": str(exc),
                }
                _upload_json(failure_payload, status_uri)
                raise BatchSourceFailure(failure_payload) from exc
            thumbnail_result = {
                "ok": False,
                "status": "failed_optional",
                "error": str(exc),
                "result_uri": thumbnail_result_uri,
            }
            thumbnail_uri = ""
        result.update(
            {
                "thumbnail_uri": thumbnail_uri,
                "thumbnail_result": thumbnail_result,
            }
        )
    if youtube_upload_enabled:
        upload_result_uri = f"{output_prefix}/final/youtube-upload.json"
        try:
            upload_result = _run_youtube_upload(
                request_data=request_data,
                batch_id=batch_id,
                chat_id=chat_id,
                final_uri=final_uri,
                metadata_uri=metadata_uri,
                upload_result_uri=upload_result_uri,
                youtube_fields=youtube_fields,
                thumbnail_uri=thumbnail_uri,
            )
        except Exception as exc:  # noqa: BLE001
            failure_payload = {
                **result,
                "event": "long.youtube.failed",
                "ok": False,
                "status": "upload_failed",
                "youtube_upload_status": "failed",
                "youtube_upload_result_uri": upload_result_uri,
                "error": str(exc),
            }
            _upload_json(failure_payload, status_uri)
            raise BatchSourceFailure(failure_payload) from exc
        result.update({
            "event": "long.youtube.completed",
            "status": "uploaded",
            "youtube_upload_status": "uploaded",
            "youtube_upload_result_uri": upload_result_uri,
            "youtube_upload": upload_result,
            "youtube_video_id": upload_result.get("youtube_video_id", ""),
            "youtube_url": upload_result.get("youtube_url", ""),
        })
    else:
        result["download_url"] = _download_url(final_uri)
    _upload_json(result, status_uri)
    _callback(result, enabled=callback_enabled, url=callback_url)
    return result


def _batch_context() -> dict:
    """Shared request context for cut/upload modes (no worker re-run)."""
    request_uri = os.environ.get("REQUEST_URI", "").strip()
    output_prefix = os.environ.get("OUTPUT_PREFIX", "").strip().rstrip("/")
    if not request_uri or not output_prefix:
        raise ValueError("REQUEST_URI and OUTPUT_PREFIX are required")
    request_data = _load_json(request_uri)
    batch_id = str(
        request_data.get("batch_id") or os.environ.get("BATCH_ID") or ""
    ).strip()
    if not batch_id:
        raise ValueError("batch_id is required")
    return {
        "output_prefix": output_prefix,
        "request_data": request_data,
        "batch_id": batch_id,
        "callback_enabled": _as_bool(request_data.get("callback_enabled")),
        "callback_url": str(request_data.get("callback_url") or "").strip(),
        "chat_id": str(request_data.get("chat_id") or "").strip(),
        "status_uri": f"{output_prefix}/status.json",
    }


def _final_result_uris(output_prefix: str) -> dict:
    base = f"{output_prefix}/final/final-long"
    return {
        "output_uri": f"{base}.mp4",
        "metadata_uri": f"{base}.json",
        "chapters_uri": f"{base}.chapters.txt",
        "subtitle_uri": f"{base}.srt",
    }


def _run_cut() -> dict:
    """Apply confirmed ad-cut spans, then re-detect and ask for review again."""
    ctx = _batch_context()
    output_prefix = ctx["output_prefix"]
    try:
        raw_spans = json.loads(os.environ.get("CUT_SPANS", "[]"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid CUT_SPANS json: {exc}") from exc
    spans = [[float(pair[0]), float(pair[1])] for pair in raw_spans]
    if not spans:
        raise ValueError("CUT_SPANS must contain at least one [start, end] span")

    uris = _final_result_uris(output_prefix)
    _wait_operation(_run_review_job(output_prefix=output_prefix, spans=spans))

    long_meta = _load_json(uris["metadata_uri"])
    candidates, detect_error = _detect_ad_candidates(
        long_meta,
        video_uri=uris["output_uri"],
    )
    result = {
        "event": "long.cut.completed",
        "ok": True,
        "status": "pending_review",
        "batch_id": ctx["batch_id"],
        "chat_id": ctx["chat_id"],
        "status_uri": ctx["status_uri"],
        "applied_cuts": spans,
        "duration": long_meta.get("duration"),
        "ad_candidates": candidates,
        "watch_url": _browser_url(uris["output_uri"]),
        "download_url": _download_url(uris["output_uri"]),
        **uris,
        **_youtube_fields(long_meta),
    }
    if detect_error:
        result["ad_detection_error"] = detect_error
    _upload_json(result, ctx["status_uri"])
    _callback(result, enabled=ctx["callback_enabled"], url=ctx["callback_url"])
    return result


def _run_review() -> dict:
    """Re-run ad detection on an already-assembled batch and re-emit review.

    Cheap: skips worker/assemble, just loads the final metadata, detects ad
    spans and writes a fresh long.review.required (no upload).
    """
    ctx = _batch_context()
    uris = _final_result_uris(ctx["output_prefix"])
    long_meta = _load_json(uris["metadata_uri"])
    candidates, detect_error = _detect_ad_candidates(
        long_meta,
        video_uri=uris["output_uri"],
    )
    result = {
        "event": "long.review.required",
        "ok": True,
        "status": "pending_review",
        "batch_id": ctx["batch_id"],
        "chat_id": ctx["chat_id"],
        "status_uri": ctx["status_uri"],
        "duration": long_meta.get("duration"),
        "ad_candidates": candidates,
        "watch_url": _browser_url(uris["output_uri"]),
        "download_url": _download_url(uris["output_uri"]),
        **uris,
        **_youtube_fields(long_meta),
    }
    if detect_error:
        result["ad_detection_error"] = detect_error
    _upload_json(result, ctx["status_uri"])
    _callback(result, enabled=ctx["callback_enabled"], url=ctx["callback_url"])
    return result


def _run_upload() -> dict:
    """Upload the (already reviewed) final video to YouTube."""
    ctx = _batch_context()
    output_prefix = ctx["output_prefix"]
    uris = _final_result_uris(output_prefix)
    long_meta = _load_json(uris["metadata_uri"])
    youtube_fields = _youtube_fields(long_meta)
    upload_result_uri = f"{output_prefix}/final/youtube-upload.json"
    result = {
        "event": "long.batch.completed",
        "ok": True,
        "status": "completed",
        "batch_id": ctx["batch_id"],
        "chat_id": ctx["chat_id"],
        "status_uri": ctx["status_uri"],
        "youtube_upload_enabled": True,
        **uris,
        **youtube_fields,
    }
    try:
        upload_result = _run_youtube_upload(
            request_data=ctx["request_data"],
            batch_id=ctx["batch_id"],
            chat_id=ctx["chat_id"],
            final_uri=uris["output_uri"],
            metadata_uri=uris["metadata_uri"],
            upload_result_uri=upload_result_uri,
            youtube_fields=youtube_fields,
        )
    except Exception as exc:  # noqa: BLE001
        failure_payload = {
            **result,
            "event": "long.youtube.failed",
            "ok": False,
            "status": "upload_failed",
            "youtube_upload_status": "failed",
            "youtube_upload_result_uri": upload_result_uri,
            "error": str(exc),
        }
        _upload_json(failure_payload, ctx["status_uri"])
        raise BatchSourceFailure(failure_payload) from exc
    result.update({
        "event": "long.youtube.completed",
        "status": "uploaded",
        "youtube_upload_status": "uploaded",
        "youtube_upload_result_uri": upload_result_uri,
        "youtube_upload": upload_result,
        "youtube_video_id": upload_result.get("youtube_video_id", ""),
        "youtube_url": upload_result.get("youtube_url", ""),
    })
    _upload_json(result, ctx["status_uri"])
    _callback(result, enabled=ctx["callback_enabled"], url=ctx["callback_url"])
    return result


def _run() -> dict:
    mode = os.environ.get("MODE", "full").strip().lower() or "full"
    if mode == "cut":
        return _run_cut()
    if mode == "upload":
        return _run_upload()
    if mode == "review":
        return _run_review()
    return _run_full()


def main() -> int:
    callback_enabled = False
    callback_url = ""
    batch_id = os.environ.get("BATCH_ID", "").strip()
    chat_id = ""
    try:
        request_uri = os.environ.get("REQUEST_URI", "").strip()
        if request_uri:
            request_data = _load_json(request_uri)
            batch_id = str(request_data.get("batch_id") or batch_id).strip()
            callback_enabled = _as_bool(
                request_data.get("callback_enabled")
            )
            callback_url = str(
                request_data.get("callback_url") or ""
            ).strip()
            chat_id = str(request_data.get("chat_id") or "").strip()
        result = _run()
    except BatchSourceFailure as exc:
        _callback(
            exc.payload,
            enabled=callback_enabled,
            url=callback_url,
        )
        print(f"[long-coordinator] FAILED: {exc}", flush=True)
        return 1
    except Exception as exc:  # noqa: BLE001
        _callback(
            {
                "event": "long.batch.failed",
                "ok": False,
                "status": "failed",
                "batch_id": batch_id,
                "chat_id": chat_id,
                "error": str(exc),
            },
            enabled=callback_enabled,
            url=callback_url,
        )
        print(f"[long-coordinator] FAILED: {exc}", flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
