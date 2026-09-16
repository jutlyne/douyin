from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import requests
from google.cloud import storage

from container_short.steps import gcsio


TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMBNAIL_UPLOAD_URL = (
    "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
CHUNK_UNIT = 256 * 1024


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _required_env(name: str) -> str:
    value = _env(name)
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _storage_client() -> storage.Client:
    return storage.Client(project=_required_env("GCP_PROJECT_ID"))


def _blob(client: storage.Client, uri: str) -> storage.Blob:
    bucket_name, blob_name = gcsio.parse_gs_uri(uri)
    return client.bucket(bucket_name).blob(blob_name)


def _load_json(client: storage.Client, uri: str) -> dict:
    blob = _blob(client, uri)
    if not blob.exists():
        return {}
    data = blob.download_as_text(encoding="utf-8")
    return json.loads(data or "{}")


def _write_json(client: storage.Client, uri: str, value: dict) -> None:
    if not uri:
        return
    blob = _blob(client, uri)
    blob.upload_from_string(
        json.dumps(value, ensure_ascii=False, indent=2),
        content_type="application/json",
    )


def _trim(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _metadata_tags(metadata: dict) -> list[str]:
    raw_tags: list[Any] = []
    raw_tags.extend(metadata.get("tags") or [])
    raw_tags.extend(metadata.get("hashtags") or [])
    seen: set[str] = set()
    tags: list[str] = []
    for item in raw_tags:
        tag = str(item or "").strip().lstrip("#")
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag[:100])
        if len(tags) >= 20:
            break
    return tags


def _video_resource(metadata: dict) -> dict:
    title = _env("YOUTUBE_TITLE") or metadata.get("youtube_title") or metadata.get("title_vi")
    description = (
        _env("YOUTUBE_DESCRIPTION")
        or metadata.get("youtube_description")
        or metadata.get("description_vi")
    )
    category_id = _env("YOUTUBE_CATEGORY_ID", "24")
    privacy_status = _env("YOUTUBE_PRIVACY_STATUS", "private")
    publish_at = _env("YOUTUBE_PUBLISH_AT")
    made_for_kids = _as_bool(_env("YOUTUBE_MADE_FOR_KIDS"), default=False)
    tags = _metadata_tags(metadata)
    env_tags = _env("YOUTUBE_TAGS")
    if env_tags:
        tags = [tag.strip().lstrip("#") for tag in env_tags.split(",") if tag.strip()]

    status = {
        "privacyStatus": str(privacy_status or "private").lower(),
        "selfDeclaredMadeForKids": made_for_kids,
    }
    if publish_at:
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at

    return {
        "snippet": {
            "title": _trim(title or "Video tong hop", 100),
            "description": _trim(description or "", 5000),
            "categoryId": str(category_id or "24"),
            "tags": tags,
        },
        "status": status,
    }


def _refresh_access_token(session: requests.Session) -> str:
    access_token = _env("YOUTUBE_ACCESS_TOKEN")
    refresh_token = _env("YOUTUBE_REFRESH_TOKEN")
    if access_token and not refresh_token:
        return access_token
    client_id = _required_env("YOUTUBE_CLIENT_ID")
    client_secret = _required_env("YOUTUBE_CLIENT_SECRET")
    refresh_token = _required_env("YOUTUBE_REFRESH_TOKEN")
    response = session.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=int(_env("YOUTUBE_TOKEN_TIMEOUT_SECONDS", "30")),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"OAuth refresh failed HTTP {response.status_code}: {response.text[:500]}")
    token = str(response.json().get("access_token") or "")
    if not token:
        raise RuntimeError("OAuth refresh response did not include access_token")
    return token


def _start_upload_session(
    session: requests.Session,
    *,
    token: str,
    resource: dict,
    total_size: int,
    content_type: str,
) -> str:
    notify = str(_as_bool(_env("YOUTUBE_NOTIFY_SUBSCRIBERS"), default=False)).lower()
    response = session.post(
        UPLOAD_URL,
        params={
            "uploadType": "resumable",
            "part": "snippet,status",
            "notifySubscribers": notify,
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(total_size),
            "X-Upload-Content-Type": content_type,
        },
        data=json.dumps(resource, ensure_ascii=False).encode("utf-8"),
        timeout=int(_env("YOUTUBE_REQUEST_TIMEOUT_SECONDS", "120")),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Start upload failed HTTP {response.status_code}: {response.text[:500]}")
    upload_url = response.headers.get("Location", "")
    if not upload_url:
        raise RuntimeError("Start upload response did not include Location header")
    return upload_url


def _next_offset_from_range(value: str, fallback: int) -> int:
    if not value:
        return fallback
    try:
        _, byte_range = value.split("=", 1)
        _, end_text = byte_range.split("-", 1)
        return int(end_text) + 1
    except Exception:  # noqa: BLE001
        return fallback


def _query_offset(
    session: requests.Session,
    *,
    upload_url: str,
    token: str,
    total_size: int,
    fallback: int,
) -> tuple[int, dict | None]:
    response = session.put(
        upload_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Length": "0",
            "Content-Range": f"bytes */{total_size}",
        },
        timeout=int(_env("YOUTUBE_REQUEST_TIMEOUT_SECONDS", "120")),
    )
    if response.status_code == 308:
        return _next_offset_from_range(response.headers.get("Range", ""), fallback), None
    if response.status_code in {200, 201}:
        return total_size, response.json()
    if response.status_code in RETRYABLE_STATUS:
        return fallback, None
    raise RuntimeError(f"Upload status failed HTTP {response.status_code}: {response.text[:500]}")


def _chunk_size() -> int:
    value = int(_env("YOUTUBE_UPLOAD_CHUNK_BYTES", str(64 * 1024 * 1024)))
    if value < CHUNK_UNIT:
        return CHUNK_UNIT
    return (value // CHUNK_UNIT) * CHUNK_UNIT


def _upload_chunks(
    session: requests.Session,
    *,
    upload_url: str,
    token: str,
    video_blob: storage.Blob,
    total_size: int,
    content_type: str,
) -> dict:
    chunk_size = _chunk_size()
    request_timeout = int(_env("YOUTUBE_CHUNK_TIMEOUT_SECONDS", "900"))
    max_retries = int(_env("YOUTUBE_UPLOAD_MAX_RETRIES", "8"))
    offset = 0
    retries = 0
    started_at = time.time()

    while offset < total_size:
        end = min(offset + chunk_size, total_size) - 1
        try:
            data = video_blob.download_as_bytes(start=offset, end=end)
            response = session.put(
                upload_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Length": str(len(data)),
                    "Content-Type": content_type,
                    "Content-Range": f"bytes {offset}-{end}/{total_size}",
                },
                data=data,
                timeout=request_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            if retries >= max_retries:
                raise RuntimeError(f"Upload interrupted after retries: {exc}") from exc
            retries += 1
            time.sleep(min(60, 2 ** retries))
            offset, completed = _query_offset(
                session,
                upload_url=upload_url,
                token=token,
                total_size=total_size,
                fallback=offset,
            )
            if completed:
                return completed
            continue

        if response.status_code == 308:
            offset = _next_offset_from_range(response.headers.get("Range", ""), end + 1)
            retries = 0
            print(f"[youtube-uploader] uploaded {offset}/{total_size} bytes", flush=True)
            continue
        if response.status_code in {200, 201}:
            print(
                f"[youtube-uploader] upload completed in {time.time() - started_at:.1f}s",
                flush=True,
            )
            return response.json()
        if response.status_code == 401 and _env("YOUTUBE_REFRESH_TOKEN"):
            token = _refresh_access_token(session)
            continue
        if response.status_code in RETRYABLE_STATUS:
            if retries >= max_retries:
                raise RuntimeError(f"Upload failed HTTP {response.status_code}: {response.text[:500]}")
            retries += 1
            time.sleep(min(60, 2 ** retries))
            offset, completed = _query_offset(
                session,
                upload_url=upload_url,
                token=token,
                total_size=total_size,
                fallback=offset,
            )
            if completed:
                return completed
            continue
        raise RuntimeError(f"Upload failed HTTP {response.status_code}: {response.text[:500]}")

    raise RuntimeError("Upload loop ended before YouTube returned a video resource")


def _set_thumbnail(
    session: requests.Session,
    *,
    token: str,
    video_id: str,
    thumbnail_blob: storage.Blob,
) -> dict:
    thumbnail_blob.reload()
    size = int(thumbnail_blob.size or 0)
    if size <= 0:
        raise RuntimeError("YouTube thumbnail object is empty")
    if size > 2 * 1024 * 1024:
        raise RuntimeError("YouTube thumbnail must be 2 MB or smaller")
    content_type = thumbnail_blob.content_type or "image/jpeg"
    if content_type not in {"image/jpeg", "image/png"}:
        raise RuntimeError(
            f"Unsupported YouTube thumbnail content type: {content_type}"
        )
    response = session.post(
        THUMBNAIL_UPLOAD_URL,
        params={"videoId": video_id, "uploadType": "media"},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
            "Content-Length": str(size),
        },
        data=thumbnail_blob.download_as_bytes(),
        timeout=int(_env("YOUTUBE_REQUEST_TIMEOUT_SECONDS", "120")),
    )
    if response.status_code >= 400:
        raise RuntimeError(
            "Set thumbnail failed HTTP "
            f"{response.status_code}: {response.text[:500]}"
        )
    return response.json()


def _run() -> dict:
    video_uri = _required_env("VIDEO_URI")
    metadata_uri = _env("METADATA_URI")
    result_uri = _env("UPLOAD_RESULT_URI")
    batch_id = _env("BATCH_ID")
    chat_id = _env("CHAT_ID")
    client = _storage_client()
    video_blob = _blob(client, video_uri)
    video_blob.reload()
    total_size = int(video_blob.size or 0)
    if total_size <= 0:
        raise RuntimeError(f"Video object is empty or missing: {video_uri}")
    content_type = video_blob.content_type or "video/mp4"
    if not content_type.startswith("video/"):
        content_type = "video/mp4"

    metadata = _load_json(client, metadata_uri) if metadata_uri else {}
    resource = _video_resource(metadata)
    session = requests.Session()
    token = _refresh_access_token(session)
    upload_url = _start_upload_session(
        session,
        token=token,
        resource=resource,
        total_size=total_size,
        content_type=content_type,
    )
    video = _upload_chunks(
        session,
        upload_url=upload_url,
        token=token,
        video_blob=video_blob,
        total_size=total_size,
        content_type=content_type,
    )
    video_id = str(video.get("id") or "")
    if not video_id:
        raise RuntimeError("YouTube upload completed without a video id")
    thumbnail_uri = _env("YOUTUBE_THUMBNAIL_URI")
    thumbnail_response: dict = {}
    if thumbnail_uri:
        thumbnail_response = _set_thumbnail(
            session,
            token=_refresh_access_token(session),
            video_id=video_id,
            thumbnail_blob=_blob(client, thumbnail_uri),
        )
    result = {
        "event": "long.youtube.uploaded",
        "ok": True,
        "status": "uploaded",
        "batch_id": batch_id,
        "chat_id": chat_id,
        "video_uri": video_uri,
        "metadata_uri": metadata_uri,
        "youtube_video_id": video_id,
        "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
        "youtube_title": resource["snippet"]["title"],
        "youtube_description": resource["snippet"].get("description", ""),
        "youtube_category_id": resource["snippet"].get("categoryId", "24"),
        "youtube_privacy_status": resource["status"].get("privacyStatus", "private"),
        "youtube_publish_at": resource["status"].get("publishAt", ""),
        "youtube_made_for_kids": resource["status"].get("selfDeclaredMadeForKids", False),
        "youtube_tags": resource["snippet"].get("tags", []),
        "youtube_thumbnail_uri": thumbnail_uri,
        "youtube_thumbnail_set": bool(thumbnail_uri),
        "youtube_thumbnail_response": thumbnail_response,
        "youtube_response": video,
    }
    _write_json(client, result_uri, result)
    return result


def main() -> int:
    client: storage.Client | None = None
    result_uri = _env("UPLOAD_RESULT_URI")
    try:
        result = _run()
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[youtube-uploader] FAILED: {exc}", flush=True)
        if result_uri:
            try:
                client = client or _storage_client()
                _write_json(
                    client,
                    result_uri,
                    {
                        "event": "long.youtube.failed",
                        "ok": False,
                        "status": "failed",
                        "batch_id": _env("BATCH_ID"),
                        "chat_id": _env("CHAT_ID"),
                        "video_uri": _env("VIDEO_URI"),
                        "metadata_uri": _env("METADATA_URI"),
                        "error": str(exc),
                    },
                )
            except Exception as write_exc:  # noqa: BLE001
                print(f"[youtube-uploader] failed to write result: {write_exc}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
