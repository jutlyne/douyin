#!/usr/bin/env python3
"""One-off isolated clean -> Vietnamese dub pipeline for the approved sample.

The Cloud Run job that executes this file uses the already deployed DESUB image.
It never changes the legacy Cover Visub job or its object prefix.  All writes
are create-only beneath one integrated attempt prefix.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable


DEFAULT_PROJECT_ID = "YOUR_GCP_PROJECT"
DEFAULT_VOICE = "BV075_streaming"
DEFAULT_RESOURCE_ID = "7102355803792740865"
REQUIRED_RATE = 1.5
REQUIRED_SAMPLE_RATE = 44_100
REQUIRED_CHANNELS = 2
SHA256_HEX_LENGTH = 64
SUBTITLE_WIDTH_MARKER_BASE = 1_000_000
SUBTITLE_WIDTH_MARKER_STRIDE = 10_000
SUBTITLE_WIDTH_NUMBER_RE = re.compile(
    r"(?<![0-9.])[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value.rstrip("/")


def require_cuda_runtime(torch_module: Any | None = None) -> dict[str, Any]:
    """Fail before downloads/inpainting if the requested L4 runtime is absent."""

    if torch_module is None:
        import torch as torch_module  # type: ignore

    if torch_module.cuda.is_available() is not True:
        raise ValueError("CUDA GPU is required; CPU fallback is forbidden")
    device_count = int(torch_module.cuda.device_count())
    if device_count != 1:
        raise ValueError(f"expected exactly one CUDA GPU, found {device_count}")
    cuda_version = str(torch_module.version.cuda or "")
    expected_prefix = os.environ.get(
        "INTEGRATED_EXPECTED_TORCH_CUDA_PREFIX",
        "12.6",
    ).strip()
    if not cuda_version.startswith(expected_prefix):
        raise ValueError(
            f"unexpected Torch CUDA runtime: {cuda_version} != {expected_prefix}.*"
        )
    return {
        "pass": True,
        "torch_version": str(torch_module.__version__),
        "torch_cuda_version": cuda_version,
        "device_count": device_count,
        "device_name": str(torch_module.cuda.get_device_name(0)),
        "device_capability": list(torch_module.cuda.get_device_capability(0)),
    }


def parse_gs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"expected gs:// URI, got {uri}")
    bucket, separator, blob = uri[5:].partition("/")
    if not bucket or not separator or not blob:
        raise ValueError(f"invalid gs:// URI: {uri}")
    return bucket, blob


def validate_resume_clean_config(
    *,
    uri: str,
    generation: str,
    sha256: str,
) -> dict[str, str] | None:
    """Validate the all-or-nothing pin for an already generated clean video."""

    values = (uri.strip(), generation.strip(), sha256.strip().lower())
    if not any(values):
        return None
    if not all(values):
        raise ValueError(
            "resume clean requires URI, exact generation, and exact SHA-256"
        )
    parse_gs_uri(values[0])
    if not values[1].isdigit() or int(values[1]) <= 0:
        raise ValueError("resume clean generation must be a positive integer")
    if (
        len(values[2]) != SHA256_HEX_LENGTH
        or any(character not in "0123456789abcdef" for character in values[2])
    ):
        raise ValueError("resume clean SHA-256 must be 64 lowercase hex characters")
    return {
        "uri": values[0],
        "generation": values[1],
        "sha256": values[2],
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def validate_cues(payload: Any, *, duration: float) -> list[dict[str, Any]]:
    raw_cues = payload.get("cues") if isinstance(payload, dict) else payload
    if not isinstance(raw_cues, list) or not raw_cues:
        raise ValueError("cue payload must contain a non-empty cues array")

    cues: list[dict[str, Any]] = []
    previous_start = -1.0
    for index, raw in enumerate(raw_cues):
        if not isinstance(raw, dict):
            raise ValueError(f"cue {index} is not an object")
        start = float(raw.get("start", -1.0))
        end = float(raw.get("end", -1.0))
        text_zh = normalized_text(raw.get("text_zh"))
        text_vi = normalized_text(raw.get("text_tts_vi") or raw.get("text_vi"))
        if not text_zh or not text_vi:
            raise ValueError(f"cue {index} has empty source or Vietnamese text")
        if start < previous_start or start < 0.0 or end <= start or end > duration + 0.05:
            raise ValueError(f"cue {index} has invalid timing {start:.6f}-{end:.6f}")
        reviewed_index = int(raw.get("reviewed_index", index))
        if reviewed_index != index:
            raise ValueError(
                f"cue indexes must be contiguous: expected {index}, got {reviewed_index}"
            )
        cues.append(
            {
                **raw,
                "reviewed_index": index,
                "start": start,
                "end": min(end, duration),
                "text_zh": text_zh,
                "text_vi": text_vi,
            }
        )
        previous_start = start
    return cues


def parse_rect(raw: str) -> tuple[int, int, int, int]:
    values = [int(value.strip()) for value in raw.replace(";", ",").split(",") if value.strip()]
    if len(values) != 4:
        raise ValueError("clean rect must contain exactly x1,y1,x2,y2")
    x1, y1, x2, y2 = values
    if x1 < 0 or y1 < 0 or x2 <= x1 or y2 <= y1:
        raise ValueError(f"invalid clean rect: {values}")
    return x1, y1, x2, y2


def validate_tts_identity(
    *,
    voice: str,
    resource_id: str,
    rate: float,
) -> dict[str, Any]:
    """Freeze the one approved CapCut voice identity and provider speed."""

    if voice != DEFAULT_VOICE:
        raise ValueError(f"TTS voice must be exactly {DEFAULT_VOICE}")
    if resource_id != DEFAULT_RESOURCE_ID:
        raise ValueError(
            f"TTS resource id must be exactly {DEFAULT_RESOURCE_ID}"
        )
    if abs(float(rate) - REQUIRED_RATE) > 1e-9:
        raise ValueError(f"provider rate must be exactly {REQUIRED_RATE:.4f}")
    return {
        "voice": voice,
        "resource_id": resource_id,
        "provider_rate": f"{float(rate):.4f}",
    }


def video_sample_frame_ids(
    *,
    frame_count: int,
    source_fps: float,
    sample_fps: float,
) -> list[int]:
    """Return deterministic nearest-frame sampling without retaining frames."""

    if frame_count <= 0 or source_fps <= 0.0:
        raise ValueError("video sampling requires positive frame count and fps")
    if sample_fps <= 0.0 or sample_fps > 24.0:
        raise ValueError("video sample fps must be in (0, 24]")
    duration = frame_count / source_fps
    sample_count = max(1, int(math.ceil(duration * sample_fps)))
    return sorted(
        {
            min(frame_count - 1, int(round(index * source_fps / sample_fps)))
            for index in range(sample_count)
        }
    )


def build_schedule(
    cues: list[dict[str, Any]],
    durations: list[float],
    *,
    video_duration: float,
    gap_seconds: float,
    max_lag_seconds: float = 0.6,
) -> list[dict[str, Any]]:
    if len(cues) != len(durations):
        raise ValueError("cue/TTS duration cardinality mismatch")
    if not cues:
        raise ValueError("cannot schedule an empty cue list")
    if gap_seconds < 0.0:
        raise ValueError("gap_seconds must be non-negative")
    if max_lag_seconds < 0.0:
        raise ValueError("max_lag_seconds must be non-negative")

    schedule: list[dict[str, Any]] = []
    previous_end_sample = 0
    gap_samples = int(round(gap_seconds * REQUIRED_SAMPLE_RATE))
    for index, (cue, clip_duration) in enumerate(zip(cues, durations)):
        clip_duration = float(clip_duration)
        if clip_duration <= 0.0:
            raise ValueError(f"TTS clip {index} has non-positive duration")
        desired_start = float(cue["start"])
        desired_start_sample = int(round(desired_start * REQUIRED_SAMPLE_RATE))
        duration_samples = int(math.ceil(clip_duration * REQUIRED_SAMPLE_RATE))
        actual_start_sample = max(
            desired_start_sample,
            previous_end_sample + (gap_samples if schedule else 0),
        )
        actual_end_sample = actual_start_sample + duration_samples
        actual_start = actual_start_sample / REQUIRED_SAMPLE_RATE
        actual_end = actual_end_sample / REQUIRED_SAMPLE_RATE
        schedule.append(
            {
                "cue_index": index,
                "source_start": desired_start,
                "source_end": float(cue["end"]),
                "start": actual_start,
                "end": actual_end,
                "duration": clip_duration,
                "lag_seconds": actual_start - desired_start,
                "start_sample": actual_start_sample,
                "end_sample": actual_end_sample,
                "duration_samples": duration_samples,
            }
        )
        previous_end_sample = actual_end_sample

    worst_lag_item = max(
        schedule,
        key=lambda item: float(item["lag_seconds"]),
    )
    worst_lag = float(worst_lag_item["lag_seconds"])
    if worst_lag > max_lag_seconds + 1e-6:
        raise ValueError(
            "fixed-rate narration exceeds maximum lag: "
            f"{worst_lag:.6f} > {max_lag_seconds:.6f} "
            f"at cue {worst_lag_item['cue_index']}"
        )
    if schedule[-1]["end"] > video_duration + 1e-6:
        raise ValueError(
            "fixed-rate narration exceeds the video duration: "
            f"{schedule[-1]['end']:.6f} > {video_duration:.6f}"
        )
    return schedule


def schedule_to_subtitle_cues(
    cues: list[dict[str, Any]],
    schedule: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if len(cues) != len(schedule):
        raise ValueError("cue/schedule cardinality mismatch")
    result: list[dict[str, Any]] = []
    for index, (cue, timing) in enumerate(zip(cues, schedule)):
        if int(timing["cue_index"]) != index:
            raise ValueError(f"schedule index mismatch at cue {index}")
        result.append(
            {
                **cue,
                "start": float(timing["start"]),
                "end": float(timing["end"]),
                # Keep one stable subtitle lane.  Some legacy OCR center_y values
                # were attracted to product labels rather than dialogue captions.
                "center_x": 0.5,
                "center_y": float(os.environ.get("INTEGRATED_VISUB_CENTER_Y", "0.735")),
            }
        )
    return result


def balanced_subtitle_lines(value: str, *, max_chars: int) -> list[str]:
    text = normalized_text(value)
    if not text:
        raise ValueError("subtitle text cannot be empty")
    if max_chars < 1:
        raise ValueError("subtitle max_chars must be positive")
    if len(text) <= max_chars or " " not in text:
        return [text]
    words = text.split()
    candidates: list[tuple[int, int, str, str]] = []
    for index in range(1, len(words)):
        left = " ".join(words[:index])
        right = " ".join(words[index:])
        overflow = max(0, len(left) - max_chars) + max(
            0, len(right) - max_chars
        )
        candidates.append((overflow, abs(len(left) - len(right)), left, right))
    _, _, left, right = min(candidates)
    return [left, right]


def subtitle_render_style(height: int) -> dict[str, int | float]:
    if height <= 0:
        raise ValueError("subtitle frame height must be positive")
    font_ratio = float(os.environ.get("DESUB_VISUB_FONT_SIZE_RATIO", "0.040"))
    border_width = float(
        os.environ.get("INTEGRATED_VISUB_BORDER_WIDTH", "2.5")
    )
    line_spacing = int(os.environ.get("INTEGRATED_VISUB_LINE_SPACING", "6"))
    if not math.isfinite(font_ratio) or font_ratio <= 0.0:
        raise ValueError("subtitle font-size ratio must be positive and finite")
    if not math.isfinite(border_width) or border_width < 0.0:
        raise ValueError("subtitle border width must be non-negative and finite")
    if line_spacing < 0:
        raise ValueError("subtitle line spacing must be non-negative")
    return {
        "font_size": max(18, int(round(height * font_ratio))),
        "border_width": border_width,
        "line_spacing": line_spacing,
    }


def build_drawtext_measure_filtergraph(
    *,
    text_paths: list[Path],
    font_path: Path,
    height: int,
) -> str:
    if not text_paths:
        raise ValueError("drawtext measurement requires text files")
    if not font_path.is_absolute() or any(
        not path.is_absolute() for path in text_paths
    ):
        raise ValueError("drawtext measurement paths must be absolute")
    style = subtitle_render_style(height)
    font_size = int(style["font_size"])
    border_width = float(style["border_width"])
    line_spacing = int(style["line_spacing"])
    parts: list[str] = []
    input_label = "0:v:0"
    for index, text_path in enumerate(text_paths):
        output_label = f"measure_{index}"
        marker = SUBTITLE_WIDTH_MARKER_BASE + (
            index * SUBTITLE_WIDTH_MARKER_STRIDE
        )
        parts.append(
            f"[{input_label}]drawtext="
            f"fontfile='{font_path.as_posix()}':"
            f"textfile='{text_path.as_posix()}':"
            "expansion=none:"
            f"fontsize={font_size}:fontcolor=white:"
            f"borderw={border_width:.3f}:bordercolor=black:"
            f"line_spacing={line_spacing}:"
            f"x='print({marker}+text_w)':y=0"
            f"[{output_label}]"
        )
        input_label = output_label
    parts.append(f"[{input_label}]null[measure_out]")
    return ";".join(parts)


def parse_drawtext_width_markers(
    output: str,
    *,
    expected_count: int,
) -> list[float]:
    if expected_count <= 0:
        raise ValueError("drawtext width marker count must be positive")
    values: dict[int, float] = {}
    occurrences: dict[int, int] = {}
    upper_bound = SUBTITLE_WIDTH_MARKER_BASE + (
        expected_count * SUBTITLE_WIDTH_MARKER_STRIDE
    )
    for line in output.splitlines():
        if "[Eval @" not in line:
            continue
        for match in SUBTITLE_WIDTH_NUMBER_RE.finditer(line):
            encoded = float(match.group(0))
            if (
                not math.isfinite(encoded)
                or encoded < SUBTITLE_WIDTH_MARKER_BASE
                or encoded >= upper_bound
            ):
                continue
            offset = encoded - SUBTITLE_WIDTH_MARKER_BASE
            index = int(offset // SUBTITLE_WIDTH_MARKER_STRIDE)
            width = offset - (index * SUBTITLE_WIDTH_MARKER_STRIDE)
            if (
                index < 0
                or index >= expected_count
                or not math.isfinite(width)
                or width <= 0.0
                or width >= SUBTITLE_WIDTH_MARKER_STRIDE
            ):
                raise ValueError("invalid drawtext width marker")
            occurrences[index] = occurrences.get(index, 0) + 1
            previous = values.get(index)
            if previous is not None and not math.isclose(
                previous,
                width,
                rel_tol=0.0,
                abs_tol=1e-3,
            ):
                raise ValueError(
                    f"conflicting drawtext width markers for cue {index}"
                )
            values[index] = width
    missing = [index for index in range(expected_count) if index not in values]
    if missing:
        raise ValueError(f"missing drawtext width markers: {missing}")
    # FFmpeg may evaluate the same x expression more than once. Repeated
    # identical markers are safe; conflicting repetitions fail above.
    if any(count < 1 for count in occurrences.values()):
        raise ValueError("invalid drawtext width marker occurrences")
    return [values[index] for index in range(expected_count)]


def measure_drawtext_widths(
    *,
    desub: Any,
    text_paths: list[Path],
    font_path: Path,
    width: int,
    height: int,
) -> list[float]:
    if width <= 0 or height <= 0:
        raise ValueError("subtitle frame geometry must be positive")
    graph = build_drawtext_measure_filtergraph(
        text_paths=text_paths,
        font_path=font_path,
        height=height,
    )
    proc = desub.cmd(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "info",
            "-xerror",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={width}x{height}:r=1:d=1",
            "-filter_complex",
            graph,
            "-map",
            "[measure_out]",
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ],
        timeout=120,
    )
    return parse_drawtext_width_markers(
        str(proc.stdout),
        expected_count=len(text_paths),
    )


def validate_subtitle_widths(
    widths: list[float],
    *,
    frame_width: int,
    border_width: float,
    safe_margin: float,
) -> dict[str, Any]:
    if not widths:
        raise ValueError("subtitle width validation requires measurements")
    if frame_width <= 0:
        raise ValueError("subtitle frame width must be positive")
    if not math.isfinite(border_width) or border_width < 0.0:
        raise ValueError("subtitle border width must be non-negative and finite")
    if not math.isfinite(safe_margin) or safe_margin < 0.0:
        raise ValueError("subtitle safe margin must be non-negative and finite")
    safe_width = float(frame_width) - (2.0 * safe_margin)
    if safe_width <= 0.0:
        raise ValueError("subtitle safe margin leaves no usable frame width")
    rendered_widths: list[float] = []
    for index, width in enumerate(widths):
        width = float(width)
        if (
            not math.isfinite(width)
            or width <= 0.0
            or width >= SUBTITLE_WIDTH_MARKER_STRIDE
        ):
            raise ValueError(f"invalid measured subtitle width at cue {index}")
        rendered_widths.append(width + (2.0 * border_width))
    overflow = [
        {
            "cue_index": index,
            "text_width": widths[index],
            "rendered_width": rendered_width,
        }
        for index, rendered_width in enumerate(rendered_widths)
        if rendered_width > safe_width + 1e-6
    ]
    if overflow:
        detail = ", ".join(
            (
                f"cue {item['cue_index']}: "
                f"{item['rendered_width']:.3f}>{safe_width:.3f}"
            )
            for item in overflow
        )
        raise ValueError(f"subtitle text exceeds safe frame width: {detail}")
    max_index = max(range(len(rendered_widths)), key=rendered_widths.__getitem__)
    return {
        "pass": True,
        "safe_margin_pixels": safe_margin,
        "safe_width_pixels": safe_width,
        "border_width_pixels": border_width,
        "max_text_width_pixels": max(widths),
        "max_rendered_width_pixels": rendered_widths[max_index],
        "max_rendered_width_cue": max_index,
        "text_widths_pixels": widths,
        "rendered_widths_pixels": rendered_widths,
        "overflow": [],
    }


def build_drawtext_filtergraph(
    cues: list[dict[str, Any]],
    *,
    text_paths: list[Path],
    font_path: Path,
    height: int,
) -> str:
    if not cues or len(cues) != len(text_paths):
        raise ValueError("drawtext cue/text-file cardinality mismatch")
    if not font_path.is_absolute() or any(not path.is_absolute() for path in text_paths):
        raise ValueError("drawtext paths must be absolute")
    style = subtitle_render_style(height)
    font_size = int(style["font_size"])
    border_width = float(style["border_width"])
    line_spacing = int(style["line_spacing"])
    parts: list[str] = []
    input_label = "0:v:0"
    for index, (cue, text_path) in enumerate(zip(cues, text_paths)):
        output_label = f"vietsub_{index}"
        center_y = int(
            round(
                height
                * float(
                    cue.get(
                        "center_y",
                        os.environ.get("INTEGRATED_VISUB_CENTER_Y", "0.735"),
                    )
                )
            )
        )
        start = float(cue["start"])
        end = float(cue["end"])
        parts.append(
            f"[{input_label}]drawtext="
            f"fontfile='{font_path.as_posix()}':"
            f"textfile='{text_path.as_posix()}':"
            "expansion=none:"
            f"fontsize={font_size}:fontcolor=white:"
            f"borderw={border_width:.3f}:bordercolor=black:"
            f"line_spacing={line_spacing}:"
            "x=(w-text_w)/2:"
            f"y={center_y}-(text_h/2):"
            f"enable='between(t,{start:.6f},{end:.6f})'"
            f"[{output_label}]"
        )
        input_label = output_label
    parts.append(f"[{input_label}]format=yuv420p[vsub]")
    return ";".join(parts)


def burn_vietsub_drawtext(
    *,
    desub: Any,
    input_path: Path,
    output_path: Path,
    cues: list[dict[str, Any]],
    width: int,
    height: int,
    work_dir: Path,
) -> dict[str, Any]:
    font_path = Path(
        os.environ.get(
            "INTEGRATED_VISUB_FONT_FILE",
            "/app/fonts/DejaVuSans-Bold.ttf",
        )
    )
    if not font_path.is_file():
        raise ValueError(f"Vietsub font is missing: {font_path}")
    text_dir = work_dir / "drawtext"
    text_dir.mkdir(parents=True, exist_ok=True)
    max_chars = int(os.environ.get("DESUB_VISUB_WRAP_CHARS", "20"))
    if max_chars < 1:
        raise ValueError("subtitle max_chars must be positive")
    style = subtitle_render_style(height)
    text_paths: list[Path] = []
    for index, cue in enumerate(cues):
        text_path = (text_dir / f"cue_{index:03d}.txt").resolve()
        lines = balanced_subtitle_lines(str(cue["text_vi"]), max_chars=max_chars)
        text_path.write_text("\n".join(lines), encoding="utf-8")
        text_paths.append(text_path)
    measured_widths = measure_drawtext_widths(
        desub=desub,
        text_paths=text_paths,
        font_path=font_path,
        width=width,
        height=height,
    )
    safe_margin = float(
        os.environ.get("INTEGRATED_VISUB_SAFE_MARGIN_PIXELS", "24")
    )
    bounds_check = validate_subtitle_widths(
        measured_widths,
        frame_width=width,
        border_width=float(style["border_width"]),
        safe_margin=safe_margin,
    )
    graph = build_drawtext_filtergraph(
        cues,
        text_paths=text_paths,
        font_path=font_path,
        height=height,
    )
    args = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-filter_complex",
        graph,
        "-map",
        "[vsub]",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        os.environ.get("DESUB_VISUB_OUTPUT_CRF", "18"),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    desub.cmd(args, timeout=1800)
    return {
        "renderer": "ffmpeg-drawtext",
        "font_file": str(font_path),
        "cue_count": len(cues),
        "frame_width": width,
        "frame_height": height,
        "wrap_chars": max_chars,
        "filter_count": len(cues),
        "font_size": int(style["font_size"]),
        "border_width": float(style["border_width"]),
        "line_spacing": int(style["line_spacing"]),
        "bounds_check": bounds_check,
    }


def stream_report(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-show_entries",
            (
                "stream=index,codec_name,codec_type,pix_fmt,width,height,"
                "avg_frame_rate,r_frame_rate,sample_rate,channels,channel_layout,"
                "duration,nb_frames,nb_read_frames"
            ),
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


def media_stream(report: dict[str, Any], kind: str) -> dict[str, Any]:
    for stream in report.get("streams") or []:
        if stream.get("codec_type") == kind:
            return stream
    return {}


def build_mix_filtergraph(
    delays_samples: Iterable[int],
    *,
    bgm_input_index: int,
    duration: float,
    bgm_gain_db: float,
) -> str:
    delays = [max(0, int(value)) for value in delays_samples]
    if not delays:
        raise ValueError("mix requires at least one voice clip")
    parts: list[str] = []
    labels: list[str] = []
    for input_index, delay in enumerate(delays, start=1):
        label = f"voice_{input_index}"
        parts.append(
            f"[{input_index}:a]aresample={REQUIRED_SAMPLE_RATE},"
            "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
            f"adelay={delay}S|{delay}S"
            f"[{label}]"
        )
        labels.append(f"[{label}]")
    parts.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:"
        "duration=longest:dropout_transition=0,"
        f"apad,atrim=0:{duration:.6f}[voice]"
    )
    parts.append("[voice]asplit=2[voice_mix][voice_key]")
    parts.append(
        f"[{bgm_input_index}:a]volume={bgm_gain_db:.3f}dB,"
        f"aresample={REQUIRED_SAMPLE_RATE},"
        "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
        f"apad,atrim=0:{duration:.6f}[bed]"
    )
    parts.append(
        "[bed][voice_key]sidechaincompress="
        "threshold=0.020000:ratio=10.000:attack=8.000:release=250.000[ducked]"
    )
    parts.append(
        "[ducked][voice_mix]amix=inputs=2:normalize=0:"
        "duration=longest:dropout_transition=0,alimiter=limit=0.95,"
        f"apad,atrim=0:{duration:.6f}[aout]"
    )
    return ";".join(parts)


def full_decode_check(path: Path, *, require_audio: bool) -> dict[str, Any]:
    """Decode all required streams; any corruption makes the job fail closed."""

    args = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-xerror",
        "-v",
        "error",
        "-i",
        str(path),
        "-map",
        "0:v:0",
    ]
    if require_audio:
        args.extend(["-map", "0:a:0"])
    args.extend(["-f", "null", "-"])
    started = time.time()
    proc = subprocess.run(
        args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=1800,
    )
    if proc.returncode != 0:
        raise ValueError(
            f"full media decode failed for {path.name}: "
            f"{normalized_text(proc.stderr)[-1000:]}"
        )
    return {
        "pass": True,
        "required_audio": require_audio,
        "elapsed_seconds": time.time() - started,
    }


def verify_media(
    *,
    source_report: dict[str, Any],
    clean_report: dict[str, Any],
    final_report: dict[str, Any],
    schedule: list[dict[str, Any]],
    clean_decode: dict[str, Any],
    final_decode: dict[str, Any],
) -> dict[str, Any]:
    source_video = media_stream(source_report, "video")
    clean_video = media_stream(clean_report, "video")
    final_video = media_stream(final_report, "video")
    final_audio = media_stream(final_report, "audio")
    source_duration = float((source_report.get("format") or {}).get("duration") or 0.0)
    clean_duration = float((clean_report.get("format") or {}).get("duration") or 0.0)
    final_duration = float((final_report.get("format") or {}).get("duration") or 0.0)
    final_audio_duration = float(final_audio.get("duration") or 0.0)
    source_video_count = sum(
        1
        for stream in source_report.get("streams") or []
        if stream.get("codec_type") == "video"
    )
    final_video_count = sum(
        1
        for stream in final_report.get("streams") or []
        if stream.get("codec_type") == "video"
    )
    final_audio_count = sum(
        1
        for stream in final_report.get("streams") or []
        if stream.get("codec_type") == "audio"
    )

    checks = {
        "geometry_equal": (
            int(source_video.get("width") or 0),
            int(source_video.get("height") or 0),
        )
        == (
            int(clean_video.get("width") or 0),
            int(clean_video.get("height") or 0),
        )
        == (
            int(final_video.get("width") or 0),
            int(final_video.get("height") or 0),
        ),
        "frame_rate_equal": (
            source_video.get("avg_frame_rate")
            == clean_video.get("avg_frame_rate")
            == final_video.get("avg_frame_rate")
        ),
        "frame_count_equal": (
            int(source_video.get("nb_read_frames") or source_video.get("nb_frames") or 0)
            == int(clean_video.get("nb_read_frames") or clean_video.get("nb_frames") or -1)
            == int(final_video.get("nb_read_frames") or final_video.get("nb_frames") or -2)
        ),
        "clean_duration_delta_seconds": abs(clean_duration - source_duration),
        "final_duration_delta_seconds": abs(final_duration - source_duration),
        "final_video_codec": final_video.get("codec_name"),
        "final_pixel_format": final_video.get("pix_fmt"),
        "final_audio_codec": final_audio.get("codec_name"),
        "final_audio_sample_rate": int(final_audio.get("sample_rate") or 0),
        "final_audio_channels": int(final_audio.get("channels") or 0),
        "source_video_stream_count": source_video_count,
        "final_video_stream_count": final_video_count,
        "final_audio_stream_count": final_audio_count,
        "final_audio_duration": final_audio_duration,
        "final_audio_tail_delta_seconds": abs(final_audio_duration - source_duration),
        "clean_full_decode": clean_decode,
        "final_full_decode": final_decode,
        "schedule_count": len(schedule),
        "schedule_overlaps": sum(
            1
            for left, right in zip(schedule, schedule[1:])
            if float(left["end"]) > float(right["start"]) + 1e-6
        ),
        "narration_end": float(schedule[-1]["end"]),
        "video_duration": source_duration,
    }
    checks["pass"] = bool(
        checks["geometry_equal"]
        and checks["frame_rate_equal"]
        and checks["frame_count_equal"]
        and checks["clean_duration_delta_seconds"] <= 0.10
        and checks["final_duration_delta_seconds"] <= 0.10
        and checks["final_video_codec"] == "h264"
        and checks["final_pixel_format"] == "yuv420p"
        and checks["final_audio_codec"] == "aac"
        and checks["final_audio_sample_rate"] == REQUIRED_SAMPLE_RATE
        and checks["final_audio_channels"] == REQUIRED_CHANNELS
        and checks["source_video_stream_count"] == 1
        and checks["final_video_stream_count"] == 1
        and checks["final_audio_stream_count"] == 1
        and checks["final_audio_duration"] > 0.0
        and checks["final_audio_tail_delta_seconds"] <= 0.10
        and checks["clean_full_decode"].get("pass") is True
        and checks["final_full_decode"].get("pass") is True
        and checks["schedule_overlaps"] == 0
        and checks["narration_end"] <= checks["video_duration"] + 1e-6
    )
    return checks


def residual_dialogue_subtitles(
    *,
    desub: Any,
    clean_path: Path,
    meta: Any,
    rect: tuple[int, int, int, int],
    cues: list[dict[str, Any]],
    cue_pad_seconds: float,
) -> dict[str, Any]:
    """Stream a second OCR pass over every active dialogue-cue window.

    Only one cropped frame is resident at a time.  This is important on Cloud
    Run because files in ``/tmp`` and Python arrays both count against the task
    memory limit.
    """

    import cv2  # type: ignore
    import easyocr  # type: ignore

    detect_fps = float(os.environ.get("INTEGRATED_RESIDUAL_QA_FPS", "8"))
    frame_ids = video_sample_frame_ids(
        frame_count=int(meta.frames),
        source_fps=float(meta.fps),
        sample_fps=detect_fps,
    )
    cue_sample_counts = [0 for _cue in cues]
    sample_plan: list[tuple[int, float, list[int]]] = []
    for frame_id in frame_ids:
        seconds = frame_id / float(meta.fps)
        active_indexes = [
            index
            for index, cue in enumerate(cues)
            if float(cue["start"]) - cue_pad_seconds
            <= seconds
            <= float(cue["end"]) + cue_pad_seconds
        ]
        if active_indexes:
            sample_plan.append((frame_id, seconds, active_indexes))

    x1, y1, x2, y2 = rect
    residuals: list[dict[str, Any]] = []
    decoded_samples = 0
    timed_out = False
    error = ""
    deadline = time.time() + float(
        os.environ.get("DESUB_DETECTOR_BUDGET_SECONDS", "600")
    )
    cap = None
    try:
        reader = easyocr.Reader(
            ["ch_sim", "en"],
            gpu=True,
            model_storage_directory=os.environ.get(
                "EASYOCR_MODEL_DIR",
                "/models/easyocr",
            ),
            download_enabled=False,
            verbose=False,
        )
        cap = cv2.VideoCapture(str(clean_path))
        if not cap.isOpened():
            raise RuntimeError("failed to open cleaned video for streaming OCR")
        for frame_id, seconds, active_indexes in sample_plan:
            if time.time() > deadline:
                timed_out = True
                break
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_id)
            ok, frame = cap.read()
            if not ok:
                raise RuntimeError(f"failed to decode OCR sample frame {frame_id}")
            decoded_samples += 1
            for cue_index in active_indexes:
                cue_sample_counts[cue_index] += 1
            crop = frame[y1:y2, x1:x2]
            results = reader.readtext(
                crop,
                detail=1,
                paragraph=False,
                text_threshold=float(
                    os.environ.get("DESUB_EASYOCR_TEXT_THRESHOLD", "0.25")
                ),
                low_text=float(
                    os.environ.get("DESUB_EASYOCR_LOW_TEXT", "0.25")
                ),
            )
            for points, text, score in results:
                raw_box = desub.poly_to_box(points)
                if raw_box is None:
                    continue
                box = desub.clamp_box(
                    (
                        raw_box[0] + x1,
                        raw_box[1] + y1,
                        raw_box[2] + x1,
                        raw_box[3] + y1,
                    ),
                    int(meta.width),
                    int(meta.height),
                )
                if (
                    box is None
                    or float(score or 0.0) < 0.20
                    or not desub.has_cjk(str(text or ""))
                ):
                    continue
                residuals.append(
                    {
                        "timestamp_seconds": seconds,
                        "score": float(score or 0.0),
                        "text": str(text or ""),
                        "box": list(box),
                    }
                )
            del crop
            del frame
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    finally:
        if cap is not None:
            cap.release()

    all_cues_sampled = bool(cue_sample_counts) and all(
        count > 0 for count in cue_sample_counts
    )
    detector_healthy = bool(
        not error
        and not timed_out
        and sample_plan
        and decoded_samples == len(sample_plan)
        and all_cues_sampled
    )
    detector_summary = {
        "easyocr_craft": {
            "ok": not error,
            "error": error or None,
            "gpu": True,
            "runtime_model_download": False,
            "timed_out": timed_out,
            "planned_samples": len(sample_plan),
            "decoded_samples": decoded_samples,
        },
        "paddle_det": {
            "ok": True,
            "skipped": True,
            "reason": "DESUB_COMPARE_PADDLE=false",
        },
    }
    return {
        "pass": detector_healthy and not residuals,
        "detector_healthy": detector_healthy,
        "mode": "streaming-one-cropped-frame",
        "detect_fps": detect_fps,
        "sample_count": decoded_samples,
        "planned_sample_count": len(sample_plan),
        "peak_resident_video_frames": 1,
        "all_cues_sampled": all_cues_sampled,
        "cue_sample_counts": cue_sample_counts,
        "detector_summary": detector_summary,
        "residual_cjk_box_count": len(residuals),
        "residuals": residuals[:100],
    }


VERTEX_QA_BOOLEAN_FIELDS = (
    "overall_pass",
    "vietnamese_voice_present",
    "vietnamese_voice_consistent",
    "vietnamese_subtitles_present",
    "voice_subtitle_sync_pass",
    "audio_tail_pass",
)
VERTEX_QA_ARRAY_FIELDS = (
    "chinese_dialogue_subtitle_residuals",
    "inpainting_artifacts",
    "notes",
)


def validate_vertex_qa_payload(payload: Any) -> bool:
    """Validate Vertex JSON types and return its strict all-gates verdict."""

    if not isinstance(payload, dict):
        raise ValueError(f"Vertex QA returned a non-object payload: {payload}")
    required = set(VERTEX_QA_BOOLEAN_FIELDS) | set(VERTEX_QA_ARRAY_FIELDS)
    if set(payload) != required:
        raise ValueError(
            "Vertex QA returned wrong fields: "
            f"missing={sorted(required - set(payload))}, "
            f"extra={sorted(set(payload) - required)}"
        )
    for field in VERTEX_QA_BOOLEAN_FIELDS:
        if type(payload[field]) is not bool:
            raise ValueError(f"Vertex QA field {field} must be a boolean")
    for field in VERTEX_QA_ARRAY_FIELDS:
        if not isinstance(payload[field], list):
            raise ValueError(f"Vertex QA field {field} must be an array")
    return bool(
        all(payload[field] is True for field in VERTEX_QA_BOOLEAN_FIELDS)
        and not payload["chinese_dialogue_subtitle_residuals"]
        and not payload["inpainting_artifacts"]
    )


def validate_clean_engine_report(
    report: Any,
    *,
    expected_cluster_count: int,
) -> dict[str, Any]:
    """Prove LaMa used cue-timed full rectangles, never segment-wide boxes."""

    if not isinstance(report, dict):
        raise ValueError("clean engine report must be an object")
    if int(report.get("cluster_count") or -1) != expected_cluster_count:
        raise ValueError("clean engine cluster count mismatch")
    results = report.get("cluster_results")
    if not isinstance(results, list) or not results:
        raise ValueError("clean engine returned no cluster results")
    observed_clusters = 0
    observed_regions = 0
    for index, result in enumerate(results):
        if not isinstance(result, dict) or result.get("mask_kind") != "stroke":
            raise ValueError(f"clean segment {index} did not use timed stroke masks")
        clusters = result.get("clusters")
        if not isinstance(clusters, list) or not clusters:
            raise ValueError(f"clean segment {index} has no cue clusters")
        observed_clusters += len(clusters)
        mask_stats = result.get("mask_stats")
        if not isinstance(mask_stats, dict):
            raise ValueError(f"clean segment {index} has no mask statistics")
        if (
            mask_stats.get("device_type") != "cuda"
            or mask_stats.get("cuda_required") is not True
        ):
            raise ValueError(f"clean segment {index} did not run fail-closed on CUDA")
        stats = mask_stats.get("static_mask")
        if not isinstance(stats, dict) or stats.get("static_mask_enabled") is not True:
            raise ValueError(f"clean segment {index} did not use static timed masks")
        processed_frames = int(mask_stats.get("processed_frames") or -1)
        input_frames = int(stats.get("input_frames") or -2)
        if processed_frames != input_frames:
            raise ValueError(f"clean segment {index} inpaint pass was incomplete")
        region_count = int(stats.get("static_region_count") or 0)
        fallback_count = int(stats.get("fallback_static_regions") or -1)
        if region_count <= 0 or fallback_count != region_count:
            raise ValueError(
                f"clean segment {index} did not force every timed region to full box"
            )
        if int(stats.get("prepass_frames") or -1) != input_frames:
            raise ValueError(f"clean segment {index} mask prepass was incomplete")
        observed_regions += region_count
    if observed_clusters != expected_cluster_count:
        raise ValueError("clean engine omitted or duplicated cue clusters")
    if observed_regions != expected_cluster_count:
        raise ValueError("clean engine timed-region cardinality mismatch")
    return {
        "pass": True,
        "segment_count": len(results),
        "cluster_count": observed_clusters,
        "timed_full_box_region_count": observed_regions,
    }


def vertex_video_qa(
    *,
    candidate_uri: str,
    project_id: str,
    location: str,
    model: str,
    video_fps: float,
) -> dict[str, Any]:
    from google import genai  # type: ignore
    from google.genai import types  # type: ignore

    prompt = """Review the complete vertical video, including its audio.

The source was a Chinese Douyin shop conversation. The candidate is required to:
1. contain no remaining burned-in Chinese dialogue subtitles at any timestamp;
2. contain exactly one consistent Vietnamese narrator voice;
3. contain Vietnamese subtitles that follow the spoken Vietnamese;
4. preserve the video timeline and have audible content through the ending;
5. avoid severe inpainting artifacts in the former subtitle lane.

Ignore Chinese text printed on physical products, shop signs, and a yellow running
price total; those are scene content, not dialogue subtitles.

Return one JSON object with:
- overall_pass: boolean
- chinese_dialogue_subtitle_residuals: array of {timestamp_seconds, description}
- inpainting_artifacts: array of {timestamp_seconds, description}
- vietnamese_voice_present: boolean
- vietnamese_voice_consistent: boolean
- vietnamese_subtitles_present: boolean
- voice_subtitle_sync_pass: boolean
- audio_tail_pass: boolean
- notes: array of short strings

Fail overall_pass when any Chinese dialogue subtitle remains or a required audio/
subtitle property is missing. Inspect the entire video, not only the opening.
"""
    if video_fps <= 0.0 or video_fps > 24.0:
        raise ValueError("Vertex video fps must be in (0, 24]")
    video_part = types.Part(
        file_data=types.FileData(
            file_uri=candidate_uri,
            mime_type="video/mp4",
        ),
        video_metadata=types.VideoMetadata(fps=video_fps),
    )
    client = genai.Client(vertexai=True, project=project_id, location=location)
    response = client.models.generate_content(
        model=model,
        contents=[
            video_part,
            prompt,
        ],
        config=types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=4096,
            response_mime_type="application/json",
            http_options=types.HttpOptions(timeout=900_000),
        ),
    )
    payload = json.loads(response.text or "{}")
    validate_vertex_qa_payload(payload)
    return payload


def main() -> int:
    # Imports stay inside the cloud entry point so pure validation/scheduling
    # helpers remain testable without cloud or media dependencies.
    from google.cloud import storage  # type: ignore

    sys.path.insert(0, "/app")
    sys.path.insert(0, "/app/experiments/desub")
    import prototype as desub  # type: ignore
    from container_short.steps import capcut_tts  # type: ignore

    started = time.time()
    cuda_runtime = require_cuda_runtime()
    print(json.dumps({"stage": "cuda-preflight", **cuda_runtime}), flush=True)
    project_id = os.environ.get("GCP_PROJECT_ID", DEFAULT_PROJECT_ID).strip()
    source_uri = required_env("INTEGRATED_SOURCE_URI")
    clean_base_uri = (
        os.environ.get("INTEGRATED_CLEAN_BASE_URI", "").strip() or source_uri
    )
    resume_clean = validate_resume_clean_config(
        uri=os.environ.get("INTEGRATED_RESUME_CLEAN_URI", ""),
        generation=os.environ.get(
            "INTEGRATED_EXPECTED_RESUME_CLEAN_GENERATION",
            "",
        ),
        sha256=os.environ.get("INTEGRATED_EXPECTED_RESUME_CLEAN_SHA256", ""),
    )
    cues_uri = required_env("INTEGRATED_CUES_URI")
    attempt_prefix = required_env("INTEGRATED_ATTEMPT_PREFIX")
    if not attempt_prefix.startswith(
        "gs://YOUR_GCP_PROJECT-media-sg/desub/integrated/v1/"
    ):
        raise ValueError("integrated output must stay under the v1 isolated prefix")

    work = Path(os.environ.get("WORK_DIR", "/tmp/integrated_desub"))
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    voice_dir = work / "voice"
    qa_dir = work / "qa"
    voice_dir.mkdir()
    qa_dir.mkdir()

    source_path = work / "source.mp4"
    clean_base_path = work / "clean_base.mp4"
    cues_path = work / "source_cues.json"
    clean_path = work / "clean.mp4"
    subtitle_path = work / "vietsub.ass"
    subtitled_path = work / "subtitled.mp4"
    final_path = work / "dubbed.mp4"
    ledger_path = work / "cue_ledger.json"
    tts_manifest_path = work / "tts_generation_manifest.json"
    report_path = work / "verification_report.json"

    client = storage.Client(project=project_id)

    def download(
        uri: str,
        path: Path,
        *,
        expected_generation: str = "",
    ) -> dict[str, Any]:
        bucket_name, blob_name = parse_gs_uri(uri)
        blob = client.bucket(bucket_name).blob(blob_name)
        blob.reload()
        generation = str(blob.generation or "")
        if expected_generation and generation != expected_generation:
            raise ValueError(
                f"generation mismatch for {uri}: {generation} != {expected_generation}"
            )
        blob.download_to_filename(
            str(path),
            if_generation_match=int(generation) if generation else None,
        )
        return {
            "uri": uri,
            "generation": generation,
            "size": int(blob.size or 0),
            "sha256": sha256_file(path),
        }

    def upload_create_only(path: Path, uri: str, content_type: str) -> dict[str, Any]:
        bucket_name, blob_name = parse_gs_uri(uri)
        blob = client.bucket(bucket_name).blob(blob_name)
        blob.upload_from_filename(
            str(path),
            content_type=content_type,
            if_generation_match=0,
        )
        blob.reload()
        return {
            "uri": uri,
            "generation": str(blob.generation),
            "size": int(blob.size or 0),
            "sha256": sha256_file(path),
        }

    print(json.dumps({"stage": "download", "source_uri": source_uri}), flush=True)
    source_object = download(
        source_uri,
        source_path,
        expected_generation=os.environ.get(
            "INTEGRATED_EXPECTED_SOURCE_GENERATION", ""
        ).strip(),
    )
    resumed_clean_object: dict[str, Any] | None = None
    if resume_clean is not None:
        resumed_clean_object = download(
            resume_clean["uri"],
            clean_path,
            expected_generation=resume_clean["generation"],
        )
        clean_base_object: dict[str, Any] | None = None
    elif clean_base_uri == source_uri:
        shutil.copy2(source_path, clean_base_path)
        clean_base_object = dict(source_object)
    else:
        clean_base_object = download(
            clean_base_uri,
            clean_base_path,
            expected_generation=os.environ.get(
                "INTEGRATED_EXPECTED_CLEAN_BASE_GENERATION", ""
            ).strip(),
        )
    cues_object = download(
        cues_uri,
        cues_path,
        expected_generation=os.environ.get(
            "INTEGRATED_EXPECTED_CUES_GENERATION", ""
        ).strip(),
    )
    expected_source_sha = os.environ.get(
        "INTEGRATED_EXPECTED_SOURCE_SHA256", ""
    ).strip().lower()
    expected_clean_base_sha = os.environ.get(
        "INTEGRATED_EXPECTED_CLEAN_BASE_SHA256", ""
    ).strip().lower()
    expected_cues_sha = os.environ.get(
        "INTEGRATED_EXPECTED_CUES_SHA256", ""
    ).strip().lower()
    expected_objects = [
        ("source", source_object["sha256"], expected_source_sha),
        ("cues", cues_object["sha256"], expected_cues_sha),
    ]
    if resumed_clean_object is not None:
        expected_objects.append(
            (
                "resume clean",
                resumed_clean_object["sha256"],
                resume_clean["sha256"] if resume_clean is not None else "",
            )
        )
    elif clean_base_object is not None:
        expected_objects.append(
            ("clean base", clean_base_object["sha256"], expected_clean_base_sha)
        )
    for label, actual, expected in expected_objects:
        if expected and actual != expected:
            raise ValueError(f"{label} SHA-256 mismatch: {actual} != {expected}")
    source_meta = desub.ffprobe(source_path)
    clean_input_meta = desub.ffprobe(
        clean_path if resumed_clean_object is not None else clean_base_path
    )
    if (
        clean_input_meta.width,
        clean_input_meta.height,
        clean_input_meta.fps,
        clean_input_meta.frames,
    ) != (
        source_meta.width,
        source_meta.height,
        source_meta.fps,
        source_meta.frames,
    ) or abs(clean_input_meta.duration - source_meta.duration) > 0.10:
        raise ValueError("clean input does not match canonical source geometry/timeline")
    source_probe = stream_report(source_path)
    cues = validate_cues(
        json.loads(cues_path.read_text(encoding="utf-8")),
        duration=source_meta.duration,
    )
    if len(cues) != int(os.environ.get("INTEGRATED_EXPECTED_CUES", "77")):
        raise ValueError(f"expected 77 source cues, found {len(cues)}")

    rect = parse_rect(os.environ.get("INTEGRATED_CLEAN_RECT", "115,870,605,1015"))
    if rect[2] > source_meta.width or rect[3] > source_meta.height:
        raise ValueError(f"clean rect {rect} exceeds source geometry")
    cue_pad = float(os.environ.get("INTEGRATED_CLEAN_CUE_PAD_SECONDS", "0.18"))
    clusters = [
        desub.MaskCluster(
            kind="integrated-dialogue-subtitle",
            t_start=max(0.0, float(cue["start"]) - cue_pad),
            t_end=min(source_meta.duration, float(cue["end"]) + cue_pad),
            context_start=max(0.0, float(cue["start"]) - max(0.75, cue_pad)),
            context_end=min(
                source_meta.duration,
                float(cue["end"]) + max(0.75, cue_pad),
            ),
            rects=(rect,),
            span_count=1,
        )
        for cue in cues
    ]

    # The legacy box path applies a union rectangle for an entire keyframe
    # segment.  The static-stroke path honors each cue's enable window.  A
    # threshold above 1.0 deterministically promotes every active stroke
    # region to its complete rectangle while keeping that timing behavior.
    os.environ["DESUB_STROKE_MASK_ENABLED"] = "true"
    os.environ["DESUB_REQUIRE_CUDA"] = "true"
    os.environ["DESUB_STROKE_FALLBACK_MIN_RATIO"] = "1.01"
    os.environ["DESUB_STATIC_MASK_MIN_HITS"] = "1"
    os.environ["DESUB_STATIC_MASK_MIN_HIT_RATIO"] = "0"
    os.environ["DESUB_LAMA_TEMPORAL_BLEND"] = "0"
    os.environ.setdefault("DESUB_OUTPUT_CRF", "18")
    os.environ.setdefault("DESUB_VISUB_OUTPUT_CRF", "18")
    os.environ.setdefault("DESUB_VISUB_WRAP_CHARS", "24")
    if resumed_clean_object is None:
        print(
            json.dumps(
                {
                    "stage": "clean",
                    "cue_count": len(cues),
                    "mask_kind": "cue-timed-full-box",
                    "rect": rect,
                }
            ),
            flush=True,
        )
        clean_engine_report = desub.apply_timed_clusters(
            model_name="lama",
            source_clip=clean_base_path,
            output_clip=clean_path,
            clusters=clusters,
            meta=source_meta,
            work_dir=work / "inpaint",
        )
        clean_engine_checks = validate_clean_engine_report(
            clean_engine_report,
            expected_cluster_count=len(cues),
        )
        clean_provenance = {
            "mode": "fresh-cue-timed-lama",
            "resumed": False,
            "input": clean_base_object,
        }
    else:
        if resume_clean is None:
            raise AssertionError("resume clean configuration disappeared")
        print(
            json.dumps(
                {
                    "stage": "resume-clean",
                    "uri": resumed_clean_object["uri"],
                    "generation": resumed_clean_object["generation"],
                    "sha256": resumed_clean_object["sha256"],
                    "full_residual_ocr_required": True,
                }
            ),
            flush=True,
        )
        clean_engine_report = {
            "engine": "pinned-precomputed-clean",
            "executed_in_this_attempt": False,
            "source": resumed_clean_object,
        }
        clean_engine_checks = {
            "pass": True,
            "resumed": True,
            "exact_generation": (
                resumed_clean_object["generation"] == resume_clean["generation"]
            ),
            "exact_sha256": (
                resumed_clean_object["sha256"] == resume_clean["sha256"]
            ),
            "full_residual_ocr_required": True,
        }
        if not all(
            clean_engine_checks[key]
            for key in ("pass", "exact_generation", "exact_sha256")
        ):
            raise ValueError(f"resume clean provenance failed: {clean_engine_checks}")
        clean_provenance = {
            "mode": "resume-pinned-clean",
            "resumed": True,
            "input": resumed_clean_object,
        }
    clean_probe = stream_report(clean_path)
    # Persist the expensive clean artifact before any external QA can fail.
    clean_artifact_uri = f"{attempt_prefix}/clean/clean.mp4"
    if resumed_clean_object is not None and resumed_clean_object["uri"] == clean_artifact_uri:
        raise ValueError("resume clean source must be outside the new attempt prefix")
    clean_artifact = upload_create_only(
        clean_path,
        clean_artifact_uri,
        "video/mp4",
    )
    if clean_base_path.exists():
        clean_base_path.unlink()
    inpaint_work = work / "inpaint"
    if inpaint_work.is_dir():
        shutil.rmtree(inpaint_work)
    clean_residual_qa = residual_dialogue_subtitles(
        desub=desub,
        clean_path=clean_path,
        meta=source_meta,
        rect=rect,
        cues=cues,
        cue_pad_seconds=cue_pad,
    )
    clean_ocr_path = qa_dir / "clean_residual_ocr.json"
    clean_ocr_path.write_text(
        json.dumps(clean_residual_qa, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    clean_ocr_artifact = upload_create_only(
        clean_ocr_path,
        f"{attempt_prefix}/verification/clean/residual_ocr.json",
        "application/json",
    )
    if not clean_residual_qa["pass"]:
        raise ValueError(f"clean residual OCR failed: {clean_residual_qa}")

    voice = os.environ.get("VISUB_TTS_VOICE", DEFAULT_VOICE).strip() or DEFAULT_VOICE
    resource_id = (
        os.environ.get("VISUB_TTS_RESOURCE_ID", DEFAULT_RESOURCE_ID).strip()
        or DEFAULT_RESOURCE_ID
    )
    rate = float(os.environ.get("VISUB_TTS_RATE", str(REQUIRED_RATE)))
    tts_identity = validate_tts_identity(
        voice=voice,
        resource_id=resource_id,
        rate=rate,
    )
    device_json = os.environ.get("VISUB_TTS_DEVICE_JSON", "").strip() or None
    device = capcut_tts.make_device(device_json)
    clip_durations: list[float] = []
    tts_rows: list[dict[str, Any]] = []
    for index, cue in enumerate(cues):
        voice_path = voice_dir / f"cue_{index:03d}.mp3"
        print(
            json.dumps(
                {
                    "stage": "tts",
                    "cue_index": index,
                    "cue_count": len(cues),
                    "rate": f"{rate:.4f}",
                }
            ),
            flush=True,
        )
        clip_duration = float(
            capcut_tts.synthesize_once(
                str(cue["text_vi"]),
                str(voice_path),
                voice=voice,
                resource_id=resource_id,
                device=device,
                rate=rate,
                poll_timeout=int(os.environ.get("VISUB_TTS_POLL_TIMEOUT", "300")),
            )
        )
        if clip_duration <= 0.0:
            raise ValueError(f"CapCut returned an invalid duration for cue {index}")
        clip_durations.append(clip_duration)
        tts_rows.append(
            {
                "cue_index": index,
                "provider": "capcut",
                "voice": voice,
                "resource_id": resource_id,
                "provider_rate": f"{rate:.4f}",
                "post_rate_filters": [],
                "duration": clip_duration,
                "sha256": sha256_file(voice_path),
                "text_vi_sha256": hashlib.sha256(
                    str(cue["text_vi"]).encode("utf-8")
                ).hexdigest(),
            }
        )

    tts_generation_manifest = {
        "schema_version": "integrated-desub-tts-generation/v1",
        "attempt_prefix": attempt_prefix,
        "cue_count": len(cues),
        "identity": tts_identity,
        "post_rate_filters": [],
        "clips": tts_rows,
    }
    tts_manifest_path.write_text(
        json.dumps(
            tts_generation_manifest,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    # Persist timing evidence before scheduling.  A fixed-rate fit failure is
    # expected to remain fail-closed, but it must be diagnosable without
    # guessing provider durations.
    tts_generation_artifact = upload_create_only(
        tts_manifest_path,
        f"{attempt_prefix}/tts/generation_manifest.json",
        "application/json",
    )

    schedule = build_schedule(
        cues,
        clip_durations,
        video_duration=source_meta.duration,
        gap_seconds=float(os.environ.get("VISUB_TTS_FIXED_GAP_SECONDS", "0.05")),
        max_lag_seconds=float(os.environ.get("VISUB_TTS_MAX_LAG_SECONDS", "0.6")),
    )
    final_cues = schedule_to_subtitle_cues(cues, schedule)
    desub.write_visub_ass(subtitle_path, final_cues, source_meta)
    subtitle_render_report = burn_vietsub_drawtext(
        desub=desub,
        input_path=clean_path,
        output_path=subtitled_path,
        cues=final_cues,
        width=source_meta.width,
        height=source_meta.height,
        work_dir=work,
    )

    voice_paths = [voice_dir / f"cue_{index:03d}.mp3" for index in range(len(cues))]
    args = ["ffmpeg", "-y", "-i", str(subtitled_path)]
    for voice_path in voice_paths:
        args.extend(["-i", str(voice_path)])
    source_audio_index = len(voice_paths) + 1
    args.extend(["-i", str(source_path)])
    graph = build_mix_filtergraph(
        [int(item["start_sample"]) for item in schedule],
        bgm_input_index=source_audio_index,
        duration=source_meta.duration,
        bgm_gain_db=float(os.environ.get("VISUB_BGM_GAIN_DB", "-20")),
    )
    args.extend(
        [
            "-filter_complex",
            graph,
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-t",
            f"{source_meta.duration:.6f}",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            str(REQUIRED_SAMPLE_RATE),
            "-ac",
            str(REQUIRED_CHANNELS),
            "-movflags",
            "+faststart",
            str(final_path),
        ]
    )
    desub.cmd(args, timeout=1800)
    final_probe = stream_report(final_path)
    clean_decode = full_decode_check(clean_path, require_audio=False)
    final_decode = full_decode_check(final_path, require_audio=True)

    machine_checks = verify_media(
        source_report=source_probe,
        clean_report=clean_probe,
        final_report=final_probe,
        schedule=schedule,
        clean_decode=clean_decode,
        final_decode=final_decode,
    )
    if not machine_checks["pass"]:
        raise ValueError(f"machine media checks failed: {machine_checks}")

    ledger = {
        "schema_version": "integrated-desub-cue-ledger/v1",
        "source_uri": source_uri,
        "source_sha256": sha256_file(source_path),
        "cue_count": len(cues),
        "voice": voice,
        "resource_id": resource_id,
        "required_provider_rate": f"{rate:.4f}",
        "post_rate_filters": [],
        "cues": [
            {
                "index": index,
                "source_start": float(cue["start"]),
                "source_end": float(cue["end"]),
                "text_zh": cue["text_zh"],
                "text_vi": cue["text_vi"],
                "tts": tts_rows[index],
                "schedule": schedule[index],
            }
            for index, cue in enumerate(cues)
        ],
    }
    ledger_path.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    candidate_artifact = upload_create_only(
        final_path,
        f"{attempt_prefix}/dub/candidate.mp4",
        "video/mp4",
    )
    ledger_artifact = upload_create_only(
        ledger_path,
        f"{attempt_prefix}/dub/cue_ledger.json",
        "application/json",
    )
    upload_create_only(
        subtitle_path,
        f"{attempt_prefix}/dub/vietsub.ass",
        "text/x-ssa",
    )
    if source_path.exists():
        source_path.unlink()
    if subtitled_path.exists():
        subtitled_path.unlink()
    if subtitle_path.exists():
        subtitle_path.unlink()
    if voice_dir.is_dir():
        shutil.rmtree(voice_dir)

    # Persist deterministic visual evidence before external model QA.  A
    # fail-closed Vertex rejection must still leave enough evidence to debug
    # without rerunning the expensive inpainting stage.
    frame_artifacts: list[dict[str, Any]] = []
    clean_points = [
        (
            index,
            (float(cue["start"]) + float(cue["end"])) / 2.0,
        )
        for index, cue in enumerate(cues)
    ]
    dub_points = [
        (
            index,
            (float(timing["start"]) + float(timing["end"])) / 2.0,
        )
        for index, timing in enumerate(schedule)
    ]
    for index, seconds in clean_points:
        clean_frame = qa_dir / f"cue_{index:03d}_{seconds:.3f}_clean.png"
        desub.extract_frame(clean_path, clean_frame, seconds)
        frame_artifacts.append(
            {
                "kind": "clean",
                "cue_index": index,
                "timestamp_seconds": seconds,
                **upload_create_only(
                    clean_frame,
                    f"{attempt_prefix}/verification/clean/frames/{clean_frame.name}",
                    "image/png",
                ),
            }
        )
    for index, seconds in dub_points:
        final_frame = qa_dir / f"cue_{index:03d}_{seconds:.3f}_dub.png"
        desub.extract_frame(final_path, final_frame, seconds)
        frame_artifacts.append(
            {
                "kind": "dub",
                "cue_index": index,
                "timestamp_seconds": seconds,
                **upload_create_only(
                    final_frame,
                    f"{attempt_prefix}/verification/dub/frames/{final_frame.name}",
                    "image/png",
                ),
            }
        )
    for kind, media_path in (("clean", clean_path), ("dub", final_path)):
        for label, seconds in (
            ("opening", 0.1),
            ("tail", max(0.1, source_meta.duration - 0.1)),
        ):
            frame_path = qa_dir / f"{label}_{seconds:.3f}_{kind}.png"
            desub.extract_frame(media_path, frame_path, seconds)
            frame_artifacts.append(
                {
                    "kind": kind,
                    "cue_index": None,
                    "label": label,
                    "timestamp_seconds": seconds,
                    **upload_create_only(
                        frame_path,
                        (
                            f"{attempt_prefix}/verification/{kind}/frames/"
                            f"{frame_path.name}"
                        ),
                        "image/png",
                    ),
                }
            )

    vertex_location = os.environ.get("VERTEX_QA_LOCATION", "asia-southeast1").strip()
    vertex_model = os.environ.get("VERTEX_QA_MODEL", "gemini-2.5-flash").strip()
    vertex_video_fps = float(os.environ.get("VERTEX_QA_VIDEO_FPS", "8"))
    print(
        json.dumps(
            {
                "stage": "vertex-qa",
                "location": vertex_location,
                "model": vertex_model,
                "video_fps": vertex_video_fps,
            }
        ),
        flush=True,
    )
    vertex_qa = vertex_video_qa(
        candidate_uri=candidate_artifact["uri"],
        project_id=project_id,
        location=vertex_location,
        model=vertex_model,
        video_fps=vertex_video_fps,
    )
    vertex_qa_path = qa_dir / "vertex_qa.json"
    vertex_qa_path.write_text(
        json.dumps(vertex_qa, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    vertex_qa_artifact = upload_create_only(
        vertex_qa_path,
        f"{attempt_prefix}/verification/vertex_qa.json",
        "application/json",
    )
    if not validate_vertex_qa_payload(vertex_qa):
        raise ValueError(f"Vertex video QA failed: {vertex_qa}")

    report = {
        "schema_version": "integrated-desub-verification/v1",
        "candidate_qa_passed": True,
        "release_authorized": False,
        "attempt_prefix": attempt_prefix,
        "cuda_runtime": cuda_runtime,
        "source": {
            **source_object,
            "probe": source_probe,
        },
        "clean_base": clean_base_object,
        "resumed_clean_source": resumed_clean_object,
        "source_cues": cues_object,
        "clean": {
            **clean_artifact,
            "probe": clean_probe,
            "engine": clean_engine_report,
            "engine_checks": clean_engine_checks,
            "provenance": clean_provenance,
            "mask_kind": "cue-timed-full-box",
            "mask_rect": list(rect),
            "cue_pad_seconds": cue_pad,
            "residual_ocr": clean_residual_qa,
            "residual_ocr_artifact": clean_ocr_artifact,
        },
        "candidate": {
            **candidate_artifact,
            "probe": final_probe,
            "subtitle_renderer": subtitle_render_report,
        },
        "ledger": ledger_artifact,
        "tts_generation": tts_generation_artifact,
        "tts_identity": tts_identity,
        "machine_checks": machine_checks,
        "vertex_qa": {
            "location": vertex_location,
            "model": vertex_model,
            "video_fps": vertex_video_fps,
            "artifact": vertex_qa_artifact,
            "verdict": vertex_qa,
        },
        "frame_evidence_count": len(frame_artifacts),
        "frame_evidence": frame_artifacts,
        "elapsed_seconds": time.time() - started,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report_artifact = upload_create_only(
        report_path,
        f"{attempt_prefix}/verification/verification_report.json",
        "application/json",
    )
    print(
        json.dumps(
            {
                "stage": "qa-candidate-only",
                "candidate_uri": candidate_artifact["uri"],
                "candidate_sha256": candidate_artifact["sha256"],
                "report_uri": report_artifact["uri"],
                "elapsed_seconds": report["elapsed_seconds"],
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
