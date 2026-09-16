from __future__ import annotations

import json
import os
import sys
import tempfile

from container_long.pipeline import LongPipelineConfig, process_source
from container_short.steps import gcsio
from container_short.steps.download import probe_douyin


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} environment variable is required")
    return value


def _flag(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in (
        "1", "true", "yes", "on"
    )


def _callback(payload: dict) -> None:
    if not _flag("CALLBACK_ENABLED"):
        return
    url = os.environ.get("CALLBACK_URL", "").strip()
    if not url:
        return
    import requests

    response = requests.post(url, json=payload, timeout=30)
    print(f"[long-worker] callback HTTP {response.status_code}", flush=True)


def _task_input() -> tuple[str, str, int, str]:
    batch_spec_uri = os.environ.get("BATCH_SPEC_URI", "").strip()
    if not batch_spec_uri:
        return (
            os.environ.get("DOUYIN_URL", "").strip(),
            os.environ.get("OUTPUT_URI", "").strip(),
            int(os.environ.get("SOURCE_INDEX", "0")),
            os.environ.get("BATCH_ID", "").strip(),
        )

    task_index = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))
    work = tempfile.mkdtemp(prefix="long_worker_spec_")
    spec_path = os.path.join(work, "batch.json")
    gcsio.download(batch_spec_uri, spec_path)
    with open(spec_path, encoding="utf-8") as handle:
        spec = json.load(handle)
    videos = spec.get("videos") or []
    if task_index < 0 or task_index >= len(videos):
        raise IndexError(
            f"CLOUD_RUN_TASK_INDEX={task_index} outside {len(videos)} videos"
        )
    item = videos[task_index]
    if isinstance(item, str):
        douyin_url = item.strip()
    else:
        douyin_url = str(item.get("douyin_url") or "").strip()
    batch_id = str(
        spec.get("batch_id") or os.environ.get("BATCH_ID") or ""
    ).strip()
    output_prefix = os.environ.get("OUTPUT_PREFIX", "").strip().rstrip("/")
    if not output_prefix:
        raise ValueError("OUTPUT_PREFIX is required with BATCH_SPEC_URI")
    output_uri = (
        f"{output_prefix}/processed/{task_index:03d}/final.mp4"
    )
    return douyin_url, output_uri, task_index, batch_id


def main() -> int:
    try:
        url, output_uri, source_index, batch_id = _task_input()
    except Exception as exc:  # noqa: BLE001
        print(f"[long-worker] invalid task input: {exc}", flush=True)
        return 2
    if not url or not output_uri:
        print("[long-worker] URL and output URI are required", flush=True)
        return 2

    if _flag("DOUYIN_PROBE_ONLY"):
        try:
            info = probe_douyin(url, cookie=os.environ.get("DOUYIN_COOKIE") or None)
        except Exception as exc:  # noqa: BLE001
            print(f"[long-worker] Douyin probe FAILED: {exc}", flush=True)
            return 1
        print(json.dumps({
            "event": "douyin.probe.completed",
            "ok": True,
            "aweme_id": info.aweme_id,
            "description": info.desc,
            "author": info.author,
            "duration_ms": info.duration_ms,
            "has_play_url": bool(info.play_url),
        }, ensure_ascii=False), flush=True)
        return 0

    cfg = LongPipelineConfig(
        project_id=_required_env("GCP_PROJECT_ID"),
        region=os.environ.get("VERTEX_REGION", "global"),
        scratch_prefix=os.environ.get(
            "SCRATCH_GS_PREFIX",
            "gs://YOUR_GCP_PROJECT-scratch-sg/long",
        ),
        model=os.environ.get("GEMINI_MODEL", "gemini-2.5-pro"),
        chunk_seconds=int(os.environ.get("LONG_CHUNK_SECONDS", "360")),
        min_chunk_seconds=int(
            os.environ.get("LONG_MIN_CHUNK_SECONDS", "240")
        ),
        max_chunk_seconds=int(
            os.environ.get("LONG_MAX_CHUNK_SECONDS", "480")
        ),
        analysis_padding_seconds=float(
            os.environ.get("LONG_ANALYSIS_PADDING_SECONDS", "3")
        ),
        bgm_gain_db=float(os.environ.get("BGM_GAIN_DB", "-20")),
        dub_max_speed=float(os.environ.get("DUB_MAX_SPEED", "1.35")),
        dub_hard_max_speed=float(os.environ.get("DUB_HARD_MAX_SPEED", "1.45")),
        dub_end_microfit_max_speed=float(
            os.environ.get("DUB_END_MICROFIT_MAX_SPEED", "1.08")
        ),
        dub_tail_pad_seconds=float(os.environ.get("DUB_TAIL_PAD_SECONDS", "3.0")),
        visual_refresh=os.environ.get("LONG_VISUAL_REFRESH", "auto"),
        video_crf=int(os.environ.get("LONG_VIDEO_CRF", "18")),
        video_width=int(os.environ.get("LONG_VIDEO_WIDTH", "1920")),
        video_height=int(os.environ.get("LONG_VIDEO_HEIGHT", "1080")),
        tts_provider=os.environ.get("TTS_PROVIDER", "capcut"),
        tts_fallback_provider=os.environ.get("TTS_FALLBACK_PROVIDER", "google"),
        tts_voice=os.environ.get("TTS_VOICE", "BV074_streaming"),
        speaking_rate=float(os.environ.get("SPEAKING_RATE", "1.0")),
        google_tts_voice=os.environ.get(
            "GOOGLE_TTS_VOICE", "vi-VN-Wavenet-B"
        ),
        capcut_resource_id=os.environ.get(
            "CAPCUT_RESOURCE_ID", "7102355709945188865"
        ),
        capcut_device_json=os.environ.get("CAPCUT_DEVICE_JSON") or None,
        capcut_poll_timeout=int(os.environ.get("CAPCUT_POLL_TIMEOUT", "300")),
        cookie=os.environ.get("DOUYIN_COOKIE") or None,
        source_start_seconds=float(
            os.environ.get("LONG_SOURCE_START_SECONDS", "0")
        ),
        source_max_seconds=float(
            os.environ.get("LONG_SOURCE_MAX_SECONDS", "0")
        ),
        cliffhanger_enabled=_flag("LONG_CLIFFHANGER_ENABLED"),
        cliffhanger_min_seconds=float(
            os.environ.get("LONG_CLIFFHANGER_MIN_SECONDS", "600")
        ),
        cliffhanger_max_seconds=float(
            os.environ.get("LONG_CLIFFHANGER_MAX_SECONDS", "900")
        ),
        raw_source_uri=os.environ.get("LONG_RAW_SOURCE_URI", "").strip(),
    )
    try:
        result = process_source(
            douyin_url=url,
            source_index=source_index,
            output_uri=output_uri,
            cfg=cfg,
        )
    except Exception as exc:  # noqa: BLE001
        _callback({
            "event": "long.source.failed",
            "ok": False,
            "batch_id": batch_id,
            "source_index": source_index,
            "error": str(exc),
        })
        print(f"[long-worker] FAILED: {exc}", flush=True)
        return 1

    _callback({
        "event": "long.source.completed",
        "ok": True,
        "batch_id": batch_id,
        "source_index": source_index,
        **result.to_dict(),
    })
    print(json.dumps(result.to_dict(), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
