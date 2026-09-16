from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit, urlunsplit


def normalize_douyin_url(raw_url: str) -> str:
    value = str(raw_url or "").strip().rstrip(".,;!?，。；！？)]}")
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if scheme not in {"http", "https"}:
        raise ValueError("douyin_url must use http or https")
    if not (
        host == "douyin.com"
        or host.endswith(".douyin.com")
        or host == "iesdouyin.com"
        or host.endswith(".iesdouyin.com")
    ):
        raise ValueError("douyin_url must point to Douyin")
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    valid_path = bool(
        (host == "v.douyin.com" and re.fullmatch(r"/[0-9A-Za-z_-]+/?", path))
        or re.fullmatch(r"/(?:video|note)/\d+/?", path)
        or re.fullmatch(r"/share/video/\d+/?", path)
    )
    if not valid_path:
        raise ValueError("douyin_url is not a supported Douyin video URL")
    return urlunsplit(("https", host, path.rstrip("/") + "/", "", ""))


def source_id_for_url(douyin_url: str) -> str:
    normalized = normalize_douyin_url(douyin_url)
    return "douyin-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def artifact_uris(
    douyin_url: str,
    *,
    result_root: str,
    pipeline_version: str,
) -> dict[str, str]:
    version = re.sub(r"[^0-9A-Za-z._-]+", "-", pipeline_version).strip("-._")
    if not version:
        raise ValueError("COVER_PIPELINE_VERSION must contain a safe name")
    source_id = source_id_for_url(douyin_url)
    prefix = f"{result_root.rstrip('/')}/{version}/{source_id}"
    return {
        "desub_id": source_id,
        "result_prefix": prefix,
        "source_uri": f"{prefix}/source.mp4",
        "status_uri": f"{prefix}/status.json",
        "output_uri": f"{prefix}/output.mp4",
        "report_uri": f"{prefix}/cover_report.json",
        "mask_uri": f"{prefix}/mask.json",
        "cues_uri": f"{prefix}/visub_cues_final.json",
    }
