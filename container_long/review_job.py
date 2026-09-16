"""Cloud Run job that cuts confirmed ad spans out of an assembled long video.

Triggered by the coordinator in ``MODE=cut`` after a human confirms the spans
to remove via Telegram. Reads ``final-long.*`` for a batch, removes the spans
(frame-accurate re-encode), shifts the subtitle/chapter/metadata timeline to
match, backs up the previous artifacts, and overwrites the final files in
place. The coordinator handles re-detection + callback after this job finishes.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

from container_long.editing import (
    keep_segments,
    normalize_cut_spans,
    shift_chapter_lines,
    shift_subtitle_lines,
)
from container_long.subtitles import write_vietnamese_srt
from container_short.steps import ffmpeg_ops, gcsio


def _load_json(uri: str) -> dict:
    work = tempfile.mkdtemp(prefix="long_review_read_")
    path = os.path.join(work, "data.json")
    gcsio.download(uri, path)
    with open(path, encoding="utf-8-sig") as handle:
        return json.load(handle)


def _final_uris(output_prefix: str) -> dict[str, str]:
    base = f"{output_prefix}/final/final-long"
    return {
        "video": f"{base}.mp4",
        "metadata": f"{base}.json",
        "subtitle": f"{base}.srt",
        "chapters": f"{base}.chapters.txt",
    }


def _backup_finals(uris: dict[str, str], backup_prefix: str) -> None:
    names = {
        "video": "final-long.mp4",
        "metadata": "final-long.json",
        "subtitle": "final-long.srt",
        "chapters": "final-long.chapters.txt",
    }
    for key, uri in uris.items():
        try:
            if gcsio.exists(uri):
                gcsio.copy(uri, f"{backup_prefix}/{names[key]}")
        except Exception as exc:  # noqa: BLE001 - backup is best-effort
            print(
                f"[long-review] backup failed for {uri}: {exc}",
                flush=True,
            )


def _run() -> dict:
    output_prefix = os.environ.get("OUTPUT_PREFIX", "").strip().rstrip("/")
    if not output_prefix:
        raise ValueError("OUTPUT_PREFIX is required")
    try:
        raw_spans = json.loads(os.environ.get("CUT_SPANS", "[]"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid CUT_SPANS json: {exc}") from exc
    cut_spans = [[float(pair[0]), float(pair[1])] for pair in raw_spans]
    if not cut_spans:
        raise ValueError("CUT_SPANS must contain at least one [start, end] span")

    uris = _final_uris(output_prefix)
    work = tempfile.mkdtemp(prefix="long_review_")
    src_mp4 = os.path.join(work, "src.mp4")
    gcsio.download(uris["video"], src_mp4)
    metadata = _load_json(uris["metadata"])

    duration = float(
        metadata.get("duration") or ffmpeg_ops.duration_seconds(src_mp4)
    )
    spans = normalize_cut_spans(cut_spans, duration=duration)
    if not spans:
        raise ValueError("no valid cut spans within the video duration")
    kept = keep_segments(spans, duration)
    if not kept:
        raise ValueError("cut spans would remove the entire video")

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    _backup_finals(uris, f"{output_prefix}/final/pre-edit-{timestamp}")

    # Video: frame-accurate re-encode keeping only the surviving segments.
    out_mp4 = os.path.join(work, "final-long.mp4")
    ffmpeg_ops.remove_spans(src_mp4, out_mp4, kept)
    new_duration = ffmpeg_ops.duration_seconds(out_mp4)

    # Subtitles: shift the structured timeline, then rewrite the SRT.
    subtitles = metadata.get("subtitles") or []
    shifted_subtitles = shift_subtitle_lines(
        [
            (item["start"], item["end"], item.get("text_vi", ""))
            for item in subtitles
        ],
        spans,
        duration=duration,
    )
    out_srt = os.path.join(work, "final-long.srt")
    with open(out_srt, "w", encoding="utf-8") as handle:
        write_vietnamese_srt(handle, shifted_subtitles)
    metadata["subtitles"] = [
        {"start": start, "end": end, "text_vi": text}
        for start, end, text in shifted_subtitles
    ]

    # Chapters: shift the HH:MM:SS markers.
    chapters = shift_chapter_lines(
        metadata.get("chapters") or [], spans, duration=duration
    )
    metadata["chapters"] = chapters
    out_chapters = os.path.join(work, "final-long.chapters.txt")
    with open(out_chapters, "w", encoding="utf-8") as handle:
        handle.write("\n".join(chapters) + "\n")

    removed_seconds = round(duration - new_duration, 3)
    metadata["duration"] = new_duration
    edits = list(metadata.get("edits") or [])
    edits.append({
        "type": "cut",
        "at": timestamp,
        "spans": [list(span) for span in spans],
        "removed_seconds": removed_seconds,
    })
    metadata["edits"] = edits

    out_meta = os.path.join(work, "final-long.json")
    with open(out_meta, "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)

    gcsio.upload(out_mp4, uris["video"], content_type="video/mp4")
    gcsio.upload(out_meta, uris["metadata"], content_type="application/json")
    gcsio.upload(out_srt, uris["subtitle"], content_type="application/x-subrip")
    gcsio.upload(out_chapters, uris["chapters"], content_type="text/plain")

    result = {
        "ok": True,
        "output_prefix": output_prefix,
        "applied_cuts": [list(span) for span in spans],
        "duration": new_duration,
        "removed_seconds": removed_seconds,
        "backup_prefix": f"{output_prefix}/final/pre-edit-{timestamp}",
    }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main() -> int:
    try:
        _run()
    except Exception as exc:  # noqa: BLE001
        print(f"[long-review] FAILED: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
