"""Cloud Run **Job** entrypoint: Douyin → YouTube Short tiếng Việt.

Mirror container_batch/batch_job.py: tham số qua env vars, chạy một lần rồi thoát.
n8n trigger job với `--update-env-vars DOUYIN_URL=...,OUTPUT_URI=...`.

Env vars:
    DOUYIN_URL        required — link/đoạn text chia sẻ Douyin
    OUTPUT_URI        required — gs://bucket/shorts/{id}/final.mp4
    SCRATCH_GS_PREFIX default gs://YOUR_GCP_PROJECT-shorts/tmp
    GCP_PROJECT_ID    default YOUR_GCP_PROJECT
    VERTEX_REGION     default us-central1
    TARGET_SECONDS    default 48        SPEED            default 1.04
    BGM_GAIN_DB       default -16        ENABLE_BGM       default true
    ENABLE_SUBTITLES  default false      tắt sub Việt, chỉ giữ lồng tiếng
    SUBTITLE_MARGIN_V default 690        nâng sub vào vùng video foreground
    ENABLE_SUBTITLE_OCR default false    OCR hiện tắt để giữ timeline Gemini ổn định
    SUBTITLE_SYNC_MODE default gemini    gemini | ocr
    SUBTITLE_OCR_FPS  default 4.0        số frame OCR mỗi giây
    DUB_MODE          default timed       (timed | continuous)
    TTS_PROVIDER      default capcut      (google | capcut)
    TTS_FALLBACK_PROVIDER default google  (google | none)
    TTS_VOICE         default BV074_streaming
    GOOGLE_TTS_VOICE  default vi-VN-Wavenet-B
    CAPCUT_RESOURCE_ID default 7102355709945188865
    CAPCUT_DEVICE_JSON optional — local path, raw JSON, or gs://... override for CapCut
    CAPCUT_POLL_TIMEOUT default 300
    GEMINI_MODEL      default gemini-2.5-flash
    DOUYIN_COOKIE     default ""         (nếu nội dung bị giới hạn)
    GOOGLE_APPLICATION_CREDENTIALS  (tùy chọn; mặc định dùng SA của Job)

Ghi ra OUTPUT_URI (final.mp4) và OUTPUT_URI(.json) chứa metadata (title/desc/hashtags)
để n8n đọc khi upload YouTube.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

from container_short.pipeline import (
    DEFAULT_PROJECT_ID,
    DEFAULT_SCRATCH_GS_PREFIX,
    PipelineConfig,
    run,
)
from container_short.steps import gcsio


def _log(msg: str) -> None:
    print(f"[short-job] {msg}", flush=True)


def _post_callback(payload: dict) -> None:
    """Báo kết quả về n8n (CALLBACK_URL). Lỗi callback không làm job fail."""
    if not _flag("CALLBACK_ENABLED", False):
        return
    callback_url = os.environ.get("CALLBACK_URL", "").strip()
    if not callback_url:
        return
    try:
        import requests

        resp = requests.post(callback_url, json=payload, timeout=30)
        _log(f"callback {callback_url} → HTTP {resp.status_code}")
    except Exception as e:  # noqa: BLE001
        _log(f"callback FAILED (bỏ qua): {e}")


def _flag(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)).strip())
    except ValueError:
        return default


def _defer_failure_callback() -> bool:
    attempt = _int_env("CLOUD_RUN_TASK_ATTEMPT", 0)
    max_retries = _int_env("JOB_MAX_RETRIES", 0)
    return attempt < max_retries


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, int(round(float(seconds) * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def main() -> int:
    url = os.environ.get("DOUYIN_URL", "").strip()
    output_uri = os.environ.get("OUTPUT_URI", "").strip()
    scratch = os.environ.get("SCRATCH_GS_PREFIX", DEFAULT_SCRATCH_GS_PREFIX).strip()
    project = os.environ.get("GCP_PROJECT_ID", DEFAULT_PROJECT_ID).strip()

    missing = [k for k, v in {
        "DOUYIN_URL": url, "OUTPUT_URI": output_uri,
    }.items() if not v]
    if missing:
        _log(f"ERROR thiếu env: {', '.join(missing)}")
        return 2

    tts_provider = os.environ.get("TTS_PROVIDER", "capcut").strip().lower() or "capcut"
    default_voice = "BV074_streaming" if tts_provider == "capcut" else "vi-VN-Wavenet-B"

    cfg = PipelineConfig(
        project_id=project,
        region=os.environ.get("VERTEX_REGION", "us-central1").strip() or "us-central1",
        service_account_path=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or None,
        scratch_gs_prefix=scratch,
        target_seconds=int(os.environ.get("TARGET_SECONDS", "40")),
        speed=float(os.environ.get("SPEED", "1.0")),
        bgm_gain_db=float(os.environ.get("BGM_GAIN_DB", "-20")),
        enable_bgm=_flag("ENABLE_BGM", True),
        enable_subtitles=_flag("ENABLE_SUBTITLES", False),
        subtitle_margin_v=int(os.environ.get("SUBTITLE_MARGIN_V", "690")),
        enable_subtitle_ocr=_flag("ENABLE_SUBTITLE_OCR", False),
        subtitle_ocr_fps=float(os.environ.get("SUBTITLE_OCR_FPS", "4.0")),
        subtitle_sync_mode=os.environ.get("SUBTITLE_SYNC_MODE", "gemini").strip() or "gemini",
        strict_subtitle_ocr=_flag("STRICT_SUBTITLE_OCR", False),
        subtitle_ocr_provider=os.environ.get(
            "SUBTITLE_OCR_PROVIDER", "tesseract"
        ).strip() or "tesseract",
        subtitle_time_offset_seconds=float(
            os.environ.get("SUBTITLE_TIME_OFFSET_SECONDS", "0")
        ),
        align_dub_to_speech=_flag("ALIGN_DUB_TO_SPEECH", False),
        dub_max_speed=float(os.environ.get("DUB_MAX_SPEED", "1.35")),
        dub_hard_max_speed=float(os.environ.get("DUB_HARD_MAX_SPEED", "1.45")),
        dub_tail_headroom_seconds=float(
            os.environ.get("DUB_TAIL_HEADROOM_SECONDS", "1.0")
        ),
        dub_end_microfit_max_speed=float(
            os.environ.get("DUB_END_MICROFIT_MAX_SPEED", "1.02")
        ),
        dub_tail_pad_seconds=float(os.environ.get("DUB_TAIL_PAD_SECONDS", "0")),
        dub_mode=os.environ.get("DUB_MODE", "timed").strip() or "timed",
        tts_provider=tts_provider,
        tts_fallback_provider=os.environ.get("TTS_FALLBACK_PROVIDER", "google").strip(),
        tts_voice=os.environ.get("TTS_VOICE", default_voice).strip(),
        speaking_rate=float(os.environ.get("SPEAKING_RATE", "1.0")),
        google_tts_voice=os.environ.get("GOOGLE_TTS_VOICE", "vi-VN-Wavenet-B").strip(),
        capcut_resource_id=os.environ.get(
            "CAPCUT_RESOURCE_ID", "7102355709945188865"
        ).strip(),
        capcut_device_json=os.environ.get("CAPCUT_DEVICE_JSON") or None,
        capcut_poll_timeout=int(os.environ.get("CAPCUT_POLL_TIMEOUT", "300")),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip(),
        cookie=os.environ.get("DOUYIN_COOKIE") or None,
    )

    _log(f"START url={url[:60]!r} output={output_uri}")
    work = tempfile.mkdtemp(prefix="shortjob_")
    local_out = os.path.join(work, "final.mp4")

    run_id = os.environ.get("RUN_ID", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()

    try:
        result = run(url, local_out, cfg)
    except Exception as e:  # noqa: BLE001
        if _defer_failure_callback():
            attempt = _int_env("CLOUD_RUN_TASK_ATTEMPT", 0)
            max_retries = _int_env("JOB_MAX_RETRIES", 0)
            _log(
                "FAILED "
                f"attempt={attempt + 1}/{max_retries + 1}; "
                f"skip callback until final attempt: {e}"
            )
            return 1
        _log(f"FAILED: {e}")
        _post_callback(
            {
                "callback_enabled": True,
                "event": "short.failed",
                "ok": False,
                "status": "failed",
                "run_id": run_id,
                "chat_id": chat_id,
                "output_uri": output_uri,
                "error": str(e),
            }
        )
        return 1

    # Upload kết quả + metadata.
    gcsio.upload(local_out, output_uri, content_type="video/mp4")
    meta_uri = output_uri.rsplit(".", 1)[0] + ".json"
    meta_local = os.path.join(work, "final.json")
    with open(meta_local, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
    gcsio.upload(meta_local, meta_uri, content_type="application/json")

    if result.subtitle_cues:
        subtitles_uri = output_uri.rsplit(".", 1)[0] + ".subtitles.json"
        subtitles_local = os.path.join(work, "final.subtitles.json")
        with open(subtitles_local, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "sync_source": result.subtitle_sync_source,
                    "cues": result.subtitle_cues,
                    "voice_sync": result.voice_sync,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        gcsio.upload(
            subtitles_local,
            subtitles_uri,
            content_type="application/json",
        )

        srt_uri = output_uri.rsplit(".", 1)[0] + ".srt"
        srt_local = os.path.join(work, "final.srt")
        with open(srt_local, "w", encoding="utf-8") as f:
            for cue in result.subtitle_cues:
                f.write(f"{int(cue['index']) + 1}\n")
                f.write(
                    f"{_srt_time(cue.get('synced_start', cue['start']))} --> "
                    f"{_srt_time(cue.get('synced_end', cue['end']))}\n"
                )
                f.write(f"{cue['text_vi']}\n")
                f.write(f"[ZH] {cue['text_zh']}\n\n")
        gcsio.upload(srt_local, srt_uri, content_type="application/x-subrip")

    _log(
        f"DONE {result.width}x{result.height} {result.duration:.1f}s → {output_uri} "
        f"(meta: {meta_uri})"
    )

    meta = result.to_dict()
    _post_callback(
        {
            "callback_enabled": True,
            "event": "short.completed",
            "ok": True,
            "status": "completed",
            "run_id": run_id,
            "chat_id": chat_id,
            "output_uri": output_uri,
            "meta_uri": meta_uri,
            "download_url": os.environ.get("DOWNLOAD_URL", "").strip(),
            "duration": result.duration,
            "width": result.width,
            "height": result.height,
            "title_vi": result.title_vi,
            "description_vi": result.description_vi,
            "hashtags": result.hashtags,
            "youtube_title": meta.get("youtube_title", ""),
            "youtube_description": meta.get("youtube_description", ""),
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
