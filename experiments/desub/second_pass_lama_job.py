#!/usr/bin/env python3
"""Run a cue-timed LaMA cleanup pass and burn reviewed Vietnamese subtitles."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from google.cloud import storage


sys.path.insert(0, "/app/experiments/desub")
import prototype as desub  # noqa: E402


WORK_DIR = Path(os.environ.get("WORK_DIR", "/tmp/desub_second_pass"))


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value.rstrip("/")


PROJECT_ID = required_env("GCP_PROJECT_ID")


def parse_gs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"Expected gs:// URI, got {uri}")
    bucket, _, blob = uri[5:].partition("/")
    if not bucket or not blob:
        raise ValueError(f"Invalid GCS URI: {uri}")
    return bucket, blob


def download(client: storage.Client, uri: str, path: Path) -> None:
    bucket_name, blob_name = parse_gs_uri(uri)
    path.parent.mkdir(parents=True, exist_ok=True)
    client.bucket(bucket_name).blob(blob_name).download_to_filename(str(path))


def upload(client: storage.Client, path: Path, uri: str, content_type: str) -> None:
    bucket_name, blob_name = parse_gs_uri(uri)
    client.bucket(bucket_name).blob(blob_name).upload_from_filename(
        str(path),
        content_type=content_type,
    )


def rect_tuple(value: dict[str, Any]) -> tuple[int, int, int, int]:
    return int(value["x1"]), int(value["y1"]), int(value["x2"]), int(value["y2"])


def temporal_gap(start: float, end: float, other_start: float, other_end: float) -> float:
    if end < other_start:
        return other_start - end
    if other_end < start:
        return start - other_end
    return 0.0


def cue_clusters(
    cues: list[dict[str, Any]],
    mask_payload: dict[str, Any],
    meta: desub.VideoMeta,
) -> tuple[list[desub.MaskCluster], list[dict[str, Any]]]:
    source_clusters = mask_payload.get("subtitle_clusters") or []
    fixed_rect_raw = os.environ.get("SECOND_PASS_FIXED_RECT", "").strip()
    fixed_rect: tuple[int, int, int, int] | None = None
    if fixed_rect_raw:
        values = [int(value.strip()) for value in fixed_rect_raw.replace(";", ",").split(",")]
        if len(values) != 4:
            raise ValueError("SECOND_PASS_FIXED_RECT must contain x1,y1,x2,y2")
        fixed_rect = tuple(values)  # type: ignore[assignment]
    if not source_clusters and fixed_rect is None:
        raise ValueError("mask payload has no subtitle_clusters")

    pad = float(os.environ.get("SECOND_PASS_CUE_PAD_SECONDS", "0.12"))
    clusters: list[desub.MaskCluster] = []
    matches: list[dict[str, Any]] = []
    for index, cue in enumerate(cues):
        start = float(cue["start"])
        end = float(cue["end"])
        cue_y = float(cue.get("center_y", 0.735)) * meta.height
        if fixed_rect is not None:
            source_index = -1
            source_start = start
            source_end = end
            rects = (fixed_rect,)
        else:
            ranked: list[tuple[float, int, dict[str, Any]]] = []
            for source_index, source in enumerate(source_clusters):
                source_start = float(source["t_start"])
                source_end = float(source["t_end"])
                overlap = max(0.0, min(end, source_end) - max(start, source_start))
                gap = temporal_gap(start, end, source_start, source_end)
                rects = [rect_tuple(rect) for rect in source.get("rects") or []]
                if not rects or (overlap <= 0.0 and gap > 0.75):
                    continue
                y_distance = min(abs(((rect[1] + rect[3]) / 2.0) - cue_y) for rect in rects)
                score = overlap * 1000.0 - gap * 100.0 - y_distance * 0.01
                ranked.append((score, source_index, source))

            if not ranked:
                raise ValueError(f"No source mask cluster near cue {index}: {start}-{end}")
            _, source_index, source = max(ranked, key=lambda item: item[0])
            source_start = float(source["t_start"])
            source_end = float(source["t_end"])
            rects = tuple(rect_tuple(rect) for rect in source.get("rects") or [])
        write_start = max(0.0, start - pad)
        write_end = min(meta.duration, end + pad)
        context_pad = max(0.75, pad)
        clusters.append(desub.MaskCluster(
            kind="subtitle-pass2",
            t_start=write_start,
            t_end=write_end,
            context_start=max(0.0, write_start - context_pad),
            context_end=min(meta.duration, write_end + context_pad),
            rects=rects,
            span_count=1,
        ))
        matches.append({
            "cue_index": index,
            "cue_start": start,
            "cue_end": end,
            "mask_start": write_start,
            "mask_end": write_end,
            "source_cluster_index": source_index,
            "source_cluster_start": source_start,
            "source_cluster_end": source_end,
            "rects": [list(rect) for rect in rects],
        })
    return clusters, matches


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def stream_report(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_name,codec_type,width,height,avg_frame_rate,channels",
            "-show_entries",
            "format=duration,size",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return json.loads(proc.stdout)


def main() -> int:
    started = time.time()
    base_uri = required_env("SECOND_PASS_BASE_URI")
    cues_uri = required_env("SECOND_PASS_CUES_URI")
    mask_uri = required_env("SECOND_PASS_MASK_URI")
    output_uri = required_env("SECOND_PASS_OUTPUT_URI")
    clean_uri = os.environ.get("SECOND_PASS_CLEAN_URI", "").strip()
    report_uri = os.environ.get("SECOND_PASS_REPORT_URI", "").strip()

    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    WORK_DIR.mkdir(parents=True)
    base_path = WORK_DIR / "lama_base.mp4"
    cues_path = WORK_DIR / "visub_cues_final.json"
    mask_path = WORK_DIR / "mask.json"
    clean_path = WORK_DIR / "lama_pass2_clean.mp4"
    ass_path = WORK_DIR / "visub_final.ass"
    output_path = WORK_DIR / "douyin_lama_visub_final.mp4"
    report_path = WORK_DIR / "second_pass_report.json"

    client = storage.Client(project=PROJECT_ID)
    print(json.dumps({"stage": "download", "base_uri": base_uri}), flush=True)
    download(client, base_uri, base_path)
    download(client, cues_uri, cues_path)
    download(client, mask_uri, mask_path)

    cue_payload = json.loads(cues_path.read_text(encoding="utf-8"))
    all_cues = cue_payload.get("cues") or []
    if not all_cues:
        raise ValueError("final cue payload is empty")
    target_raw = os.environ.get("SECOND_PASS_TARGET_CUE_INDEXES", "").strip()
    target_indexes = (
        [int(value.strip()) for value in target_raw.replace(";", ",").split(",") if value.strip()]
        if target_raw else list(range(len(all_cues)))
    )
    if any(index < 0 or index >= len(all_cues) for index in target_indexes):
        raise ValueError(f"target cue index is out of range: {target_indexes}")
    cues = [all_cues[index] for index in target_indexes]
    if any(int(cue.get("line_count", 0)) not in {1, 2} for cue in cues):
        raise ValueError("all cues must have a reviewed one- or two-line layout")
    mask_payload = json.loads(mask_path.read_text(encoding="utf-8"))
    meta = desub.ffprobe(base_path)
    clusters, matches = cue_clusters(cues, mask_payload, meta)

    # A clean first-pass frame often contains no subtitle stroke. Disabling the
    # fallback prevents LaMA from replacing the entire detection rectangle.
    os.environ["DESUB_STROKE_MASK_ENABLED"] = "true"
    os.environ["DESUB_STROKE_FALLBACK_MIN_RATIO"] = "0"
    os.environ.setdefault("DESUB_STATIC_MASK_MIN_HITS", "2")
    os.environ.setdefault("DESUB_STATIC_MASK_MIN_HIT_RATIO", "0.02")
    os.environ.setdefault("DESUB_VISUB_WRAP_CHARS", "24")
    os.environ.setdefault("DESUB_VISUB_OUTPUT_CRF", "18")

    print(json.dumps({
        "stage": "lama-pass2",
        "cue_count": len(cues),
        "target_cue_indexes": target_indexes,
        "cluster_count": len(clusters),
        "duration": meta.duration,
    }), flush=True)
    lama_report = desub.apply_timed_clusters(
        model_name="lama",
        source_clip=base_path,
        output_clip=clean_path,
        clusters=clusters,
        meta=meta,
        work_dir=WORK_DIR / "inpaint",
    )

    desub.write_visub_ass(ass_path, cues, meta)
    desub.burn_visub_subtitles(clean_path, ass_path, output_path)
    output_probe = stream_report(output_path)
    output_duration = float(output_probe.get("format", {}).get("duration") or 0.0)
    stream_types = {stream.get("codec_type") for stream in output_probe.get("streams") or []}
    if abs(output_duration - meta.duration) > 0.15:
        raise ValueError(f"duration changed: {meta.duration} -> {output_duration}")
    if not {"video", "audio"}.issubset(stream_types):
        raise ValueError(f"final output is missing a stream: {sorted(stream_types)}")

    report = {
        "ok": True,
        "base_uri": base_uri,
        "cues_uri": cues_uri,
        "mask_uri": mask_uri,
        "output_uri": output_uri,
        "clean_uri": clean_uri or None,
        "cue_count": len(cues),
        "one_line_cues": sum(1 for cue in cues if int(cue["line_count"]) == 1),
        "two_line_cues": sum(1 for cue in cues if int(cue["line_count"]) == 2),
        "duration": meta.duration,
        "mask_matches": matches,
        "lama": lama_report,
        "output_probe": output_probe,
        "output_sha256": file_sha256(output_path),
        "elapsed_seconds": time.time() - started,
    }
    desub.write_json(report_path, report)

    print(json.dumps({"stage": "upload", "output_uri": output_uri}), flush=True)
    upload(client, output_path, output_uri, "video/mp4")
    if clean_uri:
        upload(client, clean_path, clean_uri, "video/mp4")
    if report_uri:
        upload(client, report_path, report_uri, "application/json")
        ass_uri = report_uri.rsplit("/", 1)[0] + "/visub_final.ass"
        upload(client, ass_path, ass_uri, "text/x-ssa")
    print(json.dumps({
        "stage": "completed",
        "output_uri": output_uri,
        "sha256": report["output_sha256"],
        "elapsed_seconds": report["elapsed_seconds"],
    }), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
