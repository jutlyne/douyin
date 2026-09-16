from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

import prototype as desub


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def _upload_json(payload: dict[str, Any], uri: str, work: Path) -> None:
    path = work / "visub_cues_chunked.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    desub.upload_file(path, uri, content_type="application/json")


def _download_json(uri: str) -> dict[str, Any]:
    from google.cloud import storage

    bucket_name, blob_name = desub.parse_gs_uri(uri)
    text = storage.Client(project=desub.PROJECT_ID).bucket(bucket_name).blob(blob_name).download_as_text()
    return json.loads(text)


def _analyze_chunk(
    *,
    uri: str,
    duration: float,
    project_id: str,
    region: str,
    model: str,
    timeout: int,
    known_present: bool = False,
) -> list[dict[str, Any]]:
    from google import genai  # type: ignore
    from google.genai import types  # type: ignore
    from pydantic import BaseModel, Field

    class Line(BaseModel):
        start: float
        end: float
        text_zh: str = ""
        text_vi: str = ""
        center_x: float = 0.5
        center_y: float = 0.735

    class Result(BaseModel):
        lines: list[Line] = Field(default_factory=list)

    presence_note = (
        "This segment is known to contain matching lower Chinese dialogue subtitles. "
        "Re-check the lower caption band carefully before returning an empty list."
        if known_present
        else ""
    )
    prompt = f"""Inspect this {duration:.3f}-second video segment frame by frame.

Extract every change of the white Chinese dialogue subtitle with a dark outline that appears in the lower half of the video. Ignore yellow price totals, product labels, signs, watermarks, scene text, and speech that has no visible Chinese subtitle.

Requirements:
- Timestamps are relative to this segment, not the original video.
- start/end must match the actual visible frames to the nearest 0.05 second.
- Preserve every interval with no lower dialogue subtitle; never fill a blank gap.
- One JSON line per distinct visible Chinese subtitle. Do not merge consecutive captions.
- If the exact same caption stays visible across adjacent frames or a camera cut, emit ONE
  continuous line for its whole visible interval. Never split an unchanged caption into
  duplicate adjacent records.
- When the visible caption changes, end the old line and start the new line at that frame.
  Never repeat the old text over frames that already show the next caption.
- text_zh must transcribe exactly what is visible, without inventing dialogue.
- text_vi must be complete, natural spoken Vietnamese and preserve every meaning in text_zh.
- Never delete facts or shorten the message merely to fit its original screen duration. Prefer
  compact natural wording only when it keeps the full meaning; downstream narration uses a
  fixed speaking rate and may ripple later Vietnamese lines without changing video speed.
- center_x and center_y are the normalized center of the original Chinese subtitle box.
- If there is no matching Chinese dialogue subtitle, return lines=[].
{presence_note}
"""
    client = genai.Client(vertexai=True, project=project_id, location=region)
    response = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_uri(file_uri=uri, mime_type="video/mp4"),
            prompt,
        ],
        config=types.GenerateContentConfig(
            temperature=0.0,
            thinking_config=types.ThinkingConfig(thinking_budget=4096 if known_present else 2048),
            max_output_tokens=6144,
            response_mime_type="application/json",
            response_schema=Result,
            http_options=types.HttpOptions(timeout=timeout * 1000),
        ),
    )
    parsed = response.parsed
    if parsed is None:
        parsed = Result(**json.loads(response.text or "{}"))

    lines: list[dict[str, Any]] = []
    for item in parsed.lines:
        start = max(0.0, min(float(item.start), duration))
        end = max(0.0, min(float(item.end), duration))
        text_zh = " ".join(str(item.text_zh or "").split())
        text_vi = " ".join(str(item.text_vi or "").split())
        if not text_zh or not text_vi or end <= start:
            continue
        lines.append({
            "start": start,
            "end": end,
            "text_zh": text_zh,
            "text_vi": text_vi,
            "center_x": max(0.0, min(float(item.center_x), 1.0)),
            "center_y": max(0.0, min(float(item.center_y), 1.0)),
        })
    return lines


def _cluster_intervals(mask: dict[str, Any], width: int, height: int) -> list[dict[str, float]]:
    intervals: list[dict[str, float]] = []
    for cluster in mask.get("subtitle_clusters") or []:
        rects = cluster.get("rects") or []
        if not rects:
            continue
        x1 = min(float(rect["x1"]) for rect in rects)
        y1 = min(float(rect["y1"]) for rect in rects)
        x2 = max(float(rect["x2"]) for rect in rects)
        y2 = max(float(rect["y2"]) for rect in rects)
        intervals.append({
            "start": float(cluster["t_start"]),
            "end": float(cluster["t_end"]),
            "center_x": ((x1 + x2) / 2.0) / max(1, width),
            "center_y": ((y1 + y2) / 2.0) / max(1, height),
        })
    return sorted(intervals, key=lambda item: item["start"])


def _gate_to_detected_subtitles(
    cues: list[dict[str, Any]],
    intervals: list[dict[str, float]],
    duration: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for cue in sorted(cues, key=lambda item: (float(item["start"]), float(item["end"]))):
        start = max(0.0, min(float(cue["start"]), duration))
        end = max(0.0, min(float(cue["end"]), duration))
        candidates: list[tuple[float, float, dict[str, float]]] = []
        for interval in intervals:
            overlap = max(0.0, min(end, interval["end"]) - max(start, interval["start"]))
            distance = max(interval["start"] - end, start - interval["end"], 0.0)
            if overlap > 0.0 or distance <= 0.35:
                candidates.append((overlap, -distance, interval))
        if not candidates:
            rejected.append({**cue, "reason": "no_detected_subtitle_interval"})
            continue
        _, _, interval = max(candidates, key=lambda item: (item[0], item[1]))
        gated = dict(cue)
        gated["start"] = max(start, interval["start"])
        gated["end"] = min(end, interval["end"])
        gated["center_x"] = interval["center_x"]
        gated["center_y"] = interval["center_y"]
        if float(gated["end"]) - float(gated["start"]) < 0.12:
            rejected.append({**cue, "reason": "too_short_after_mask_gate"})
            continue
        kept.append(gated)

    normalized: list[dict[str, Any]] = []
    for cue in kept:
        if normalized and float(cue["start"]) < float(normalized[-1]["end"]):
            previous = normalized[-1]
            boundary = (float(previous["end"]) + float(cue["start"])) / 2.0
            previous["end"] = max(float(previous["start"]) + 0.12, boundary)
            cue["start"] = max(float(previous["end"]), float(cue["start"]))
        if float(cue["end"]) - float(cue["start"]) >= 0.12:
            normalized.append(cue)
        else:
            rejected.append({**cue, "reason": "too_short_after_overlap_normalization"})
    return normalized, rejected


def _retry_chunk() -> None:
    source_payload = _download_json(_env("CHUNKED_EXISTING_CUES_URI"))
    retry_index = int(_env("CHUNKED_RETRY_INDEX"))
    report = next(
        (item for item in source_payload.get("chunks") or [] if int(item.get("index", -1)) == retry_index),
        None,
    )
    if report is None:
        raise RuntimeError(f"retry chunk report not found: {retry_index}")

    output_uri = _env("CHUNKED_OUTPUT_URI")
    cues_uri = _env("CHUNKED_CUES_URI")
    chunk_prefix = _env("CHUNKED_WORK_PREFIX").rstrip("/")
    base_uri = _env("CHUNKED_BASE_URI")
    mask_uri = _env("CHUNKED_MASK_URI")
    project_id = os.environ.get("GCP_PROJECT_ID", desub.PROJECT_ID)
    region = os.environ.get("VERTEX_REGION", "global")
    model = os.environ.get("GEMINI_MODEL", "gemini-3.1-pro-preview")
    timeout = int(os.environ.get("DESUB_VISUB_GEMINI_TIMEOUT_SECONDS", "900"))
    overlap_seconds = float(os.environ.get("CHUNKED_OVERLAP_SECONDS", "2"))
    core_start = float(report["core_start"])
    core_end = float(report["core_end"])
    original_segment_start = float(report["segment_start"])
    original_segment_end = float(report["segment_end"])
    split = (core_start + core_end) / 2.0

    with tempfile.TemporaryDirectory(prefix="desub-chunk-retry-") as temp_dir:
        work = Path(temp_dir)
        original_chunk = work / "original_chunk.mp4"
        desub.download_source_video(source_uri=str(report["uri"]), douyin_url="", out_path=original_chunk)
        retry_cues: list[dict[str, Any]] = []
        retry_reports: list[dict[str, Any]] = []
        for part_index, (part_core_start, part_core_end) in enumerate(
            ((core_start, split), (split, core_end))
        ):
            part_segment_start = max(original_segment_start, part_core_start - overlap_seconds)
            part_segment_end = min(original_segment_end, part_core_end + overlap_seconds)
            local_start = part_segment_start - original_segment_start
            part_duration = part_segment_end - part_segment_start
            part = work / f"retry_{retry_index:02d}_{part_index}.mp4"
            desub.cmd([
                "ffmpeg", "-y", "-ss", f"{local_start:.3f}", "-i", str(original_chunk),
                "-t", f"{part_duration:.3f}", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "20", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(part),
            ])
            part_uri = f"{chunk_prefix}/{part.name}"
            desub.upload_file(part, part_uri, content_type="video/mp4")
            local_cues = _analyze_chunk(
                uri=part_uri,
                duration=part_duration,
                project_id=project_id,
                region=region,
                model=model,
                timeout=timeout,
                known_present=True,
            )
            accepted = 0
            for cue in local_cues:
                absolute = dict(cue)
                absolute["start"] = float(cue["start"]) + part_segment_start
                absolute["end"] = float(cue["end"]) + part_segment_start
                midpoint = (float(absolute["start"]) + float(absolute["end"])) / 2.0
                is_last = part_index == 1
                if part_core_start <= midpoint < part_core_end or (is_last and midpoint <= part_core_end):
                    retry_cues.append(absolute)
                    accepted += 1
            retry_reports.append({
                "part": part_index,
                "core_start": part_core_start,
                "core_end": part_core_end,
                "segment_start": part_segment_start,
                "segment_end": part_segment_end,
                "model_cues": len(local_cues),
                "accepted_cues": accepted,
                "uri": part_uri,
            })
            desub.log(
                "chunked_visub_retry_analyzed",
                retry_index=retry_index,
                part=part_index,
                model_cues=len(local_cues),
                accepted=accepted,
            )

        existing = []
        for cue in source_payload.get("cues") or []:
            midpoint = (float(cue["start"]) + float(cue["end"])) / 2.0
            if midpoint < core_start or midpoint >= core_end:
                existing.append(dict(cue))
        combined = existing + retry_cues
        mask = _download_json(mask_uri)
        source_meta = mask.get("clip_meta") or {}
        width = int(source_meta.get("width") or 720)
        height = int(source_meta.get("height") or 1280)
        duration = float(mask.get("clip_duration_seconds") or original_segment_end)
        intervals = _cluster_intervals(mask, width, height)
        cues, rejected = _gate_to_detected_subtitles(combined, intervals, duration)
        payload = dict(source_payload)
        payload.update({
            "base_uri": base_uri,
            "mask_uri": mask_uri,
            "retry_index": retry_index,
            "retry_parts": retry_reports,
            "raw_cue_count": len(combined),
            "cue_count": len(cues),
            "rejected": rejected,
            "cues": cues,
        })
        _upload_json(payload, cues_uri, work)
        if not retry_cues:
            raise RuntimeError(f"retry chunk {retry_index} still produced no accepted cues")

        base = work / "clip_lama.mp4"
        desub.download_source_video(source_uri=base_uri, douyin_url="", out_path=base)
        base_meta = desub.ffprobe(base)
        ass = work / "visub_chunked_retry.ass"
        output = work / "clip_lama_visub_chunked_retry.mp4"
        desub.write_visub_ass(ass, cues, base_meta)
        desub.burn_visub_subtitles(base, ass, output)
        desub.upload_file(output, output_uri, content_type="video/mp4")
        desub.upload_file(ass, f"{chunk_prefix}/visub_chunked_retry.ass", content_type="text/plain")
        desub.log(
            "chunked_visub_retry_completed",
            retry_index=retry_index,
            output_uri=output_uri,
            cues_uri=cues_uri,
            cue_count=len(cues),
            retry_cue_count=len(retry_cues),
            bytes=output.stat().st_size,
        )


def main() -> None:
    if os.environ.get("CHUNKED_RETRY_INDEX", "").strip():
        _retry_chunk()
        return
    source_uri = _env("CHUNKED_SOURCE_URI")
    base_uri = _env("CHUNKED_BASE_URI")
    mask_uri = _env("CHUNKED_MASK_URI")
    output_uri = _env("CHUNKED_OUTPUT_URI")
    cues_uri = _env("CHUNKED_CUES_URI")
    chunk_prefix = _env("CHUNKED_WORK_PREFIX").rstrip("/")
    project_id = os.environ.get("GCP_PROJECT_ID", desub.PROJECT_ID)
    region = os.environ.get("VERTEX_REGION", "global")
    model = os.environ.get("GEMINI_MODEL", "gemini-3.1-pro-preview")
    core_seconds = float(os.environ.get("CHUNKED_CORE_SECONDS", "30"))
    overlap_seconds = float(os.environ.get("CHUNKED_OVERLAP_SECONDS", "2"))
    timeout = int(os.environ.get("DESUB_VISUB_GEMINI_TIMEOUT_SECONDS", "900"))

    with tempfile.TemporaryDirectory(prefix="desub-chunked-") as temp_dir:
        work = Path(temp_dir)
        source = work / "source.mp4"
        desub.download_source_video(source_uri=source_uri, douyin_url="", out_path=source)
        meta = desub.ffprobe(source)
        mask = _download_json(mask_uri)
        intervals = _cluster_intervals(mask, meta.width, meta.height)
        all_cues: list[dict[str, Any]] = []
        chunk_reports: list[dict[str, Any]] = []
        chunk_count = int(math.ceil(meta.duration / core_seconds))

        for index in range(chunk_count):
            core_start = index * core_seconds
            core_end = min(meta.duration, core_start + core_seconds)
            segment_start = max(0.0, core_start - overlap_seconds)
            segment_end = min(meta.duration, core_end + overlap_seconds)
            segment_duration = segment_end - segment_start
            chunk = work / f"chunk_{index:02d}.mp4"
            desub.cmd([
                "ffmpeg", "-y", "-ss", f"{segment_start:.3f}", "-i", str(source),
                "-t", f"{segment_duration:.3f}", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "20", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(chunk),
            ])
            chunk_uri = f"{chunk_prefix}/{chunk.name}"
            desub.upload_file(chunk, chunk_uri, content_type="video/mp4")
            desub.log(
                "chunked_visub_analyzing",
                index=index,
                count=chunk_count,
                core_start=core_start,
                core_end=core_end,
                uri=chunk_uri,
            )
            known_present = any(
                min(core_end, interval["end"]) - max(core_start, interval["start"]) >= 0.12
                for interval in intervals
            )
            local_cues = _analyze_chunk(
                uri=chunk_uri,
                duration=segment_duration,
                project_id=project_id,
                region=region,
                model=model,
                timeout=timeout,
                known_present=known_present,
            )
            retry_count = 0
            if known_present and not local_cues:
                retry_count = 1
                desub.log("chunked_visub_retrying_empty", index=index, uri=chunk_uri)
                local_cues = _analyze_chunk(
                    uri=chunk_uri,
                    duration=segment_duration,
                    project_id=project_id,
                    region=region,
                    model=model,
                    timeout=timeout,
                    known_present=True,
                )
            accepted = 0
            for cue in local_cues:
                absolute = dict(cue)
                absolute["start"] = float(cue["start"]) + segment_start
                absolute["end"] = float(cue["end"]) + segment_start
                midpoint = (float(absolute["start"]) + float(absolute["end"])) / 2.0
                in_core = core_start <= midpoint < core_end or (
                    index == chunk_count - 1 and core_start <= midpoint <= core_end
                )
                if in_core:
                    all_cues.append(absolute)
                    accepted += 1
            chunk_reports.append({
                "index": index,
                "core_start": core_start,
                "core_end": core_end,
                "segment_start": segment_start,
                "segment_end": segment_end,
                "model_cues": len(local_cues),
                "accepted_cues": accepted,
                "retry_count": retry_count,
                "known_present": known_present,
                "uri": chunk_uri,
            })
            desub.log("chunked_visub_analyzed", index=index, model_cues=len(local_cues), accepted=accepted)

        cues, rejected = _gate_to_detected_subtitles(all_cues, intervals, meta.duration)
        payload = {
            "model": model,
            "region": region,
            "source_uri": source_uri,
            "base_uri": base_uri,
            "mask_uri": mask_uri,
            "chunk_core_seconds": core_seconds,
            "chunk_overlap_seconds": overlap_seconds,
            "chunks": chunk_reports,
            "raw_cue_count": len(all_cues),
            "cue_count": len(cues),
            "rejected": rejected,
            "cues": cues,
        }
        _upload_json(payload, cues_uri, work)
        if not cues:
            raise RuntimeError("chunked analysis produced no mask-gated cues")

        base = work / "clip_lama.mp4"
        desub.download_source_video(source_uri=base_uri, douyin_url="", out_path=base)
        base_meta = desub.ffprobe(base)
        ass = work / "visub_chunked.ass"
        output = work / "clip_lama_visub_chunked.mp4"
        desub.write_visub_ass(ass, cues, base_meta)
        desub.burn_visub_subtitles(base, ass, output)
        desub.upload_file(output, output_uri, content_type="video/mp4")
        desub.upload_file(ass, f"{chunk_prefix}/visub_chunked.ass", content_type="text/plain")
        desub.log(
            "chunked_visub_completed",
            output_uri=output_uri,
            cues_uri=cues_uri,
            cue_count=len(cues),
            rejected_count=len(rejected),
            bytes=output.stat().st_size,
        )


if __name__ == "__main__":
    main()
