from __future__ import annotations

import json
import os
import sys
import tempfile

from container_long.gemini_long import compilation_metadata
from container_long.models import BatchItem, LongDialogueLine
from container_long.subtitles import write_vietnamese_srt
from container_long.text_cleanup import (
    normalize_long_text_value,
    normalize_long_vi_text,
)
from container_long.utils import non_overlapping_dialogue_lines
from container_short.steps import ffmpeg_ops, gcsio


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} environment variable is required")
    return value


def _chapter_time(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _callback(payload: dict) -> None:
    enabled = os.environ.get("CALLBACK_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on"
    )
    url = os.environ.get("CALLBACK_URL", "").strip()
    if not enabled or not url:
        return
    import requests

    response = requests.post(url, json=payload, timeout=30)
    print(f"[long-assembler] callback HTTP {response.status_code}", flush=True)


def _run() -> dict:
    manifest_uri = os.environ.get("MANIFEST_URI", "").strip()
    output_uri = os.environ.get("OUTPUT_URI", "").strip()
    batch_id = os.environ.get("BATCH_ID", "").strip()
    if not manifest_uri or not output_uri:
        raise ValueError("MANIFEST_URI and OUTPUT_URI are required")

    work = tempfile.mkdtemp(prefix="long_assembler_")
    manifest_path = os.path.join(work, "manifest.json")
    gcsio.download(manifest_uri, manifest_path)
    with open(manifest_path, encoding="utf-8-sig") as handle:
        manifest = json.load(handle)
    items = sorted(
        [BatchItem.from_dict(item) for item in manifest.get("items", [])],
        key=lambda item: item.index,
    )
    if not items:
        raise RuntimeError("manifest contains no processed items")
    expected_count = int(manifest.get("expected_count") or len(items))
    actual_indices = [item.index for item in items]
    if len(items) != expected_count:
        raise RuntimeError(
            f"manifest has {len(items)}/{expected_count} processed items"
        )
    if actual_indices != list(range(expected_count)):
        raise RuntimeError(
            f"manifest indices must be contiguous from 0: {actual_indices}"
        )

    local_inputs: list[str] = []
    summaries: list[str] = []
    chapter_lines: list[str] = []
    subtitle_blocks: list[tuple[float, float, str]] = []
    source_parts: list[dict] = []
    cursor = 0.0
    for item in items:
        local = os.path.join(work, f"{item.index:03d}.mp4")
        gcsio.download(item.output_uri, local)
        duration = item.duration or ffmpeg_ops.duration_seconds(local)
        title = item.title_vi or f"Phần {item.index + 1}"
        chapter_lines.append(
            normalize_long_vi_text(f"{_chapter_time(cursor)} {title}")
        )
        local_inputs.append(local)
        if item.metadata_uri:
            meta_path = os.path.join(work, f"{item.index:03d}.json")
            gcsio.download(item.metadata_uri, meta_path)
            with open(meta_path, encoding="utf-8-sig") as handle:
                meta = json.load(handle)
            summaries.append(
                normalize_long_vi_text(str(meta.get("description_vi") or title))
            )
            source_parts.append({
                "source_index": item.index,
                "douyin_url": str(meta.get("douyin_url") or ""),
                "source_duration": float(meta.get("source_duration") or 0.0),
                "source_start": float(meta.get("source_start") or 0.0),
                "source_processed_end": float(
                    meta.get("source_processed_end") or 0.0
                ),
                "cliffhanger": meta.get("cliffhanger") or {},
            })
            for chunk in meta.get("chunks", []):
                chunk_start = float(
                    chunk.get("output_start", chunk.get("source_start") or 0.0)
                )
                for line in chunk.get("lines", []):
                    subtitle_blocks.append((
                        cursor + chunk_start + float(line["start"]),
                        cursor + chunk_start + float(line["end"]),
                        normalize_long_vi_text(str(line.get("text_vi") or "")),
                    ))
        cursor += duration

    final_path = os.path.join(work, "final-long.mp4")
    ffmpeg_ops.concat_mp4(local_inputs, final_path)
    gcsio.upload(final_path, output_uri, content_type="video/mp4")
    metadata = compilation_metadata(
        summaries=summaries,
        project_id=_required_env("GCP_PROJECT_ID"),
        region=os.environ.get("VERTEX_REGION", "global"),
        model=os.environ.get("GEMINI_MODEL", "gemini-2.5-pro"),
    )
    subtitle_lines = non_overlapping_dialogue_lines(
        [
            LongDialogueLine(start=start, end=end, text_zh="", text_vi=text)
            for start, end, text in subtitle_blocks
        ]
    )
    metadata.update({
        "batch_id": batch_id,
        "duration": ffmpeg_ops.duration_seconds(final_path),
        "chapters": chapter_lines,
        # Persist the globalized subtitle timeline so the ad-review/cut job and
        # ad detection can read structured lines without re-parsing the SRT.
        "subtitles": [
            {"start": line.start, "end": line.end, "text_vi": line.text_vi}
            for line in subtitle_lines
        ],
        "items": [item.__dict__ for item in items],
        "source_parts": source_parts,
    })
    metadata = normalize_long_text_value(metadata)
    metadata_uri = output_uri.rsplit(".", 1)[0] + ".json"
    chapters_uri = output_uri.rsplit(".", 1)[0] + ".chapters.txt"
    subtitle_uri = output_uri.rsplit(".", 1)[0] + ".srt"
    metadata_path = os.path.join(work, "final-long.json")
    chapters_path = os.path.join(work, "chapters.txt")
    subtitle_path = os.path.join(work, "final-long.srt")
    with open(metadata_path, "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
    with open(chapters_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(chapter_lines) + "\n")
    with open(subtitle_path, "w", encoding="utf-8") as handle:
        write_vietnamese_srt(
            handle,
            (
                (line.start, line.end, line.text_vi)
                for line in subtitle_lines
            ),
        )
    gcsio.upload(metadata_path, metadata_uri, content_type="application/json")
    gcsio.upload(chapters_path, chapters_uri, content_type="text/plain")
    gcsio.upload(
        subtitle_path,
        subtitle_uri,
        content_type="application/x-subrip",
    )
    payload = {
        "event": "long.batch.completed",
        "ok": True,
        "batch_id": batch_id,
        "output_uri": output_uri,
        "metadata_uri": metadata_uri,
        "chapters_uri": chapters_uri,
        "subtitle_uri": subtitle_uri,
        **metadata,
    }
    _callback(payload)
    return payload


def main() -> int:
    batch_id = os.environ.get("BATCH_ID", "").strip()
    try:
        _run()
    except Exception as exc:  # noqa: BLE001
        _callback({
            "event": "long.batch.failed",
            "ok": False,
            "batch_id": batch_id,
            "error": str(exc),
        })
        print(f"[long-assembler] FAILED: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
