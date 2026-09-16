"""Isolated Cloud Run Job entrypoint for OCR-synced Vietnamese subtitles.

This wrapper deliberately keeps the production ``short-maker`` defaults
unchanged. It forces the experimental OCR/subtitle flags on and only allows
outputs below a dedicated GCS prefix.
"""

from __future__ import annotations

import os
import sys


DEFAULT_OUTPUT_PREFIX = (
    "gs://YOUR_GCP_PROJECT-shorts/out/subtitle-dev/"
)


def _log(message: str) -> None:
    print(f"[subtitle-job] {message}", flush=True)


def _normalized_prefix(value: str) -> str:
    prefix = value.strip().rstrip("/") + "/"
    if not prefix.startswith("gs://") or prefix.count("/") < 3:
        raise ValueError("SUBTITLE_OUTPUT_PREFIX must be a gs:// prefix")
    return prefix


def main() -> int:
    douyin_url = os.environ.get("DOUYIN_URL", "").strip()
    output_uri = os.environ.get("OUTPUT_URI", "").strip()

    missing = [
        name
        for name, value in (
            ("DOUYIN_URL", douyin_url),
            ("OUTPUT_URI", output_uri),
        )
        if not value
    ]
    if missing:
        _log(f"ERROR missing env: {', '.join(missing)}")
        return 2

    try:
        allowed_prefix = _normalized_prefix(
            os.environ.get("SUBTITLE_OUTPUT_PREFIX", DEFAULT_OUTPUT_PREFIX)
        )
    except ValueError as exc:
        _log(f"ERROR: {exc}")
        return 2

    if not output_uri.startswith(allowed_prefix):
        _log(
            "ERROR: OUTPUT_URI must stay inside the isolated subtitle prefix "
            f"{allowed_prefix}"
        )
        return 2
    if not output_uri.lower().endswith(".mp4"):
        _log("ERROR: OUTPUT_URI must point to an .mp4 object")
        return 2

    # These values are intentionally forced for this job. Production continues
    # to use container_short.short_job with its existing defaults.
    os.environ["ENABLE_SUBTITLES"] = "true"
    os.environ["ENABLE_SUBTITLE_OCR"] = "true"
    os.environ["SUBTITLE_SYNC_MODE"] = "ocr"
    os.environ["STRICT_SUBTITLE_OCR"] = "true"
    os.environ["SUBTITLE_OCR_PROVIDER"] = "gemini"
    os.environ["ALIGN_DUB_TO_SPEECH"] = "true"
    os.environ.setdefault("DUB_MAX_SPEED", "1.35")
    os.environ.setdefault("DUB_HARD_MAX_SPEED", "1.45")
    os.environ.setdefault("DUB_TAIL_HEADROOM_SECONDS", "1.0")
    os.environ.setdefault("SUBTITLE_TIME_OFFSET_SECONDS", "0")

    # Delay the heavy pipeline import until after the output isolation guard.
    from container_short import short_job

    _log(
        "OCR subtitle mode enabled; "
        f"allowed output prefix={allowed_prefix}"
    )
    return short_job.main()


if __name__ == "__main__":
    sys.exit(main())
