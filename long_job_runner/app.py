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
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage

from long_job_runner.payload import parse_cut_spans as _parse_cut_spans
from long_job_runner.payload import parse_videos as _parse_videos


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} environment variable is required")
    return value


PROJECT_ID = _required_env("GCP_PROJECT_ID")
REGION = os.environ.get("RUN_REGION", "asia-southeast1")
JOB_NAME = os.environ.get("JOB_NAME", "long-coordinator")
OUTPUT_ROOT = os.environ.get(
    "OUTPUT_ROOT",
    "gs://YOUR_GCP_PROJECT-media-sg/long",
).rstrip("/")
SCRATCH_ROOT = os.environ.get(
    "SCRATCH_ROOT",
    "gs://YOUR_GCP_PROJECT-scratch-sg/long",
).rstrip("/")
API_KEY = os.environ.get("API_KEY", "")
DOWNLOAD_SECRET = os.environ.get("DOWNLOAD_SECRET", "")
DOWNLOAD_TTL_SECONDS = int(os.environ.get("DOWNLOAD_TTL_SECONDS", "43200"))
SERVICE_BASE_URL = os.environ.get("SERVICE_BASE_URL", "").rstrip("/")

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


def _safe_id(value: str) -> str:
    safe = "".join(
        char for char in value if char.isalnum() or char in "-_"
    )[:80]
    return safe or uuid.uuid4().hex


def _parse_gs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith(OUTPUT_ROOT.rstrip("/") + "/"):
        raise ValueError("URI must stay inside the configured output root")
    if not uri.startswith("gs://"):
        raise ValueError("expected gs:// URI")
    bucket, separator, name = uri[5:].partition("/")
    if not separator or not bucket or not name:
        raise ValueError("invalid gs:// URI")
    return bucket, name


def _signature(uri: str, expires: int) -> str:
    payload = f"{uri}\n{expires}".encode()
    digest = hmac.new(DOWNLOAD_SECRET.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _download_url(uri: str) -> str:
    expires = int(time.time()) + DOWNLOAD_TTL_SECONDS
    query = urlencode({
        "uri": uri,
        "expires": expires,
        "sig": _signature(uri, expires),
    })
    base_url = SERVICE_BASE_URL or request.host_url.rstrip("/")
    return f"{base_url}/download?{query}"


def _upload_request(uri: str, value: dict) -> None:
    bucket_name, blob_name = _parse_gs_uri(uri)
    storage_client.bucket(bucket_name).blob(blob_name).upload_from_string(
        json.dumps(value, ensure_ascii=False, indent=2),
        content_type="application/json",
    )


def _batch_prefixes(raw_batch_id) -> tuple[str, str, str, str]:
    batch_id = _safe_id(str(raw_batch_id or "").strip())
    output_prefix = f"{OUTPUT_ROOT}/{batch_id}"
    work_prefix = f"{SCRATCH_ROOT}/{batch_id}"
    request_uri = f"{output_prefix}/request.json"
    return batch_id, output_prefix, work_prefix, request_uri


def _request_exists(request_uri: str) -> bool:
    bucket_name, blob_name = _parse_gs_uri(request_uri)
    return storage_client.bucket(bucket_name).blob(blob_name).exists()


def _run_coordinator(
    *,
    request_uri: str,
    output_prefix: str,
    work_prefix: str,
    batch_id: str,
    extra_env: dict | None = None,
) -> str:
    endpoint = (
        f"https://run.googleapis.com/v2/projects/{PROJECT_ID}/locations/"
        f"{REGION}/jobs/{JOB_NAME}:run"
    )
    env = {
        "REQUEST_URI": request_uri,
        "OUTPUT_PREFIX": output_prefix,
        "WORK_PREFIX": work_prefix,
        "BATCH_ID": batch_id,
    }
    if extra_env:
        env.update({k: v for k, v in extra_env.items() if v is not None})
    body = {
        "overrides": {
            "containerOverrides": [{
                "env": [
                    {"name": key, "value": value}
                    for key, value in env.items()
                ]
            }],
            "taskCount": 1,
        }
    }
    response = auth_session.post(endpoint, json=body, timeout=60)
    response.raise_for_status()
    operation_name = str(response.json().get("name") or "")
    if not operation_name:
        raise RuntimeError("coordinator jobs.run returned no operation name")
    return operation_name


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "job": JOB_NAME,
        "region": REGION,
        "output_root": OUTPUT_ROOT,
        "scratch_root": SCRATCH_ROOT,
    })


@app.post("/run")
def run_long():
    if not _check_api_key():
        return _error("unauthorized", 401)
    payload = request.get_json(silent=True) or {}
    raw_videos = payload.get("videos") or []
    force_refresh_all = _as_bool(payload.get("force_refresh"))
    videos, parse_error = _parse_videos(
        raw_videos,
        force_refresh_all=force_refresh_all,
    )
    if parse_error:
        return _error(parse_error, 400)

    batch_id = _safe_id(str(payload.get("id") or uuid.uuid4().hex))
    output_prefix = f"{OUTPUT_ROOT}/{batch_id}"
    work_prefix = f"{SCRATCH_ROOT}/{batch_id}"
    request_uri = f"{output_prefix}/request.json"
    callback_url = str(payload.get("callback_url") or "").strip()
    callback_enabled = _as_bool(payload.get("callback_enabled"))
    request_data = {
        "batch_id": batch_id,
        "videos": videos,
        "force_refresh": force_refresh_all,
        "callback_enabled": callback_enabled,
        "callback_url": callback_url,
        "chat_id": str(payload.get("chat_id") or "").strip(),
    }
    for key in (
        "review_mode",
        "youtube_upload_enabled",
        "youtube_privacy_status",
        "youtube_category_id",
        "youtube_made_for_kids",
        "youtube_notify_subscribers",
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
        if key in payload:
            request_data[key] = payload.get(key)
    try:
        _upload_request(request_uri, request_data)
        operation_name = _run_coordinator(
            request_uri=request_uri,
            output_prefix=output_prefix,
            work_prefix=work_prefix,
            batch_id=batch_id,
        )
    except Exception as exc:  # noqa: BLE001
        return _error(str(exc), 500)

    return (
        jsonify({
            "ok": True,
            "status": "queued",
            "batch_id": batch_id,
            "video_count": len(videos),
            "operation": operation_name,
            "request_uri": request_uri,
            "status_uri": f"{output_prefix}/status.json",
            "output_uri": f"{output_prefix}/final/final-long.mp4",
            "metadata_uri": f"{output_prefix}/final/final-long.json",
            "download_url": _download_url(
                f"{output_prefix}/final/final-long.mp4"
            ),
        }),
        202,
    )


@app.get("/download")
def download():
    uri = request.args.get("uri", "")
    supplied_signature = request.args.get("sig", "")
    try:
        expires = int(request.args.get("expires", ""))
        if expires < int(time.time()):
            return _error("download link expired", 410)
        if not DOWNLOAD_SECRET or not hmac.compare_digest(
            supplied_signature,
            _signature(uri, expires),
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
            "Content-Disposition": 'attachment; filename="final-long.mp4"',
            "Cache-Control": "private, no-store",
        },
    )


@app.post("/cut")
def cut_long():
    """Remove confirmed ad spans from an already-assembled batch (review flow)."""
    if not _check_api_key():
        return _error("unauthorized", 401)
    payload = request.get_json(silent=True) or {}
    raw_batch_id = payload.get("batch_id") or payload.get("id")
    if not raw_batch_id:
        return _error("batch_id is required", 400)
    spans, spans_error = _parse_cut_spans(payload.get("spans"))
    if spans_error:
        return _error(spans_error, 400)
    batch_id, output_prefix, work_prefix, request_uri = _batch_prefixes(
        raw_batch_id
    )
    try:
        if not _request_exists(request_uri):
            return _error(f"unknown batch_id: {batch_id}", 404)
        operation_name = _run_coordinator(
            request_uri=request_uri,
            output_prefix=output_prefix,
            work_prefix=work_prefix,
            batch_id=batch_id,
            extra_env={"MODE": "cut", "CUT_SPANS": json.dumps(spans)},
        )
    except Exception as exc:  # noqa: BLE001
        return _error(str(exc), 500)
    return (
        jsonify({
            "ok": True,
            "status": "cutting",
            "batch_id": batch_id,
            "spans": spans,
            "operation": operation_name,
            "status_uri": f"{output_prefix}/status.json",
        }),
        202,
    )


@app.post("/upload")
def upload_long():
    """Upload the (already reviewed) final video for a batch to YouTube."""
    if not _check_api_key():
        return _error("unauthorized", 401)
    payload = request.get_json(silent=True) or {}
    raw_batch_id = payload.get("batch_id") or payload.get("id")
    if not raw_batch_id:
        return _error("batch_id is required", 400)
    batch_id, output_prefix, work_prefix, request_uri = _batch_prefixes(
        raw_batch_id
    )
    try:
        if not _request_exists(request_uri):
            return _error(f"unknown batch_id: {batch_id}", 404)
        operation_name = _run_coordinator(
            request_uri=request_uri,
            output_prefix=output_prefix,
            work_prefix=work_prefix,
            batch_id=batch_id,
            extra_env={"MODE": "upload"},
        )
    except Exception as exc:  # noqa: BLE001
        return _error(str(exc), 500)
    return (
        jsonify({
            "ok": True,
            "status": "uploading",
            "batch_id": batch_id,
            "operation": operation_name,
            "status_uri": f"{output_prefix}/status.json",
        }),
        202,
    )


@app.post("/review")
def review_long():
    """Re-run ad detection on an assembled batch and re-emit review (no rebuild)."""
    if not _check_api_key():
        return _error("unauthorized", 401)
    payload = request.get_json(silent=True) or {}
    raw_batch_id = payload.get("batch_id") or payload.get("id")
    if not raw_batch_id:
        return _error("batch_id is required", 400)
    batch_id, output_prefix, work_prefix, request_uri = _batch_prefixes(
        raw_batch_id
    )
    try:
        if not _request_exists(request_uri):
            return _error(f"unknown batch_id: {batch_id}", 404)
        operation_name = _run_coordinator(
            request_uri=request_uri,
            output_prefix=output_prefix,
            work_prefix=work_prefix,
            batch_id=batch_id,
            extra_env={"MODE": "review"},
        )
    except Exception as exc:  # noqa: BLE001
        return _error(str(exc), 500)
    return (
        jsonify({
            "ok": True,
            "status": "reviewing",
            "batch_id": batch_id,
            "operation": operation_name,
            "status_uri": f"{output_prefix}/status.json",
        }),
        202,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
