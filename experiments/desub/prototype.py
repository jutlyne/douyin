from __future__ import annotations

import json
import math
import mimetypes
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable


PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
DEFAULT_RESULT_ROOT = "gs://YOUR_GCP_PROJECT-media-sg/desub/_lab"


def _require_project_id() -> str:
    if not PROJECT_ID:
        raise ValueError("GCP_PROJECT_ID environment variable is required")
    return PROJECT_ID
DEFAULT_CLIP_SECONDS = 30.0
DEFAULT_DETECT_FPS = 8.0
DEFAULT_MASK_DILATE_PX = 10
DEFAULT_BAND_TOP_RATIO = 0.66
GPU_HOURLY_USD = float(os.environ.get("DESUB_L4_USD_PER_HOUR", "0.70"))


@dataclass(frozen=True)
class VideoMeta:
    width: int
    height: int
    fps: float
    frames: int
    duration: float


@dataclass(frozen=True)
class Detection:
    detector: str
    frame: int
    t: float
    x1: int
    y1: int
    x2: int
    y2: int
    score: float
    text: str = ""

    @property
    def box(self) -> tuple[int, int, int, int]:
        return self.x1, self.y1, self.x2, self.y2


@dataclass(frozen=True)
class Span:
    t_start: float
    t_end: float
    frame_start: int
    frame_end: int
    box: tuple[int, int, int, int]
    detector_counts: dict[str, int]
    samples: int


@dataclass(frozen=True)
class MaskCluster:
    kind: str
    t_start: float
    t_end: float
    context_start: float
    context_end: float
    rects: tuple[tuple[int, int, int, int], ...]
    span_count: int = 0


@dataclass(frozen=True)
class SegmentCluster:
    clusters: tuple[MaskCluster, ...]
    segment_start: float
    segment_end: float


@dataclass(frozen=True)
class OverlayRegion:
    rect: tuple[int, int, int, int]
    enable_start: float | None = None
    enable_end: float | None = None


def log(message: str, **fields: Any) -> None:
    payload = {"message": message, **fields}
    print("[desub-lab] " + json.dumps(payload, ensure_ascii=False, default=str), flush=True)


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    return float(raw)


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    return int(raw)


def run_id() -> str:
    supplied = os.environ.get("RUN_ID") or os.environ.get("DESUB_RUN_ID")
    if supplied:
        safe = "".join(ch for ch in supplied if ch.isalnum() or ch in "-_")[:96]
        if safe:
            return safe
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]


def parse_gs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"expected gs:// URI, got {uri}")
    bucket, sep, blob = uri[5:].partition("/")
    if not bucket or not sep or not blob:
        raise ValueError(f"invalid gs:// URI: {uri}")
    return bucket, blob


def normalize_result_prefix(raw: str, rid: str) -> str:
    prefix = (raw or DEFAULT_RESULT_ROOT).rstrip("/")
    if not prefix.startswith("gs://YOUR_GCP_PROJECT-media-sg/desub/"):
        raise ValueError("RESULT_PREFIX must stay under gs://YOUR_GCP_PROJECT-media-sg/desub/")
    if prefix.endswith("/_lab"):
        return f"{prefix}/{rid}"
    return prefix


def cmd(args: list[str], *, timeout: int | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    log("run_cmd", args=args, cwd=str(cwd) if cwd else None)
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if proc.returncode:
        raise RuntimeError(
            "command failed"
            + f" rc={proc.returncode} args={args}\n"
            + proc.stdout[-4000:]
        )
    if proc.stdout.strip():
        log("cmd_output", tail=proc.stdout[-2000:])
    return proc


def cmd_logged(
    args: list[str],
    *,
    log_path: Path,
    timeout: int | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log("run_cmd", args=args, cwd=str(cwd) if cwd else None, log=str(log_path))
    with log_path.open("w", encoding="utf-8", errors="replace") as handle:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=handle,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    output = log_path.read_text(encoding="utf-8", errors="replace")
    if proc.returncode:
        raise RuntimeError(
            "command failed"
            + f" rc={proc.returncode} args={args} log={log_path}\n"
            + output[-4000:]
        )
    if output.strip():
        log("cmd_output", tail=output[-2000:], log=str(log_path))
    return proc


def ffmpeg_ts(seconds: float) -> str:
    return f"{max(0.0, seconds):.6f}"


def ffprobe(path: Path) -> VideoMeta:
    proc = cmd([
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ])
    data = json.loads(proc.stdout)
    video = next(stream for stream in data["streams"] if stream.get("codec_type") == "video")
    fps_text = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    num, _, den = fps_text.partition("/")
    fps = float(num) / float(den or 1)
    duration = float(video.get("duration") or data.get("format", {}).get("duration") or 0)
    frames_raw = video.get("nb_frames")
    frames = int(frames_raw) if str(frames_raw or "").isdigit() else int(duration * fps + 0.5)
    return VideoMeta(
        width=int(video["width"]),
        height=int(video["height"]),
        fps=fps,
        frames=frames,
        duration=duration,
    )


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def upload_file(local: Path, gs_uri: str, *, content_type: str | None = None) -> None:
    from google.cloud import storage

    bucket_name, blob_name = parse_gs_uri(gs_uri)
    storage.Client(project=_require_project_id()).bucket(bucket_name).blob(blob_name).upload_from_filename(
        str(local),
        content_type=content_type,
    )


def upload_tree(local_root: Path, result_prefix: str) -> list[str]:
    uploaded: list[str] = []
    for path in sorted(local_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(local_root).as_posix()
        gs_uri = f"{result_prefix}/{rel}"
        content_type = mimetypes.guess_type(path.name)[0]
        upload_file(path, gs_uri, content_type=content_type)
        uploaded.append(gs_uri)
    return uploaded


def upload_status(work: Path, result_prefix: str, status: str, **fields: Any) -> None:
    payload = {
        "ok": status == "completed",
        "status": status,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **fields,
    }
    status_path = work / "status.json"
    write_json(status_path, payload)
    upload_file(status_path, f"{result_prefix}/status.json", content_type="application/json")


def download_douyin(douyin_url: str, out_path: Path) -> dict[str, Any]:
    sys.path.insert(0, "/app")
    import requests
    from douyin_api.douyin_downloader import DouyinError, open_stream, resolve

    info = resolve(douyin_url, cookie=os.environ.get("DOUYIN_COOKIE"))
    candidates = [info.play_url, *info.play_url_candidates]
    candidates.extend(extra_douyin_candidates(douyin_url, cookie=os.environ.get("DOUYIN_COOKIE")))
    unique_candidates = [url for i, url in enumerate(candidates) if url and url not in candidates[:i]]
    errors: list[str] = []
    for idx, play_url in enumerate(unique_candidates, start=1):
        try:
            log("download_candidate", index=idx, total=len(unique_candidates), aweme_id=info.aweme_id)
            try:
                response = open_stream(play_url, cookie=os.environ.get("DOUYIN_COOKIE"))
            except Exception:
                session = requests.Session()
                session.headers.update({
                    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
                    "Accept": "*/*",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Origin": "https://www.douyin.com",
                    "Referer": "https://www.douyin.com/",
                    "Range": "bytes=0-",
                })
                response = session.get(play_url, stream=True, timeout=30)
                response.raise_for_status()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with response, out_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
            if out_path.stat().st_size > 1024 * 1024:
                return {"aweme": info.to_dict(), "selected_candidate": idx, "bytes": out_path.stat().st_size}
            errors.append(f"candidate {idx}: too small")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"candidate {idx}: {exc}")
    try:
        log("download_ytdlp_fallback", aweme_id=info.aweme_id)
        download_with_ytdlp(douyin_url, out_path, aweme_id=info.aweme_id)
        if out_path.stat().st_size > 1024 * 1024:
            return {
                "aweme": info.to_dict(),
                "selected_candidate": "yt-dlp",
                "bytes": out_path.stat().st_size,
                "resolver_errors": errors,
            }
        errors.append("yt-dlp: output too small")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"yt-dlp: {exc}")
    raise DouyinError("; ".join(errors) or "no playable Douyin URL candidates")


def download_source_video(*, source_uri: str, douyin_url: str, out_path: Path) -> dict[str, Any]:
    if source_uri:
        if not source_uri.startswith("gs://"):
            raise ValueError("SOURCE_URI must be a gs:// URI")
        sys.path.insert(0, "/app")
        from container_short.steps import gcsio

        out_path.parent.mkdir(parents=True, exist_ok=True)
        gcsio.download(source_uri, str(out_path))
        return {"source_uri": source_uri, "selected_candidate": "gcsio", "bytes": out_path.stat().st_size}
    if not douyin_url:
        raise ValueError("either SOURCE_URI or DOUYIN_URL is required")
    return download_douyin(douyin_url, out_path)


def extra_douyin_candidates(douyin_url: str, cookie: str | None = None) -> list[str]:
    from douyin_api.douyin_downloader import _new_session, _parse_router_data, _resolve_aweme_id, extract_url

    session = _new_session(cookie)
    aweme_id = _resolve_aweme_id(extract_url(douyin_url), session)
    response = session.get(f"https://www.iesdouyin.com/share/video/{aweme_id}/", timeout=15)
    response.raise_for_status()
    item = _parse_router_data(response.text, aweme_id)
    video = item.get("video") or {}
    candidates: list[str] = []

    def add_url(url: Any) -> None:
        if not isinstance(url, str) or not url.startswith("http"):
            return
        candidates.append(url)
        if "playwm" in url:
            candidates.append(url.replace("playwm", "play"))

    def collect_addr(addr: Any) -> None:
        if not isinstance(addr, dict):
            return
        for url in addr.get("url_list") or []:
            add_url(url)

    for key in ("play_addr", "download_addr", "play_addr_h264", "play_addr_bytevc1"):
        collect_addr(video.get(key))
    for bitrate in video.get("bit_rate") or []:
        if not isinstance(bitrate, dict):
            continue
        for value in bitrate.values():
            collect_addr(value)
    return [url for i, url in enumerate(candidates) if url and url not in candidates[:i]]


def mint_ttwid() -> str:
    """Register an anonymous ttwid cookie; some CDN/API paths refuse without it."""
    import requests

    body = {
        "region": "cn",
        "aid": 1768,
        "needFid": False,
        "service": "www.ixigua.com",
        "migrate_info": {"ticket": "", "source": "node"},
        "cbUrlProtocol": "https",
        "union": True,
    }
    try:
        response = requests.post(
            "https://ttwid.bytedance.com/ttwid/union/register/",
            json=body,
            timeout=15,
        )
        return response.cookies.get("ttwid") or ""
    except Exception:  # noqa: BLE001
        return ""


def _douyin_cookie_string() -> str:
    """DOUYIN_COOKIE env (browser export) plus an auto-minted ttwid."""
    parts = [os.environ.get("DOUYIN_COOKIE", "").strip().rstrip(";")]
    if "ttwid=" not in parts[0]:
        ttwid = mint_ttwid()
        if ttwid:
            parts.append(f"ttwid={ttwid}")
    return "; ".join(part for part in parts if part)


def _write_netscape_cookies(cookie_string: str, path: Path) -> None:
    lines = ["# Netscape HTTP Cookie File"]
    for pair in cookie_string.split(";"):
        name, _, value = pair.strip().partition("=")
        if not name or not value:
            continue
        lines.append(f".douyin.com\tTRUE\t/\tTRUE\t0\t{name}\t{value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def download_with_ytdlp(douyin_url: str, out_path: Path, aweme_id: str = "") -> None:
    if out_path.exists():
        out_path.unlink()
    # The short link may redirect to iesdouyin.com/share/... which yt-dlp does
    # not support; the canonical /video/{id} URL always hits the Douyin extractor.
    target_url = (
        f"https://www.douyin.com/video/{aweme_id}" if aweme_id else douyin_url
    )
    args = [
        "python",
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--retries",
        "3",
        "--fragment-retries",
        "3",
        "--merge-output-format",
        "mp4",
        "-f",
        "best[ext=mp4]/best",
        "-o",
        str(out_path),
    ]
    cookie_string = _douyin_cookie_string()
    cookie_file = out_path.parent / "douyin_cookies.txt"
    if cookie_string:
        _write_netscape_cookies(cookie_string, cookie_file)
        args += ["--cookies", str(cookie_file)]
    args.append(target_url)
    try:
        cmd(args, timeout=1800)
    finally:
        if cookie_file.exists():
            cookie_file.unlink()


def make_clip(src: Path, dst: Path, start: float, seconds: float, *, crf: str = "18") -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd([
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-ss",
        ffmpeg_ts(start),
        "-t",
        ffmpeg_ts(seconds),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        crf,
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(dst),
    ], timeout=1800)


def output_crf() -> str:
    return os.environ.get("DESUB_OUTPUT_CRF", "0")


def copy_clip_segment(src: Path, dst: Path, start: float, seconds: float) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if seconds <= 0.02:
        return
    cmd([
        "ffmpeg",
        "-y",
        "-ss",
        ffmpeg_ts(start),
        "-t",
        ffmpeg_ts(seconds),
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        "-reset_timestamps",
        "1",
        str(dst),
    ], timeout=1800)


def concat_clips(segments: list[Path], dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not segments:
        raise ValueError("concat_clips requires at least one segment")
    if len(segments) == 1:
        shutil.copy2(segments[0], dst)
        return
    list_path = dst.parent / f"{dst.stem}_concat.txt"
    lines = []
    for segment in segments:
        safe = str(segment.resolve()).replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cmd([
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(dst),
    ], timeout=1800)


def compose_overlay_segments(
    base_clip: Path,
    overlays: list[tuple[Path, float, float]],
    dst: Path,
) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not overlays:
        shutil.copy2(base_clip, dst)
        return
    args = ["ffmpeg", "-y", "-i", str(base_clip)]
    for overlay_clip, _start, _end in overlays:
        args.extend(["-i", str(overlay_clip)])
    chains: list[str] = []
    current = "[0:v]"
    for idx, (_overlay_clip, start, end) in enumerate(overlays, start=1):
        shifted = f"seg{idx}"
        out = f"v{idx}"
        chains.append(f"[{idx}:v]setpts=PTS+{start:.6f}/TB[{shifted}]")
        chains.append(
            f"{current}[{shifted}]overlay=0:0:eof_action=pass:"
            f"enable='between(t,{start:.6f},{end:.6f})'[{out}]"
        )
        current = f"[{out}]"
    args.extend([
        "-filter_complex",
        ";".join(chains),
        "-map",
        current,
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        output_crf(),
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(dst),
    ])
    cmd(args, timeout=2400)


def video_keyframes(src: Path, meta: VideoMeta) -> list[float]:
    proc = cmd([
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-skip_frame",
        "nokey",
        "-show_entries",
        "frame=best_effort_timestamp_time,pkt_pts_time",
        "-of",
        "json",
        str(src),
    ], timeout=120)
    frames = json.loads(proc.stdout or "{}").get("frames", [])
    times: list[float] = []
    for frame in frames:
        raw = frame.get("best_effort_timestamp_time") or frame.get("pkt_pts_time")
        if raw in (None, "N/A"):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if 0.0 <= value <= meta.duration + 0.5:
            times.append(value)
    times.extend([0.0, meta.duration])
    return sorted({round(value, 6) for value in times})


def keyframe_at_or_before(keyframes: list[float], seconds: float) -> float:
    target = max(0.0, seconds)
    before = [value for value in keyframes if value <= target + 0.001]
    return before[-1] if before else 0.0


def keyframe_at_or_after(keyframes: list[float], seconds: float, duration: float) -> float:
    target = min(duration, max(0.0, seconds))
    for value in keyframes:
        if value >= target - 0.001:
            return min(duration, value)
    return duration


def align_clusters_to_keyframes(
    clusters: list[MaskCluster],
    source_clip: Path,
    meta: VideoMeta,
) -> tuple[list[SegmentCluster], dict[str, Any]]:
    keyframes = video_keyframes(source_clip, meta)
    aligned: list[SegmentCluster] = []
    for cluster in sorted(clusters, key=lambda item: (item.context_start, item.context_end)):
        segment_start = keyframe_at_or_before(keyframes, cluster.context_start)
        segment_end = keyframe_at_or_after(keyframes, cluster.context_end, meta.duration)
        if segment_end <= segment_start + 0.02:
            segment_end = min(meta.duration, max(segment_end, cluster.context_end))
        candidate = SegmentCluster(clusters=(cluster,), segment_start=segment_start, segment_end=segment_end)
        if aligned and candidate.segment_start <= aligned[-1].segment_end + 0.001:
            previous = aligned[-1]
            aligned[-1] = SegmentCluster(
                clusters=(*previous.clusters, cluster),
                segment_start=previous.segment_start,
                segment_end=max(previous.segment_end, candidate.segment_end),
            )
            continue
        aligned.append(candidate)
    return aligned, {
        "keyframe_count": len(keyframes),
        "keyframes_preview": keyframes[:8],
        "keyframes_tail": keyframes[-8:],
    }


def crop_video(src: Path, dst: Path, y1: int, y2: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    height = y2 - y1
    cmd([
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-vf",
        f"crop=iw:{height}:0:{y1}",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        output_crf(),
        str(dst),
    ], timeout=1800)


def overlay_crop(
    src: Path,
    crop: Path,
    dst: Path,
    y1: int,
    *,
    rects: list[tuple[int, int, int, int]] | None = None,
    enable_between: tuple[float, float] | None = None,
    overlay_regions: list[OverlayRegion] | None = None,
    alpha_mask: Path | None = None,
) -> None:
    def enable_expr(start: float | None, end: float | None) -> str:
        if start is None or end is None:
            return ""
        return f":enable='between(t,{max(0.0, start):.3f},{max(0.0, end):.3f})'"

    if alpha_mask is not None:
        start, end = enable_between if enable_between is not None else (None, None)
        filter_complex = (
            "[1:v]format=rgba[patchrgb];"
            "[2:v]format=gray[alpha];"
            "[patchrgb][alpha]alphamerge[patcha];"
            f"[0:v][patcha]overlay=0:{y1}:eof_action=pass"
            f"{enable_expr(start, end)}[v]"
        )
        current = "[v]"
    else:
        regions = overlay_regions
        if regions is None and rects:
            start, end = enable_between if enable_between is not None else (None, None)
            regions = [OverlayRegion(rect=rect, enable_start=start, enable_end=end) for rect in rects]
    if alpha_mask is None and regions:
        chains: list[str] = []
        current = "[0:v]"
        for idx, region in enumerate(regions):
            x1, ry1, x2, ry2 = region.rect
            width = max(1, x2 - x1)
            height = max(1, ry2 - ry1)
            crop_y = max(0, ry1 - y1)
            patch = f"patch{idx}"
            out = f"v{idx}"
            chains.append(f"[1:v]crop={width}:{height}:{x1}:{crop_y}[{patch}]")
            chains.append(
                f"{current}[{patch}]overlay={x1}:{ry1}:eof_action=pass"
                f"{enable_expr(region.enable_start, region.enable_end)}[{out}]"
            )
            current = f"[{out}]"
        filter_complex = ";".join(chains)
    elif alpha_mask is None:
        start, end = enable_between if enable_between is not None else (None, None)
        filter_complex = f"[0:v][1:v]overlay=0:{y1}:eof_action=pass{enable_expr(start, end)}[v]"
        current = "[v]"
    args = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-i",
        str(crop),
    ]
    if alpha_mask is not None:
        args.extend(["-i", str(alpha_mask)])
    args.extend([
        "-filter_complex",
        filter_complex,
        "-map",
        current,
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        output_crf(),
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(dst),
    ])
    cmd(args, timeout=1800)


def extract_frame(video: Path, out_png: Path, seconds: float) -> None:
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cmd([
        "ffmpeg",
        "-y",
        "-i",
        str(video),
        "-ss",
        ffmpeg_ts(seconds),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(out_png),
    ], timeout=120)


def parse_seconds_list(raw: str) -> list[float]:
    values: list[float] = []
    for part in raw.split(","):
        text = part.strip()
        if not text:
            continue
        try:
            values.append(float(text))
        except ValueError:
            log("invalid_seconds_value", value=text)
    return values


def zoom_crop_box(
    rects: list[tuple[int, int, int, int]],
    meta: VideoMeta,
    *,
    pad: int,
) -> tuple[int, int, int, int]:
    if rects:
        x1, y1, x2, y2 = union_boxes(rects)
    else:
        band_start, band_end = env_ratio_pair("DESUB_SUB_BAND_Y", (0.55, 0.85))
        x1 = int(meta.width * 0.10)
        x2 = int(meta.width * 0.90)
        y1 = int(meta.height * band_start)
        y2 = int(meta.height * band_end)
    x1 = max(0, x1 - pad)
    y1 = max(0, y1 - pad)
    x2 = min(meta.width, x2 + pad)
    y2 = min(meta.height, y2 + pad)
    if x2 <= x1 or y2 <= y1:
        return (0, 0, meta.width, meta.height)
    return x1, y1, x2, y2


def extract_zoom_frame(
    video: Path,
    out_png: Path,
    seconds: float,
    box: tuple[int, int, int, int],
    *,
    scale: int = 2,
) -> None:
    out_png.parent.mkdir(parents=True, exist_ok=True)
    x1, y1, x2, y2 = box
    width = max(1, x2 - x1)
    height = max(1, y2 - y1)
    scale = max(1, scale)
    cmd([
        "ffmpeg",
        "-y",
        "-i",
        str(video),
        "-ss",
        ffmpeg_ts(seconds),
        "-frames:v",
        "1",
        "-vf",
        f"crop={width}:{height}:{x1}:{y1},scale=iw*{scale}:ih*{scale}:flags=lanczos",
        "-q:v",
        "2",
        str(out_png),
    ], timeout=120)


def frame_diff_metrics(
    before_png: Path,
    after_png: Path,
    *,
    ignore_rects: list[tuple[int, int, int, int]],
) -> dict[str, Any]:
    import cv2
    import numpy as np

    before = cv2.imread(str(before_png), cv2.IMREAD_COLOR)
    after = cv2.imread(str(after_png), cv2.IMREAD_COLOR)
    if before is None or after is None or before.shape != after.shape:
        return {"ok": False, "error": "frame read failed or shape mismatch"}
    diff = cv2.absdiff(before, after)
    mask = np.ones(before.shape[:2], dtype=np.uint8)
    height, width = mask.shape
    for x1, y1, x2, y2 in ignore_rects:
        clipped = clamp_box((x1, y1, x2, y2), width, height)
        if clipped:
            cx1, cy1, cx2, cy2 = clipped
            mask[cy1:cy2, cx1:cx2] = 0
    active = mask.astype(bool)
    if not active.any():
        return {"ok": True, "ignored_all_pixels": True}
    outside = diff[active]
    per_pixel = outside.max(axis=1)
    changed = int((per_pixel > 0).sum())
    return {
        "ok": True,
        "ignored_rects": [{"x1": x1, "y1": y1, "x2": x2, "y2": y2} for x1, y1, x2, y2 in ignore_rects],
        "outside_pixels": int(per_pixel.size),
        "changed_pixels": changed,
        "changed_ratio": changed / max(1, int(per_pixel.size)),
        "max_abs_diff": int(per_pixel.max()) if per_pixel.size else 0,
        "mean_abs_diff": float(outside.mean()) if outside.size else 0.0,
    }


def sample_frames(video: Path, meta: VideoMeta, detect_fps: float) -> list[tuple[int, float, Any]]:
    import cv2

    step = max(1, int(round(meta.fps / max(0.1, detect_fps))))
    frame_ids = list(range(0, max(1, meta.frames), step))
    cap = cv2.VideoCapture(str(video))
    frames: list[tuple[int, float, Any]] = []
    for frame_id in frame_ids:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_id)
        ok, frame = cap.read()
        if ok:
            frames.append((frame_id, frame_id / meta.fps, frame))
    cap.release()
    return frames


def clamp_box(box: tuple[float, float, float, float], width: int, height: int) -> tuple[int, int, int, int] | None:
    x1, y1, x2, y2 = box
    x1 = max(0, min(width - 1, int(math.floor(x1))))
    y1 = max(0, min(height - 1, int(math.floor(y1))))
    x2 = max(0, min(width, int(math.ceil(x2))))
    y2 = max(0, min(height, int(math.ceil(y2))))
    if x2 - x1 < 4 or y2 - y1 < 4:
        return None
    return x1, y1, x2, y2


def poly_to_box(points: Any) -> tuple[float, float, float, float] | None:
    try:
        coords = [(float(p[0]), float(p[1])) for p in points]
    except Exception:  # noqa: BLE001
        return None
    if not coords:
        return None
    xs = [p[0] for p in coords]
    ys = [p[1] for p in coords]
    return min(xs), min(ys), max(xs), max(ys)


def detection_from_crop_box(
    detector: str,
    frame_id: int,
    frame_time: float,
    crop_y: int,
    width: int,
    height: int,
    raw_box: tuple[float, float, float, float],
    score: float,
    text: str = "",
) -> Detection | None:
    x1, y1, x2, y2 = raw_box
    box = clamp_box((x1, y1 + crop_y, x2, y2 + crop_y), width, height)
    if not box:
        return None
    return Detection(detector, frame_id, frame_time, *box, score=float(score or 0), text=text or "")


def detect_easyocr(
    frames: list[tuple[int, float, Any]],
    meta: VideoMeta,
    crop_y: int,
    deadline: float | None = None,
) -> tuple[list[Detection], dict[str, Any]]:
    import easyocr

    allow_download = env_bool("DESUB_ALLOW_RUNTIME_MODEL_DOWNLOAD", False)
    use_gpu = env_bool("DESUB_EASYOCR_GPU", True)
    model_dir = os.environ.get("EASYOCR_MODEL_DIR", "/models/easyocr")
    reader = easyocr.Reader(
        ["ch_sim", "en"],
        gpu=use_gpu,
        model_storage_directory=model_dir,
        download_enabled=allow_download,
        verbose=False,
    )
    detections: list[Detection] = []
    timed_out = False
    for frame_id, frame_time, frame in frames:
        if deadline is not None and time.time() > deadline:
            timed_out = True
            break
        crop = frame[crop_y:meta.height, :]
        results = reader.readtext(
            crop,
            detail=1,
            paragraph=False,
            text_threshold=float(os.environ.get("DESUB_EASYOCR_TEXT_THRESHOLD", "0.25")),
            low_text=float(os.environ.get("DESUB_EASYOCR_LOW_TEXT", "0.25")),
        )
        for points, text, score in results:
            raw_box = poly_to_box(points)
            if raw_box is None:
                continue
            detection = detection_from_crop_box(
                "easyocr_craft",
                frame_id,
                frame_time,
                crop_y,
                meta.width,
                meta.height,
                raw_box,
                float(score or 0),
                str(text or ""),
            )
            if detection:
                detections.append(detection)
    return detections, {
        "ok": True,
        "count": len(detections),
        "model_dir": model_dir,
        "gpu": use_gpu,
        "timed_out": timed_out,
    }


def flatten_paddle_result(value: Any) -> Iterable[tuple[Any, str, float]]:
    if value is None:
        return
    if hasattr(value, "json"):
        try:
            yield from flatten_paddle_result(value.json)
            return
        except Exception:  # noqa: BLE001
            pass
    if isinstance(value, dict):
        polys = value.get("dt_polys") or value.get("rec_polys") or value.get("polys") or []
        if len(polys) == 0:
            # PaddleOCR 3.x wraps payloads as {"res": {...}} — recurse into values.
            for child in value.values():
                yield from flatten_paddle_result(child)
            return
        texts = value.get("rec_texts") or value.get("texts") or [""] * len(polys)
        scores = (
            value.get("rec_scores")
            or value.get("dt_scores")
            or value.get("scores")
            or [0.0] * len(polys)
        )
        for idx, poly in enumerate(polys):
            yield poly, str(texts[idx] if idx < len(texts) else ""), float(scores[idx] if idx < len(scores) else 0.0)
        return
    if isinstance(value, (list, tuple)):
        if len(value) >= 2 and isinstance(value[1], (list, tuple)) and poly_to_box(value[0]) is not None:
            text_score = value[1]
            text = str(text_score[0] if len(text_score) > 0 else "")
            score = float(text_score[1] if len(text_score) > 1 else 0.0)
            yield value[0], text, score
            return
        for item in value:
            yield from flatten_paddle_result(item)


def detect_paddle(
    frames: list[tuple[int, float, Any]],
    meta: VideoMeta,
    crop_y: int,
    deadline: float | None = None,
) -> tuple[list[Detection], dict[str, Any]]:
    # The paddlepaddle wheel in this image is CPU-only; full PaddleOCR predict
    # (det+rec) costs 10s+/frame there. Masking only needs boxes, so use the
    # detection-only module and keep the full pipeline as a last resort.
    # paddle 3.3.1 PIR + oneDNN crashes on this det model
    # (ConvertPirAttribute2RuntimeAttribute); plain CPU kernels avoid it.
    os.environ.setdefault("FLAGS_use_mkldnn", "0")
    os.environ.setdefault("FLAGS_enable_pir_api", "0")
    try:
        from paddleocr import TextDetection

        try:
            ocr = TextDetection(enable_mkldnn=False)
        except TypeError:
            ocr = TextDetection()
        api_used = "text_detection"
    except Exception:  # noqa: BLE001
        from paddleocr import PaddleOCR

        try:
            ocr = PaddleOCR(
                lang="ch",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
        except TypeError:
            ocr = PaddleOCR(lang="ch", use_angle_cls=False, show_log=False)
        api_used = "predict" if hasattr(ocr, "predict") else "ocr"

    detections: list[Detection] = []
    timed_out = False
    processed = 0
    for frame_id, frame_time, frame in frames:
        if deadline is not None and time.time() > deadline:
            timed_out = True
            break
        crop = frame[crop_y:meta.height, :]
        if api_used in ("text_detection", "predict"):
            result = ocr.predict(crop)
        else:
            result = ocr.ocr(crop, det=True, rec=False, cls=False)
        processed += 1
        for points, text, score in flatten_paddle_result(result):
            raw_box = poly_to_box(points)
            if raw_box is None:
                continue
            detection = detection_from_crop_box(
                "paddle_det",
                frame_id,
                frame_time,
                crop_y,
                meta.width,
                meta.height,
                raw_box,
                score,
                text,
            )
            if detection:
                detections.append(detection)
    return detections, {
        "ok": True,
        "count": len(detections),
        "api": api_used,
        "frames_processed": processed,
        "frames_total": len(frames),
        "timed_out": timed_out,
    }


def box_iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
    area_b = max(1, (bx2 - bx1) * (by2 - by1))
    return inter / float(area_a + area_b - inter)


def dilate_box(box: tuple[int, int, int, int], px: int, width: int, height: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    return max(0, x1 - px), max(0, y1 - px), min(width, x2 + px), min(height, y2 + px)


def union_boxes(boxes: Iterable[tuple[int, int, int, int]]) -> tuple[int, int, int, int]:
    collected = list(boxes)
    return (
        min(box[0] for box in collected),
        min(box[1] for box in collected),
        max(box[2] for box in collected),
        max(box[3] for box in collected),
    )


def dedupe_detections(detections: list[Detection]) -> list[Detection]:
    grouped: dict[int, list[Detection]] = {}
    for det in detections:
        grouped.setdefault(det.frame, []).append(det)
    result: list[Detection] = []
    for frame in sorted(grouped):
        selected: list[Detection] = []
        for det in sorted(grouped[frame], key=lambda item: item.score, reverse=True):
            if all(box_iou(det.box, existing.box) < 0.55 for existing in selected):
                selected.append(det)
        result.extend(selected)
    return result


def build_spans(detections: list[Detection], meta: VideoMeta, *, dilate_px: int) -> list[Span]:
    detections = dedupe_detections(detections)
    if not detections:
        return []
    max_gap_frames = max(2, int(round(meta.fps * 0.6)))
    tracks: list[dict[str, Any]] = []
    active: list[dict[str, Any]] = []

    for det in sorted(detections, key=lambda item: (item.frame, item.y1, item.x1)):
        still_active: list[dict[str, Any]] = []
        for track in active:
            if det.frame - track["last_frame"] <= max_gap_frames:
                still_active.append(track)
            else:
                tracks.append(track)
        active = still_active
        best_track: dict[str, Any] | None = None
        best_score = 0.0
        for track in active:
            score = box_iou(det.box, track["last_box"])
            if score > best_score:
                best_score = score
                best_track = track
        if best_track is None or best_score < 0.18:
            active.append({
                "detections": [det],
                "first_frame": det.frame,
                "last_frame": det.frame,
                "last_box": det.box,
            })
            continue
        best_track["detections"].append(det)
        best_track["last_frame"] = det.frame
        best_track["last_box"] = det.box
    tracks.extend(active)

    spans: list[Span] = []
    for track in tracks:
        items: list[Detection] = track["detections"]
        if not items:
            continue
        detector_counts: dict[str, int] = {}
        for item in items:
            detector_counts[item.detector] = detector_counts.get(item.detector, 0) + 1
        box = dilate_box(union_boxes(item.box for item in items), dilate_px, meta.width, meta.height)
        first = min(item.frame for item in items)
        last = max(item.frame for item in items)
        pad_t = max(0.25, 1.0 / max(0.1, env_float("DESUB_DETECT_FPS", DEFAULT_DETECT_FPS)))
        spans.append(Span(
            t_start=max(0.0, first / meta.fps - pad_t),
            t_end=min(meta.duration, last / meta.fps + pad_t),
            frame_start=max(0, int(first - meta.fps * pad_t)),
            frame_end=min(meta.frames, int(last + meta.fps * pad_t)),
            box=box,
            detector_counts=detector_counts,
            samples=len(items),
        ))
    return merge_spans(spans, meta)


def merge_spans(spans: list[Span], meta: VideoMeta) -> list[Span]:
    merged: list[Span] = []
    for span in sorted(spans, key=lambda item: (item.t_start, item.box[1], item.box[0])):
        if not merged:
            merged.append(span)
            continue
        last = merged[-1]
        time_touch = span.t_start <= last.t_end + 0.35
        same_area = box_iou(span.box, last.box) >= 0.25
        if time_touch and same_area:
            counts = dict(last.detector_counts)
            for key, value in span.detector_counts.items():
                counts[key] = counts.get(key, 0) + value
            box = union_boxes([last.box, span.box])
            merged[-1] = Span(
                t_start=min(last.t_start, span.t_start),
                t_end=max(last.t_end, span.t_end),
                frame_start=min(last.frame_start, span.frame_start),
                frame_end=max(last.frame_end, span.frame_end),
                box=dilate_box(box, 0, meta.width, meta.height),
                detector_counts=counts,
                samples=last.samples + span.samples,
            )
        else:
            merged.append(span)
    return merged


def box_area(box: tuple[int, int, int, int]) -> int:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def dedupe_rects(
    rects: Iterable[tuple[int, int, int, int]],
    *,
    iou_threshold: float = 0.85,
) -> list[tuple[int, int, int, int]]:
    deduped: list[tuple[int, int, int, int]] = []
    for rect in sorted(rects, key=lambda box: (box[1], box[0], box[3], box[2])):
        if all(box_iou(rect, existing) < iou_threshold for existing in deduped):
            deduped.append(rect)
    return deduped


def env_ratio_pair(name: str, default: tuple[float, float]) -> tuple[float, float]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    left, sep, right = raw.partition(",")
    if not sep:
        return default
    lo = max(0.0, min(1.0, float(left.strip())))
    hi = max(0.0, min(1.0, float(right.strip())))
    return (min(lo, hi), max(lo, hi))


def filter_spans_for_inpaint(
    spans: list[Span],
    meta: VideoMeta,
    *,
    crop_y: int,
) -> tuple[list[Span], list[dict[str, Any]]]:
    kept: list[Span] = []
    rejected: list[dict[str, Any]] = []
    band_area = meta.width * max(1, meta.height - crop_y)
    sub_y_min, sub_y_max = env_ratio_pair("DESUB_SUB_BAND_Y", (0.55, 0.90))
    sub_x_min, sub_x_max = env_ratio_pair("DESUB_SUB_BAND_X", (0.15, 0.85))
    for idx, span in enumerate(spans):
        x1, y1, x2, y2 = span.box
        width = x2 - x1
        height = y2 - y1
        area = max(0, width) * max(0, height)
        center_x = ((x1 + x2) / 2.0) / max(1, meta.width)
        center_y = ((y1 + y2) / 2.0) / max(1, meta.height)
        reason = ""
        min_samples = env_int("DESUB_MIN_SPAN_SAMPLES", 3)
        if span.samples < min_samples:
            reason = f"samples_lt_{min_samples}"
        elif not (sub_y_min <= center_y <= sub_y_max):
            reason = "center_y_out_sub_band"
        elif not (sub_x_min <= center_x <= sub_x_max):
            reason = "center_x_out_sub_band"
        elif height > env_int("DESUB_MAX_SPAN_HEIGHT_PX", 120):
            reason = "height_gt_120"
        elif height > width:
            reason = "height_gt_width"
        elif area > band_area * env_float("DESUB_MAX_SPAN_BAND_AREA_RATIO", 0.20):
            reason = "area_gt_maxpct_band"
        if reason:
            rejected.append({"index": idx, "reason": reason, "span": asdict(span)})
            continue
        kept.append(span)
    return kept, rejected


def split_watermark_spans(spans: list[Span], meta: VideoMeta) -> tuple[list[Span], list[Span]]:
    if not env_bool("DESUB_WATERMARK_MASKS_ENABLED", True):
        return spans, []
    min_duration_ratio = env_float("DESUB_WATERMARK_MIN_DURATION_RATIO", 0.80)
    watermark: list[Span] = []
    subtitle: list[Span] = []
    for span in spans:
        if box_center_in_sub_band(span.box, meta):
            subtitle.append(span)
            continue
        if span.t_end - span.t_start >= meta.duration * min_duration_ratio:
            watermark.append(span)
        else:
            subtitle.append(span)
    return subtitle, watermark


def build_subtitle_clusters(spans: list[Span], meta: VideoMeta) -> list[MaskCluster]:
    if not spans:
        return []
    gap_seconds = env_float("DESUB_CLUSTER_GAP_SECONDS", 0.5)
    pad_seconds = env_float("DESUB_CLUSTER_PAD_SECONDS", 0.25)
    min_context = env_float("DESUB_MIN_CONTEXT_SECONDS", 1.5)

    grouped: list[list[Span]] = []
    for span in sorted(spans, key=lambda item: (item.t_start, item.t_end)):
        if grouped and span.t_start <= max(item.t_end for item in grouped[-1]) + gap_seconds:
            grouped[-1].append(span)
        else:
            grouped.append([span])

    clusters: list[MaskCluster] = []
    for group in grouped:
        write_start = max(0.0, min(span.t_start for span in group) - pad_seconds)
        write_end = min(meta.duration, max(span.t_end for span in group) + pad_seconds)
        if write_end - write_start <= 0.05:
            continue
        context_start = write_start
        context_end = write_end
        if context_end - context_start < min_context:
            center = (context_start + context_end) / 2.0
            half = min_context / 2.0
            context_start = max(0.0, center - half)
            context_end = min(meta.duration, center + half)
            if context_end - context_start < min_context:
                if context_start <= 0.0:
                    context_end = min(meta.duration, min_context)
                elif context_end >= meta.duration:
                    context_start = max(0.0, meta.duration - min_context)
        rects = tuple(dedupe_rects((span.box for span in group), iou_threshold=0.65))
        if not rects:
            continue
        clusters.append(MaskCluster(
            kind="subtitle",
            t_start=write_start,
            t_end=write_end,
            context_start=context_start,
            context_end=context_end,
            rects=rects,
            span_count=len(group),
        ))
    return merge_clusters(clusters)


def merge_clusters(clusters: list[MaskCluster]) -> list[MaskCluster]:
    merged: list[MaskCluster] = []
    for cluster in sorted(clusters, key=lambda item: (item.t_start, item.t_end)):
        if not merged or cluster.t_start > merged[-1].t_end + 0.001:
            merged.append(cluster)
            continue
        last = merged[-1]
        rects = tuple(dedupe_rects([*last.rects, *cluster.rects], iou_threshold=0.65))
        merged[-1] = MaskCluster(
            kind=last.kind,
            t_start=min(last.t_start, cluster.t_start),
            t_end=max(last.t_end, cluster.t_end),
            context_start=min(last.context_start, cluster.context_start),
            context_end=max(last.context_end, cluster.context_end),
            rects=rects,
            span_count=last.span_count + cluster.span_count,
        )
    return merged


def cluster_dict(cluster: MaskCluster) -> dict[str, Any]:
    return {
        "kind": cluster.kind,
        "t_start": cluster.t_start,
        "t_end": cluster.t_end,
        "context_start": cluster.context_start,
        "context_end": cluster.context_end,
        "rects": [{"x1": x1, "y1": y1, "x2": x2, "y2": y2} for x1, y1, x2, y2 in cluster.rects],
        "span_count": cluster.span_count,
    }


def parse_watermark_masks(meta: VideoMeta) -> list[tuple[int, int, int, int]]:
    if not env_bool("DESUB_WATERMARK_MASKS_ENABLED", True):
        return []
    raw = os.environ.get("DESUB_WATERMARK_MASKS", "").strip()
    if not raw:
        return []
    masks: list[tuple[int, int, int, int]] = []
    for part in raw.split(";"):
        values = [value.strip() for value in part.split(",") if value.strip()]
        if len(values) != 4:
            continue
        nums = [float(value) for value in values]
        if all(0.0 <= value <= 1.0 for value in nums):
            x1, y1, x2, y2 = (
                nums[0] * meta.width,
                nums[1] * meta.height,
                nums[2] * meta.width,
                nums[3] * meta.height,
            )
        else:
            x1, y1, x2, y2 = nums
        box = clamp_box((x1, y1, x2, y2), meta.width, meta.height)
        if box:
            masks.append(box)
    return masks


def mask_rects_from_spans(spans: list[Span], meta: VideoMeta) -> list[tuple[int, int, int, int]]:
    rects = [union_boxes(span.box for span in spans)] if spans else []
    rects.extend(parse_watermark_masks(meta))
    if not rects:
        top = int(meta.height * env_float("DESUB_BAND_TOP_RATIO", DEFAULT_BAND_TOP_RATIO))
        rects.append((0, top, meta.width, meta.height))
    return dedupe_rects(rects)


def build_watermark_cluster(meta: VideoMeta, spans: list[Span]) -> MaskCluster | None:
    span_rects = [span.box for span in spans]
    rects = tuple(dedupe_rects([*span_rects, *parse_watermark_masks(meta)], iou_threshold=0.65))
    if not rects:
        return None
    return MaskCluster(
        kind="watermark",
        t_start=0.0,
        t_end=meta.duration,
        context_start=0.0,
        context_end=meta.duration,
        rects=rects,
        span_count=0,
    )


def crop_bounds(rects: list[tuple[int, int, int, int]], meta: VideoMeta) -> tuple[int, int]:
    pad = env_int("DESUB_CROP_PAD_PX", 24)
    y1 = max(0, min(rect[1] for rect in rects) - pad)
    y2 = min(meta.height, max(rect[3] for rect in rects) + pad)
    min_height = min(meta.height, max(96, int(meta.width * 5 / 18) + 8))
    if y2 - y1 < min_height:
        center = (y1 + y2) // 2
        y1 = max(0, center - min_height // 2)
        y2 = min(meta.height, y1 + min_height)
        y1 = max(0, y2 - min_height)
    return y1, y2


class VramMonitor:
    def __init__(self) -> None:
        self.max_mb: int | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "VramMonitor":
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                proc = subprocess.run(
                    [
                        "nvidia-smi",
                        "--query-gpu=memory.used",
                        "--format=csv,noheader,nounits",
                    ],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                if proc.returncode == 0:
                    for line in proc.stdout.splitlines():
                        value = int(float(line.strip()))
                        self.max_mb = value if self.max_mb is None else max(self.max_mb, value)
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(1.0)


def write_mask_inpaint_runner(path: Path) -> None:
    path.write_text(
        r'''
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np
import torch


sys.path.insert(0, "/opt/video-subtitle-remover")

from backend.inpaint.lama_inpaint import LamaInpaint
from backend.inpaint.sttn_det_inpaint import STTNDetInpaint
from backend.tools.model_config import ModelConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", required=True, choices=("sttn-det", "lama-mask"))
    parser.add_argument("--rects-json", required=True)
    parser.add_argument("--batch-frames", type=int, default=48)
    parser.add_argument("--mask-pad", type=int, default=0)
    parser.add_argument("--mask-kind", choices=("box", "stroke"), default="box")
    parser.add_argument("--mask-video")
    parser.add_argument("--mask-stats-json")
    parser.add_argument("--stroke-white-threshold", type=int, default=200)
    parser.add_argument("--stroke-black-threshold", type=int, default=60)
    parser.add_argument("--stroke-touch-radius", type=int, default=4)
    parser.add_argument("--stroke-close-px", type=int, default=3)
    parser.add_argument("--stroke-dilate-px", type=int, default=3)
    parser.add_argument("--stroke-feather-px", type=int, default=2)
    parser.add_argument("--stroke-fallback-min-ratio", type=float, default=0.15)
    parser.add_argument("--lama-temporal-blend", type=float, default=0.0)
    return parser.parse_args()


def build_mask(height: int, width: int, rects: list[list[int]], pad: int) -> np.ndarray:
    mask = np.zeros((height, width), dtype=np.uint8)
    for x1, y1, x2, y2 in rects:
        left = max(0, int(x1) - pad)
        top = max(0, int(y1) - pad)
        right = min(width, int(x2) + pad)
        bottom = min(height, int(y2) + pad)
        if right > left and bottom > top:
            cv2.rectangle(mask, (left, top), (right - 1, bottom - 1), 255, thickness=-1)
    return mask


def odd_kernel(size: int) -> np.ndarray:
    size = max(1, int(size))
    if size % 2 == 0:
        size += 1
    return np.ones((size, size), dtype=np.uint8)


def ellipse_kernel(size: int) -> np.ndarray:
    size = max(1, int(size))
    if size % 2 == 0:
        size += 1
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))


def rect_area(rect: list[int]) -> int:
    x1, y1, x2, y2 = rect
    return max(0, int(x2) - int(x1)) * max(0, int(y2) - int(y1))


def keep_core_components(core: np.ndarray, dark: np.ndarray, touch_radius: int) -> np.ndarray:
    if not np.any(core):
        return core
    near_dark = cv2.dilate(dark.astype(np.uint8), odd_kernel(touch_radius * 2 + 1), iterations=1).astype(bool)
    labels_count, labels, stats, _centroids = cv2.connectedComponentsWithStats(core.astype(np.uint8), 8)
    kept = np.zeros_like(core, dtype=bool)
    min_core_area = max(2, int(os.environ.get("DESUB_STROKE_MIN_CORE_AREA_PX", "4")))
    require_dark_touch = os.environ.get("DESUB_STROKE_REQUIRE_DARK_TOUCH", "").strip().lower() in {"1", "true", "yes", "on"}
    for label in range(1, labels_count):
        component = labels == label
        if int(stats[label, cv2.CC_STAT_AREA]) < min_core_area:
            continue
        if not require_dark_touch or np.any(component & near_dark):
            kept[component] = True
    return kept


def stroke_env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def stroke_from_polarity(
    core: np.ndarray,
    outline: np.ndarray,
    args: argparse.Namespace,
    area: int,
    *,
    polarity: str,
    require_outline_touch: bool,
    max_core_bbox_ratio: float,
) -> tuple[np.ndarray, dict[str, float | int | str]]:
    stroke = np.zeros_like(core, dtype=np.uint8)
    if not np.any(core):
        return stroke, {
            "polarity": polarity,
            "core_pixels": 0,
            "stroke_pixels": 0,
            "component_count": 0,
            "component_pixels_total": 0,
            "max_component_pixels": 0,
            "ratio": 0.0,
            "rejected_large_components": 0,
            "rejected_no_outline_touch": 0,
        }

    touch_radius = max(1, int(args.stroke_touch_radius))
    close_px = max(0, int(args.stroke_close_px))
    dilate_px = max(0, int(args.stroke_dilate_px))
    min_core_area = max(2, int(os.environ.get("DESUB_STROKE_MIN_CORE_AREA_PX", "4")))
    near_outline = cv2.dilate(
        outline.astype(np.uint8),
        ellipse_kernel(touch_radius * 2 + 1),
        iterations=1,
    ).astype(bool)
    labels_count, labels, comp_stats, _centroids = cv2.connectedComponentsWithStats(core.astype(np.uint8), 8)
    component_count = 0
    component_pixels_total = 0
    max_component_pixels = 0
    rejected_large = 0
    rejected_no_touch = 0
    kept_core_pixels = 0
    margin = max(1, touch_radius + dilate_px + close_px + 2)
    for label in range(1, labels_count):
        comp_area = int(comp_stats[label, cv2.CC_STAT_AREA])
        if comp_area < min_core_area:
            continue
        cx = int(comp_stats[label, cv2.CC_STAT_LEFT])
        cy = int(comp_stats[label, cv2.CC_STAT_TOP])
        cw = int(comp_stats[label, cv2.CC_STAT_WIDTH])
        ch = int(comp_stats[label, cv2.CC_STAT_HEIGHT])
        bbox_ratio = (cw * ch) / max(1, area)
        if max_core_bbox_ratio > 0 and bbox_ratio > max_core_bbox_ratio:
            rejected_large += 1
            continue
        component = labels == label
        if require_outline_touch and not np.any(component & near_outline):
            rejected_no_touch += 1
            continue
        lx = max(0, cx - margin)
        ly = max(0, cy - margin)
        rx = min(core.shape[1], cx + cw + margin)
        by = min(core.shape[0], cy + ch + margin)
        local_component = labels[ly:by, lx:rx] == label
        local_outline = outline[ly:by, lx:rx]
        near_component = cv2.dilate(
            local_component.astype(np.uint8),
            ellipse_kernel(touch_radius * 2 + 1),
            iterations=1,
        ).astype(bool)
        local_stroke = (local_component | (local_outline & near_component)).astype(np.uint8) * 255
        if close_px > 1:
            local_stroke = cv2.morphologyEx(local_stroke, cv2.MORPH_CLOSE, ellipse_kernel(close_px), iterations=1)
        if dilate_px > 0:
            local_stroke = cv2.dilate(local_stroke, ellipse_kernel(dilate_px * 2 + 1), iterations=1)
        target = stroke[ly:by, lx:rx]
        np.maximum(target, local_stroke, out=target)
        component_count += 1
        kept_core_pixels += comp_area
        component_pixels = int((local_stroke > 0).sum())
        component_pixels_total += component_pixels
        max_component_pixels = max(max_component_pixels, component_pixels)

    stroke_pixels = int((stroke > 0).sum())
    return stroke, {
        "polarity": polarity,
        "core_pixels": kept_core_pixels,
        "stroke_pixels": stroke_pixels,
        "component_count": component_count,
        "component_pixels_total": component_pixels_total,
        "max_component_pixels": max_component_pixels,
        "ratio": stroke_pixels / max(1, area),
        "rejected_large_components": rejected_large,
        "rejected_no_outline_touch": rejected_no_touch,
    }


def choose_stroke_candidate(candidates: list[tuple[np.ndarray, dict[str, float | int | str]]], args: argparse.Namespace) -> tuple[np.ndarray, dict[str, float | int | str]]:
    target_ratio = stroke_env_float("DESUB_STROKE_TARGET_RATIO", 0.24)
    max_ratio = stroke_env_float("DESUB_STROKE_MAX_RATIO", 0.34)
    min_ratio = max(0.005, min(float(args.stroke_fallback_min_ratio), target_ratio) * 0.35)

    def score(candidate: tuple[np.ndarray, dict[str, float | int | str]]) -> tuple[float, float]:
        _mask, stats = candidate
        stroke_pixels = int(stats.get("stroke_pixels", 0))
        if stroke_pixels <= 0:
            return (999.0, 999.0)
        ratio = float(stats.get("ratio", 0.0))
        penalty = abs(ratio - target_ratio)
        if ratio > max_ratio:
            penalty += (ratio - max_ratio) * 5.0
        if ratio < min_ratio:
            penalty += (min_ratio - ratio) * 4.0
        if int(stats.get("component_count", 0)) <= 1:
            penalty += 0.05
        return (penalty, ratio)

    return min(candidates, key=score)


def stroke_mask_for_rect(
    frame: np.ndarray,
    rect: list[int],
    args: argparse.Namespace,
    *,
    allow_fallback: bool = True,
) -> tuple[np.ndarray, dict]:
    x1, y1, x2, y2 = [int(v) for v in rect]
    height, width = frame.shape[:2]
    x1 = max(0, min(width, x1))
    x2 = max(0, min(width, x2))
    y1 = max(0, min(height, y1))
    y2 = max(0, min(height, y2))
    mask = np.zeros((height, width), dtype=np.uint8)
    area = max(1, (x2 - x1) * (y2 - y1))
    if x2 <= x1 or y2 <= y1:
        return mask, {
            "box_pixels": 0,
            "core_pixels": 0,
            "stroke_pixels": 0,
            "effective_pixels": 0,
            "fallback": 0,
            "empty_core": 1,
            "component_count": 0,
            "ratio": 0.0,
        }

    roi = frame[y1:y2, x1:x2]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    light = gray >= int(args.stroke_white_threshold)
    dark = gray <= int(args.stroke_black_threshold)
    white_max_ratio = stroke_env_float("DESUB_STROKE_WHITE_CORE_MAX_BBOX_RATIO", 0.75)
    black_max_ratio = stroke_env_float("DESUB_STROKE_BLACK_CORE_MAX_BBOX_RATIO", 0.35)
    white_candidate = stroke_from_polarity(
        light,
        dark,
        args,
        area,
        polarity="light_core_dark_outline",
        require_outline_touch=os.environ.get("DESUB_STROKE_REQUIRE_DARK_TOUCH", "").strip().lower() in {"1", "true", "yes", "on"},
        max_core_bbox_ratio=white_max_ratio,
    )
    black_candidate = stroke_from_polarity(
        dark,
        light,
        args,
        area,
        polarity="dark_core_light_outline",
        require_outline_touch=True,
        max_core_bbox_ratio=black_max_ratio,
    )
    stroke, chosen_stats = choose_stroke_candidate([white_candidate, black_candidate], args)
    core_pixels = int(chosen_stats.get("core_pixels", 0))
    stroke_pixels = int(chosen_stats.get("stroke_pixels", 0))
    if stroke_pixels <= 0:
        return mask, {
            "box_pixels": area,
            "core_pixels": 0,
            "stroke_pixels": 0,
            "effective_pixels": 0,
            "fallback": 0,
            "empty_core": 1,
            "component_count": 0,
            "polarity": str(chosen_stats.get("polarity", "none")),
            "candidate_ratios": {
                "light_core_dark_outline": float(white_candidate[1].get("ratio", 0.0)),
                "dark_core_light_outline": float(black_candidate[1].get("ratio", 0.0)),
            },
            "ratio": 0.0,
        }
    ratio = stroke_pixels / area
    fallback = int(allow_fallback and stroke_pixels > 0 and ratio < float(args.stroke_fallback_min_ratio))
    if fallback:
        cv2.rectangle(mask, (x1, y1), (x2 - 1, y2 - 1), 255, thickness=-1)
    else:
        mask[y1:y2, x1:x2] = np.maximum(mask[y1:y2, x1:x2], stroke)
    effective_pixels = area if fallback else stroke_pixels
    return mask, {
        "box_pixels": area,
        "core_pixels": core_pixels,
        "stroke_pixels": stroke_pixels,
        "effective_pixels": effective_pixels,
        "fallback": fallback,
        "empty_core": 0,
        "component_count": int(chosen_stats.get("component_count", 0)),
        "component_pixels_total": int(chosen_stats.get("component_pixels_total", 0)),
        "max_component_pixels": int(chosen_stats.get("max_component_pixels", 0)),
        "polarity": str(chosen_stats.get("polarity", "")),
        "candidate_ratios": {
            "light_core_dark_outline": float(white_candidate[1].get("ratio", 0.0)),
            "dark_core_light_outline": float(black_candidate[1].get("ratio", 0.0)),
        },
        "rejected_large_components": int(chosen_stats.get("rejected_large_components", 0)),
        "rejected_no_outline_touch": int(chosen_stats.get("rejected_no_outline_touch", 0)),
        "ratio": ratio,
    }


def build_dynamic_stroke_mask(frame: np.ndarray, regions: list[dict], frame_seconds: float, args: argparse.Namespace) -> tuple[np.ndarray, dict]:
    height, width = frame.shape[:2]
    mask = np.zeros((height, width), dtype=np.uint8)
    stats: dict[str, float | int] = {
        "active_regions": 0,
        "fallback_regions": 0,
        "empty_core_regions": 0,
        "box_pixels": 0,
        "core_pixels": 0,
        "stroke_pixels": 0,
        "effective_pixels": 0,
        "component_count": 0,
        "max_component_pixels": 0,
        "light_core_dark_outline_regions": 0,
        "dark_core_light_outline_regions": 0,
    }
    for region in regions:
        start = region.get("start")
        end = region.get("end")
        if start is not None and frame_seconds < float(start):
            continue
        if end is not None and frame_seconds > float(end):
            continue
        rect = region.get("rect") or region
        rect_mask, rect_stats = stroke_mask_for_rect(frame, rect, args)
        mask = np.maximum(mask, rect_mask)
        stats["active_regions"] = int(stats["active_regions"]) + 1
        stats["fallback_regions"] = int(stats["fallback_regions"]) + int(rect_stats["fallback"])
        stats["empty_core_regions"] = int(stats["empty_core_regions"]) + int(rect_stats.get("empty_core", 0))
        stats["box_pixels"] = int(stats["box_pixels"]) + int(rect_stats["box_pixels"])
        stats["core_pixels"] = int(stats["core_pixels"]) + int(rect_stats.get("core_pixels", 0))
        stats["stroke_pixels"] = int(stats["stroke_pixels"]) + int(rect_stats["stroke_pixels"])
        stats["effective_pixels"] = int(stats["effective_pixels"]) + int(rect_stats.get("effective_pixels", 0))
        stats["component_count"] = int(stats["component_count"]) + int(rect_stats.get("component_count", 0))
        stats["max_component_pixels"] = max(int(stats["max_component_pixels"]), int(rect_stats.get("max_component_pixels", 0)))
        polarity = str(rect_stats.get("polarity", ""))
        if polarity == "light_core_dark_outline":
            stats["light_core_dark_outline_regions"] = int(stats["light_core_dark_outline_regions"]) + 1
        elif polarity == "dark_core_light_outline":
            stats["dark_core_light_outline_regions"] = int(stats["dark_core_light_outline_regions"]) + 1
    return mask, stats


def region_active(region: dict, frame_seconds: float) -> bool:
    start = region.get("start")
    end = region.get("end")
    if start is not None and frame_seconds < float(start):
        return False
    if end is not None and frame_seconds > float(end):
        return False
    return True


def region_mid_seconds(region: dict, duration: float) -> float:
    start = 0.0 if region.get("start") is None else float(region.get("start"))
    end = duration if region.get("end") is None else float(region.get("end"))
    return max(0.0, min(duration, (start + end) / 2.0))


def box_mask_for_rect(height: int, width: int, rect: list[int]) -> np.ndarray:
    mask = np.zeros((height, width), dtype=np.uint8)
    x1, y1, x2, y2 = [int(v) for v in rect]
    x1 = max(0, min(width, x1))
    x2 = max(0, min(width, x2))
    y1 = max(0, min(height, y1))
    y2 = max(0, min(height, y2))
    if x2 > x1 and y2 > y1:
        cv2.rectangle(mask, (x1, y1), (x2 - 1, y2 - 1), 255, thickness=-1)
    return mask


def active_static_masks(
    static_regions: list[dict],
    frame_seconds: float,
    height: int,
    width: int,
) -> tuple[np.ndarray, list[int], int]:
    mask = np.zeros((height, width), dtype=np.uint8)
    active_indexes: list[int] = []
    fallback_regions = 0
    for idx, region in enumerate(static_regions):
        if not region_active(region["region"], frame_seconds):
            continue
        active_indexes.append(idx)
        if int(region.get("fallback", 0)):
            fallback_regions += 1
        mask = np.maximum(mask, region["mask"])
    return mask, active_indexes, fallback_regions


def build_static_stroke_regions(
    input_path: str,
    regions: list[dict],
    width: int,
    height: int,
    fps: float,
    frame_count: int,
    args: argparse.Namespace,
) -> tuple[list[dict], dict]:
    static_regions: list[dict] = []
    for region in regions:
        rect = region.get("rect") or region
        static_regions.append({
            "region": region,
            "rect": rect,
            "mask": np.zeros((height, width), dtype=np.uint8),
            "hits": np.zeros((height, width), dtype=np.uint32),
            "box_pixels": max(1, rect_area(rect)),
            "active_frames": 0,
            "empty_core_regions": 0,
            "core_pixels": 0,
            "stroke_pixels": 0,
            "component_count": 0,
            "max_component_pixels": 0,
            "light_core_dark_outline_regions": 0,
            "dark_core_light_outline_regions": 0,
            "fallback": 0,
            "union_pixels": 0,
        })

    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        raise RuntimeError(f"failed to open input video for static mask prepass: {input_path}")
    count = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame_seconds = count / fps
        for item in static_regions:
            if not region_active(item["region"], frame_seconds):
                continue
            item["active_frames"] = int(item["active_frames"]) + 1
            rect_mask, rect_stats = stroke_mask_for_rect(frame, item["rect"], args, allow_fallback=False)
            item["hits"] += (rect_mask > 0).astype(np.uint32)
            item["empty_core_regions"] = int(item["empty_core_regions"]) + int(rect_stats.get("empty_core", 0))
            item["core_pixels"] = int(item["core_pixels"]) + int(rect_stats.get("core_pixels", 0))
            item["stroke_pixels"] = int(item["stroke_pixels"]) + int(rect_stats.get("stroke_pixels", 0))
            item["component_count"] = int(item["component_count"]) + int(rect_stats.get("component_count", 0))
            item["max_component_pixels"] = max(int(item["max_component_pixels"]), int(rect_stats.get("max_component_pixels", 0)))
            polarity = str(rect_stats.get("polarity", ""))
            if polarity == "light_core_dark_outline":
                item["light_core_dark_outline_regions"] = int(item["light_core_dark_outline_regions"]) + 1
            elif polarity == "dark_core_light_outline":
                item["dark_core_light_outline_regions"] = int(item["dark_core_light_outline_regions"]) + 1
        count += 1
    cap.release()

    fallback_static_regions = 0
    static_union_pixels = 0
    min_hits_floor = max(1, int(os.environ.get("DESUB_STATIC_MASK_MIN_HITS", "2")))
    min_hit_ratio = max(0.0, stroke_env_float("DESUB_STATIC_MASK_MIN_HIT_RATIO", 0.02))
    for item in static_regions:
        min_hits = max(min_hits_floor, int(np.ceil(max(1, int(item["active_frames"])) * min_hit_ratio)))
        item["min_hits"] = min_hits
        item["mask"] = np.where(item["hits"] >= min_hits, 255, 0).astype(np.uint8)
        union_pixels = int((item["mask"] > 0).sum())
        item["union_pixels"] = union_pixels
        ratio = union_pixels / max(1, int(item["box_pixels"]))
        item["union_area_ratio_vs_box"] = ratio
        if int(item["active_frames"]) > 0 and ratio < float(args.stroke_fallback_min_ratio):
            item["mask"] = box_mask_for_rect(height, width, item["rect"])
            item["fallback"] = 1
            fallback_static_regions += 1
        static_union_pixels += int((item["mask"] > 0).sum())

    total_box_pixels = sum(int(item["box_pixels"]) * max(1, int(item["active_frames"])) for item in static_regions)
    stats = {
        "static_mask_enabled": True,
        "static_region_count": len(static_regions),
        "prepass_frames": count,
        "input_frames": frame_count,
        "min_hits_floor": min_hits_floor,
        "min_hit_ratio": min_hit_ratio,
        "fallback_static_regions": fallback_static_regions,
        "static_union_pixels": static_union_pixels,
        "static_union_area_ratio_vs_box": static_union_pixels / max(1, sum(int(item["box_pixels"]) for item in static_regions)),
        "box_reference_pixels": max(1, total_box_pixels),
        "regions": [
            {
                "rect": item["rect"],
                "start": item["region"].get("start"),
                "end": item["region"].get("end"),
                "active_frames": item["active_frames"],
                "fallback": item["fallback"],
                "min_hits": item["min_hits"],
                "union_pixels": item["union_pixels"],
                "union_area_ratio_vs_box": item["union_area_ratio_vs_box"],
                "box_pixels": item["box_pixels"],
                "light_core_dark_outline_regions": item["light_core_dark_outline_regions"],
                "dark_core_light_outline_regions": item["dark_core_light_outline_regions"],
            }
            for item in static_regions
        ],
    }
    return static_regions, stats


def read_frame_at(input_path: str, frame_index: int) -> np.ndarray | None:
    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(frame_index)))
    ok, frame = cap.read()
    cap.release()
    return frame if ok else None


def build_lama_reference_patches(
    model: LamaInpaint,
    input_path: str,
    static_regions: list[dict],
    fps: float,
    duration: float,
    blend: float,
) -> dict[int, np.ndarray]:
    patches: dict[int, np.ndarray] = {}
    if blend <= 0:
        return patches
    for idx, item in enumerate(static_regions):
        mask = item["mask"]
        if not np.any(mask):
            continue
        mid_seconds = region_mid_seconds(item["region"], duration)
        frame = read_frame_at(input_path, int(round(mid_seconds * fps)))
        if frame is None:
            continue
        patches[idx] = model.inpaint(frame, mask)
    return patches


def apply_reference_blend(
    inpainted: np.ndarray,
    reference_patches: dict[int, np.ndarray],
    static_regions: list[dict],
    active_indexes: list[int],
    blend: float,
) -> np.ndarray:
    if blend <= 0 or not active_indexes:
        return inpainted
    blend = max(0.0, min(1.0, float(blend)))
    output = inpainted.astype(np.float32)
    for idx in active_indexes:
        reference = reference_patches.get(idx)
        if reference is None:
            continue
        region_mask = static_regions[idx]["mask"] > 0
        if not np.any(region_mask):
            continue
        output[region_mask] = output[region_mask] * (1.0 - blend) + reference.astype(np.float32)[region_mask] * blend
    return output


def feather_mask(mask: np.ndarray, feather_px: int) -> np.ndarray:
    if feather_px <= 0 or not np.any(mask):
        return mask
    kernel = max(3, int(feather_px) * 2 + 1)
    if kernel % 2 == 0:
        kernel += 1
    return cv2.GaussianBlur(mask, (kernel, kernel), 0)


def open_video(path: str) -> tuple[cv2.VideoCapture, int, int, float, int]:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError(f"failed to open input video: {path}")
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) + 0.5)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) + 0.5)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 24.0)
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) + 0.5)
    return cap, width, height, fps, frames


def open_writer(path: str, width: int, height: int, fps: float) -> cv2.VideoWriter:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError(f"failed to open output video: {path}")
    return writer


def open_mask_writer(path: str, width: int, height: int, fps: float) -> cv2.VideoWriter:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"FFV1"), fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError(f"failed to open lossless mask video: {path}")
    return writer


def write_frame(writer: cv2.VideoWriter, frame: np.ndarray) -> None:
    writer.write(np.clip(frame, 0, 255).astype(np.uint8))


def alpha_composite(base: np.ndarray, patch: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    alpha_f = (alpha.astype(np.float32) / 255.0)[:, :, None]
    return base.astype(np.float32) * (1.0 - alpha_f) + patch.astype(np.float32) * alpha_f


def run_sttn(cap: cv2.VideoCapture, writer: cv2.VideoWriter, mask: np.ndarray, model_path: str, device: torch.device, batch_frames: int) -> int:
    model = STTNDetInpaint(device, model_path)
    batch: list[np.ndarray] = []
    count = 0
    batch_frames = max(1, batch_frames)
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        batch.append(frame)
        if len(batch) >= batch_frames:
            for out_frame in model(batch, mask):
                write_frame(writer, out_frame)
                count += 1
            batch.clear()
    if batch:
        for out_frame in model(batch, mask):
            write_frame(writer, out_frame)
            count += 1
    return count


def run_lama(
    cap: cv2.VideoCapture,
    writer: cv2.VideoWriter,
    box_mask: np.ndarray,
    rect_payload: dict,
    model_path: str,
    device: torch.device,
    args: argparse.Namespace,
) -> tuple[int, dict]:
    model = LamaInpaint(device, model_path)
    mask_writer = None
    if args.mask_video:
        mask_writer = open_mask_writer(
            args.mask_video,
            int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) + 0.5),
            int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) + 0.5),
            float(cap.get(cv2.CAP_PROP_FPS) or 24.0),
        )
    regions = rect_payload.get("regions") or [{"rect": rect, "start": None, "end": None} for rect in rect_payload.get("rects", [])]
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 24.0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) + 0.5)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) + 0.5)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) + 0.5)
    duration = frame_count / max(0.1, fps)
    static_regions: list[dict] = []
    static_mask_stats: dict = {"static_mask_enabled": False}
    reference_patches: dict[int, np.ndarray] = {}
    temporal_blend = max(0.0, min(1.0, float(args.lama_temporal_blend)))
    if args.mask_kind == "stroke":
        static_regions, static_mask_stats = build_static_stroke_regions(
            args.input,
            regions,
            width,
            height,
            fps,
            frame_count,
            args,
        )
        reference_patches = build_lama_reference_patches(
            model,
            args.input,
            static_regions,
            fps,
            duration,
            temporal_blend,
        )
    count = 0
    fallback_frames = 0
    fallback_regions = 0
    empty_core_regions = 0
    active_frames = 0
    total_box_pixels = 0
    total_core_pixels = 0
    total_stroke_pixels = 0
    total_effective_pixels = 0
    total_component_count = 0
    max_component_pixels = 0
    light_core_dark_outline_regions = 0
    dark_core_light_outline_regions = 0
    empty_mask_frames = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if args.mask_kind == "stroke":
            frame_seconds = count / fps
            mask, active_indexes, active_fallback_regions = active_static_masks(static_regions, frame_seconds, height, width)
            fallback_regions += active_fallback_regions
            total_box_pixels += sum(int(static_regions[idx]["box_pixels"]) for idx in active_indexes)
            if active_indexes:
                active_frames += 1
            if active_fallback_regions > 0:
                fallback_frames += 1
            if not np.any(mask):
                empty_mask_frames += 1
                output = frame
            else:
                total_effective_pixels += int((mask > 0).sum())
                inpainted = model.inpaint(frame, mask)
                inpainted = apply_reference_blend(
                    inpainted,
                    reference_patches,
                    static_regions,
                    active_indexes,
                    temporal_blend,
                )
                alpha = feather_mask(mask, int(args.stroke_feather_px))
                output = alpha_composite(frame, inpainted, alpha)
        else:
            mask = box_mask
            if not np.any(mask):
                empty_mask_frames += 1
                output = frame
            else:
                total_box_pixels += int((mask > 0).sum())
                total_effective_pixels += int((mask > 0).sum())
                inpainted = model.inpaint(frame, mask)
                alpha = feather_mask(mask, int(args.stroke_feather_px))
                output = alpha_composite(frame, inpainted, alpha)
        if mask_writer is not None:
            alpha = feather_mask(mask, int(args.stroke_feather_px))
            mask_writer.write(cv2.cvtColor(alpha, cv2.COLOR_GRAY2BGR))
        write_frame(writer, output)
        count += 1
    if mask_writer is not None:
        mask_writer.release()
    if args.mask_kind == "stroke":
        empty_core_regions = sum(int(item["empty_core_regions"]) for item in static_regions)
        total_core_pixels = sum(int(item["core_pixels"]) for item in static_regions)
        total_stroke_pixels = sum(int(item["stroke_pixels"]) for item in static_regions)
        total_component_count = sum(int(item["component_count"]) for item in static_regions)
        max_component_pixels = max((int(item["max_component_pixels"]) for item in static_regions), default=0)
        light_core_dark_outline_regions = sum(int(item["light_core_dark_outline_regions"]) for item in static_regions)
        dark_core_light_outline_regions = sum(int(item["dark_core_light_outline_regions"]) for item in static_regions)
    total_box_pixels = max(1, total_box_pixels)
    stats = {
        "mask_kind": args.mask_kind,
        "mask_time_strategy": "static_union_per_span" if args.mask_kind == "stroke" else "static_box",
        "static_mask": static_mask_stats,
        "lama_temporal_blend": temporal_blend,
        "lama_reference_patch_count": len(reference_patches),
        "processed_frames": count,
        "active_frames": active_frames,
        "empty_mask_frames": empty_mask_frames,
        "fallback_frames": fallback_frames,
        "fallback_regions": fallback_regions,
        "empty_core_regions": empty_core_regions,
        "fallback_frame_ratio": fallback_frames / max(1, count),
        "fallback_active_frame_ratio": fallback_frames / max(1, active_frames),
        "core_pixels": total_core_pixels,
        "stroke_candidate_pixels": total_stroke_pixels,
        "effective_mask_pixels": total_effective_pixels,
        "box_reference_pixels": total_box_pixels,
        "component_count": total_component_count,
        "component_count_per_active_frame": total_component_count / max(1, active_frames),
        "max_component_pixels": max_component_pixels,
        "light_core_dark_outline_regions": light_core_dark_outline_regions,
        "dark_core_light_outline_regions": dark_core_light_outline_regions,
        "core_area_ratio_vs_box": total_core_pixels / total_box_pixels,
        "candidate_area_ratio_vs_box": total_stroke_pixels / total_box_pixels,
        "effective_area_ratio_vs_box": total_effective_pixels / total_box_pixels,
        "effective_area_reduction_vs_box": 1.0 - (total_effective_pixels / total_box_pixels),
        "thresholds": {
            "white": args.stroke_white_threshold,
            "black": args.stroke_black_threshold,
            "touch_radius_px": args.stroke_touch_radius,
            "close_px": args.stroke_close_px,
            "dilate_px": args.stroke_dilate_px,
            "feather_px": args.stroke_feather_px,
            "fallback_min_ratio": args.stroke_fallback_min_ratio,
            "target_ratio": stroke_env_float("DESUB_STROKE_TARGET_RATIO", 0.24),
            "max_ratio": stroke_env_float("DESUB_STROKE_MAX_RATIO", 0.34),
            "white_core_max_bbox_ratio": stroke_env_float("DESUB_STROKE_WHITE_CORE_MAX_BBOX_RATIO", 0.75),
            "black_core_max_bbox_ratio": stroke_env_float("DESUB_STROKE_BLACK_CORE_MAX_BBOX_RATIO", 0.35),
        },
    }
    return count, stats


def main() -> int:
    args = parse_args()
    rect_payload = json.loads(Path(args.rects_json).read_text(encoding="utf-8"))
    rects = rect_payload.get("rects") or []
    cap, width, height, fps, frame_count = open_video(args.input)
    writer = open_writer(args.output, width, height, fps)
    mask = build_mask(height, width, rects, args.mask_pad)
    cuda_required = os.environ.get("DESUB_REQUIRE_CUDA", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    cuda_available = torch.cuda.is_available()
    if cuda_required and not cuda_available:
        raise RuntimeError("CUDA is required; child inpaint CPU fallback is forbidden")
    if cuda_available:
        device = torch.device("cuda")
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        device = torch.device("mps")
    else:
        device = torch.device("cpu")
    mask_stats = {
        "mask_kind": args.mask_kind,
        "processed_frames": 0,
        "fallback_frames": 0,
        "fallback_frame_ratio": 0.0,
    }
    if not np.any(mask):
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            write_frame(writer, frame)
        processed = frame_count
    else:
        model_config = ModelConfig()
        if args.mode == "sttn-det":
            processed = run_sttn(
                cap,
                writer,
                mask,
                model_config.STTN_DET_MODEL_PATH,
                device,
                args.batch_frames,
            )
        else:
            processed, mask_stats = run_lama(
                cap,
                writer,
                mask,
                rect_payload,
                os.path.join(model_config.LAMA_MODEL_DIR, "big-lama.pt"),
                device,
                args,
            )
    cap.release()
    writer.release()
    mask_stats["device_type"] = str(device.type)
    mask_stats["cuda_required"] = cuda_required
    if args.mask_stats_json:
        Path(args.mask_stats_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.mask_stats_json).write_text(
            json.dumps(mask_stats, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    print(json.dumps({
        "mode": args.mode,
        "processed_frames": processed,
        "input_frames": frame_count,
        "width": width,
        "height": height,
        "fps": fps,
        "rect_count": len(rects),
        "mask_pixels": int((mask > 0).sum()),
        "mask_stats": mask_stats,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''.lstrip(),
        encoding="utf-8",
    )
def run_vsr_model(
    *,
    model_name: str,
    input_clip: Path,
    output_clip: Path,
    rects: list[tuple[int, int, int, int]],
    bounds: tuple[int, int],
    work_dir: Path,
    log_name: str = "vsr",
    overlay_enable: tuple[float, float] | None = None,
    overlay_regions: list[OverlayRegion] | None = None,
) -> dict[str, Any]:
    y1, y2 = bounds
    model_dir = work_dir / model_name
    crop_in = model_dir / "crop_input.mp4"
    crop_out = model_dir / "crop_output.mp4"
    crop_video(input_clip, crop_in, y1, y2)

    crop_meta = ffprobe(crop_in)
    relative_rects = []
    relative_regions: list[dict[str, Any]] = []
    crop_height = y2 - y1
    for x1, ry1, x2, ry2 in rects:
        clipped = clamp_box((x1, ry1 - y1, x2, ry2 - y1), width=crop_meta.width, height=crop_height)
        if clipped:
            relative_rects.append(clipped)
    if not relative_rects:
        relative_rects.append((0, 0, crop_meta.width, crop_height))
    if overlay_regions:
        for region in overlay_regions:
            x1, ry1, x2, ry2 = region.rect
            clipped = clamp_box((x1, ry1 - y1, x2, ry2 - y1), width=crop_meta.width, height=crop_height)
            if clipped:
                relative_regions.append({
                    "rect": list(clipped),
                    "start": region.enable_start,
                    "end": region.enable_end,
                })
    else:
        relative_regions = [
            {"rect": list(rect), "start": None, "end": None}
            for rect in relative_rects
        ]

    stroke_mask_enabled = model_name == "lama" and env_bool("DESUB_STROKE_MASK_ENABLED", True)
    mask_kind = "stroke" if stroke_mask_enabled else "box"
    mode = "sttn-det-custom-mask" if model_name == "sttn" else f"lama-{mask_kind}-mask"
    rects_json = model_dir / "rects.json"
    write_json(rects_json, {
        "rects": [list(rect) for rect in relative_rects],
        "regions": relative_regions,
    })
    runner = model_dir / "mask_inpaint_runner.py"
    write_mask_inpaint_runner(runner)
    helper_mode = "sttn-det" if model_name == "sttn" else "lama-mask"
    alpha_mask = model_dir / "alpha_mask.mkv" if model_name == "lama" else None
    mask_stats_json = model_dir / "mask_stats.json"
    vsr_cmd = [
        "python",
        str(runner),
        "--input",
        str(crop_in),
        "--output",
        str(crop_out),
        "--mode",
        helper_mode,
        "--rects-json",
        str(rects_json),
        "--batch-frames",
        str(env_int("DESUB_STTN_BATCH_FRAMES", 48)),
        "--mask-pad",
        str(env_int("DESUB_INPAINT_MASK_EXTRA_PAD_PX", 0)),
        "--mask-kind",
        mask_kind,
        "--mask-stats-json",
        str(mask_stats_json),
        "--stroke-white-threshold",
        str(env_int("DESUB_STROKE_WHITE_THRESHOLD", 200)),
        "--stroke-black-threshold",
        str(env_int("DESUB_STROKE_BLACK_THRESHOLD", 60)),
        "--stroke-touch-radius",
        str(env_int("DESUB_STROKE_TOUCH_RADIUS_PX", 4)),
        "--stroke-close-px",
        str(env_int("DESUB_STROKE_CLOSE_PX", 3)),
        "--stroke-dilate-px",
        str(env_int("DESUB_STROKE_DILATE_PX", 3)),
        "--stroke-feather-px",
        str(env_int("DESUB_STROKE_FEATHER_PX", 2)),
        "--stroke-fallback-min-ratio",
        str(env_float("DESUB_STROKE_FALLBACK_MIN_RATIO", 0.15)),
        "--lama-temporal-blend",
        str(env_float("DESUB_LAMA_TEMPORAL_BLEND", 0.5)),
    ]
    if alpha_mask is not None:
        vsr_cmd.extend(["--mask-video", str(alpha_mask)])

    started = time.time()
    with VramMonitor() as monitor:
        vsr_log = model_dir / f"{log_name}.log"
        cmd_logged(vsr_cmd, timeout=7200, cwd=Path("/opt/video-subtitle-remover"), log_path=vsr_log)
    elapsed = time.time() - started
    mask_stats: dict[str, Any] = {}
    if mask_stats_json.exists():
        try:
            mask_stats = json.loads(mask_stats_json.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            mask_stats = {"ok": False, "error": str(exc)}
    overlay_crop(
        input_clip,
        crop_out,
        output_clip,
        y1,
        rects=rects,
        enable_between=overlay_enable,
        overlay_regions=overlay_regions,
        alpha_mask=alpha_mask if alpha_mask is not None and alpha_mask.exists() else None,
    )
    return {
        "model": model_name,
        "vsr_mode": mode,
        "seconds": elapsed,
        "vram_peak_mb": monitor.max_mb,
        "crop_bounds": {"y1": y1, "y2": y2},
        "mask_rect_count": len(relative_rects),
        "mask_kind": mask_kind,
        "mask_stats": mask_stats,
        "alpha_mask": str(alpha_mask) if alpha_mask is not None else None,
        "vsr_log": str(vsr_log),
        "overlay_enable": (
            {"start": overlay_enable[0], "end": overlay_enable[1]}
            if overlay_enable is not None else None
        ),
        "overlay_regions": (
            [
                {
                    "rect": {"x1": r.rect[0], "y1": r.rect[1], "x2": r.rect[2], "y2": r.rect[3]},
                    "start": r.enable_start,
                    "end": r.enable_end,
                }
                for r in overlay_regions
            ]
            if overlay_regions is not None else None
        ),
    }


def run_vsr_for_segment(
    *,
    model_name: str,
    source_clip: Path,
    clusters: tuple[MaskCluster, ...],
    meta: VideoMeta,
    work_dir: Path,
    index: int,
    segment_start: float,
    segment_end: float,
) -> tuple[Path, dict[str, Any]]:
    if not clusters:
        raise ValueError("run_vsr_for_segment requires at least one cluster")
    kind = clusters[0].kind
    label_start = min(cluster.t_start for cluster in clusters)
    label_end = max(cluster.t_end for cluster in clusters)
    cluster_dir = work_dir / f"{kind}_{index:03d}_{label_start:.2f}_{label_end:.2f}"
    segment_clip = cluster_dir / "segment_input.mp4"
    segment_out = cluster_dir / "segment_output.mp4"
    start = segment_start
    end = segment_end
    make_clip(source_clip, segment_clip, start, end - start, crf=output_crf())
    vsr_rects = dedupe_rects(
        (rect for cluster in clusters for rect in cluster.rects),
        iou_threshold=0.65,
    )
    overlay_regions = [
        OverlayRegion(
            rect=rect,
            enable_start=max(0.0, cluster.t_start - start),
            enable_end=max(0.0, cluster.t_end - start),
        )
        for cluster in clusters
        for rect in cluster.rects
    ]
    result = run_vsr_model(
        model_name=model_name,
        input_clip=segment_clip,
        output_clip=segment_out,
        rects=vsr_rects,
        bounds=crop_bounds(vsr_rects, meta),
        work_dir=cluster_dir,
        log_name="vsr",
        overlay_regions=overlay_regions,
    )
    result["clusters"] = [cluster_dict(cluster) for cluster in clusters]
    result["segment_start"] = start
    result["segment_end"] = end
    return segment_out, result


def apply_timed_clusters(
    *,
    model_name: str,
    source_clip: Path,
    output_clip: Path,
    clusters: list[MaskCluster],
    meta: VideoMeta,
    work_dir: Path,
) -> dict[str, Any]:
    started = time.time()
    if not clusters:
        shutil.copy2(source_clip, output_clip)
        return {
            "seconds": 0.0,
            "vram_peak_mb": None,
            "cluster_count": 0,
            "cluster_results": [],
            "concat_segments": 1,
        }

    overlays: list[tuple[Path, float, float]] = []
    cluster_results: list[dict[str, Any]] = []
    aligned_clusters, keyframe_info = align_clusters_to_keyframes(clusters, source_clip, meta)
    cursor = 0.0
    for idx, segment_cluster in enumerate(aligned_clusters, start=1):
        segment_start = max(cursor, segment_cluster.segment_start)
        segment_end = segment_cluster.segment_end
        if segment_end <= segment_start + 0.02:
            continue
        write_segment, result = run_vsr_for_segment(
            model_name=model_name,
            source_clip=source_clip,
            clusters=segment_cluster.clusters,
            meta=meta,
            work_dir=work_dir,
            index=idx,
            segment_start=segment_start,
            segment_end=segment_end,
        )
        overlays.append((write_segment, segment_start, segment_end))
        cluster_results.append(result)
        cursor = max(cursor, segment_end)

    compose_overlay_segments(source_clip, overlays, output_clip)
    vram_values = [result.get("vram_peak_mb") for result in cluster_results if result.get("vram_peak_mb") is not None]
    return {
        "seconds": time.time() - started,
        "vsr_seconds_total": sum(float(result.get("seconds") or 0) for result in cluster_results),
        "vram_peak_mb": max(vram_values) if vram_values else None,
        "cluster_count": len(clusters),
        "segment_cluster_count": len(aligned_clusters),
        "cluster_results": cluster_results,
        "concat_segments": 0,
        "overlay_segments": len(overlays),
        "composition_mode": "full_length_timed_overlay",
        "keyframe_alignment": keyframe_info,
    }


def run_model_pipeline(
    *,
    model_name: str,
    input_clip: Path,
    output_clip: Path,
    subtitle_clusters: list[MaskCluster],
    watermark_cluster: MaskCluster | None,
    meta: VideoMeta,
    work_dir: Path,
) -> dict[str, Any]:
    started = time.time()
    current = input_clip
    watermark_result: dict[str, Any] | None = None
    if watermark_cluster is not None:
        watermark_out = work_dir / model_name / "watermark_output.mp4"
        watermark_result = run_vsr_model(
            model_name=model_name,
            input_clip=current,
            output_clip=watermark_out,
            rects=list(watermark_cluster.rects),
            bounds=crop_bounds(list(watermark_cluster.rects), meta),
            work_dir=work_dir / model_name / "watermark",
            log_name="watermark",
        )
        watermark_result["cluster"] = cluster_dict(watermark_cluster)
        current = watermark_out

    timed_result = apply_timed_clusters(
        model_name=model_name,
        source_clip=current,
        output_clip=output_clip,
        clusters=subtitle_clusters,
        meta=meta,
        work_dir=work_dir / model_name / "subtitle",
    )
    vram_values = [
        value for value in [
            watermark_result.get("vram_peak_mb") if watermark_result else None,
            timed_result.get("vram_peak_mb"),
        ] if value is not None
    ]
    return {
        "model": model_name,
        "vsr_mode": "sttn-det-custom-mask" if model_name == "sttn" else "lama-custom-mask",
        "seconds": time.time() - started,
        "vram_peak_mb": max(vram_values) if vram_values else None,
        "watermark": watermark_result,
        "subtitle": timed_result,
        "subtitle_cluster_count": len(subtitle_clusters),
        "watermark_enabled": watermark_cluster is not None,
        "audio_policy": "source audio stream copied into final output; video track composed lossless to avoid concat timestamp drift",
    }


def package_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for package in [
        "torch",
        "torchvision",
        "easyocr",
        "paddleocr",
        "paddlepaddle",
        "cv2",
        "google.cloud.storage",
    ]:
        try:
            if package == "cv2":
                import cv2

                versions[package] = cv2.__version__
            elif package == "paddlepaddle":
                import paddle

                versions[package] = paddle.__version__
            elif package == "google.cloud.storage":
                import google.cloud.storage

                versions[package] = google.cloud.storage.__version__
            else:
                module = __import__(package)
                versions[package] = str(getattr(module, "__version__", "unknown"))
        except Exception as exc:  # noqa: BLE001
            versions[package] = f"unavailable: {exc}"
    try:
        import torch

        versions["torch_cuda_available"] = str(torch.cuda.is_available())
        if torch.cuda.is_available():
            versions["torch_cuda_device"] = torch.cuda.get_device_name(0)
    except Exception as exc:  # noqa: BLE001
        versions["torch_cuda_available"] = f"unavailable: {exc}"
    return versions


def patch_vsr_tree(root: Path) -> None:
    stub = "\n".join([
        "class ConfigItem:",
        "    def __init__(self, *args, **kwargs):",
        "        self.value = args[2] if len(args) >= 3 else None",
        "",
        "class OptionsConfigItem(ConfigItem):",
        "    pass",
        "",
        "class OptionsValidator:",
        "    def __init__(self, *args, **kwargs):",
        "        pass",
        "",
        "class BoolValidator:",
        "    def __init__(self, *args, **kwargs):",
        "        pass",
        "",
        "class RangeConfigItem(ConfigItem):",
        "    pass",
        "",
        "class RangeValidator:",
        "    def __init__(self, *args, **kwargs):",
        "        pass",
        "",
        "class ConfigValidator:",
        "    def __init__(self, *args, **kwargs):",
        "        pass",
        "",
        "class EnumSerializer:",
        "    def __init__(self, *args, **kwargs):",
        "        pass",
        "",
        "class QConfig:",
        "    def set(self, item, value):",
        "        item.value = value",
        "",
        "class _QConfigLoader:",
        "    def load(self, *args, **kwargs):",
        "        return None",
        "",
        "qconfig = _QConfigLoader()",
        "",
    ])
    (root / "qfluentwidgets.py").write_text(stub, encoding="utf-8")
    shim_dir = root / "fsplit"
    shim_dir.mkdir(exist_ok=True)
    (shim_dir / "__init__.py").write_text("", encoding="utf-8")
    (shim_dir / "filesplit.py").write_text(
        "\n".join([
            "from pathlib import Path",
            "",
            "class Filesplit:",
            "    def merge(self, input_dir=None, output_file=None, *args, **kwargs):",
            "        source = Path(input_dir or kwargs.get('inputdir') or kwargs.get('input_dir') or args[0])",
            "        names = []",
            "        manifest = source / 'fs_manifest.csv'",
            "        if manifest.exists():",
            "            for line in manifest.read_text(encoding='utf-8').splitlines():",
            "                if line.startswith('filename') or not line.strip():",
            "                    continue",
            "                names.append(line.strip().split(',')[0])",
            "        manifest = source / 'manifest'",
            "        if not names and manifest.exists():",
            "            names = [line.strip().split(',')[0] for line in manifest.read_text(encoding='utf-8').splitlines() if line.strip()]",
            "        if output_file is None and len(args) < 3 and not kwargs.get('outputdir') and not kwargs.get('output_dir'):",
            "            first = names[0] if names else sorted(p.name for p in source.iterdir() if p.is_file())[0]",
            "            stem, dot, ext = first.rpartition('.')",
            "            if '_' in stem and stem.rsplit('_', 1)[1].isdigit():",
            "                stem = stem.rsplit('_', 1)[0]",
            "            target = source / (stem + (dot + ext if dot else ''))",
            "        elif output_file is None:",
            "            output_dir = kwargs.get('outputdir') or kwargs.get('output_dir') or args[1]",
            "            output_name = kwargs.get('outputfilename') or kwargs.get('output_filename') or args[2]",
            "            target = Path(output_dir) / output_name",
            "        else:",
            "            target = Path(output_file)",
            "        if names:",
            "            parts = [source / name for name in names if (source / name).is_file()]",
            "        else:",
            "            parts = [p for p in sorted(source.iterdir()) if p.is_file() and p.name != target.name and not p.name.startswith('manifest') and p.name != 'fs_manifest.csv']",
            "        target.parent.mkdir(parents=True, exist_ok=True)",
            "        with target.open('wb') as out:",
            "            for part in parts:",
            "                with part.open('rb') as src:",
            "                    while True:",
            "                        chunk = src.read(1024 * 1024)",
            "                        if not chunk:",
            "                            break",
            "                        out.write(chunk)",
            "        return str(target)",
            "",
        ]),
        encoding="utf-8",
    )

    main_path = root / "backend" / "main.py"
    text = main_path.read_text(encoding="utf-8")
    text = text.replace("from backend.inpaint.propainter_inpaint import PropainterInpaint", "PropainterInpaint = None")
    text = text.replace(
        "PropainterInpaint(device, self.model_config.PROPAINTER_MODEL_DIR, config.propainterMaxLoadNum.value)",
        "(_ for _ in ()).throw(RuntimeError(\"ProPainter is disabled in DESUB\"))",
    )
    main_path.write_text(text, encoding="utf-8")

    model_config = root / "backend" / "tools" / "model_config.py"
    text = model_config.read_text(encoding="utf-8")
    text = text.replace(
        "merge_big_file_if_not_exists(self.LAMA_MODEL_DIR, 'bit-lama.pt')",
        "merge_big_file_if_not_exists(self.LAMA_MODEL_DIR, 'big-lama.pt')",
    )
    text = text.replace(
        "merge_big_file_if_not_exists(self.PROPAINTER_MODEL_DIR, 'ProPainter.pth')",
        "# ProPainter disabled for DESUB license policy",
    )
    model_config.write_text(text, encoding="utf-8")

    for path in (root / "backend").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        text = text.replace(
            "torch.load(model_path, map_location='cpu')",
            "torch.load(model_path, map_location='cpu', weights_only=True)",
        )
        text = text.replace(
            'torch.load(model_path, map_location="cpu")',
            'torch.load(model_path, map_location="cpu", weights_only=True)',
        )
        path.write_text(text, encoding="utf-8")


def bake_model_weights(root: Path) -> None:
    sys.path.insert(0, str(root))
    from backend.tools.model_config import ModelConfig

    model_config = ModelConfig()
    required = [
        Path(model_config.STTN_AUTO_MODEL_PATH),
        Path(model_config.STTN_DET_MODEL_PATH),
        Path(model_config.LAMA_MODEL_DIR) / "big-lama.pt",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit("Missing VSR model weights: " + ", ".join(missing))

    import easyocr

    easyocr.Reader(["ch_sim", "en"], gpu=False, model_storage_directory="/models/easyocr", verbose=False)

    if env_bool("DESUB_COMPARE_PADDLE", False):
        try:
            from paddleocr import TextDetection

            TextDetection()
        except Exception:  # noqa: BLE001
            from paddleocr import PaddleOCR

            try:
                PaddleOCR(lang="ch", use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False)
            except TypeError:
                PaddleOCR(lang="ch", use_angle_cls=False, show_log=False)


def detector_comparison(
    frames: list[tuple[int, float, Any]],
    meta: VideoMeta,
    crop_y: int,
) -> tuple[list[Detection], dict[str, Any]]:
    summary: dict[str, Any] = {}
    all_detections: list[Detection] = []
    # Per-detector time budget: a slow detector must not eat the Cloud Run task
    # timeout (SIGTERM loses status.json); it gets marked timed_out instead.
    budget = env_float("DESUB_DETECTOR_BUDGET_SECONDS", 600.0)
    detectors: list[tuple[str, Any]] = [("easyocr_craft", detect_easyocr)]
    if env_bool("DESUB_COMPARE_PADDLE", False):
        detectors.append(("paddle_det", detect_paddle))
    else:
        summary["paddle_det"] = {"ok": True, "skipped": True, "reason": "DESUB_COMPARE_PADDLE=false"}
    for name, detector in detectors:
        started = time.time()
        try:
            detections, info = detector(
                frames,
                meta,
                crop_y,
                deadline=started + budget if budget > 0 else None,
            )
            info["seconds"] = time.time() - started
            summary[name] = info
            all_detections.extend(detections)
            log("detector_completed", detector=name, count=len(detections), seconds=info["seconds"])
        except Exception as exc:  # noqa: BLE001
            summary[name] = {"ok": False, "error": str(exc), "seconds": time.time() - started}
            log("detector_failed", detector=name, error=str(exc))
    return all_detections, summary


def has_cjk(text: str) -> bool:
    return any("\u3400" <= ch <= "\u9fff" for ch in text or "")


def box_center_in_sub_band(box: tuple[int, int, int, int], meta: VideoMeta) -> bool:
    sub_y_min, sub_y_max = env_ratio_pair("DESUB_SUB_BAND_Y", (0.55, 0.90))
    sub_x_min, sub_x_max = env_ratio_pair("DESUB_SUB_BAND_X", (0.15, 0.85))
    x1, y1, x2, y2 = box
    center_x = ((x1 + x2) / 2.0) / max(1, meta.width)
    center_y = ((y1 + y2) / 2.0) / max(1, meta.height)
    return sub_x_min <= center_x <= sub_x_max and sub_y_min <= center_y <= sub_y_max


def sub_band_geometry_reason(det: Detection, meta: VideoMeta) -> str:
    width = det.x2 - det.x1
    height = det.y2 - det.y1
    if not box_center_in_sub_band(det.box, meta):
        return "center_out_sub_band"
    scale = meta.height / 1024.0
    min_height = env_float("DESUB_SUB_BOX_MIN_HEIGHT_PX", 25.0) * scale
    max_height = env_float("DESUB_SUB_BOX_MAX_HEIGHT_PX", 100.0) * scale
    if height < min_height:
        return "height_lt_sub_min"
    if height > max_height:
        return "height_gt_sub_max"
    if width / max(1.0, float(height)) < env_float("DESUB_SUB_BOX_MIN_ASPECT", 1.2):
        return "aspect_lt_sub_min"
    return ""


def sub_band_detection_priority(det: Detection) -> float:
    priority = float(det.score or 0.0)
    if has_cjk(det.text):
        priority += 1.0
    if det.score >= env_float("DESUB_EASYOCR_MASK_MIN_SCORE", 0.3):
        priority += 0.35
    priority += min(0.25, box_area(det.box) / 50000.0)
    return priority


def persistent_sub_band_detections(
    detections: list[Detection],
    *,
    meta: VideoMeta,
    sample_frame_ids: list[int],
) -> tuple[list[Detection], dict[str, Any]]:
    iou_threshold = env_float("DESUB_SUB_PERSIST_IOU", 0.5)
    min_samples = env_int("DESUB_SUB_PERSIST_MIN_SAMPLES", 3)
    max_sample_gap = env_int("DESUB_SUB_PERSIST_MAX_SAMPLE_GAP", 1)
    sample_order = {frame_id: idx for idx, frame_id in enumerate(sample_frame_ids)}
    tracks: list[dict[str, Any]] = []

    candidates = sorted(
        detections,
        key=lambda det: (
            sample_order.get(det.frame, det.frame),
            -sub_band_detection_priority(det),
            det.y1,
            det.x1,
        ),
    )
    for det in candidates:
        order = sample_order.get(det.frame)
        if order is None:
            continue
        best_track: dict[str, Any] | None = None
        best_iou = 0.0
        for track in tracks:
            gap = order - int(track["last_order"])
            if gap <= 0 or gap > max_sample_gap:
                continue
            score = box_iou(det.box, track["last_box"])
            if score >= iou_threshold and score > best_iou:
                best_iou = score
                best_track = track
        if best_track is None:
            tracks.append({
                "detections": [det],
                "last_order": order,
                "last_box": det.box,
            })
            continue
        best_track["detections"].append(det)
        best_track["last_order"] = order
        best_track["last_box"] = det.box

    kept_by_key: dict[tuple[str, int, int, int, int, int, str], Detection] = {}
    valid_tracks = 0
    rejected_tracks = 0
    for track in tracks:
        items: list[Detection] = track["detections"]
        unique_frames = {det.frame for det in items}
        if len(unique_frames) < min_samples:
            rejected_tracks += 1
            continue
        valid_tracks += 1
        for det in items:
            kept_by_key[(det.detector, det.frame, det.x1, det.y1, det.x2, det.y2, det.text)] = det
    kept = list(kept_by_key.values())
    return kept, {
        "candidate_count": len(detections),
        "kept_count": len(kept),
        "track_count": len(tracks),
        "valid_track_count": valid_tracks,
        "rejected_track_count": rejected_tracks,
        "min_samples": min_samples,
        "iou_threshold": iou_threshold,
        "max_sample_gap": max_sample_gap,
    }


def dedupe_with_priority(detections: list[Detection]) -> list[Detection]:
    grouped: dict[int, list[Detection]] = {}
    for det in detections:
        grouped.setdefault(det.frame, []).append(det)
    result: list[Detection] = []
    for frame in sorted(grouped):
        selected: list[Detection] = []
        for det in sorted(grouped[frame], key=sub_band_detection_priority, reverse=True):
            if all(box_iou(det.box, existing.box) < 0.55 for existing in selected):
                selected.append(det)
        result.extend(selected)
    return result


def filter_detections_for_masks(
    detections: list[Detection],
    meta: VideoMeta,
    *,
    sample_frame_ids: list[int],
) -> tuple[list[Detection], dict[str, Any]]:
    min_easy_score = env_float("DESUB_EASYOCR_MASK_MIN_SCORE", 0.3)
    min_paddle_iou = env_float("DESUB_PADDLE_CONFIRM_IOU", 0.5)
    sub_candidates: list[Detection] = []
    kept_outside_easy: list[Detection] = []
    kept_outside_paddle: list[Detection] = []
    rejected: list[dict[str, Any]] = []

    for det in detections:
        if box_center_in_sub_band(det.box, meta):
            reason = sub_band_geometry_reason(det, meta)
            if reason:
                rejected.append({"reason": reason, "detection": asdict(det)})
            else:
                sub_candidates.append(det)
        elif det.detector == "easyocr_craft":
            reason = ""
            if det.score < min_easy_score:
                reason = "outside_easyocr_score_lt_threshold"
            elif not has_cjk(det.text):
                reason = "outside_easyocr_no_cjk"
            if reason:
                rejected.append({"reason": reason, "detection": asdict(det)})
            else:
                kept_outside_easy.append(det)
        else:
            # Outside the subtitle band, Paddle boxes are only considered after
            # they overlap a CJK EasyOCR box on the same sampled frame.
            pass

    easy_by_frame: dict[int, list[Detection]] = {}
    for det in kept_outside_easy:
        easy_by_frame.setdefault(det.frame, []).append(det)

    for det in detections:
        if det.detector != "paddle_det":
            continue
        if box_center_in_sub_band(det.box, meta):
            continue
        best_iou = max((box_iou(det.box, easy.box) for easy in easy_by_frame.get(det.frame, [])), default=0.0)
        if best_iou >= min_paddle_iou:
            kept_outside_paddle.append(det)
        else:
            rejected.append({"reason": "outside_paddle_without_easyocr_iou", "best_iou": best_iou, "detection": asdict(det)})

    persistent_sub, persistence_summary = persistent_sub_band_detections(
        sub_candidates,
        meta=meta,
        sample_frame_ids=sample_frame_ids,
    )
    persistent_keys = {
        (det.detector, det.frame, det.x1, det.y1, det.x2, det.y2, det.text)
        for det in persistent_sub
    }
    for det in sub_candidates:
        key = (det.detector, det.frame, det.x1, det.y1, det.x2, det.y2, det.text)
        if key not in persistent_keys:
            rejected.append({"reason": "sub_band_not_persistent", "detection": asdict(det)})

    kept = dedupe_with_priority([*persistent_sub, *kept_outside_easy, *kept_outside_paddle])
    kept = sorted(kept, key=lambda item: (item.frame, item.y1, item.x1, item.detector))
    return kept, {
        "input_count": len(detections),
        "kept_count": len(kept),
        "sub_band_candidate_count": len(sub_candidates),
        "sub_band_kept": len(persistent_sub),
        "outside_easyocr_kept": len(kept_outside_easy),
        "outside_paddle_kept": len(kept_outside_paddle),
        "easyocr_kept": sum(1 for det in kept if det.detector == "easyocr_craft"),
        "paddle_kept": sum(1 for det in kept if det.detector == "paddle_det"),
        "rejected_count": len(rejected),
        "strategy": "sub_band_geometry_persistence; outside_band_cjk_score",
        "min_easy_score": min_easy_score,
        "min_paddle_iou": min_paddle_iou,
        "sub_band_persistence": persistence_summary,
        "sub_band_cjk_evidence_count": sum(1 for det in sub_candidates if has_cjk(det.text)),
        "sub_band_score_evidence_count": sum(1 for det in sub_candidates if det.score >= min_easy_score),
        "rejected": rejected,
    }


def frame_times_for_qa(spans: list[Span], duration: float) -> list[float]:
    if spans:
        times = [min(duration - 0.1, max(0.0, (span.t_start + span.t_end) / 2.0)) for span in spans[:4]]
    else:
        times = [min(duration - 0.1, value) for value in (0.5, 5.0, 10.0, 20.0)]
    deduped: list[float] = []
    for value in times:
        if value >= 0 and all(abs(value - existing) > 0.5 for existing in deduped):
            deduped.append(value)
    return deduped[:4] or [0.0]


def fixed_frame_times_for_qa(duration: float) -> list[float]:
    last = max(0.0, duration - 0.1)
    values: list[float] = []
    current = 5.0
    while current <= last + 0.001:
        values.append(current)
        current += 5.0
    return values


def detection_center_in_sub_band(det: Detection, meta: VideoMeta) -> bool:
    return box_center_in_sub_band(det.box, meta)


def qa_avoid_spans(raw_detections: list[Detection], meta: VideoMeta) -> list[Span]:
    candidates: list[Detection] = []
    for det in raw_detections:
        if sub_band_geometry_reason(det, meta):
            continue
        candidates.append(det)
    return build_spans(candidates, meta, dilate_px=0)


def frame_times_without_clusters(
    clusters: list[MaskCluster],
    duration: float,
    *,
    avoid_spans: list[Span] | None = None,
) -> list[float]:
    candidates = [
        0.12,
        0.5,
        1.0,
        3.0,
        8.0,
        15.0,
        25.0,
        35.0,
        66.0,
        75.0,
        max(0.0, duration - 0.5),
    ]
    result: list[float] = []
    for value in candidates:
        seconds = min(max(0.0, value), max(0.0, duration - 0.1))
        if any(cluster.t_start <= seconds <= cluster.t_end for cluster in clusters):
            continue
        if any(span.t_start <= seconds <= span.t_end for span in (avoid_spans or [])):
            continue
        if all(abs(seconds - existing) > 0.5 for existing in result):
            result.append(seconds)
    return result[:2]


def active_rects_at(clusters: list[MaskCluster], seconds: float) -> list[tuple[int, int, int, int]]:
    rects: list[tuple[int, int, int, int]] = []
    for cluster in clusters:
        if cluster.t_start <= seconds <= cluster.t_end:
            rects.extend(cluster.rects)
    return dedupe_rects(rects, iou_threshold=0.65)


def diff_ignore_rects(rects: list[tuple[int, int, int, int]], meta: VideoMeta) -> list[tuple[int, int, int, int]]:
    pad = env_int("DESUB_DIFF_IGNORE_PAD_PX", 2)
    return dedupe_rects((dilate_box(rect, pad, meta.width, meta.height) for rect in rects), iou_threshold=0.65)


def residual_text_boxes_report(
    output_clip: Path,
    meta: VideoMeta,
    *,
    crop_y: int,
    model_name: str,
    work_dir: Path,
) -> dict[str, Any]:
    started = time.time()
    detect_fps = env_float("DESUB_RESIDUAL_QA_FPS", 2.0)
    frames = sample_frames(output_clip, meta, detect_fps)
    deadline = time.time() + env_float("DESUB_RESIDUAL_QA_BUDGET_SECONDS", 240.0)
    detections, info = detect_easyocr(frames, meta, crop_y, deadline=deadline)
    geometry_boxes = dedupe_with_priority([
        det for det in detections
        if not sub_band_geometry_reason(det, meta)
    ])
    persistent_boxes, persistence = persistent_sub_band_detections(
        geometry_boxes,
        meta=meta,
        sample_frame_ids=[frame_id for frame_id, _seconds, _frame in frames],
    )
    boxes_by_second: dict[str, dict[str, int]] = {}
    for det in geometry_boxes:
        key = str(int(math.floor(max(0.0, det.t))))
        item = boxes_by_second.setdefault(key, {"boxes": 0, "persistent_boxes": 0})
        item["boxes"] += 1
    for det in persistent_boxes:
        key = str(int(math.floor(max(0.0, det.t))))
        item = boxes_by_second.setdefault(key, {"boxes": 0, "persistent_boxes": 0})
        item["persistent_boxes"] += 1
    report = {
        "ok": True,
        "model": model_name,
        "fps": detect_fps,
        "sampled_frames": len(frames),
        "seconds": time.time() - started,
        "detector": info,
        "box_count": len(geometry_boxes),
        "persistent_box_count": len(persistent_boxes),
        "boxes_by_second": dict(sorted(boxes_by_second.items(), key=lambda item: int(item[0]))),
        "persistence": persistence,
        "boxes_preview": [asdict(det) for det in geometry_boxes[:100]],
    }
    write_json(work_dir / f"residual_text_boxes_{model_name}.json", report)
    return report


def selected_models() -> tuple[str, ...]:
    raw = os.environ.get("DESUB_MODEL", "lama").strip().lower()
    if raw in {"", "lama"}:
        return ("lama",)
    if raw == "sttn":
        return ("sttn",)
    if raw in {"both", "all", "compare"}:
        return ("sttn", "lama")
    values: list[str] = []
    for part in raw.replace(";", ",").split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name not in {"sttn", "lama"}:
            raise ValueError(f"unsupported DESUB_MODEL value: {name}")
        if name not in values:
            values.append(name)
    return tuple(values or ["lama"])


def _ass_time(seconds: float) -> str:
    total_cs = max(0, int(round(seconds * 100)))
    cs = total_cs % 100
    total_seconds = total_cs // 100
    s = total_seconds % 60
    total_minutes = total_seconds // 60
    m = total_minutes % 60
    h = total_minutes // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _balanced_subtitle_lines(value: str) -> list[str]:
    text = " ".join(str(value or "").split())
    max_chars = env_int("DESUB_VISUB_WRAP_CHARS", 24)
    if len(text) <= max_chars or " " not in text:
        return [text]

    words = text.split()
    candidates: list[tuple[int, int, str, str]] = []
    for index in range(1, len(words)):
        left = " ".join(words[:index])
        right = " ".join(words[index:])
        overflow = max(0, len(left) - max_chars) + max(0, len(right) - max_chars)
        candidates.append((overflow, abs(len(left) - len(right)), left, right))
    _, _, left, right = min(candidates)
    return [left, right]


def _ass_text(value: str, *, position_x: int, position_y: int) -> str:
    lines = _balanced_subtitle_lines(value)
    escaped = [line.replace("{", "(").replace("}", ")") for line in lines]
    return rf"{{\an5\pos({position_x},{position_y})}}" + r"\N".join(escaped)


def analyze_visible_subtitles_vi(
    *,
    clip_uri: str,
    clip_duration: float,
    project_id: str,
    region: str,
    model: str,
    timeout: int,
) -> list[dict[str, Any]]:
    from google import genai  # type: ignore
    from google.genai import types  # type: ignore
    from pydantic import BaseModel, Field

    class _VisubLine(BaseModel):
        start: float
        end: float
        text_zh: str = ""
        text_vi: str = ""
        center_x: float = 0.5
        center_y: float = 0.705

    class _VisubSchema(BaseModel):
        lines: list[_VisubLine] = Field(default_factory=list)

    prompt = """Analyze this short video clip and extract ONLY the Chinese burned-in subtitles/captions visible on screen.

Return JSON lines in chronological order. Requirements:
- Each line must correspond to visible Chinese subtitle/caption text, not scene description.
- start/end are seconds from the beginning of the clip and must match when that visible text is on screen.
- Inspect the actual caption transitions frame by frame. Use precise timestamps to the nearest 0.05 second, preserve blank gaps, and do not infer timing from narration.
- text_zh is the original visible Chinese text.
- text_vi is a natural Vietnamese translation, concise enough to fit as a subtitle.
- center_x and center_y are the center of that Chinese text box, normalized to the video width/height from 0 to 1.
- If no visible Chinese subtitle is present, return lines=[].
"""
    client = genai.Client(vertexai=True, project=project_id, location=region)
    response = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_uri(file_uri=clip_uri, mime_type="video/mp4"),
            prompt,
        ],
        config=types.GenerateContentConfig(
            temperature=0.1,
            thinking_config=types.ThinkingConfig(thinking_budget=1024),
            max_output_tokens=8192,
            response_mime_type="application/json",
            response_schema=_VisubSchema,
            http_options=types.HttpOptions(timeout=timeout * 1000),
        ),
    )
    parsed = response.parsed
    if parsed is None:
        parsed = _VisubSchema(**json.loads(response.text or "{}"))
    cues: list[dict[str, Any]] = []
    previous_start = -1.0
    for item in parsed.lines:
        start = max(0.0, min(float(item.start), clip_duration))
        end = min(float(item.end), clip_duration)
        text_vi = " ".join(str(item.text_vi or "").split())
        text_zh = " ".join(str(item.text_zh or "").split())
        if not text_vi or start < previous_start or end <= start:
            continue
        cues.append({
            "start": start,
            "end": end,
            "text_zh": text_zh,
            "text_vi": text_vi,
            "center_x": max(0.0, min(float(item.center_x), 1.0)),
            "center_y": max(0.0, min(float(item.center_y), 1.0)),
        })
        previous_start = start
    return cues


def load_visub_cues_override(gs_uri: str, clip_duration: float) -> list[dict[str, Any]]:
    from google.cloud import storage

    bucket_name, blob_name = parse_gs_uri(gs_uri)
    payload = json.loads(
        storage.Client(project=_require_project_id()).bucket(bucket_name).blob(blob_name).download_as_text(encoding="utf-8")
    )
    raw_cues = payload.get("cues") if isinstance(payload, dict) else payload
    if not isinstance(raw_cues, list):
        raise ValueError("DESUB_VISUB_CUES_URI must contain a JSON array or an object with a cues array")

    cues: list[dict[str, Any]] = []
    previous_start = -1.0
    for item in raw_cues:
        if not isinstance(item, dict):
            raise ValueError("each overridden visub cue must be a JSON object")
        start = max(0.0, min(float(item.get("start", 0.0)), clip_duration))
        end = max(0.0, min(float(item.get("end", 0.0)), clip_duration))
        text_vi = " ".join(str(item.get("text_vi") or "").split())
        if not text_vi or start < previous_start or end <= start:
            raise ValueError(f"invalid overridden visub cue: {item}")
        cues.append({
            "start": start,
            "end": end,
            "text_zh": " ".join(str(item.get("text_zh") or "").split()),
            "text_vi": text_vi,
            "center_x": max(0.0, min(float(item.get("center_x", 0.5)), 1.0)),
            "center_y": max(0.0, min(float(item.get("center_y", 0.705)), 1.0)),
        })
        previous_start = start
    return cues


def _cjk_only(value: str) -> str:
    return "".join(ch for ch in str(value or "") if "\u3400" <= ch <= "\u9fff")


def _cjk_similarity(expected: str, detected: str) -> float:
    left = _cjk_only(expected)
    right = _cjk_only(detected)
    if not left or not right:
        return 0.0
    sequence = SequenceMatcher(None, left, right).ratio()
    overlap = len(set(left) & set(right)) / max(1, len(set(left)))
    return max(sequence, overlap * 0.9)


def refine_visub_cues_with_ocr(
    cues: list[dict[str, Any]],
    detections: list[Detection],
    meta: VideoMeta,
    detect_fps: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    grouped: dict[int, list[Detection]] = {}
    for det in detections:
        if det.detector != "easyocr_craft" or not has_cjk(det.text) or not box_center_in_sub_band(det.box, meta):
            continue
        grouped.setdefault(det.frame, []).append(det)

    samples: list[dict[str, Any]] = []
    for frame_id, items in sorted(grouped.items()):
        ordered = sorted(items, key=lambda item: (item.x1, item.y1))
        text = "".join(_cjk_only(item.text) for item in ordered)
        if not text:
            continue
        box = union_boxes(item.box for item in ordered)
        samples.append({
            "frame": frame_id,
            "t": statistics.median(item.t for item in ordered),
            "text": text,
            "box": box,
        })

    threshold = env_float("DESUB_VISUB_OCR_SIMILARITY", 0.34)
    search_seconds = env_float("DESUB_VISUB_OCR_SEARCH_SECONDS", 3.0)
    frame_step = 1.0 / max(0.1, detect_fps)
    refined: list[dict[str, Any]] = []
    report_items: list[dict[str, Any]] = []
    search_floor = 0.0

    for cue in cues:
        expected = str(cue.get("text_zh") or "")
        predicted_start = float(cue["start"])
        predicted_end = float(cue["end"])
        predicted_mid = (predicted_start + predicted_end) / 2.0
        search_start = max(search_floor, predicted_start - search_seconds)
        search_end = min(meta.duration, predicted_end + search_seconds)
        matches: list[dict[str, Any]] = []
        for sample in samples:
            if sample["t"] < search_start or sample["t"] > search_end:
                continue
            similarity = _cjk_similarity(expected, str(sample["text"]))
            if similarity >= threshold:
                matches.append({**sample, "similarity": similarity})

        runs: list[list[dict[str, Any]]] = []
        for match in matches:
            if not runs or float(match["t"]) - float(runs[-1][-1]["t"]) > frame_step * 2.5:
                runs.append([match])
            else:
                runs[-1].append(match)

        best_run: list[dict[str, Any]] = []
        best_score = -1.0
        for run in runs:
            run_mid = (float(run[0]["t"]) + float(run[-1]["t"])) / 2.0
            average_similarity = statistics.mean(float(item["similarity"]) for item in run)
            score = average_similarity + min(len(run), 8) * 0.03 - abs(run_mid - predicted_mid) * 0.02
            if score > best_score:
                best_score = score
                best_run = run

        updated = dict(cue)
        if best_run:
            boxes = [tuple(item["box"]) for item in best_run]
            box = union_boxes(boxes)
            updated["start"] = max(0.0, float(best_run[0]["t"]) - frame_step / 2.0)
            updated["end"] = min(meta.duration, float(best_run[-1]["t"]) + frame_step / 2.0)
            updated["center_x"] = ((box[0] + box[2]) / 2.0) / max(1, meta.width)
            updated["center_y"] = ((box[1] + box[3]) / 2.0) / max(1, meta.height)
            method = "ocr"
        else:
            method = "model_fallback"
        refined.append(updated)
        search_floor = max(search_floor, float(updated["end"]) - frame_step)
        report_items.append({
            "text_zh": expected,
            "method": method,
            "model_start": predicted_start,
            "model_end": predicted_end,
            "refined_start": float(updated["start"]),
            "refined_end": float(updated["end"]),
            "matched_samples": len(best_run),
            "score": best_score if best_run else None,
        })

    for index in range(1, len(refined)):
        previous = refined[index - 1]
        current = refined[index]
        if float(previous["end"]) <= float(current["start"]):
            continue
        midpoint = (float(previous["end"]) + float(current["start"])) / 2.0
        previous["end"] = midpoint
        current["start"] = midpoint

    return refined, {
        "enabled": True,
        "sample_count": len(samples),
        "cue_count": len(cues),
        "ocr_refined_count": sum(1 for item in report_items if item["method"] == "ocr"),
        "model_fallback_count": sum(1 for item in report_items if item["method"] == "model_fallback"),
        "detect_fps": detect_fps,
        "similarity_threshold": threshold,
        "search_seconds": search_seconds,
        "items": report_items,
    }


def write_visub_ass(path: Path, cues: list[dict[str, Any]], meta: VideoMeta) -> None:
    font = os.environ.get("DESUB_VISUB_FONT", "DejaVu Sans").strip() or "DejaVu Sans"
    font_size = max(18, int(round(meta.height * env_float("DESUB_VISUB_FONT_SIZE_RATIO", 0.040))))
    margin_lr = max(20, int(round(meta.width * 0.08)))
    position_x = env_int("DESUB_VISUB_POSITION_X", meta.width // 2)
    position_y = env_int(
        "DESUB_VISUB_POSITION_Y",
        int(round(meta.height * env_float("DESUB_VISUB_POSITION_Y_RATIO", 0.705))),
    )
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {meta.width}",
        f"PlayResY: {meta.height}",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,2.5,0,5,{margin_lr},{margin_lr},0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for cue in cues:
        cue_position_x = int(round(meta.width * float(cue.get("center_x", position_x / max(1, meta.width)))))
        cue_position_y = int(round(meta.height * float(cue.get("center_y", position_y / max(1, meta.height)))))
        lines.append(
            "Dialogue: 0,"
            f"{_ass_time(float(cue['start']))},{_ass_time(float(cue['end']))},"
            f"Default,,0,0,0,,{_ass_text(str(cue.get('text_vi') or ''), position_x=cue_position_x, position_y=cue_position_y)}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def burn_visub_subtitles(input_clip: Path, ass_path: Path, output_clip: Path) -> None:
    crf = os.environ.get("DESUB_VISUB_OUTPUT_CRF", "18")
    cmd([
        "ffmpeg",
        "-y",
        "-i",
        str(input_clip),
        "-vf",
        f"subtitles={ass_path}",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        crf,
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(output_clip),
    ], timeout=900)


def main() -> int:
    started = time.time()
    source_uri = os.environ.get("SOURCE_URI", "").strip()
    douyin_url = os.environ.get("DOUYIN_URL", "").strip()
    if not source_uri and not douyin_url:
        raise SystemExit("SOURCE_URI or DOUYIN_URL is required")
    rid = run_id()
    result_prefix = normalize_result_prefix(os.environ.get("RESULT_PREFIX", DEFAULT_RESULT_ROOT), rid)
    work = Path(os.environ.get("WORK_DIR", "/tmp/desub_lab")) / rid
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    upload_status(work, result_prefix, "downloading", run_id=rid, gcs_prefix=result_prefix)

    try:
        source = work / "source.mp4"
        source_info = download_source_video(source_uri=source_uri, douyin_url=douyin_url, out_path=source)
        source_meta = ffprobe(source)
        clip_seconds = min(env_float("DESUB_CLIP_SECONDS", DEFAULT_CLIP_SECONDS), source_meta.duration)
        clip_start = min(env_float("DESUB_CLIP_START_SECONDS", 0.0), max(0.0, source_meta.duration - clip_seconds))
        clip = work / "source_30s.mp4"
        make_clip(source, clip, clip_start, clip_seconds)
        clip_meta = ffprobe(clip)
        visub_enabled = env_bool("DESUB_VISUB", False)
        visub_cues_uri = os.environ.get("DESUB_VISUB_CUES_URI", "").strip()
        visub_clip_uri = source_uri
        clip_matches_source = clip_start <= 0.01 and abs(source_meta.duration - clip_meta.duration) <= 0.1
        if visub_enabled and not visub_cues_uri and (not source_uri or not clip_matches_source):
            visub_clip_uri = f"{result_prefix}/visub_input.mp4"
            upload_file(clip, visub_clip_uri, content_type="video/mp4")
            log("visub_input_uploaded", uri=visub_clip_uri, bytes=clip.stat().st_size)
        upload_status(
            work,
            result_prefix,
            "detecting",
            run_id=rid,
            source_meta=asdict(source_meta),
            clip_meta=asdict(clip_meta),
        )

        detect_fps = env_float("DESUB_DETECT_FPS", DEFAULT_DETECT_FPS)
        crop_y = int(clip_meta.height * env_float("DESUB_BAND_TOP_RATIO", DEFAULT_BAND_TOP_RATIO))
        frames = sample_frames(clip, clip_meta, detect_fps)
        raw_detections, detector_summary = detector_comparison(frames, clip_meta, crop_y)
        detections, detection_filter_summary = filter_detections_for_masks(
            raw_detections,
            clip_meta,
            sample_frame_ids=[frame_id for frame_id, _seconds, _frame in frames],
        )
        raw_spans = build_spans(detections, clip_meta, dilate_px=env_int("DESUB_MASK_DILATE_PX", DEFAULT_MASK_DILATE_PX))
        subtitle_span_candidates, watermark_spans = split_watermark_spans(raw_spans, clip_meta)
        spans, rejected_spans = filter_spans_for_inpaint(subtitle_span_candidates, clip_meta, crop_y=crop_y)
        subtitle_clusters = build_subtitle_clusters(spans, clip_meta)
        watermark_cluster = build_watermark_cluster(clip_meta, watermark_spans)
        watermark_rects = list(watermark_cluster.rects) if watermark_cluster else []
        no_sub_avoid_spans = qa_avoid_spans(raw_detections, clip_meta)

        mask_payload = {
            "run_id": rid,
            "source_uri": source_uri,
            "douyin_url": douyin_url,
            "clip_start_seconds": clip_start,
            "clip_duration_seconds": clip_meta.duration,
            "detect_fps": detect_fps,
            "band_top_ratio": env_float("DESUB_BAND_TOP_RATIO", DEFAULT_BAND_TOP_RATIO),
            "detectors": detector_summary,
            "detection_filter": detection_filter_summary,
            "raw_detections": [asdict(det) for det in raw_detections],
            "detections": [asdict(det) for det in detections],
            "raw_spans": [asdict(span) for span in raw_spans],
            "watermark_spans": [asdict(span) for span in watermark_spans],
            "qa_avoid_spans": [asdict(span) for span in no_sub_avoid_spans],
            "rejected_spans": rejected_spans,
            "spans": [asdict(span) for span in spans],
            "subtitle_clusters": [cluster_dict(cluster) for cluster in subtitle_clusters],
            "watermark_cluster": cluster_dict(watermark_cluster) if watermark_cluster else None,
        }
        write_json(work / "mask.json", mask_payload)

        frames_dir = work / "frames"
        qa_points: list[dict[str, Any]] = []
        for idx, seconds in enumerate(
            frame_times_without_clusters(
                subtitle_clusters,
                clip_meta.duration,
                avoid_spans=no_sub_avoid_spans,
            ),
            start=1,
        ):
            qa_points.append({"label": f"nosub{idx:02d}", "seconds": seconds})
        for idx, seconds in enumerate(frame_times_for_qa(spans, clip_meta.duration), start=1):
            qa_points.append({"label": f"sub{idx:02d}", "seconds": seconds})
        for seconds in fixed_frame_times_for_qa(clip_meta.duration):
            qa_points.append({"label": f"fixed{int(seconds):02d}", "seconds": seconds})
        for point in qa_points:
            extract_frame(clip, frames_dir / f"{point['label']}_{point['seconds']:.2f}_before.png", float(point["seconds"]))
        zoom_points: list[dict[str, Any]] = []
        for seconds in parse_seconds_list(os.environ.get("DESUB_QA_ZOOM_SECONDS", "2")):
            if seconds < 0 or seconds > clip_meta.duration:
                continue
            active = active_rects_at(subtitle_clusters, seconds)
            box = zoom_crop_box(active, clip_meta, pad=env_int("DESUB_QA_ZOOM_PAD_PX", 80))
            label = f"zoom2x_{seconds:.2f}"
            extract_zoom_frame(
                clip,
                frames_dir / f"{label}_before.png",
                seconds,
                box,
                scale=env_int("DESUB_QA_ZOOM_SCALE", 2),
            )
            zoom_points.append({
                "label": label,
                "seconds": seconds,
                "box": {"x1": box[0], "y1": box[1], "x2": box[2], "y2": box[3]},
            })

        upload_status(
            work,
            result_prefix,
            "inpainting",
            run_id=rid,
            raw_span_count=len(raw_spans),
            watermark_span_count=len(watermark_spans),
            span_count=len(spans),
            rejected_span_count=len(rejected_spans),
            subtitle_cluster_count=len(subtitle_clusters),
            watermark_rect_count=len(watermark_rects),
        )
        model_results: dict[str, Any] = {}
        qa_diffs: dict[str, Any] = {}
        model_names = selected_models()
        for model_name in model_names:
            output_clip = work / f"clip_{model_name}_30s.mp4"
            try:
                result = run_model_pipeline(
                    model_name=model_name,
                    input_clip=clip,
                    output_clip=output_clip,
                    work_dir=work / "model_work",
                    subtitle_clusters=subtitle_clusters,
                    watermark_cluster=watermark_cluster,
                    meta=clip_meta,
                )
                model_diffs: list[dict[str, Any]] = []
                for point in qa_points:
                    seconds = float(point["seconds"])
                    label = str(point["label"])
                    before_png = frames_dir / f"{label}_{seconds:.2f}_before.png"
                    after_png = frames_dir / f"{label}_{seconds:.2f}_after_{model_name}.png"
                    extract_frame(output_clip, after_png, seconds)
                    ignore_rects = diff_ignore_rects(
                        [*watermark_rects, *active_rects_at(subtitle_clusters, seconds)],
                        clip_meta,
                    )
                    model_diffs.append({
                        "label": label,
                        "seconds": seconds,
                        **frame_diff_metrics(before_png, after_png, ignore_rects=ignore_rects),
                    })
                zoom_frames: list[dict[str, Any]] = []
                for zoom in zoom_points:
                    seconds = float(zoom["seconds"])
                    label = str(zoom["label"])
                    box_dict = zoom["box"]
                    box = (int(box_dict["x1"]), int(box_dict["y1"]), int(box_dict["x2"]), int(box_dict["y2"]))
                    after_zoom = frames_dir / f"{label}_after_{model_name}.png"
                    extract_zoom_frame(
                        output_clip,
                        after_zoom,
                        seconds,
                        box,
                        scale=env_int("DESUB_QA_ZOOM_SCALE", 2),
                    )
                    mask_png: Path | None = None
                    for cluster_result in (result.get("subtitle") or {}).get("cluster_results") or []:
                        alpha_path = cluster_result.get("alpha_mask")
                        seg_start = float(cluster_result.get("segment_start") or 0.0)
                        seg_end = float(cluster_result.get("segment_end") or 0.0)
                        if not alpha_path or seconds < seg_start or seconds > seg_end:
                            continue
                        candidate = Path(alpha_path)
                        if not candidate.exists():
                            continue
                        mask_png = frames_dir / f"{label}_mask_{model_name}.png"
                        extract_frame(candidate, mask_png, seconds - seg_start)
                        break
                    zoom_frames.append({
                        "label": label,
                        "seconds": seconds,
                        "before": str(frames_dir / f"{label}_before.png"),
                        "after": str(after_zoom),
                        "mask": str(mask_png) if mask_png else None,
                        "box": box_dict,
                    })
                result["qa_zoom_frames"] = zoom_frames
                qa_diffs[model_name] = model_diffs
                try:
                    result["residual_text_boxes"] = residual_text_boxes_report(
                        output_clip,
                        clip_meta,
                        crop_y=crop_y,
                        model_name=model_name,
                        work_dir=work / "qa",
                    )
                except Exception as residual_exc:  # noqa: BLE001
                    result["residual_text_boxes"] = {"ok": False, "error": str(residual_exc)}
                    log("residual_text_qa_failed", model=model_name, error=str(residual_exc))
                if visub_enabled:
                    if not visub_clip_uri and not visub_cues_uri:
                        raise RuntimeError("DESUB_VISUB requires a GCS clip URI or DESUB_VISUB_CUES_URI")
                    visub_model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
                    visub_region = os.environ.get("VERTEX_REGION", "global").strip() or "global"
                    if visub_cues_uri:
                        cues = load_visub_cues_override(visub_cues_uri, clip_meta.duration)
                        cue_source = visub_cues_uri
                        timing_refinement = {"enabled": False, "reason": "cue_override"}
                    else:
                        cues = analyze_visible_subtitles_vi(
                            clip_uri=visub_clip_uri,
                            clip_duration=clip_meta.duration,
                            project_id=_require_project_id(),
                            region=visub_region,
                            model=visub_model,
                            timeout=env_int("DESUB_VISUB_GEMINI_TIMEOUT_SECONDS", 300),
                        )
                        cue_source = f"vertex:{visub_model}"
                        if env_bool("DESUB_VISUB_REFINE_WITH_OCR", True):
                            cues, timing_refinement = refine_visub_cues_with_ocr(
                                cues,
                                raw_detections,
                                clip_meta,
                                detect_fps,
                            )
                        else:
                            timing_refinement = {"enabled": False, "reason": "disabled"}
                    if not cues:
                        raise RuntimeError("DESUB_VISUB produced no Vietnamese subtitle cues")
                    write_json(work / f"visub_cues_{model_name}.json", {
                        "model": visub_model,
                        "region": visub_region,
                        "source_uri": visub_clip_uri,
                        "cue_source": cue_source,
                        "timing_refinement": timing_refinement,
                        "cues": cues,
                    })
                    ass_path = work / f"visub_{model_name}.ass"
                    write_visub_ass(ass_path, cues, clip_meta)
                    visub_clip = work / f"clip_{model_name}_visub_10s.mp4"
                    burn_visub_subtitles(output_clip, ass_path, visub_clip)
                    result["visub"] = {
                        "ok": True,
                        "model": visub_model,
                        "region": visub_region,
                        "cue_source": cue_source,
                        "timing_refinement": timing_refinement,
                        "cue_count": len(cues),
                        "ass": str(ass_path),
                        "output": str(visub_clip),
                        "output_uri": f"{result_prefix}/{visub_clip.relative_to(work).as_posix()}",
                        "cues": cues,
                    }
                model_results[model_name] = {"ok": True, **result}
            except Exception as exc:  # noqa: BLE001
                model_results[model_name] = {"ok": False, "error": str(exc)}
                log("model_failed", model=model_name, error=str(exc))

        if not any(result.get("ok") for result in model_results.values()):
            raise RuntimeError(f"selected DESUB_MODEL runs failed: {','.join(model_names)}")

        elapsed_total = time.time() - started
        gpu_seconds = sum(float(result.get("seconds") or 0) for result in model_results.values() if result.get("ok"))
        benchmark = {
            "run_id": rid,
            "result_prefix": result_prefix,
            "source": source_info,
            "source_meta": asdict(source_meta),
            "clip_meta": asdict(clip_meta),
            "package_versions": package_versions(),
            "detectors": detector_summary,
            "detection_filter": detection_filter_summary,
            "raw_spans": len(raw_spans),
            "watermark_spans": len(watermark_spans),
            "spans": len(spans),
            "rejected_spans": len(rejected_spans),
            "subtitle_clusters": len(subtitle_clusters),
            "watermark_cluster": cluster_dict(watermark_cluster) if watermark_cluster else None,
            "selected_models": list(model_names),
            "qa_zoom_points": zoom_points,
            "models": model_results,
            "qa_diffs": qa_diffs,
            "residual_text_boxes": {
                name: result.get("residual_text_boxes")
                for name, result in model_results.items()
                if result.get("ok")
            },
            "residual_text_box_counts": {
                name: (result.get("residual_text_boxes") or {}).get("box_count")
                for name, result in model_results.items()
                if result.get("ok")
            },
            "elapsed_total_seconds": elapsed_total,
            "gpu_seconds_total": gpu_seconds,
            "gpu_seconds_per_video_minute": gpu_seconds / max(clip_meta.duration / 60.0, 0.01),
            "estimated_l4_cost_usd_for_clip": gpu_seconds / 3600.0 * GPU_HOURLY_USD,
            "estimated_l4_cost_usd_per_source_video": (gpu_seconds / max(clip_meta.duration, 0.01) * source_meta.duration) / 3600.0 * GPU_HOURLY_USD,
            "audio_policy": "original audio stream copied during final overlay encode",
            "resolution_duration_policy": "clip outputs preserve source clip resolution and duration",
        }
        write_json(work / "benchmark.json", benchmark)
        write_json(work / "qa_diffs.json", qa_diffs)
        write_json(work / "input_meta.json", {
            "source_uri": source_uri,
            "douyin_url": douyin_url,
            "run_id": rid,
            "result_prefix": result_prefix,
        })

        uploaded = upload_tree(work, result_prefix)
        upload_status(
            work,
            result_prefix,
            "completed",
            run_id=rid,
            uploaded=uploaded,
            benchmark_uri=f"{result_prefix}/benchmark.json",
            mask_uri=f"{result_prefix}/mask.json",
        )
        log("completed", result_prefix=result_prefix, uploaded=len(uploaded))
        return 0
    except Exception as exc:  # noqa: BLE001
        upload_status(work, result_prefix, "failed", run_id=rid, error=str(exc))
        log("failed", error=str(exc), result_prefix=result_prefix)
        raise


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--patch-vsr":
        patch_vsr_tree(Path(sys.argv[2]))
        raise SystemExit(0)
    if len(sys.argv) >= 3 and sys.argv[1] == "--bake-models":
        bake_model_weights(Path(sys.argv[2]))
        raise SystemExit(0)
    raise SystemExit(main())
