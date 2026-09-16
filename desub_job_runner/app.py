from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import google.auth
from flask import Flask, Response, jsonify, request, stream_with_context
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage

from desub_job_runner import core


PROJECT_ID = os.environ.get(
    "GCP_PROJECT_ID", "YOUR_GCP_PROJECT"
)
REGION = os.environ.get("RUN_REGION", "asia-southeast1")
JOB_NAME = os.environ.get("JOB_NAME", "cover-visub-lab")
RESULT_ROOT = os.environ.get(
    "COVER_RESULT_ROOT",
    "gs://YOUR_GCP_PROJECT-media-sg/desub/cover-visub",
).rstrip("/")
PIPELINE_VERSION = os.environ.get("COVER_PIPELINE_VERSION", "v9").strip() or "v9"
API_KEY = os.environ.get("API_KEY", "")
DOWNLOAD_SECRET = os.environ.get("DOWNLOAD_SECRET", "")
DOWNLOAD_TTL_SECONDS = int(os.environ.get("DOWNLOAD_TTL_SECONDS", "86400"))
SERVICE_BASE_URL = os.environ.get("SERVICE_BASE_URL", "").rstrip("/")
RUNNING_STATUS_TTL_SECONDS = int(
    os.environ.get("RUNNING_STATUS_TTL_SECONDS", "14400")
)
RUNNING_STATUSES = {
    "queued",
    "validating",
    "downloading",
    "loading_source",
    "detecting",
    "loading_mask",
    "translating",
    "generating_cues",
    "loading_cues",
    "rendering",
    "synthesizing",
    "synthesizing_tts",
    "mixing_audio",
    "verifying",
}

app = Flask(__name__)
_AUTH_SESSION: AuthorizedSession | None = None
_STORAGE_CLIENT: storage.Client | None = None


def _clients() -> tuple[AuthorizedSession, storage.Client]:
    global _AUTH_SESSION, _STORAGE_CLIENT
    if _AUTH_SESSION is None or _STORAGE_CLIENT is None:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        _AUTH_SESSION = AuthorizedSession(credentials)
        _STORAGE_CLIENT = storage.Client(
            project=PROJECT_ID,
            credentials=credentials,
        )
    return _AUTH_SESSION, _STORAGE_CLIENT


def _error(message: str, status: int):
    return jsonify({"ok": False, "error": message}), status


def _check_api_key() -> bool:
    supplied = request.headers.get("X-API-Key") or request.args.get("api_key", "")
    return bool(API_KEY) and hmac.compare_digest(str(supplied), API_KEY)


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def normalize_douyin_url(raw_url: str) -> str:
    return core.normalize_douyin_url(raw_url)


def source_id_for_url(douyin_url: str) -> str:
    return core.source_id_for_url(douyin_url)


def artifact_uris(douyin_url: str) -> dict[str, str]:
    return core.artifact_uris(
        douyin_url,
        result_root=RESULT_ROOT,
        pipeline_version=PIPELINE_VERSION,
    )


def _parse_result_uri(uri: str) -> tuple[str, str]:
    allowed_prefix = RESULT_ROOT + "/"
    if not uri.startswith(allowed_prefix):
        raise ValueError("URI must stay inside COVER_RESULT_ROOT")
    rest = uri[5:]
    bucket, separator, name = rest.partition("/")
    if not separator or not bucket or not name or ".." in name.split("/"):
        raise ValueError("invalid GCS result URI")
    return bucket, name


def _blob(uri: str):
    bucket, name = _parse_result_uri(uri)
    _, client = _clients()
    return client.bucket(bucket).blob(name)


def _read_json(uri: str) -> dict:
    blob = _blob(uri)
    if not blob.exists():
        return {}
    try:
        value = json.loads(blob.download_as_text(encoding="utf-8"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_status(uri: str, status: str, **fields: object) -> None:
    payload = {
        "status": status,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **fields,
    }
    _blob(uri).upload_from_string(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        content_type="application/json",
    )


def _signature(uri: str, expires: int) -> str:
    payload = f"{uri}\n{expires}".encode("utf-8")
    digest = hmac.new(DOWNLOAD_SECRET.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _download_url(uri: str) -> str:
    expires = int(time.time()) + DOWNLOAD_TTL_SECONDS
    query = urlencode({"uri": uri, "expires": expires, "sig": _signature(uri, expires)})
    base_url = _external_base_url()
    return f"{base_url}/download?{query}"


def _status_url(desub_id: str) -> str:
    base_url = _external_base_url()
    return f"{base_url}/status?{urlencode({'desub_id': desub_id})}"


def _external_base_url() -> str:
    if SERVICE_BASE_URL:
        return SERVICE_BASE_URL
    forwarded_proto = str(request.headers.get("X-Forwarded-Proto") or "")
    scheme = forwarded_proto.split(",", 1)[0].strip() or request.scheme
    forwarded_host = str(request.headers.get("X-Forwarded-Host") or "")
    host = forwarded_host.split(",", 1)[0].strip() or request.host
    return f"{scheme}://{host}".rstrip("/")


def _run_job(douyin_url: str, artifacts: dict[str, str], force_refresh: bool) -> str:
    auth_session, _ = _clients()
    endpoint = (
        f"https://run.googleapis.com/v2/projects/{PROJECT_ID}/locations/"
        f"{REGION}/jobs/{JOB_NAME}:run"
    )
    env = {
        # Clear the lab job's persisted GCS input so exactly one input remains.
        "COVER_SOURCE_URI": "",
        "COVER_DOUYIN_URL": douyin_url,
        "COVER_OUTPUT_URI": artifacts["output_uri"],
        "COVER_MASK_URI": "",
        "COVER_CUES_URI": "",
        "COVER_PIPELINE_VERSION": PIPELINE_VERSION,
        "COVER_AUTO_RESUME": "false" if force_refresh else "true",
    }
    body = {
        "overrides": {
            "containerOverrides": [
                {"env": [{"name": key, "value": value} for key, value in env.items()]}
            ],
            "taskCount": 1,
        }
    }
    response = auth_session.post(endpoint, json=body, timeout=60)
    response.raise_for_status()
    operation = str(response.json().get("name") or "")
    if not operation:
        raise RuntimeError("Cloud Run jobs.run returned no operation name")
    return operation


def _is_fresh_running_status(status: dict, status_uri: str) -> bool:
    if status.get("status") not in RUNNING_STATUSES:
        return False
    blob = _blob(status_uri)
    try:
        blob.reload()
        updated = blob.updated
        return bool(updated and time.time() - updated.timestamp() < RUNNING_STATUS_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        return False


def _response_payload(
    artifacts: dict[str, str],
    *,
    status: str,
    operation: str = "",
    cached: bool = False,
) -> dict[str, object]:
    return {
        "ok": True,
        "status": status,
        "cached": cached,
        "desub_id": artifacts["desub_id"],
        "operation": operation,
        "status_url": _status_url(artifacts["desub_id"]),
        "status_uri": artifacts["status_uri"],
        "output_uri": artifacts["output_uri"],
        "report_uri": artifacts["report_uri"],
        "download_url": _download_url(artifacts["output_uri"]),
        "expires_in": DOWNLOAD_TTL_SECONDS,
    }


@app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "job": JOB_NAME,
            "region": REGION,
            "pipeline_version": PIPELINE_VERSION,
            "result_root": RESULT_ROOT,
            "input": "douyin_url",
        }
    )


@app.post("/run")
def run_cover_visub():
    if not _check_api_key():
        return _error("unauthorized", 401)
    payload = request.get_json(silent=True) or {}
    try:
        douyin_url = normalize_douyin_url(payload.get("douyin_url", ""))
        force_refresh = _as_bool(payload.get("force_refresh"))
        artifacts = artifact_uris(douyin_url)
        current = _read_json(artifacts["status_uri"])
        output_exists = _blob(artifacts["output_uri"]).exists()
        report_exists = _blob(artifacts["report_uri"]).exists()
        if (
            not force_refresh
            and current.get("status") == "completed"
            and output_exists
            and report_exists
        ):
            return jsonify(
                _response_payload(
                    artifacts,
                    status="completed",
                    cached=True,
                )
            )
        if not force_refresh and _is_fresh_running_status(
            current, artifacts["status_uri"]
        ):
            return (
                jsonify(
                    _response_payload(
                        artifacts,
                        status=str(current.get("status") or "running"),
                    )
                ),
                202,
            )
        _write_status(
            artifacts["status_uri"],
            "queued",
            input_kind="douyin_url",
            douyin_url=douyin_url,
            source_uri=artifacts["source_uri"],
            output_uri=artifacts["output_uri"],
            report_uri=artifacts["report_uri"],
            force_refresh=force_refresh,
        )
        operation = _run_job(douyin_url, artifacts, force_refresh)
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        try:
            if "artifacts" in locals():
                _write_status(
                    artifacts["status_uri"],
                    "failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        except Exception:  # noqa: BLE001
            pass
        return _error(str(exc), 500)
    return (
        jsonify(
            _response_payload(
                artifacts,
                status="queued",
                operation=operation,
            )
        ),
        202,
    )


@app.get("/status")
def status():
    if not _check_api_key():
        return _error("unauthorized", 401)
    desub_id = str(request.args.get("desub_id") or "").strip()
    if not re.fullmatch(r"douyin-[0-9a-f]{12}", desub_id):
        return _error("invalid desub_id", 400)
    prefix = f"{RESULT_ROOT}/{PIPELINE_VERSION}/{desub_id}"
    artifacts = {
        "desub_id": desub_id,
        "status_uri": f"{prefix}/status.json",
        "output_uri": f"{prefix}/output.mp4",
        "report_uri": f"{prefix}/cover_report.json",
    }
    value = _read_json(artifacts["status_uri"])
    if not value:
        return _error("unknown desub_id", 404)
    value.update(
        {
            "ok": value.get("status") != "failed",
            "desub_id": desub_id,
            "status_uri": artifacts["status_uri"],
            "output_uri": artifacts["output_uri"],
            "report_uri": artifacts["report_uri"],
            "download_url": _download_url(artifacts["output_uri"]),
            "expires_in": DOWNLOAD_TTL_SECONDS,
        }
    )
    return jsonify(value)


@app.get("/download")
def download():
    uri = str(request.args.get("uri") or "")
    supplied = str(request.args.get("sig") or "")
    try:
        expires = int(request.args.get("expires") or "")
        if expires < int(time.time()):
            return _error("download link expired", 410)
        if not DOWNLOAD_SECRET or not hmac.compare_digest(
            supplied, _signature(uri, expires)
        ):
            return _error("invalid download signature", 403)
        blob = _blob(uri)
        if not blob.exists():
            return _error("file not found", 404)
        stream = blob.open("rb")
    except Exception as exc:  # noqa: BLE001
        return _error(str(exc), 400)
    return Response(
        stream_with_context(stream),
        mimetype="video/mp4",
        headers={
            "Content-Disposition": 'attachment; filename="visub.mp4"',
            "Cache-Control": "private, no-store",
        },
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
