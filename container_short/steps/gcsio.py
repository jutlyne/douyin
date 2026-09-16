"""GCS helpers — upload/download/xoá object, dùng google-cloud-storage.

Auth qua Application Default Credentials (GOOGLE_APPLICATION_CREDENTIALS hoặc
service account của Cloud Run Job) — giống các service khác trong project.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlparse


def parse_gs_uri(uri: str) -> tuple[str, str]:
    """gs://bucket/path/to/obj → (bucket, path/to/obj)."""
    if not uri.startswith("gs://"):
        raise ValueError(f"URI không phải gs://: {uri}")
    p = urlparse(uri)
    return p.netloc, p.path.lstrip("/")


@lru_cache(maxsize=1)
def _client():
    from google.cloud import storage  # type: ignore

    return storage.Client()


def upload(local_path: str, gs_uri: str, content_type: str | None = None) -> None:
    bucket_name, blob_path = parse_gs_uri(gs_uri)
    blob = _client().bucket(bucket_name).blob(blob_path)
    blob.upload_from_filename(local_path, content_type=content_type)


def download(gs_uri: str, local_path: str) -> None:
    bucket_name, blob_path = parse_gs_uri(gs_uri)
    blob = _client().bucket(bucket_name).blob(blob_path)
    blob.download_to_filename(local_path)


def exists(gs_uri: str) -> bool:
    bucket_name, blob_path = parse_gs_uri(gs_uri)
    return _client().bucket(bucket_name).blob(blob_path).exists()


def copy(src_uri: str, dst_uri: str) -> None:
    src_bucket_name, src_blob_path = parse_gs_uri(src_uri)
    dst_bucket_name, dst_blob_path = parse_gs_uri(dst_uri)
    client = _client()
    src_bucket = client.bucket(src_bucket_name)
    src_blob = src_bucket.blob(src_blob_path)
    dst_bucket = client.bucket(dst_bucket_name)
    src_bucket.copy_blob(src_blob, dst_bucket, dst_blob_path)


def delete(gs_uri: str) -> None:
    """Xoá object; bỏ qua lỗi (dùng để dọn file tạm)."""
    try:
        bucket_name, blob_path = parse_gs_uri(gs_uri)
        _client().bucket(bucket_name).blob(blob_path).delete()
    except Exception:  # noqa: BLE001
        pass
