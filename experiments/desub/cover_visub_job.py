#!/usr/bin/env python3
"""CPU-only Chinese-subtitle detection, translation, cover, and Vietnamese TTS.

The job accepts either ``COVER_SOURCE_URI`` or ``COVER_DOUYIN_URL`` for a new
source.  When mask/cue/output URIs are omitted it creates a deterministic GCS
result prefix, detects subtitle clusters on CPU, asks Gemini for complete
Vietnamese cues, then renders the rounded cover and fixed-rate narration.
Explicit artifact URIs remain supported for reviewed/reproducible lab runs.  No
OCR path enters LaMa/STTN inpainting.
"""

from __future__ import annotations

import hashlib
import difflib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit, urlunsplit


sys.path.insert(0, "/app")
sys.path.insert(0, str(Path.cwd()))
sys.path.insert(0, "/app/experiments/desub")
import prototype as desub  # noqa: E402
from container_short.steps import capcut_tts, ffmpeg_ops  # noqa: E402
from container_short.steps.tts import synthesize_once as synthesize_google_once  # noqa: E402


Rect = tuple[int, int, int, int]
VALID_STYLES = {"box_black", "box_white", "blur"}
VALID_UNMATCHED = {"cover", "ignore"}
VALID_RECT_MODES = {"per_event", "cluster_union", "per_cue"}
VALID_FILL_MODES = {"extend_text", "blur_only"}
VALID_TTS_SUBTITLE_TIMING = {"cluster", "voice"}
DEFAULT_WORK_ROOT = Path("/tmp/desub_cover")
MIN_COVER_SUB_BAND_TOP_RATIO = 0.66
DEFAULT_RESULT_ROOT = (
    "gs://YOUR_GCP_PROJECT-media-sg/desub/cover-visub"
)
DEFAULT_PIPELINE_VERSION = "v1"


@dataclass(frozen=True)
class TextEvent:
    cue_index: int
    start: float
    end: float
    original_start: float
    original_end: float
    text_vi: str
    text_zh: str
    line_count: int
    text_x: int
    text_y: int
    font_size: int = 0

    def report_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PerEventGeometry:
    rect: Rect
    zh_rect: Rect
    text_block_rect: Rect | None
    previous_per_event_rect: Rect
    cluster_reference_rect: Rect
    candidate_detection_count: int
    accepted_detection_count: int
    rejected_detections: tuple[dict[str, Any], ...]
    fallback: bool
    warnings: tuple[dict[str, Any], ...]
    text_event: TextEvent | None = None


@dataclass(frozen=True)
class SubtitleLane:
    center_x: float
    center_y: float
    median_height: float
    tolerance_x: float
    tolerance_y: float
    min_height: float
    max_height: float
    calibration_detection_count: int
    support_detection_count: int = 0
    support_ratio: float = 0.0

    def report_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TtsCuePlan:
    cue_index: int
    text_vi: str
    original_start: float
    original_end: float
    slot_seconds: float
    fit_target_seconds: float
    delay_ms: int
    display_text_vi: str = ""
    source_cluster_index: int = -1


@dataclass(frozen=True)
class TtsMergeGroup:
    start_index: int
    end_index: int
    original_start: float
    slot_seconds: float
    fit_target_seconds: float
    trimmed_duration: float
    required_speed: float


@dataclass(frozen=True)
class TtsCascadeCue:
    plan_index: int
    cue_index: int
    source_cluster_index: int
    voice_start: float
    voice_end: float
    lag: float
    anchor: float
    target_seconds: float
    fit_target_seconds: float
    trimmed_duration: float
    fitted_duration: float
    required_speed: float
    speed_limit: float
    speed_applied: float
    used_hard_max_speed: bool
    overflow_into_next_slot: float
    delay_ms: int


@dataclass(frozen=True)
class SpeechTiming:
    cue_index: int
    visual_start: float
    visual_end: float
    speech_start: float
    speech_end: float
    raw_speech_start: float
    raw_speech_end: float
    text_zh: str
    text_vi: str


@dataclass(frozen=True)
class TtsSpeechCue:
    plan_index: int
    cue_index: int
    voice_start: float
    voice_end: float
    speech_start: float
    speech_end: float
    anchor: float
    target_seconds: float
    trimmed_duration: float
    fitted_duration: float
    required_speed: float
    speed_limit: float
    speed_applied: float
    used_hard_max_speed: bool
    delay_ms: int


@dataclass(frozen=True)
class TtsFixedCue:
    plan_index: int
    cue_index: int
    requested_start: float
    voice_start: float
    voice_end: float
    duration: float
    lag: float
    delay_ms: int


@dataclass
class CoverEvent:
    event_id: int
    start: float
    end: float
    rect: Rect
    source_cluster_index: int
    source_cluster_start: float
    source_cluster_end: float
    cue_index: int | None = None
    cue_start: float | None = None
    cue_end: float | None = None
    text_vi: str = ""
    text_zh: str = ""
    line_count: int = 0
    text_x: int | None = None
    text_y: int | None = None
    unmatched: bool = False
    rect_mode: str = "per_event"
    text_events: tuple[TextEvent, ...] = ()
    filled_intervals: tuple[dict[str, Any], ...] = ()
    zh_rect: Rect | None = None
    text_block_rect: Rect | None = None
    cluster_union_rect: Rect | None = None
    per_event_rect: Rect | None = None
    previous_per_event_rect: Rect | None = None
    cluster_reference_rect: Rect | None = None
    candidate_detection_count: int = 0
    active_detection_count: int = 0
    rejected_detection_count: int = 0
    rejected_detections: tuple[dict[str, Any], ...] = ()
    detection_fallback: bool = False
    geometry_warnings: tuple[dict[str, Any], ...] = ()

    def report_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["rect"] = list(self.rect)
        value["rect_size"] = [self.rect[2] - self.rect[0], self.rect[3] - self.rect[1]]
        for name in (
            "zh_rect",
            "text_block_rect",
            "cluster_union_rect",
            "per_event_rect",
            "previous_per_event_rect",
            "cluster_reference_rect",
        ):
            rect = getattr(self, name)
            value[name] = list(rect) if rect is not None else None
            value[f"{name}_size"] = (
                [rect[2] - rect[0], rect[3] - rect[1]] if rect is not None else None
            )
        value["text_events"] = [item.report_dict() for item in self.text_events]
        value["filled_intervals"] = [dict(item) for item in self.filled_intervals]
        value["rejected_detections"] = [dict(item) for item in self.rejected_detections]
        value["geometry_warnings"] = [dict(item) for item in self.geometry_warnings]
        return value


def log(message: str, **fields: Any) -> None:
    print(
        "[cover-visub] " + json.dumps({"message": message, **fields}, ensure_ascii=False, default=str),
        flush=True,
    )


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value, got {raw!r}")


def axis_padding_from_env(
    x_name: str,
    y_name: str,
    legacy_name: str,
    *,
    default_x: int,
    default_y: int,
) -> tuple[int, int, int]:
    legacy_raw = os.environ.get(legacy_name, "").strip()
    legacy = max(0, int(legacy_raw)) if legacy_raw else default_x
    x_raw = os.environ.get(x_name, "").strip()
    y_raw = os.environ.get(y_name, "").strip()
    x_value = max(0, int(x_raw)) if x_raw else (legacy if legacy_raw else default_x)
    y_value = max(0, int(y_raw)) if y_raw else (legacy if legacy_raw else default_y)
    return x_value, y_value, legacy


def parse_style() -> str:
    style = os.environ.get("VISUB_COVER_STYLE", "blur").strip().lower()
    if style not in VALID_STYLES:
        raise ValueError(f"VISUB_COVER_STYLE must be one of {sorted(VALID_STYLES)}, got {style!r}")
    return style


def parse_rect_mode() -> str:
    mode = os.environ.get("VISUB_COVER_RECT_MODE", "per_event").strip().lower()
    if mode not in VALID_RECT_MODES:
        raise ValueError(f"VISUB_COVER_RECT_MODE must be one of {sorted(VALID_RECT_MODES)}, got {mode!r}")
    return mode


def parse_fill_mode() -> str:
    mode = os.environ.get("VISUB_COVER_FILL", "extend_text").strip().lower()
    if mode not in VALID_FILL_MODES:
        raise ValueError(f"VISUB_COVER_FILL must be one of {sorted(VALID_FILL_MODES)}, got {mode!r}")
    return mode


def parse_tts_subtitle_timing() -> str:
    mode = os.environ.get("VISUB_TTS_SUBTITLE_TIMING", "cluster").strip().lower()
    if mode not in VALID_TTS_SUBTITLE_TIMING:
        raise ValueError(
            "VISUB_TTS_SUBTITLE_TIMING must be one of "
            f"{sorted(VALID_TTS_SUBTITLE_TIMING)}, got {mode!r}"
        )
    return mode


def parse_unmatched_mode() -> str:
    mode = os.environ.get("VISUB_COVER_UNMATCHED", "cover").strip().lower()
    if mode not in VALID_UNMATCHED:
        raise ValueError(
            f"VISUB_COVER_UNMATCHED must be one of {sorted(VALID_UNMATCHED)}, got {mode!r}"
        )
    return mode


def rect_tuple(value: Any) -> Rect:
    if isinstance(value, dict):
        result = (int(value["x1"]), int(value["y1"]), int(value["x2"]), int(value["y2"]))
    elif isinstance(value, (list, tuple)) and len(value) == 4:
        result = tuple(int(part) for part in value)  # type: ignore[assignment]
    else:
        raise ValueError(f"invalid rectangle: {value!r}")
    if result[2] <= result[0] or result[3] <= result[1]:
        raise ValueError(f"empty rectangle: {result}")
    return result


def union_rects(rects: Iterable[Rect]) -> Rect:
    values = list(rects)
    if not values:
        raise ValueError("cannot union an empty rectangle list")
    return (
        min(rect[0] for rect in values),
        min(rect[1] for rect in values),
        max(rect[2] for rect in values),
        max(rect[3] for rect in values),
    )


def dilate_rect(rect: Rect, pixels: int, width: int, height: int) -> Rect:
    pixels = max(0, pixels)
    return (
        max(0, rect[0] - pixels),
        max(0, rect[1] - pixels),
        min(width, rect[2] + pixels),
        min(height, rect[3] + pixels),
    )


def dilate_rect_xy(
    rect: Rect,
    pixels_x: int,
    pixels_y: int,
    width: int,
    height: int,
) -> Rect:
    pixels_x = max(0, pixels_x)
    pixels_y = max(0, pixels_y)
    return (
        max(0, rect[0] - pixels_x),
        max(0, rect[1] - pixels_y),
        min(width, rect[2] + pixels_x),
        min(height, rect[3] + pixels_y),
    )


def intersect_rects(left: Rect, right: Rect) -> Rect | None:
    result = (
        max(left[0], right[0]),
        max(left[1], right[1]),
        min(left[2], right[2]),
        min(left[3], right[3]),
    )
    return result if result[2] > result[0] and result[3] > result[1] else None


def _display_units(value: str) -> float:
    units = 0.0
    for char in value:
        if unicodedata.combining(char):
            continue
        if char.isspace():
            units += 0.34
        elif char in "ilI.,:;!'|`":
            units += 0.30
        elif char in "mwMW@%&":
            units += 0.86
        elif unicodedata.east_asian_width(char) in {"W", "F"}:
            units += 1.0
        else:
            units += 0.56
    return units


def subtitle_font_size(meta: desub.VideoMeta) -> int:
    return max(18, int(round(meta.height * env_float("DESUB_VISUB_FONT_SIZE_RATIO", 0.040))))


def fitted_subtitle_font_size(
    text: str,
    meta: desub.VideoMeta,
    *,
    padding_x_pixels: int = 0,
) -> int:
    lines = desub._balanced_subtitle_lines(text)  # noqa: SLF001
    base_size = subtitle_font_size(meta)
    minimum_size = max(
        18,
        min(
            base_size,
            int(round(meta.height * env_float("VISUB_COVER_MIN_FONT_SIZE_RATIO", 0.030))),
        ),
    )
    maximum_width = max(
        1,
        int(round(meta.width * env_float("VISUB_COVER_MAX_TEXT_WIDTH_RATIO", 0.88))),
    )
    outline = math.ceil(max(0.0, env_float("VISUB_COVER_TEXT_OUTLINE_PX", 2.5)))
    available_width = max(1, maximum_width - 2 * (max(0, padding_x_pixels) + outline))
    units = max((_display_units(line) for line in lines), default=1.0)
    fitted = int(math.floor(available_width / max(0.1, units)))
    # The configured minimum is a preference, while keeping the subtitle inside
    # the video safe width is a hard layout constraint.  Extremely long reviewed
    # captions may therefore shrink below that preference, but never below the
    # renderer's absolute 18px floor.
    if fitted < minimum_size:
        return max(18, min(base_size, fitted))
    return max(minimum_size, min(base_size, fitted))


def estimate_text_size(
    text: str,
    meta: desub.VideoMeta,
    *,
    font_size: int | None = None,
) -> tuple[int, int, list[str]]:
    lines = desub._balanced_subtitle_lines(text)  # noqa: SLF001 - deliberately reused lab helper
    font_size = font_size or fitted_subtitle_font_size(text, meta)
    outline = max(0.0, env_float("VISUB_COVER_TEXT_OUTLINE_PX", 2.5))
    padding = max(4, int(round(font_size * 0.15)))
    width = int(math.ceil(max((_display_units(line) for line in lines), default=0.0) * font_size))
    height = int(math.ceil(max(1, len(lines)) * font_size * 1.18))
    return width + 2 * (padding + math.ceil(outline)), height + 2 * (padding + math.ceil(outline)), lines


def estimate_text_block_size(
    text: str,
    meta: desub.VideoMeta,
    padding_x_pixels: int,
    padding_y_pixels: int | None = None,
    *,
    font_size: int | None = None,
) -> tuple[int, int, list[str]]:
    lines = desub._balanced_subtitle_lines(text)  # noqa: SLF001 - deliberately reused lab helper
    font_size = font_size or fitted_subtitle_font_size(
        text,
        meta,
        padding_x_pixels=max(0, int(padding_x_pixels)),
    )
    outline = max(0.0, env_float("VISUB_COVER_TEXT_OUTLINE_PX", 2.5))
    padding_x = max(0, int(padding_x_pixels)) + math.ceil(outline)
    padding_y = max(0, int(
        padding_x_pixels if padding_y_pixels is None else padding_y_pixels
    )) + math.ceil(outline)
    width = int(math.ceil(max((_display_units(line) for line in lines), default=0.0) * font_size))
    height = int(math.ceil(max(1, len(lines)) * font_size * 1.18))
    return width + 2 * padding_x, height + 2 * padding_y, lines


def expand_rect_to_size(rect: Rect, target_width: int, target_height: int, width: int, height: int) -> Rect:
    current_width = rect[2] - rect[0]
    current_height = rect[3] - rect[1]
    wanted_width = min(width, max(current_width, int(math.ceil(target_width))))
    wanted_height = min(height, max(current_height, int(math.ceil(target_height))))
    center_x = (rect[0] + rect[2]) / 2.0
    center_y = (rect[1] + rect[3]) / 2.0
    x1 = int(math.floor(center_x - wanted_width / 2.0))
    y1 = int(math.floor(center_y - wanted_height / 2.0))
    x1 = min(max(0, x1), width - wanted_width)
    y1 = min(max(0, y1), height - wanted_height)
    return x1, y1, x1 + wanted_width, y1 + wanted_height


def rect_centered_at(center_x: int, center_y: int, target_width: int, target_height: int, width: int, height: int) -> Rect:
    wanted_width = min(width, max(1, int(math.ceil(target_width))))
    wanted_height = min(height, max(1, int(math.ceil(target_height))))
    x1 = int(math.floor(center_x - wanted_width / 2.0))
    y1 = int(math.floor(center_y - wanted_height / 2.0))
    x1 = min(max(0, x1), width - wanted_width)
    y1 = min(max(0, y1), height - wanted_height)
    return x1, y1, x1 + wanted_width, y1 + wanted_height


def text_block_rect_for_event(
    text_event: TextEvent,
    meta: desub.VideoMeta,
    padding_x_pixels: int,
    padding_y_pixels: int | None = None,
) -> Rect:
    width, height, _ = estimate_text_block_size(
        text_event.text_vi,
        meta,
        padding_x_pixels,
        padding_y_pixels,
        font_size=(text_event.font_size or None),
    )
    return rect_centered_at(
        text_event.text_x,
        text_event.text_y,
        width,
        height,
        meta.width,
        meta.height,
    )


def cover_rect_for_cluster(
    rects: Sequence[Rect],
    text_vi: str,
    meta: desub.VideoMeta,
    dilate_pixels: int,
    *,
    text_center: tuple[int, int] | None = None,
) -> tuple[Rect, list[str]]:
    rect = dilate_rect(union_rects(rects), dilate_pixels, meta.width, meta.height)
    if not text_vi.strip():
        return rect, []
    text_width, text_height, lines = estimate_text_size(text_vi, meta)
    if text_center is None:
        return expand_rect_to_size(rect, text_width, text_height, meta.width, meta.height), lines
    text_rect = rect_centered_at(
        text_center[0],
        text_center[1],
        text_width,
        text_height,
        meta.width,
        meta.height,
    )
    return union_rects((rect, text_rect)), lines


def temporal_gap(start: float, end: float, other_start: float, other_end: float) -> float:
    if end < other_start:
        return other_start - end
    if other_end < start:
        return start - other_end
    return 0.0


def _normalized_clusters(mask_payload: dict[str, Any], duration: float) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    for source_index, source in enumerate(mask_payload.get("subtitle_clusters") or []):
        start = max(0.0, float(source["t_start"]))
        end = min(duration, float(source["t_end"]))
        rects = tuple(rect_tuple(value) for value in source.get("rects") or [])
        if not rects or end <= start:
            continue
        clusters.append({
            "source_index": source_index,
            "t_start": start,
            "t_end": end,
            "rects": rects,
        })
    if not clusters:
        raise ValueError("mask payload has no usable subtitle_clusters")
    return clusters


def _normalized_detections(mask_payload: dict[str, Any], duration: float) -> list[dict[str, Any]]:
    detections: list[dict[str, Any]] = []
    for source_index, source in enumerate(mask_payload.get("detections") or []):
        try:
            seconds = float(source["t"])
            rect = rect_tuple(source)
        except (KeyError, TypeError, ValueError):
            continue
        if seconds < 0.0 or seconds > duration:
            continue
        detections.append({
            "source_detection_index": source_index,
            "t": seconds,
            "rect": rect,
            "text": str(source.get("text") or ""),
            "detector": str(source.get("detector") or ""),
            "score": source.get("score"),
            "frame": source.get("frame"),
        })
    return detections


def calibrate_subtitle_lane(
    detections: Sequence[dict[str, Any]],
    meta: desub.VideoMeta,
) -> SubtitleLane:
    if not detections:
        return SubtitleLane(
            center_x=meta.width / 2.0,
            center_y=meta.height * 0.735,
            median_height=max(1.0, meta.height * 0.047),
            tolerance_x=max(1.0, meta.width * 0.125),
            tolerance_y=max(1.0, meta.height * 0.035),
            min_height=max(1.0, meta.height * 0.02),
            max_height=max(2.0, meta.height * 0.08),
            calibration_detection_count=0,
        )
    expected_x = meta.width / 2.0
    expected_y = meta.height * 0.735
    seed_candidates = [
        item
        for item in detections
        if abs(((item["rect"][1] + item["rect"][3]) / 2.0) - expected_y)
        <= meta.height * 0.07
        and abs(((item["rect"][0] + item["rect"][2]) / 2.0) - expected_x)
        <= meta.width * 0.25
    ] or list(detections)
    seed_center_x = float(statistics.median(
        (item["rect"][0] + item["rect"][2]) / 2.0
        for item in seed_candidates
    ))
    seed_center_y = float(statistics.median(
        (item["rect"][1] + item["rect"][3]) / 2.0
        for item in seed_candidates
    ))
    seed_height = max(1.0, float(statistics.median(
        item["rect"][3] - item["rect"][1]
        for item in seed_candidates
    )))
    support = [
        item
        for item in detections
        if abs(((item["rect"][0] + item["rect"][2]) / 2.0) - seed_center_x)
        <= max(meta.width * 0.125, seed_height * 1.5)
        and abs(((item["rect"][1] + item["rect"][3]) / 2.0) - seed_center_y)
        <= max(meta.height * 0.035, seed_height * 0.75)
        and seed_height * 0.70
        <= item["rect"][3] - item["rect"][1]
        <= seed_height * 1.25
    ] or list(detections)
    centers_x = sorted((item["rect"][0] + item["rect"][2]) / 2.0 for item in support)
    centers_y = sorted((item["rect"][1] + item["rect"][3]) / 2.0 for item in support)
    heights = sorted(item["rect"][3] - item["rect"][1] for item in support)
    center_x = float(statistics.median(centers_x))
    center_y = float(statistics.median(centers_y))
    median_height = max(1.0, float(statistics.median(heights)))
    return SubtitleLane(
        center_x=center_x,
        center_y=center_y,
        median_height=median_height,
        tolerance_x=max(meta.width * 0.125, median_height * 1.5),
        tolerance_y=max(meta.height * 0.035, median_height * 0.75),
        min_height=max(1.0, median_height * 0.70),
        max_height=max(2.0, median_height * 1.25),
        calibration_detection_count=len(detections),
        support_detection_count=len(support),
        support_ratio=len(support) / max(1, len(detections)),
    )


def detection_lane_rejection_reasons(
    detection: dict[str, Any],
    lane: SubtitleLane,
) -> list[str]:
    rect = detection["rect"]
    center_x = (rect[0] + rect[2]) / 2.0
    center_y = (rect[1] + rect[3]) / 2.0
    height = rect[3] - rect[1]
    reasons: list[str] = []
    if abs(center_x - lane.center_x) > lane.tolerance_x:
        reasons.append("outside_dominant_subtitle_lane_x")
    if abs(center_y - lane.center_y) > lane.tolerance_y:
        reasons.append("outside_dominant_subtitle_lane_y")
    if height < lane.min_height or height > lane.max_height:
        reasons.append("outside_dominant_subtitle_height")
    return reasons


def _normalized_detection_text(value: Any) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", str(value or "")).casefold()
        if character.isalnum() or "\u4e00" <= character <= "\u9fff"
    )


def detection_text_similarity(expected: str, observed: str) -> float:
    left = _normalized_detection_text(expected)
    right = _normalized_detection_text(observed)
    if not left or not right:
        return 0.0
    sequence = difflib.SequenceMatcher(None, left, right).ratio()
    left_chars = set(left)
    right_chars = set(right)
    overlap = len(left_chars & right_chars) / max(1, len(left_chars | right_chars))
    containment = (
        min(len(left), len(right)) / max(len(left), len(right))
        if left in right or right in left
        else 0.0
    )
    return max(sequence, overlap, containment)


def split_detection_tracks(
    detections: Sequence[dict[str, Any]],
    lane: SubtitleLane,
    *,
    maximum_gap_seconds: float = 0.35,
) -> list[list[dict[str, Any]]]:
    """Group temporally adjacent OCR boxes into stable spatial tracks."""
    tracks: list[list[dict[str, Any]]] = []
    for detection in sorted(detections, key=lambda item: (float(item["t"]), int(item["source_detection_index"]))):
        rect = detection["rect"]
        center_x = (rect[0] + rect[2]) / 2.0
        center_y = (rect[1] + rect[3]) / 2.0
        candidates: list[tuple[float, int]] = []
        for track_index, track in enumerate(tracks):
            previous = track[-1]
            gap = float(detection["t"]) - float(previous["t"])
            # One physical OCR track cannot yield two different boxes at the
            # exact same sampled frame.
            if gap <= 1e-6 or gap > max(0.01, maximum_gap_seconds):
                continue
            previous_rect = previous["rect"]
            previous_x = (previous_rect[0] + previous_rect[2]) / 2.0
            previous_y = (previous_rect[1] + previous_rect[3]) / 2.0
            delta_x = abs(center_x - previous_x)
            delta_y = abs(center_y - previous_y)
            if delta_x > max(36.0, lane.median_height * 1.20):
                continue
            if delta_y > max(18.0, lane.median_height * 0.55):
                continue
            previous_text = str(previous.get("text") or "")
            current_text = str(detection.get("text") or "")
            if (
                _normalized_detection_text(previous_text)
                and _normalized_detection_text(current_text)
                and detection_text_similarity(previous_text, current_text) < 0.25
            ):
                continue
            candidates.append((delta_x + 2.0 * delta_y, track_index))
        if candidates:
            tracks[min(candidates)[1]].append(detection)
        else:
            tracks.append([detection])
    return tracks


def select_event_detection_track(
    detections: Sequence[dict[str, Any]],
    text_event: TextEvent,
    lane: SubtitleLane,
    *,
    selection_guard_seconds: float,
    similarity_threshold: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    guard = max(0.0, selection_guard_seconds)
    original_window = [
        item
        for item in detections
        if text_event.original_start - guard
        <= float(item["t"])
        <= text_event.original_end + guard
    ]
    core_window = [
        item
        for item in original_window
        if text_event.original_start - 1e-6
        <= float(item["t"])
        <= text_event.original_end + 1e-6
    ]
    pool = core_window or original_window or list(detections)
    tracks = split_detection_tracks(pool, lane)
    if not tracks:
        return [], {"track_count": 0, "selection": "none"}

    threshold = max(0.0, similarity_threshold)
    scored: list[tuple[tuple[Any, ...], int, dict[str, Any]]] = []
    for track_index, track in enumerate(tracks):
        similarities = [
            detection_text_similarity(
                text_event.text_zh, str(item.get("text") or "")
            )
            for item in track
        ]
        hit_count = sum(value >= threshold for value in similarities)
        best_similarity = max(similarities, default=0.0)
        center_x = statistics.median(
            (item["rect"][0] + item["rect"][2]) / 2.0 for item in track
        )
        center_y = statistics.median(
            (item["rect"][1] + item["rect"][3]) / 2.0 for item in track
        )
        lane_distance = (
            abs(center_x - lane.center_x) / max(1.0, lane.tolerance_x)
            + abs(center_y - lane.center_y) / max(1.0, lane.tolerance_y)
        )
        report = {
            "track_index": track_index,
            "detection_count": len(track),
            "similarity_hit_count": hit_count,
            "best_similarity": best_similarity,
            "center_x": center_x,
            "center_y": center_y,
            "lane_distance": lane_distance,
            "detection_indexes": [
                int(item["source_detection_index"]) for item in track
            ],
        }
        score = (
            hit_count > 0,
            hit_count,
            best_similarity,
            -lane_distance,
            len(track),
        )
        scored.append((score, track_index, report))
    scored.sort(reverse=True, key=lambda item: item[0])
    _, selected_index, selected_report = scored[0]
    return tracks[selected_index], {
        "track_count": len(tracks),
        "selection": "ocr_and_lane" if selected_report["similarity_hit_count"] else "lane_fallback",
        "selected_track": selected_report,
        "candidate_tracks": [item[2] for item in scored],
    }


def robust_cluster_reference_rect(
    cluster: dict[str, Any],
    detections: Sequence[dict[str, Any]],
    lane: SubtitleLane,
    meta: desub.VideoMeta,
    *,
    temporal_margin_seconds: float = 0.30,
    connection_margin_pixels: int = 26,
) -> Rect:
    detection_times = [float(item["t"]) for item in detections]
    cluster_start = float(
        cluster.get("t_start", min(detection_times) if detection_times else 0.0)
    )
    cluster_end = float(
        cluster.get("t_end", max(detection_times) if detection_times else meta.duration)
    )
    rows = [
        item
        for item in detections
        if cluster_start - temporal_margin_seconds
        <= float(item["t"])
        <= cluster_end + temporal_margin_seconds
        and not detection_lane_rejection_reasons(item, lane)
    ]
    if rows:
        return union_rects([item["rect"] for item in rows])
    return subtitle_cluster_reference_rect(
        cluster["rects"],
        meta,
        connection_margin_pixels=connection_margin_pixels,
    )


def effective_subtitle_clusters(
    mask_payload: dict[str, Any],
    meta: desub.VideoMeta,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], SubtitleLane, dict[str, Any]]:
    raw_clusters = _normalized_clusters(mask_payload, meta.duration)
    detections = _normalized_detections(mask_payload, meta.duration)
    lane = calibrate_subtitle_lane(detections, meta)
    effective: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for cluster in raw_clusters:
        temporal_inliers = [
            item
            for item in detections
            if float(cluster["t_start"]) - 1e-6
            <= float(item["t"])
            <= float(cluster["t_end"]) + 1e-6
            and not (
                set(detection_lane_rejection_reasons(item, lane))
                - {"outside_dominant_subtitle_height"}
            )
        ]
        geometry_inliers = [
            item
            for item in temporal_inliers
            if not detection_lane_rejection_reasons(item, lane)
        ]
        if temporal_inliers:
            detection_start = min(float(item["t"]) for item in temporal_inliers)
            detection_end = max(float(item["t"]) for item in temporal_inliers)
            # A motion-blurred transition can be taller than the stable OCR lane.
            # It belongs to the cover timing but must not inflate the cover geometry.
            rects = tuple(
                item["rect"] for item in (geometry_inliers or temporal_inliers)
            )
        else:
            detection_start = None
            detection_end = None
            rects = tuple(cluster["rects"])
        # Mask clusters intentionally include fade/context frames which OCR may
        # miss.  Preserve their reviewed time bounds; only geometry is rebuilt
        # from the dominant subtitle lane.
        effective_start = float(cluster["t_start"])
        effective_end = float(cluster["t_end"])
        effective.append({
            **cluster,
            "mask_t_start": float(cluster["t_start"]),
            "mask_t_end": float(cluster["t_end"]),
            "t_start": effective_start,
            "t_end": effective_end,
            "rects": rects,
            "inlier_detection_count": len(geometry_inliers),
            "temporal_inlier_detection_count": len(temporal_inliers),
            "first_inlier_detection": detection_start,
            "last_inlier_detection": detection_end,
        })
        rows.append({
            "source_cluster_index": int(cluster["source_index"]),
            "mask_start": float(cluster["t_start"]),
            "mask_end": float(cluster["t_end"]),
            "first_inlier_detection": detection_start,
            "last_inlier_detection": detection_end,
            "effective_start": effective_start,
            "effective_end": effective_end,
            "trimmed_head_seconds": max(
                0.0, effective_start - float(cluster["t_start"])
            ),
            "trimmed_tail_seconds": max(
                0.0, float(cluster["t_end"]) - effective_end
            ),
            "inlier_detection_count": len(geometry_inliers),
            "temporal_inlier_detection_count": len(temporal_inliers),
            "height_relaxed_temporal_detection_count": (
                len(temporal_inliers) - len(geometry_inliers)
            ),
            "mask_context_before_first_detection_seconds": (
                max(0.0, detection_start - float(cluster["t_start"]))
                if detection_start is not None else None
            ),
            "mask_context_after_last_detection_seconds": (
                max(0.0, float(cluster["t_end"]) - detection_end)
                if detection_end is not None else None
            ),
            "fallback_to_mask_bounds": not temporal_inliers,
        })
    return effective, detections, lane, {
        "lane": lane.report_dict(),
        "cluster_count": len(rows),
        "fallback_cluster_count": sum(
            1 for item in rows if item["fallback_to_mask_bounds"]
        ),
        "total_trimmed_head_seconds": sum(
            float(item["trimmed_head_seconds"]) for item in rows
        ),
        "total_trimmed_tail_seconds": sum(
            float(item["trimmed_tail_seconds"]) for item in rows
        ),
        "clusters": rows,
    }


def cover_sub_band_top_ratio(mask_payload: dict[str, Any]) -> tuple[float, float]:
    raw = max(0.0, min(1.0, float(mask_payload.get("band_top_ratio", MIN_COVER_SUB_BAND_TOP_RATIO))))
    return raw, max(raw, MIN_COVER_SUB_BAND_TOP_RATIO)


def active_detections(
    detections: Sequence[dict[str, Any]],
    event_start: float,
    event_end: float,
    guard_seconds: float,
) -> list[dict[str, Any]]:
    start = event_start - max(0.0, guard_seconds)
    end = event_end + max(0.0, guard_seconds)
    return [item for item in detections if start - 1e-9 <= float(item["t"]) <= end + 1e-9]


def subtitle_cluster_reference_rect(
    rects: Sequence[Rect],
    meta: desub.VideoMeta,
    *,
    connection_margin_pixels: int,
) -> Rect:
    """Return the bottom connected component of a cluster's rectangle summary.

    Some historical masks grouped product/package text with the subtitle cluster.
    The real burned-in subtitle is the bottom-most component; only rectangles that
    connect to it after the same small gate expansion are used as the per-event
    detection reference.  The raw union remains available in the report.
    """
    values = list(rects)
    if not values:
        raise ValueError("cannot select a subtitle component from an empty rectangle list")
    seed = max(
        values,
        key=lambda rect: (
            (rect[1] + rect[3]) / 2.0,
            (rect[2] - rect[0]) * (rect[3] - rect[1]),
        ),
    )
    selected = [seed]
    remaining = list(values)
    remaining.remove(seed)
    while remaining:
        gate = dilate_rect(
            union_rects(selected),
            max(0, connection_margin_pixels),
            meta.width,
            meta.height,
        )
        connected = [rect for rect in remaining if intersect_rects(rect, gate) is not None]
        if not connected:
            break
        selected.extend(connected)
        remaining = [rect for rect in remaining if rect not in connected]
    return union_rects(selected)


def anchor_disconnected_text_events(
    text_events: Sequence[TextEvent],
    cluster: dict[str, Any],
    meta: desub.VideoMeta,
    *,
    text_padding_x_pixels: int,
    text_padding_y_pixels: int,
    connection_margin_pixels: int,
) -> tuple[list[TextEvent], dict[int, dict[str, Any]]]:
    """Anchor a vertically disconnected reviewed text position to the subtitle band.

    Reviewed cue coordinates normally sit on the source subtitle. One lab cue has
    a stale y coordinate on product text well above its matched subtitle cluster;
    honoring it creates a large blur bridge between the two regions. Only a text
    block with no vertical contact with the subtitle reference gate is re-anchored.
    """
    if not text_events:
        return [], {}
    reference = subtitle_cluster_reference_rect(
        cluster["rects"],
        meta,
        connection_margin_pixels=max(0, connection_margin_pixels),
    )
    gate_top = max(0, reference[1] - max(0, connection_margin_pixels))
    gate_bottom = min(meta.height, reference[3] + max(0, connection_margin_pixels))
    anchored_y = int(round((reference[1] + reference[3]) / 2.0))
    adjusted: list[TextEvent] = []
    warnings: dict[int, dict[str, Any]] = {}
    for text_event in text_events:
        block = text_block_rect_for_event(
            text_event,
            meta,
            text_padding_x_pixels,
            text_padding_y_pixels,
        )
        vertically_disconnected = block[3] < gate_top or block[1] > gate_bottom
        if not vertically_disconnected:
            adjusted.append(text_event)
            continue
        anchored = replace(text_event, text_y=anchored_y)
        anchored_block = text_block_rect_for_event(
            anchored,
            meta,
            text_padding_x_pixels,
            text_padding_y_pixels,
        )
        adjusted.append(anchored)
        warnings[text_event.cue_index] = {
            "kind": "text_position_anchored_to_subtitle_cluster",
            "cue_index": text_event.cue_index,
            "original_text_y": text_event.text_y,
            "anchored_text_y": anchored_y,
            "original_text_block_rect": list(block),
            "anchored_text_block_rect": list(anchored_block),
            "cluster_reference_rect": list(reference),
        }
    return adjusted, warnings


def per_event_cover_rect(
    cluster: dict[str, Any],
    detections: Sequence[dict[str, Any]],
    text_event: TextEvent | None,
    event_start: float,
    event_end: float,
    meta: desub.VideoMeta,
    *,
    dilate_pixels: int,
    dilate_x_pixels: int | None = None,
    dilate_y_pixels: int | None = None,
    detection_guard_seconds: float = 0.15,
    text_padding_pixels: int = 12,
    text_padding_x_pixels: int | None = None,
    text_padding_y_pixels: int | None = None,
    band_top_ratio: float = 0.66,
    cluster_intersection_margin_pixels: int = 16,
    zh_clamp_margin_pixels: int = 40,
    subtitle_lane: SubtitleLane | None = None,
    text_similarity_threshold: float = 0.42,
) -> PerEventGeometry:
    dilate_x = dilate_pixels if dilate_x_pixels is None else max(0, dilate_x_pixels)
    dilate_y = dilate_pixels if dilate_y_pixels is None else max(0, dilate_y_pixels)
    text_padding_x = (
        text_padding_pixels
        if text_padding_x_pixels is None
        else max(0, text_padding_x_pixels)
    )
    text_padding_y = (
        text_padding_pixels
        if text_padding_y_pixels is None
        else max(0, text_padding_y_pixels)
    )
    candidates = active_detections(detections, event_start, event_end, detection_guard_seconds)
    lane = subtitle_lane or calibrate_subtitle_lane(detections, meta)
    use_dominant_lane = subtitle_lane is not None
    cluster_union = union_rects(cluster["rects"])
    if use_dominant_lane:
        cluster_reference = robust_cluster_reference_rect(
            cluster,
            detections,
            lane,
            meta,
            connection_margin_pixels=dilate_pixels + max(0, cluster_intersection_margin_pixels),
        )
    else:
        # Direct callers may only provide detections for one event.  Such a tiny
        # sample cannot establish the global subtitle lane, so retain the legacy
        # cluster-component reference.  Production always passes the lane that
        # was calibrated from the complete mask timeline.
        cluster_reference = subtitle_cluster_reference_rect(
            cluster["rects"],
            meta,
            connection_margin_pixels=dilate_pixels + max(0, cluster_intersection_margin_pixels),
        )
    cluster_gate = dilate_rect(
        cluster_reference,
        dilate_pixels + max(0, cluster_intersection_margin_pixels),
        meta.width,
        meta.height,
    )
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    track_selection: dict[str, Any] | None = None
    band_top_y = max(0.0, min(1.0, band_top_ratio)) * meta.height
    for detection in candidates:
        rect = detection["rect"]
        center_y = (rect[1] + rect[3]) / 2.0
        reasons: list[str] = []
        prototype_band_match = desub.box_center_in_sub_band(rect, meta)
        if center_y < band_top_y or not prototype_band_match:
            reasons.append("center_out_sub_band")
        if use_dominant_lane:
            reasons.extend(detection_lane_rejection_reasons(detection, lane))
        if intersect_rects(rect, cluster_gate) is None:
            reasons.append("no_cluster_intersection")
        if reasons:
            rejected.append({
                "source_detection_index": detection["source_detection_index"],
                "t": detection["t"],
                "rect": list(rect),
                "center_y": center_y,
                "reasons": reasons,
            })
        else:
            accepted.append(detection)

    lane_accepted = list(accepted)
    anchor_detections = list(accepted)
    if text_event is not None and accepted:
        selection_guard = max(0.20, detection_guard_seconds)
        selected, track_selection = select_event_detection_track(
            accepted,
            text_event,
            lane,
            selection_guard_seconds=selection_guard,
            similarity_threshold=text_similarity_threshold,
        )
        selected_indexes = {
            int(item["source_detection_index"]) for item in selected
        }
        for item in accepted:
            if int(item["source_detection_index"]) in selected_indexes:
                continue
            if event_start - 1e-6 <= float(item["t"]) <= event_end + 1e-6:
                # The OCR track drives the text anchor, but an adjacent Chinese
                # caption can already be visible before a reviewed cue boundary.
                # Keep every active lane detection in blur geometry so no glyph
                # leaks during that transition.
                continue
            rect = item["rect"]
            rejected.append({
                "source_detection_index": item["source_detection_index"],
                "t": item["t"],
                "rect": list(rect),
                "center_y": (rect[1] + rect[3]) / 2.0,
                "ocr_text": str(item.get("text") or ""),
                "ocr_similarity": detection_text_similarity(
                    text_event.text_zh, str(item.get("text") or "")
                ),
                "reasons": ["ocr_track_mismatch"],
            })
        anchor_detections = selected

    geometry_detections_by_index = {
        int(item["source_detection_index"]): item
        for item in anchor_detections
    }
    for item in lane_accepted:
        if event_start - 1e-6 <= float(item["t"]) <= event_end + 1e-6:
            geometry_detections_by_index[int(item["source_detection_index"])] = item
    accepted = list(geometry_detections_by_index.values())

    fallback = not accepted
    zh_rect_unclamped = union_rects(
        [item["rect"] for item in accepted] if accepted else [cluster_reference]
    )
    clamp_bounds = dilate_rect(
        cluster_reference,
        max(0, zh_clamp_margin_pixels),
        meta.width,
        meta.height,
    )
    zh_rect = zh_rect_unclamped
    warnings: list[dict[str, Any]] = []
    if text_event is not None and track_selection is not None:
        warnings.append({
            "kind": "subtitle_detection_track_selected",
            "cue_index": text_event.cue_index,
            **track_selection,
        })
    if not rect_contains(clamp_bounds, zh_rect_unclamped):
        clamped = intersect_rects(zh_rect_unclamped, clamp_bounds)
        if clamped is None:
            clamped = cluster_reference
        warnings.append({
            "kind": "zh_rect_clamped_to_cluster_margin",
            "event_start": event_start,
            "event_end": event_end,
            "source_cluster_index": int(cluster["source_index"]),
            "original_zh_rect": list(zh_rect_unclamped),
            "raw_cluster_union_rect": list(cluster_union),
            "cluster_reference_rect": list(cluster_reference),
            "clamp_bounds": list(clamp_bounds),
            "clamped_zh_rect": list(clamped),
            "accepted_detection_indexes": [
                item["source_detection_index"] for item in accepted
            ],
        })
        zh_rect = clamped

    anchored_text_event = text_event
    if text_event is not None:
        if anchor_detections:
            center_x = int(round(statistics.median(
                (item["rect"][0] + item["rect"][2]) / 2.0 for item in anchor_detections
            )))
            subtitle_center_y = float(statistics.median(
                (item["rect"][1] + item["rect"][3]) / 2.0 for item in anchor_detections
            ))
        else:
            center_x = int(round((zh_rect[0] + zh_rect[2]) / 2.0))
            subtitle_center_y = (zh_rect[1] + zh_rect[3]) / 2.0
        line_spacing = (text_event.font_size or subtitle_font_size(meta)) * 1.18
        center_y = int(round(
            subtitle_center_y
            - max(0, text_event.line_count - 1) * line_spacing / 2.0
        ))
        anchored_text_event = replace(
            text_event,
            text_x=min(meta.width, max(0, center_x)),
            text_y=min(meta.height, max(0, center_y)),
        )
        if (
            anchored_text_event.text_x != text_event.text_x
            or anchored_text_event.text_y != text_event.text_y
        ):
            warnings.append({
                "kind": "text_position_anchored_to_selected_subtitle_track",
                "cue_index": text_event.cue_index,
                "original_text_x": text_event.text_x,
                "original_text_y": text_event.text_y,
                "anchored_text_x": anchored_text_event.text_x,
                "anchored_text_y": anchored_text_event.text_y,
                "selected_detection_indexes": [
                    item["source_detection_index"] for item in anchor_detections
                ],
            })
    text_rect = (
        text_block_rect_for_event(
            anchored_text_event,
            meta,
            text_padding_x,
            text_padding_y,
        )
        if anchored_text_event is not None
        else None
    )
    zh_cover_rect = dilate_rect_xy(
        zh_rect,
        dilate_x,
        dilate_y,
        meta.width,
        meta.height,
    )
    rect = union_rects(
        [zh_cover_rect, text_rect] if text_rect is not None else [zh_cover_rect]
    )
    previous_text_rect = (
        text_block_rect_for_event(anchored_text_event, meta, text_padding_pixels)
        if anchored_text_event is not None
        else None
    )
    previous_combined = union_rects(
        [zh_rect, previous_text_rect]
        if previous_text_rect is not None
        else [zh_rect]
    )
    previous_rect = dilate_rect(previous_combined, dilate_pixels, meta.width, meta.height)
    return PerEventGeometry(
        rect=rect,
        zh_rect=zh_rect,
        text_block_rect=text_rect,
        previous_per_event_rect=previous_rect,
        cluster_reference_rect=cluster_reference,
        candidate_detection_count=len(candidates),
        accepted_detection_count=len(accepted),
        rejected_detections=tuple(rejected),
        fallback=fallback,
        warnings=tuple(warnings),
        text_event=anchored_text_event,
    )


def _match_cluster(cue: dict[str, Any], clusters: Sequence[dict[str, Any]], meta: desub.VideoMeta) -> dict[str, Any]:
    start = float(cue["start"])
    end = float(cue["end"])
    cue_y = float(cue.get("center_y", 0.735)) * meta.height
    ranked: list[tuple[float, int, dict[str, Any]]] = []
    for source in clusters:
        source_start = float(source["t_start"])
        source_end = float(source["t_end"])
        overlap = max(0.0, min(end, source_end) - max(start, source_start))
        gap = temporal_gap(start, end, source_start, source_end)
        if overlap <= 0.0 and gap > 0.75:
            continue
        rects: Sequence[Rect] = source["rects"]
        y_distance = min(abs(((rect[1] + rect[3]) / 2.0) - cue_y) for rect in rects)
        score = overlap * 1000.0 - gap * 100.0 - y_distance * 0.01
        ranked.append((score, int(source["source_index"]), source))
    if not ranked:
        raise ValueError(f"no source mask cluster near cue {start:.3f}-{end:.3f}")
    return max(ranked, key=lambda item: (item[0], -item[1]))[2]


def normalize_cues(payload: Any, duration: float) -> list[dict[str, Any]]:
    raw_cues = payload.get("cues") if isinstance(payload, dict) else payload
    if not isinstance(raw_cues, list):
        raise ValueError("COVER_CUES_URI must contain an array or an object with a cues array")
    cues: list[dict[str, Any]] = []
    previous_start = -1.0
    for source_index, raw in enumerate(raw_cues):
        if not isinstance(raw, dict):
            raise ValueError(f"cue {source_index} is not an object")
        raw_start = float(raw.get("start", 0.0))
        raw_end = float(raw.get("end", 0.0))
        if raw_end <= 0.0 or raw_start >= duration:
            continue
        start = max(0.0, raw_start)
        end = min(duration, raw_end)
        text_vi = " ".join(str(raw.get("text_vi") or "").split())
        if not text_vi or end <= start or start < previous_start:
            raise ValueError(f"invalid reviewed cue {source_index}: {raw}")
        reviewed_lines = int(raw.get("line_count", len(desub._balanced_subtitle_lines(text_vi))))  # noqa: SLF001
        if reviewed_lines not in {1, 2}:
            raise ValueError(f"cue {source_index} must have a reviewed one- or two-line layout")
        cue = dict(raw)
        cue.update({
            "start": start,
            "end": end,
            "text_vi": text_vi,
            "text_zh": " ".join(str(raw.get("text_zh") or "").split()),
            "center_x": max(0.0, min(1.0, float(raw.get("center_x", 0.5)))),
            "center_y": max(0.0, min(1.0, float(raw.get("center_y", 0.735)))),
            "line_count": reviewed_lines,
            "source_cue_index": source_index,
        })
        cues.append(cue)
        previous_start = start
    if not cues:
        raise ValueError("reviewed cue payload is empty for the requested duration")
    return cues


def cluster_cover_windows(
    clusters: Sequence[dict[str, Any]],
    duration: float,
    pad_seconds: float,
) -> dict[int, tuple[float, float]]:
    """Pad cluster windows without crossing the midpoint of an adjacent gap."""
    ordered = sorted(clusters, key=lambda item: (float(item["t_start"]), float(item["t_end"])))
    windows: dict[int, tuple[float, float]] = {}
    for index, cluster in enumerate(ordered):
        source_index = int(cluster["source_index"])
        cluster_start = float(cluster["t_start"])
        cluster_end = float(cluster["t_end"])
        start = max(0.0, cluster_start - max(0.0, pad_seconds))
        end = min(duration, cluster_end + max(0.0, pad_seconds))
        if index > 0:
            previous_end = float(ordered[index - 1]["t_end"])
            if cluster_start >= previous_end:
                start = max(start, (previous_end + cluster_start) / 2.0)
        if index + 1 < len(ordered):
            following_start = float(ordered[index + 1]["t_start"])
            if following_start >= cluster_end:
                end = min(end, (cluster_end + following_start) / 2.0)
        windows[source_index] = (min(start, cluster_start), max(end, cluster_end))
    return windows


def tile_text_events(
    cues: Sequence[dict[str, Any]],
    cover_start: float,
    cover_end: float,
    meta: desub.VideoMeta,
    *,
    fill_mode: str = "extend_text",
    text_padding_x_pixels: int = 0,
) -> tuple[list[TextEvent], list[dict[str, Any]]]:
    """Create non-overlapping text windows within one cluster cover window."""
    if fill_mode not in VALID_FILL_MODES:
        raise ValueError(f"invalid fill mode: {fill_mode}")
    ordered = sorted(cues, key=lambda cue: (float(cue["start"]), float(cue["end"])))
    text_events: list[TextEvent] = []
    filled: list[dict[str, Any]] = []
    for index, cue in enumerate(ordered):
        original_start = max(cover_start, min(cover_end, float(cue["start"])))
        original_end = max(cover_start, min(cover_end, float(cue["end"])))
        if fill_mode == "extend_text":
            start = cover_start if index == 0 else original_start
            end = cover_end if index + 1 == len(ordered) else max(
                start,
                min(cover_end, float(ordered[index + 1]["start"])),
            )
        else:
            start = original_start
            end = original_end
            if index + 1 < len(ordered):
                end = min(end, max(start, float(ordered[index + 1]["start"])))
        if end <= start + 1e-6:
            continue
        text = str(cue["text_vi"])
        _, _, lines = estimate_text_size(text, meta)
        font_size = fitted_subtitle_font_size(
            text,
            meta,
            padding_x_pixels=text_padding_x_pixels,
        )
        cue_index = int(cue.get("source_cue_index", index))
        text_event = TextEvent(
            cue_index=cue_index,
            start=start,
            end=end,
            original_start=float(cue["start"]),
            original_end=float(cue["end"]),
            text_vi=text,
            text_zh=str(cue.get("text_zh") or ""),
            line_count=len(lines),
            text_x=min(meta.width, max(0, int(round(meta.width * float(cue.get("center_x", 0.5)))))),
            text_y=min(meta.height, max(0, int(round(meta.height * float(cue.get("center_y", 0.735)))))),
            font_size=font_size,
        )
        text_events.append(text_event)
        if fill_mode == "extend_text":
            if start < original_start - 1e-6:
                filled.append({
                    "cue_index": cue_index,
                    "start": start,
                    "end": original_start,
                    "kind": "head" if index == 0 else "gap",
                })
            if end > original_end + 1e-6:
                filled.append({
                    "cue_index": cue_index,
                    "start": original_end,
                    "end": end,
                    "kind": "tail" if index + 1 == len(ordered) else "gap",
                })
    return text_events, filled


def snap_text_event_boundaries_to_detections(
    text_events: Sequence[TextEvent],
    cluster: dict[str, Any],
    detections: Sequence[dict[str, Any]],
    lane: SubtitleLane,
    *,
    maximum_shift_seconds: float = 0.60,
    similarity_threshold: float = 0.42,
    maximum_transition_gap_seconds: float = 0.25,
) -> tuple[list[TextEvent], list[dict[str, Any]]]:
    if len(text_events) < 2:
        return list(text_events), []
    maximum_shift = max(0.0, maximum_shift_seconds)
    supports: list[list[dict[str, Any]]] = []
    for text_event in text_events:
        supports.append([
            detection
            for detection in detections
            if float(cluster["t_start"]) - 1e-6
            <= float(detection["t"])
            <= float(cluster["t_end"]) + 1e-6
            and text_event.original_start - maximum_shift
            <= float(detection["t"])
            <= text_event.original_end + maximum_shift
            and not detection_lane_rejection_reasons(detection, lane)
            and detection_text_similarity(
                text_event.text_zh, str(detection.get("text") or "")
            ) >= similarity_threshold
        ])
    boundaries = [float(item.start) for item in text_events]
    warnings: list[dict[str, Any]] = []
    for index in range(1, len(text_events)):
        previous_support = supports[index - 1]
        current_support = supports[index]
        if not previous_support or not current_support:
            continue
        previous_last = max(float(item["t"]) for item in previous_support)
        current_first = min(float(item["t"]) for item in current_support)
        if current_first < previous_last:
            continue
        if current_first - previous_last > max(0.0, maximum_transition_gap_seconds) + 1e-6:
            continue
        proposed = (previous_last + current_first) / 2.0
        original = float(text_events[index].start)
        if abs(proposed - original) > maximum_shift + 1e-6:
            continue
        proposed = max(float(text_events[index - 1].start), proposed)
        proposed = min(float(text_events[index].end), proposed)
        boundaries[index] = proposed
        warnings.append({
            "kind": "text_boundary_snapped_to_ocr_transition",
            "previous_cue_index": text_events[index - 1].cue_index,
            "cue_index": text_events[index].cue_index,
            "original_boundary": original,
            "snapped_boundary": proposed,
            "shift_seconds": proposed - original,
            "previous_last_match": previous_last,
            "current_first_match": current_first,
        })
    adjusted: list[TextEvent] = []
    for index, text_event in enumerate(text_events):
        start = float(text_event.start) if index == 0 else boundaries[index]
        end = (
            float(text_event.end)
            if index + 1 == len(text_events)
            else boundaries[index + 1]
        )
        adjusted.append(replace(text_event, start=start, end=max(start, end)))
    return adjusted, warnings


def _cluster_union_rect(
    cluster: dict[str, Any],
    text_events: Sequence[TextEvent],
    meta: desub.VideoMeta,
    dilate_x_pixels: int,
    dilate_y_pixels: int,
    text_padding_x_pixels: int,
    text_padding_y_pixels: int,
) -> Rect:
    rect = dilate_rect_xy(
        union_rects(cluster["rects"]),
        dilate_x_pixels,
        dilate_y_pixels,
        meta.width,
        meta.height,
    )
    text_rects = [
        text_block_rect_for_event(
            event,
            meta,
            text_padding_x_pixels,
            text_padding_y_pixels,
        )
        for event in text_events
    ]
    return union_rects([rect, *text_rects])


def _interval_complement(start: float, end: float, intervals: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    gaps: list[tuple[float, float]] = []
    cursor = start
    for left, right in sorted(intervals):
        left = max(start, left)
        right = min(end, right)
        if left > cursor + 1e-6:
            gaps.append((cursor, left))
        cursor = max(cursor, right)
    if cursor < end - 1e-6:
        gaps.append((cursor, end))
    return gaps


def build_cover_events(
    cues: list[dict[str, Any]],
    mask_payload: dict[str, Any],
    meta: desub.VideoMeta,
    *,
    dilate_pixels: int = 10,
    dilate_x_pixels: int | None = None,
    dilate_y_pixels: int | None = None,
    pad_seconds: float = 0.12,
    unmatched_mode: str = "cover",
    rect_mode: str = "per_event",
    fill_mode: str = "extend_text",
    detection_guard_seconds: float = 0.15,
    text_padding_pixels: int = 12,
    text_padding_x_pixels: int | None = None,
    text_padding_y_pixels: int | None = None,
) -> tuple[list[CoverEvent], list[dict[str, Any]], list[dict[str, Any]]]:
    if unmatched_mode not in VALID_UNMATCHED:
        raise ValueError(f"invalid unmatched mode: {unmatched_mode}")
    if rect_mode not in VALID_RECT_MODES:
        raise ValueError(f"invalid rect mode: {rect_mode}")
    if fill_mode not in VALID_FILL_MODES:
        raise ValueError(f"invalid fill mode: {fill_mode}")
    dilate_x = dilate_pixels if dilate_x_pixels is None else max(0, dilate_x_pixels)
    dilate_y = dilate_pixels if dilate_y_pixels is None else max(0, dilate_y_pixels)
    text_padding_x = (
        text_padding_pixels
        if text_padding_x_pixels is None
        else max(0, text_padding_x_pixels)
    )
    text_padding_y = (
        text_padding_pixels
        if text_padding_y_pixels is None
        else max(0, text_padding_y_pixels)
    )
    clusters, detections, subtitle_lane, _ = effective_subtitle_clusters(
        mask_payload, meta
    )
    _, band_top_ratio = cover_sub_band_top_ratio(mask_payload)
    windows = cluster_cover_windows(clusters, meta.duration, pad_seconds)
    grouped_cues: dict[int, list[dict[str, Any]]] = {}
    matched_indexes: set[int] = set()
    matches: list[dict[str, Any]] = []
    for cue_index, cue in enumerate(cues):
        cluster = _match_cluster(cue, clusters, meta)
        source_index = int(cluster["source_index"])
        matched_indexes.add(source_index)
        grouped_cues.setdefault(source_index, []).append(cue)
        matches.append({
            "cue_index": int(cue.get("source_cue_index", cue_index)),
            "cue_start": float(cue["start"]),
            "cue_end": float(cue["end"]),
            "source_cluster_index": source_index,
            "source_cluster_start": float(cluster["t_start"]),
            "source_cluster_end": float(cluster["t_end"]),
        })

    events: list[CoverEvent] = []
    orphans: list[dict[str, Any]] = []
    for cluster in clusters:
        source_index = int(cluster["source_index"])
        cover_start, cover_end = windows[source_index]
        cluster_cues = grouped_cues.get(source_index, [])
        text_events, filled = tile_text_events(
            cluster_cues,
            cover_start,
            cover_end,
            meta,
            fill_mode=fill_mode,
            text_padding_x_pixels=text_padding_x,
        )
        boundary_warnings: list[dict[str, Any]] = []
        if fill_mode == "extend_text":
            text_events, boundary_warnings = snap_text_event_boundaries_to_detections(
                text_events,
                cluster,
                detections,
                subtitle_lane,
                maximum_shift_seconds=max(
                    0.0, env_float("VISUB_COVER_MAX_BOUNDARY_SNAP_SECONDS", 0.30)
                ),
                similarity_threshold=max(
                    0.0, env_float("VISUB_COVER_OCR_SIMILARITY_THRESHOLD", 0.42)
                ),
                maximum_transition_gap_seconds=max(
                    0.0,
                    env_float(
                        "VISUB_COVER_MAX_OCR_TRANSITION_GAP_SECONDS", 0.25
                    ),
                ),
            )
        boundary_warnings_by_cue: dict[int, list[dict[str, Any]]] = {}
        for warning in boundary_warnings:
            boundary_warnings_by_cue.setdefault(
                int(warning["cue_index"]), []
            ).append(warning)
        geometry_by_cue: dict[int, PerEventGeometry] = {}
        anchored_text_events: list[TextEvent] = []
        for text_event in text_events:
            geometry = per_event_cover_rect(
                cluster,
                detections,
                text_event,
                text_event.start,
                text_event.end,
                meta,
                dilate_pixels=dilate_pixels,
                dilate_x_pixels=dilate_x,
                dilate_y_pixels=dilate_y,
                detection_guard_seconds=detection_guard_seconds,
                text_padding_pixels=text_padding_pixels,
                text_padding_x_pixels=text_padding_x,
                text_padding_y_pixels=text_padding_y,
                band_top_ratio=band_top_ratio,
                subtitle_lane=subtitle_lane,
                text_similarity_threshold=max(
                    0.0, env_float("VISUB_COVER_OCR_SIMILARITY_THRESHOLD", 0.42)
                ),
            )
            geometry_by_cue[text_event.cue_index] = geometry
            anchored_text_events.append(geometry.text_event or text_event)
        text_events = anchored_text_events
        cluster_reference = robust_cluster_reference_rect(
            cluster,
            detections,
            subtitle_lane,
            meta,
            connection_margin_pixels=dilate_pixels + 16,
        )
        base_rect = dilate_rect_xy(
            cluster_reference,
            dilate_x,
            dilate_y,
            meta.width,
            meta.height,
        )
        stable_rect = _cluster_union_rect(
            cluster,
            text_events,
            meta,
            dilate_x,
            dilate_y,
            text_padding_x,
            text_padding_y,
        )
        unmatched = source_index not in matched_indexes
        if unmatched:
            orphan = {
                "source_cluster_index": source_index,
                "source_cluster_start": float(cluster["t_start"]),
                "source_cluster_end": float(cluster["t_end"]),
                "cover_start": cover_start,
                "cover_end": cover_end,
                "rect": list(base_rect),
                "covered": unmatched_mode == "cover",
            }
            orphans.append(orphan)
            if unmatched_mode == "ignore":
                continue

        common = {
            "event_id": -1,
            "source_cluster_index": source_index,
            "source_cluster_start": float(cluster["t_start"]),
            "source_cluster_end": float(cluster["t_end"]),
            "unmatched": unmatched,
            "rect_mode": rect_mode,
        }
        if rect_mode == "cluster_union":
            events.append(CoverEvent(
                start=cover_start,
                end=cover_end,
                rect=stable_rect if not unmatched else base_rect,
                text_events=tuple(text_events),
                filled_intervals=tuple(filled),
                cluster_union_rect=stable_rect if not unmatched else base_rect,
                **common,
            ))
            continue

        for text_event in text_events:
            geometry = geometry_by_cue[text_event.cue_index]
            cue_rect = union_rects(
                [base_rect, geometry.text_block_rect]
                if geometry.text_block_rect is not None
                else [base_rect]
            )
            events.append(CoverEvent(
                start=text_event.start,
                end=text_event.end,
                rect=geometry.rect if rect_mode == "per_event" else cue_rect,
                text_events=(text_event,),
                filled_intervals=tuple(
                    item for item in filled if int(item["cue_index"]) == text_event.cue_index
                ),
                zh_rect=geometry.zh_rect,
                text_block_rect=geometry.text_block_rect,
                cluster_union_rect=stable_rect,
                per_event_rect=geometry.rect,
                previous_per_event_rect=geometry.previous_per_event_rect,
                cluster_reference_rect=geometry.cluster_reference_rect,
                candidate_detection_count=geometry.candidate_detection_count,
                active_detection_count=geometry.accepted_detection_count,
                rejected_detection_count=len(geometry.rejected_detections),
                rejected_detections=geometry.rejected_detections,
                detection_fallback=geometry.fallback,
                geometry_warnings=(
                    geometry.warnings
                    + tuple(boundary_warnings_by_cue.get(text_event.cue_index, []))
                ),
                **common,
            ))
        for gap_start, gap_end in _interval_complement(
            cover_start,
            cover_end,
            [(event.start, event.end) for event in text_events],
        ):
            geometry = per_event_cover_rect(
                cluster,
                detections,
                None,
                gap_start,
                gap_end,
                meta,
                dilate_pixels=dilate_pixels,
                dilate_x_pixels=dilate_x,
                dilate_y_pixels=dilate_y,
                detection_guard_seconds=detection_guard_seconds,
                text_padding_pixels=text_padding_pixels,
                text_padding_x_pixels=text_padding_x,
                text_padding_y_pixels=text_padding_y,
                band_top_ratio=band_top_ratio,
                subtitle_lane=subtitle_lane,
            )
            events.append(CoverEvent(
                start=gap_start,
                end=gap_end,
                rect=geometry.rect if rect_mode == "per_event" else base_rect,
                zh_rect=geometry.zh_rect,
                cluster_union_rect=stable_rect,
                per_event_rect=geometry.rect,
                previous_per_event_rect=geometry.previous_per_event_rect,
                cluster_reference_rect=geometry.cluster_reference_rect,
                candidate_detection_count=geometry.candidate_detection_count,
                active_detection_count=geometry.accepted_detection_count,
                rejected_detection_count=len(geometry.rejected_detections),
                rejected_detections=geometry.rejected_detections,
                detection_fallback=geometry.fallback,
                geometry_warnings=geometry.warnings,
                **common,
            ))

    events.sort(key=lambda event: (event.start, event.end, event.unmatched, event.source_cluster_index))
    for event_id, event in enumerate(events):
        event.event_id = event_id
    for match in matches:
        cue_event = next(
            (
                event
                for event in events
                if any(text.cue_index == int(match["cue_index"]) for text in event.text_events)
            ),
            None,
        )
        if cue_event is not None:
            match["rect"] = list(cue_event.rect)
            match["rect_size"] = [
                cue_event.rect[2] - cue_event.rect[0],
                cue_event.rect[3] - cue_event.rect[1],
            ]
    return events, matches, orphans


def rect_iou(left: Rect, right: Rect) -> float:
    x1 = max(left[0], right[0])
    y1 = max(left[1], right[1])
    x2 = min(left[2], right[2])
    y2 = min(left[3], right[3])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    left_area = (left[2] - left[0]) * (left[3] - left[1])
    right_area = (right[2] - right[0]) * (right[3] - right[1])
    return intersection / max(1, left_area + right_area - intersection)


def _rects_near(left: Rect, right: Rect, iou_threshold: float, near_pixels: int) -> bool:
    if rect_iou(left, right) >= iou_threshold:
        return True
    return max(abs(a - b) for a, b in zip(left, right)) <= near_pixels


def merge_blur_events(
    events: Sequence[CoverEvent],
    *,
    gap_seconds: float = 0.20,
    iou_threshold: float = 0.82,
    near_pixels: int = 12,
) -> list[CoverEvent]:
    merged: list[CoverEvent] = []
    for event in sorted(events, key=lambda item: (item.start, item.end)):
        if not merged:
            merged.append(replace(event))
            continue
        previous = merged[-1]
        if (
            event.source_cluster_index == previous.source_cluster_index
            and
            event.start <= previous.end + gap_seconds
            and _rects_near(previous.rect, event.rect, iou_threshold, near_pixels)
        ):
            previous.end = max(previous.end, event.end)
            previous.start = min(previous.start, event.start)
            previous.rect = union_rects((previous.rect, event.rect))
            previous.unmatched = previous.unmatched and event.unmatched
            previous.text_events = tuple(sorted(
                {item: None for item in (*previous.text_events, *event.text_events)},
                key=lambda item: (item.start, item.end, item.cue_index),
            ))
            previous.filled_intervals = (*previous.filled_intervals, *event.filled_intervals)
        else:
            merged.append(replace(event))
    for event_id, event in enumerate(merged):
        event.event_id = event_id
    return merged


def _ass_colour(style: str) -> tuple[str, str]:
    if style == "box_white":
        return "&H00FFFFFF", "&H00000000"
    return "&H00000000", "&H00FFFFFF"


def event_text_events(event: CoverEvent) -> tuple[TextEvent, ...]:
    if event.text_events:
        return event.text_events
    if not event.text_vi or event.cue_start is None or event.cue_end is None:
        return ()
    return (TextEvent(
        cue_index=event.cue_index if event.cue_index is not None else event.event_id,
        start=event.cue_start,
        end=event.cue_end,
        original_start=event.cue_start,
        original_end=event.cue_end,
        text_vi=event.text_vi,
        text_zh=event.text_zh,
        line_count=event.line_count,
        text_x=event.text_x if event.text_x is not None else (event.rect[0] + event.rect[2]) // 2,
        text_y=event.text_y if event.text_y is not None else (event.rect[1] + event.rect[3]) // 2,
    ),)


def build_tts_cue_plans(
    text_events: Sequence[TextEvent],
    video_duration: float,
    *,
    safety_seconds: float = 0.05,
    cluster_indexes_by_cue: dict[int, int] | None = None,
    tts_text_by_cue: dict[int, str] | None = None,
) -> list[TtsCuePlan]:
    unique: dict[int, TextEvent] = {}
    for event in text_events:
        previous = unique.get(event.cue_index)
        if previous is not None:
            same_source = (
                abs(previous.original_start - event.original_start) <= 1e-6
                and abs(previous.original_end - event.original_end) <= 1e-6
                and previous.text_vi == event.text_vi
            )
            if not same_source:
                raise ValueError(f"cue index {event.cue_index} has conflicting TTS events")
            continue
        unique[event.cue_index] = event
    ordered = sorted(unique.values(), key=lambda item: (item.original_start, item.cue_index))
    plans: list[TtsCuePlan] = []
    for index, event in enumerate(ordered):
        start = max(0.0, float(event.original_start))
        if start >= video_duration - 1e-6:
            continue
        next_start = (
            max(start, float(ordered[index + 1].original_start))
            if index + 1 < len(ordered)
            else video_duration
        )
        slot = min(video_duration, next_start) - start
        remaining = video_duration - start
        fit_target = min(slot - max(0.0, safety_seconds), remaining)
        if slot <= 0.0 or fit_target <= 0.05:
            raise ValueError(
                f"TTS cue {event.cue_index} has no safe slot: "
                f"start={start:.3f}, next={next_start:.3f}, slot={slot:.3f}"
            )
        display_text = event.text_vi.strip()
        spoken_text = (tts_text_by_cue or {}).get(event.cue_index, display_text).strip()
        if not spoken_text:
            raise ValueError(f"TTS cue {event.cue_index} has empty spoken text")
        plans.append(TtsCuePlan(
            cue_index=event.cue_index,
            text_vi=spoken_text,
            original_start=start,
            original_end=min(video_duration, max(start, float(event.original_end))),
            slot_seconds=slot,
            fit_target_seconds=fit_target,
            delay_ms=int(round(start * 1000.0)),
            display_text_vi=display_text,
            source_cluster_index=(cluster_indexes_by_cue or {}).get(event.cue_index, -1),
        ))
    return plans


def build_tts_fixed_rate_schedule(
    plans: Sequence[TtsCuePlan],
    trimmed_durations: Sequence[float],
    speech_timings_by_cue: dict[int, SpeechTiming],
    video_duration: float,
    *,
    gap_seconds: float = 0.08,
) -> list[TtsFixedCue]:
    """Schedule already-synthesized clips without any per-cue time scaling.

    The source video timeline is immutable.  A cue starts at its detected speech
    anchor when possible, otherwise it ripples after the preceding Vietnamese
    voice clip.  Large gaps later in the source let the schedule catch up.
    """
    if len(plans) != len(trimmed_durations):
        raise ValueError(
            f"TTS plan/duration count mismatch: {len(plans)} != {len(trimmed_durations)}"
        )
    missing = [plan.cue_index for plan in plans if plan.cue_index not in speech_timings_by_cue]
    if missing:
        raise ValueError(f"fixed-rate narration is missing speech timings: {missing}")
    schedule: list[TtsFixedCue] = []
    previous_end: float | None = None
    for plan_index, (plan, duration_value) in enumerate(zip(plans, trimmed_durations)):
        duration = max(0.0, float(duration_value))
        requested_start = max(
            0.0,
            float(speech_timings_by_cue[plan.cue_index].speech_start),
        )
        unrounded_start = max(
            requested_start,
            previous_end + max(0.0, gap_seconds)
            if previous_end is not None
            else requested_start,
        )
        delay_ms = int(math.ceil((unrounded_start - 1e-9) * 1000.0))
        voice_start = delay_ms / 1000.0
        voice_end = voice_start + duration
        schedule.append(TtsFixedCue(
            plan_index=plan_index,
            cue_index=plan.cue_index,
            requested_start=requested_start,
            voice_start=voice_start,
            voice_end=voice_end,
            duration=duration,
            lag=max(0.0, voice_start - requested_start),
            delay_ms=delay_ms,
        ))
        previous_end = voice_end
    validate_tts_fixed_rate_schedule(
        schedule,
        video_duration,
        gap_seconds=gap_seconds,
    )
    return schedule


def validate_tts_fixed_rate_schedule(
    schedule: Sequence[TtsFixedCue],
    video_duration: float,
    *,
    gap_seconds: float = 0.08,
    tolerance_seconds: float = 0.002,
) -> None:
    failures: list[dict[str, Any]] = []
    for previous, current in zip(schedule, schedule[1:]):
        actual_gap = current.voice_start - previous.voice_end
        if actual_gap + tolerance_seconds < max(0.0, gap_seconds):
            failures.append({
                "kind": "overlap",
                "previous_cue_index": previous.cue_index,
                "cue_index": current.cue_index,
                "gap": actual_gap,
            })
    if schedule and schedule[-1].voice_end > video_duration + tolerance_seconds:
        failures.append({
            "kind": "video_end",
            "cue_index": schedule[-1].cue_index,
            "voice_end": schedule[-1].voice_end,
            "video_duration": video_duration,
        })
    if failures:
        raise ValueError(f"TTS fixed-rate narration assertions failed: {failures}")


def retime_cover_text_to_voice(
    events: Sequence[CoverEvent],
    cue_reports: Sequence[dict[str, Any]],
) -> list[CoverEvent]:
    """Move only Vietnamese text timing; blur/cover geometry stays untouched."""
    timings = {
        int(item["cue_index"]): (
            float(item["voice_start"]),
            float(item["voice_end"]),
        )
        for item in cue_reports
        if item.get("voice_start") is not None and item.get("voice_end") is not None
    }
    updated_events: list[CoverEvent] = []
    seen: set[int] = set()
    for event in events:
        updated_text: list[TextEvent] = []
        for text_event in event_text_events(event):
            timing = timings.get(text_event.cue_index)
            if timing is None:
                raise ValueError(
                    f"missing fixed-rate voice timing for subtitle cue {text_event.cue_index}"
                )
            updated_text.append(replace(
                text_event,
                start=timing[0],
                end=timing[1],
            ))
            seen.add(text_event.cue_index)
        updated_events.append(replace(event, text_events=tuple(updated_text)))
    missing = sorted(set(timings) - seen)
    if missing:
        raise ValueError(f"fixed-rate voice timings have no subtitle events: {missing}")
    return updated_events


def tts_text_overrides_from_payload(payload: Any) -> dict[int, str]:
    if payload is None:
        return {}
    if isinstance(payload, dict) and "overrides" in payload:
        payload = payload["overrides"]
    overrides: dict[int, str] = {}
    if isinstance(payload, dict):
        items = payload.items()
    elif isinstance(payload, list):
        items = (
            (item.get("cue_index"), item.get("text_tts_vi"))
            for item in payload
            if isinstance(item, dict)
        )
    else:
        raise ValueError("TTS text overrides must be an object or a list")
    for raw_index, raw_text in items:
        try:
            cue_index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid TTS override cue index: {raw_index!r}") from exc
        text = str(raw_text or "").strip()
        if not text:
            raise ValueError(f"TTS override cue {cue_index} is empty")
        if cue_index in overrides:
            raise ValueError(f"duplicate TTS override cue {cue_index}")
        overrides[cue_index] = text
    return overrides


def stabilize_speech_timings(
    cues: Sequence[dict[str, Any]],
    raw_timings: Sequence[tuple[float, float]],
    video_duration: float,
    *,
    max_early_seconds: float = 0.30,
    max_late_seconds: float = 0.25,
) -> list[SpeechTiming]:
    if len(cues) != len(raw_timings):
        raise ValueError(
            f"speech timing/cue count mismatch: {len(raw_timings)} != {len(cues)}"
        )
    result: list[SpeechTiming] = []
    previous_start = -1.0
    for position, (cue, timing) in enumerate(zip(cues, raw_timings)):
        visual_start = max(0.0, float(cue["start"]))
        visual_end = min(video_duration, max(visual_start, float(cue["end"])))
        raw_start = max(0.0, float(timing[0]))
        raw_end = min(video_duration, float(timing[1]))
        if raw_end <= raw_start:
            raise ValueError(
                f"speech timing cue {position} has end <= start: {raw_start}, {raw_end}"
            )
        lower = max(0.0, visual_start - max(0.0, max_early_seconds))
        upper = min(video_duration, visual_start + max(0.0, max_late_seconds))
        speech_start = min(upper, max(lower, raw_start))
        speech_duration = raw_end - raw_start
        speech_end = min(video_duration, speech_start + speech_duration)
        speech_end = max(speech_start + 0.05, speech_end)
        if speech_start + 1e-6 < previous_start:
            raise ValueError(
                f"speech timing cue {position} is not ordered: "
                f"{speech_start:.3f} < {previous_start:.3f}"
            )
        cue_index = int(cue.get("source_cue_index", position))
        result.append(SpeechTiming(
            cue_index=cue_index,
            visual_start=visual_start,
            visual_end=visual_end,
            speech_start=speech_start,
            speech_end=speech_end,
            raw_speech_start=raw_start,
            raw_speech_end=raw_end,
            text_zh=str(cue.get("text_zh") or "").strip(),
            text_vi=str(cue.get("text_vi") or "").strip(),
        ))
        previous_start = speech_start
    return result


def speech_timings_from_payload(
    payload: Any,
    cues: Sequence[dict[str, Any]],
    video_duration: float,
    *,
    max_early_seconds: float = 0.30,
    max_late_seconds: float = 0.25,
) -> list[SpeechTiming]:
    items = payload.get("timings") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError("speech timing payload must contain a timings list")
    by_index: dict[int, tuple[float, float]] = {}
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"speech timing item {position} is not an object")
        cue_index = int(item.get("cue_index", item.get("index", position)))
        start = item.get("speech_start", item.get("start"))
        end = item.get("speech_end", item.get("end"))
        if start is None or end is None:
            raise ValueError(f"speech timing cue {cue_index} is missing start/end")
        if cue_index in by_index:
            raise ValueError(f"duplicate speech timing cue {cue_index}")
        by_index[cue_index] = (float(start), float(end))
    selected: list[tuple[float, float]] = []
    missing: list[int] = []
    for position, cue in enumerate(cues):
        cue_index = int(cue.get("source_cue_index", position))
        timing = by_index.get(cue_index)
        if timing is None:
            missing.append(cue_index)
        else:
            selected.append(timing)
    if missing:
        raise ValueError(f"speech timing payload is missing cues: {missing}")
    return stabilize_speech_timings(
        cues,
        selected,
        video_duration,
        max_early_seconds=max_early_seconds,
        max_late_seconds=max_late_seconds,
    )


def build_tts_speech_cue(
    plan: TtsCuePlan,
    timing: SpeechTiming,
    trimmed_duration: float,
    video_duration: float,
    *,
    next_speech_start: float | None,
    max_fit_speed: float,
    hard_max_speed: float,
    gap_seconds: float = 0.03,
    tail_seconds: float = 0.10,
    fit_guard_seconds: float = 0.0,
    plan_index: int = 0,
) -> TtsSpeechCue:
    delay_ms = int(math.ceil((max(0.0, timing.speech_start) - 1e-9) * 1000.0))
    voice_start = delay_ms / 1000.0
    anchor = min(video_duration, timing.speech_end + max(0.0, tail_seconds))
    if next_speech_start is not None:
        anchor = min(anchor, next_speech_start - max(0.0, gap_seconds))
    # fit_audio_duration/atempo can overshoot its requested duration by roughly
    # one audio frame. Reserve a small guard so the encoded clip still stays
    # inside the speech anchor and never touches the following voice clip.
    target_seconds = anchor - voice_start - max(0.0, fit_guard_seconds)
    if target_seconds <= 0.05:
        raise ValueError(
            f"speech-aligned cue {plan.cue_index} has no safe audio slot: "
            f"start={voice_start:.3f}, anchor={anchor:.3f}"
        )
    trimmed_duration = max(0.0, float(trimmed_duration))
    required_speed = trimmed_duration / target_seconds
    soft_limit = max(1.0, float(max_fit_speed))
    hard_limit = max(soft_limit, float(hard_max_speed))
    use_hard = required_speed > soft_limit + 1e-9
    speed_limit = hard_limit if use_hard else soft_limit
    expected_speed = (
        1.0
        if trimmed_duration <= target_seconds
        else min(speed_limit, required_speed)
    )
    fitted_duration = trimmed_duration / max(1.0, expected_speed)
    return TtsSpeechCue(
        plan_index=plan_index,
        cue_index=plan.cue_index,
        voice_start=voice_start,
        voice_end=voice_start + fitted_duration,
        speech_start=timing.speech_start,
        speech_end=timing.speech_end,
        anchor=anchor,
        target_seconds=target_seconds,
        trimmed_duration=trimmed_duration,
        fitted_duration=fitted_duration,
        required_speed=required_speed,
        speed_limit=speed_limit,
        speed_applied=expected_speed,
        used_hard_max_speed=use_hard,
        delay_ms=delay_ms,
    )


def validate_tts_speech_schedule(
    schedule: Sequence[TtsSpeechCue],
    video_duration: float,
    *,
    hard_max_speed: float,
    gap_seconds: float = 0.03,
    max_onset_error_seconds: float = 0.03,
    tolerance_seconds: float = 0.005,
) -> None:
    failures: list[dict[str, Any]] = []
    for item in schedule:
        onset_error = abs(item.voice_start - item.speech_start)
        if onset_error > max(0.0, max_onset_error_seconds) + tolerance_seconds:
            failures.append({
                "cue_index": item.cue_index,
                "kind": "onset_error",
                "onset_error": onset_error,
            })
        if item.speed_applied > hard_max_speed + 0.03:
            failures.append({
                "cue_index": item.cue_index,
                "kind": "speed",
                "speed_applied": item.speed_applied,
            })
        if item.voice_end > item.anchor + tolerance_seconds:
            failures.append({
                "cue_index": item.cue_index,
                "kind": "overflow",
                "voice_end": item.voice_end,
                "anchor": item.anchor,
            })
    for previous, current in zip(schedule, schedule[1:]):
        actual_gap = current.voice_start - previous.voice_end
        if actual_gap + tolerance_seconds < max(0.0, gap_seconds):
            failures.append({
                "cue_index": current.cue_index,
                "previous_cue_index": previous.cue_index,
                "kind": "overlap",
                "gap": actual_gap,
            })
    if schedule and schedule[-1].voice_end > video_duration + tolerance_seconds:
        failures.append({
            "cue_index": schedule[-1].cue_index,
            "kind": "video_end",
            "voice_end": schedule[-1].voice_end,
            "video_duration": video_duration,
        })
    if failures:
        raise ValueError(f"TTS speech-aligned schedule assertions failed: {failures}")


def generate_speech_timings_with_gemini(
    source_uri: str,
    cues: Sequence[dict[str, Any]],
    video_duration: float,
    *,
    project_id: str,
    region: str,
    model: str,
    attempts: int,
    max_early_seconds: float,
    max_late_seconds: float,
) -> list[SpeechTiming]:
    from container_short.steps import gemini_script  # noqa: PLC0415

    ocr_cues = [
        SimpleNamespace(
            start=float(cue["start"]),
            end=float(cue["end"]),
            text_zh=str(cue.get("text_zh") or ""),
        )
        for cue in cues
    ]
    raw = gemini_script.align_ocr_cues_to_speech(
        clip_gs_uri=source_uri,
        ocr_cues=ocr_cues,
        project_id=project_id,
        region=region,
        service_account_path=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or None,
        model=model,
        timeout=max(60, env_int("VISUB_TTS_ALIGN_TIMEOUT", 600)),
        attempts=max(1, attempts),
    )
    return stabilize_speech_timings(
        cues,
        raw,
        video_duration,
        max_early_seconds=max_early_seconds,
        max_late_seconds=max_late_seconds,
    )


def speech_timing_payload(
    timings: Sequence[SpeechTiming],
    *,
    source_uri: str,
    model: str,
    region: str,
) -> dict[str, Any]:
    return {
        "source_uri": source_uri,
        "model": model,
        "region": region,
        "cue_count": len(timings),
        "timings": [asdict(item) for item in timings],
    }


def tts_cascade_anchor(
    plans: Sequence[TtsCuePlan],
    index: int,
    video_duration: float,
    *,
    last_cluster_breath_seconds: float = 1.0,
) -> float:
    plan = plans[index]
    if index + 1 < len(plans):
        following = plans[index + 1]
        if following.source_cluster_index == plan.source_cluster_index:
            return min(video_duration, following.original_start)
        return min(
            video_duration,
            plan.original_end + max(0.0, last_cluster_breath_seconds),
            following.original_start,
        )
    return min(
        video_duration,
        plan.original_end + max(0.0, last_cluster_breath_seconds),
    )


def build_tts_cascade_cue(
    plans: Sequence[TtsCuePlan],
    index: int,
    trimmed_duration: float,
    previous_voice_end: float | None,
    video_duration: float,
    *,
    max_fit_speed: float,
    hard_max_speed: float,
    gap_seconds: float = 0.03,
    safety_seconds: float = 0.05,
    last_cluster_breath_seconds: float = 1.0,
) -> TtsCascadeCue:
    plan = plans[index]
    requested_start = max(0.0, plan.original_start)
    unrounded_start = max(
        requested_start,
        (previous_voice_end + max(0.0, gap_seconds))
        if previous_voice_end is not None
        else requested_start,
    )
    delay_ms = int(math.ceil((unrounded_start - 1e-9) * 1000.0))
    voice_start = delay_ms / 1000.0
    lag = max(0.0, voice_start - requested_start)
    anchor = tts_cascade_anchor(
        plans,
        index,
        video_duration,
        last_cluster_breath_seconds=last_cluster_breath_seconds,
    )
    target_seconds = anchor - voice_start - max(0.0, safety_seconds)
    fit_target_seconds = max(0.05, target_seconds)
    trimmed_duration = max(0.0, float(trimmed_duration))
    required_speed = (
        trimmed_duration / target_seconds if target_seconds > 0.0 else math.inf
    )
    normal_limit = max(1.0, float(max_fit_speed))
    hard_limit = max(normal_limit, float(hard_max_speed))
    use_hard = required_speed > normal_limit + 1e-9
    speed_limit = hard_limit if use_hard else normal_limit
    if trimmed_duration <= fit_target_seconds:
        expected_speed = 1.0
        fitted_duration = trimmed_duration
    else:
        expected_speed = min(speed_limit, trimmed_duration / fit_target_seconds)
        fitted_duration = trimmed_duration / max(1.0, expected_speed)
    voice_end = voice_start + fitted_duration
    next_slot_start = (
        plans[index + 1].original_start if index + 1 < len(plans) else video_duration
    )
    return TtsCascadeCue(
        plan_index=index,
        cue_index=plan.cue_index,
        source_cluster_index=plan.source_cluster_index,
        voice_start=voice_start,
        voice_end=voice_end,
        lag=lag,
        anchor=anchor,
        target_seconds=target_seconds,
        fit_target_seconds=fit_target_seconds,
        trimmed_duration=trimmed_duration,
        fitted_duration=fitted_duration,
        required_speed=required_speed,
        speed_limit=speed_limit,
        speed_applied=expected_speed,
        used_hard_max_speed=use_hard,
        overflow_into_next_slot=max(0.0, voice_end - next_slot_start),
        delay_ms=delay_ms,
    )


def validate_tts_cascade_schedule(
    schedule: Sequence[TtsCascadeCue],
    video_duration: float,
    *,
    max_lag_seconds: float,
    gap_seconds: float = 0.03,
    tolerance_seconds: float = 0.002,
) -> None:
    overlap_failures: list[dict[str, Any]] = []
    for previous, current in zip(schedule, schedule[1:]):
        actual_gap = current.voice_start - previous.voice_end
        if actual_gap + tolerance_seconds < max(0.0, gap_seconds):
            overlap_failures.append({
                "previous_cue_index": previous.cue_index,
                "cue_index": current.cue_index,
                "previous_voice_end": previous.voice_end,
                "voice_start": current.voice_start,
                "gap": actual_gap,
            })
    if overlap_failures:
        raise ValueError(f"TTS cascade overlap/gap failures: {overlap_failures}")

    lag_failures = [
        {
            "cue_index": item.cue_index,
            "requested_start": plans_start,
            "voice_start": item.voice_start,
            "lag": item.lag,
        }
        for item in schedule
        if item.lag > max(0.0, max_lag_seconds) + tolerance_seconds
        for plans_start in [item.voice_start - item.lag]
    ]
    if lag_failures:
        raise ValueError(
            f"TTS cascade max lag exceeds {max_lag_seconds:.3f}s: {lag_failures}"
        )
    if schedule and schedule[-1].voice_end > video_duration + tolerance_seconds:
        last = schedule[-1]
        raise ValueError(
            f"TTS final cue {last.cue_index} exceeds video duration: "
            f"voice_end={last.voice_end:.3f}, duration={video_duration:.3f}"
        )


def build_tts_cascade_schedule(
    plans: Sequence[TtsCuePlan],
    trimmed_durations: Sequence[float],
    video_duration: float,
    *,
    max_fit_speed: float,
    hard_max_speed: float,
    max_lag_seconds: float,
    gap_seconds: float = 0.03,
    safety_seconds: float = 0.05,
    last_cluster_breath_seconds: float = 1.0,
) -> list[TtsCascadeCue]:
    if len(plans) != len(trimmed_durations):
        raise ValueError(
            f"TTS plan/duration count mismatch: {len(plans)} != {len(trimmed_durations)}"
        )
    schedule: list[TtsCascadeCue] = []
    for index, duration in enumerate(trimmed_durations):
        schedule.append(build_tts_cascade_cue(
            plans,
            index,
            duration,
            schedule[-1].voice_end if schedule else None,
            video_duration,
            max_fit_speed=max_fit_speed,
            hard_max_speed=hard_max_speed,
            gap_seconds=gap_seconds,
            safety_seconds=safety_seconds,
            last_cluster_breath_seconds=last_cluster_breath_seconds,
        ))
    validate_tts_cascade_schedule(
        schedule,
        video_duration,
        max_lag_seconds=max_lag_seconds,
        gap_seconds=gap_seconds,
    )
    return schedule


def validate_tts_fits(
    plans: Sequence[TtsCuePlan],
    fitted_durations: Sequence[float],
    *,
    tolerance_seconds: float = 0.002,
) -> None:
    if len(plans) != len(fitted_durations):
        raise ValueError(
            f"TTS plan/result count mismatch: {len(plans)} != {len(fitted_durations)}"
        )
    for plan, fitted_duration in zip(plans, fitted_durations):
        actual_start = plan.delay_ms / 1000.0
        slot_end = plan.original_start + plan.slot_seconds
        voice_end = actual_start + max(0.0, float(fitted_duration))
        if voice_end > slot_end + tolerance_seconds:
            raise ValueError(
                f"TTS cue {plan.cue_index} overlaps the next cue: "
                f"voice_end={voice_end:.3f}, slot_end={slot_end:.3f}, "
                f"duration={fitted_duration:.3f}"
            )


def build_tts_merge_groups(
    plans: Sequence[TtsCuePlan],
    trimmed_durations: Sequence[float],
    *,
    max_fit_speed: float,
    policy: str,
    safety_seconds: float = 0.05,
) -> list[TtsMergeGroup]:
    if len(plans) != len(trimmed_durations):
        raise ValueError(
            f"TTS plan/duration count mismatch: {len(plans)} != {len(trimmed_durations)}"
        )
    policy = policy.strip().lower()
    if policy not in {"fail", "merge_short_slots"}:
        raise ValueError("VISUB_TTS_SHORT_SLOT_POLICY must be fail or merge_short_slots")
    speed_limit = max(1.0, float(max_fit_speed))
    groups: list[TtsMergeGroup] = []
    start_index = 0
    while start_index < len(plans):
        end_index = start_index
        while True:
            start = plans[start_index].original_start
            group_end = plans[end_index].original_start + plans[end_index].slot_seconds
            slot = group_end - start
            target = slot - max(0.0, safety_seconds)
            trimmed = sum(
                max(0.0, float(value))
                for value in trimmed_durations[start_index : end_index + 1]
            )
            required_speed = trimmed / target if target > 0.0 else math.inf
            if target > 0.05 and required_speed <= speed_limit + 1e-6:
                groups.append(TtsMergeGroup(
                    start_index=start_index,
                    end_index=end_index,
                    original_start=start,
                    slot_seconds=slot,
                    fit_target_seconds=target,
                    trimmed_duration=trimmed,
                    required_speed=max(1.0, required_speed),
                ))
                start_index = end_index + 1
                break
            if policy == "fail":
                indexes = [plan.cue_index for plan in plans[start_index : end_index + 1]]
                raise ValueError(
                    f"TTS cues {indexes} need {required_speed:.3f}x to fit "
                    f"slot={slot:.3f}s (target={target:.3f}s), above "
                    f"VISUB_TTS_MAX_FIT_SPEED={speed_limit:.3f}"
                )
            if end_index + 1 < len(plans):
                end_index += 1
                continue
            if groups:
                # A too-short final cue has no following slot to borrow. Merge it
                # backwards with the previous finalized group and reevaluate the
                # combined window. Repeated pops handle pathological short tails.
                previous = groups.pop()
                start_index = previous.start_index
                continue
            indexes = [plan.cue_index for plan in plans[start_index : end_index + 1]]
            raise ValueError(
                f"TTS cues {indexes} need {required_speed:.3f}x to fit "
                f"slot={slot:.3f}s (target={target:.3f}s), above "
                f"VISUB_TTS_MAX_FIT_SPEED={speed_limit:.3f}"
            )
    return groups


def concat_tts_wavs(paths: Sequence[Path], destination: Path) -> float:
    if not paths:
        raise ValueError("cannot concatenate an empty TTS group")
    if len(paths) == 1:
        return ffmpeg_ops.duration_seconds(str(paths[0]))
    args = ["ffmpeg", "-y"]
    for path in paths:
        args.extend(["-i", str(path)])
    normalized = [
        f"[{index}:a]aformat=sample_fmts=s16:sample_rates=44100:channel_layouts=mono[a{index}]"
        for index in range(len(paths))
    ]
    labels = "".join(f"[a{index}]" for index in range(len(paths)))
    graph = ";".join([
        *normalized,
        f"{labels}concat=n={len(paths)}:v=0:a=1[aout]",
    ])
    args.extend([
        "-filter_complex", graph,
        "-map", "[aout]",
        "-c:a", "pcm_s16le",
        "-ar", "44100",
        "-ac", "1",
        str(destination),
    ])
    desub.cmd(args, timeout=_ffmpeg_timeout())
    return ffmpeg_ops.duration_seconds(str(destination))


def atempo_filter_chain(speed: float) -> str:
    """Build an ffmpeg atempo chain for any positive playback speed."""
    remaining = float(speed)
    if remaining <= 0.0:
        raise ValueError(f"audio speed must be positive, got {speed}")
    factors: list[float] = []
    while remaining > 2.0 + 1e-9:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5 - 1e-9:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    return ",".join(f"atempo={factor:.6f}" for factor in factors)


def apply_uniform_audio_speed(source: Path, destination: Path, speed: float) -> float:
    """Apply a measurable uniform speed after synthesis and return duration."""
    desub.cmd([
        "ffmpeg", "-y", "-i", str(source),
        "-filter:a", atempo_filter_chain(speed),
        "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "1",
        str(destination),
    ], timeout=_ffmpeg_timeout())
    return ffmpeg_ops.duration_seconds(str(destination))


def synthesize_tts_voice_clips(
    events: Sequence[CoverEvent],
    work: Path,
    video_duration: float,
    *,
    voice: str,
    resource_id: str,
    rate: float,
    max_fit_speed: float,
    hard_max_speed: float,
    max_lag_seconds: float,
    fallback: str,
    short_slot_policy: str,
    tts_text_by_cue: dict[int, str] | None = None,
    speech_timings_by_cue: dict[int, SpeechTiming] | None = None,
    speech_tail_seconds: float = 0.20,
    fit_guard_seconds: float = 0.015,
    max_onset_error_seconds: float = 0.03,
    fixed_rate_gap_seconds: float = 0.05,
) -> tuple[list[tuple[str, int]], dict[str, Any]]:
    started = time.time()
    all_text_events: list[TextEvent] = []
    cluster_indexes_by_cue: dict[int, int] = {}
    for event in events:
        for text_event in event_text_events(event):
            previous_cluster = cluster_indexes_by_cue.get(text_event.cue_index)
            if (
                previous_cluster is not None
                and previous_cluster != event.source_cluster_index
            ):
                raise ValueError(
                    f"cue {text_event.cue_index} maps to multiple subtitle clusters"
                )
            cluster_indexes_by_cue[text_event.cue_index] = event.source_cluster_index
            all_text_events.append(text_event)
    plans = build_tts_cue_plans(
        all_text_events,
        video_duration,
        cluster_indexes_by_cue=cluster_indexes_by_cue,
        tts_text_by_cue=tts_text_by_cue,
    )
    if not plans:
        raise ValueError("VISUB_TTS_ENABLED=true but there are no reviewed text cues")
    short_slot_policy = short_slot_policy.strip().lower()
    if short_slot_policy not in {
        "fixed_rate_narration",
        "speech_aligned",
        "cascade",
        "merge_short_slots",
        "fail",
    }:
        raise ValueError(
            "VISUB_TTS_SHORT_SLOT_POLICY must be fixed_rate_narration, "
            "speech_aligned, cascade, merge_short_slots, or fail"
        )
    fallback = fallback.strip().lower()
    if fallback not in {"google", "none"}:
        raise ValueError("VISUB_TTS_FALLBACK must be google or none")
    device_json = (
        os.environ.get("VISUB_TTS_DEVICE_JSON", "").strip()
        or os.environ.get("CAPCUT_DEVICE_JSON", "").strip()
        or None
    )
    device = capcut_tts.make_device(device_json)
    poll_timeout = max(10, env_int("VISUB_TTS_POLL_TIMEOUT", 300))
    google_voice = os.environ.get("GOOGLE_TTS_VOICE", "vi-VN-Wavenet-B").strip()
    service_account_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or None
    trimmed_paths: list[Path] = []
    trimmed_durations: list[float] = []
    cue_reports: list[dict[str, Any]] = []
    total_synthesis_seconds = 0.0
    google_fallback_count = 0
    for index, plan in enumerate(plans):
        raw_mp3 = work / f"voice_{index:03d}_raw.mp3"
        trim_wav = work / f"voice_{index:03d}_trim.wav"
        base_trim_wav = (
            work / f"voice_{index:03d}_trim_base.wav"
            if short_slot_policy == "fixed_rate_narration"
            and abs(rate - 1.0) > 1e-6
            else trim_wav
        )
        provider_synthesis_rate = (
            1.0 if short_slot_policy == "fixed_rate_narration" else rate
        )
        provider = "capcut"
        capcut_errors: list[str] = []
        capcut_attempts = 0
        synthesized = False
        for attempt in range(1, 4):
            capcut_attempts = attempt
            raw_mp3.unlink(missing_ok=True)
            attempt_started = time.time()
            try:
                capcut_tts.synthesize_once(
                    plan.text_vi,
                    str(raw_mp3),
                    voice=voice,
                    resource_id=resource_id,
                    device=device,
                    rate=provider_synthesis_rate,
                    poll_timeout=poll_timeout,
                )
                synthesized = True
                total_synthesis_seconds += time.time() - attempt_started
                break
            except Exception as exc:  # noqa: BLE001
                total_synthesis_seconds += time.time() - attempt_started
                capcut_errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
                log(
                    "capcut_tts_retry",
                    cue_index=plan.cue_index,
                    attempt=attempt,
                    max_attempts=3,
                    error=str(exc),
                )
        if not synthesized:
            if fallback == "none":
                raise RuntimeError(
                    f"CapCut TTS failed after 3 attempts for cue {plan.cue_index}: "
                    + " | ".join(capcut_errors)
                )
            provider = "google"
            google_fallback_count += 1
            raw_mp3.unlink(missing_ok=True)
            fallback_started = time.time()
            synthesize_google_once(
                plan.text_vi,
                str(raw_mp3),
                voice=google_voice,
                service_account_path=service_account_path,
                rate=(
                    provider_synthesis_rate
                    if short_slot_policy == "fixed_rate_narration"
                    else 1.25
                ),
            )
            total_synthesis_seconds += time.time() - fallback_started

        raw_duration = ffmpeg_ops.duration_seconds(str(raw_mp3))
        base_trimmed_duration = ffmpeg_ops.trim_tts_silence(
            str(raw_mp3),
            str(base_trim_wav),
        )
        if base_trim_wav != trim_wav:
            trimmed_duration = apply_uniform_audio_speed(
                base_trim_wav,
                trim_wav,
                rate,
            )
        else:
            trimmed_duration = base_trimmed_duration
        trimmed_paths.append(trim_wav)
        trimmed_durations.append(trimmed_duration)
        cue_reports.append({
            "cue_index": plan.cue_index,
            "text_vi": plan.display_text_vi or plan.text_vi,
            "text_tts_vi": plan.text_vi,
            "tts_text_compacted": plan.text_vi != (plan.display_text_vi or plan.text_vi),
            "original_start": plan.original_start,
            "original_end": plan.original_end,
            "slot_seconds": plan.slot_seconds,
            "fit_target_seconds": plan.fit_target_seconds,
            "provider": provider,
            "capcut_attempts": capcut_attempts,
            "capcut_errors": capcut_errors,
            "raw_dur": raw_duration,
            "trimmed_dur_before_rate": base_trimmed_duration,
            "trimmed_dur": trimmed_duration,
            "provider_synthesis_rate": provider_synthesis_rate,
            "delay_ms": plan.delay_ms,
        })

    if short_slot_policy == "fixed_rate_narration":
        timings = speech_timings_by_cue or {}
        schedule = build_tts_fixed_rate_schedule(
            plans,
            trimmed_durations,
            timings,
            video_duration,
            gap_seconds=fixed_rate_gap_seconds,
        )
        voice_clips: list[tuple[str, int]] = []
        segment_reports: list[dict[str, Any]] = []
        for index, item in enumerate(schedule):
            voice_clips.append((str(trimmed_paths[index]), item.delay_ms))
            timing = timings[item.cue_index]
            next_requested = (
                schedule[index + 1].requested_start
                if index + 1 < len(schedule)
                else video_duration
            )
            cue_reports[index].update({
                "fitted_dur": item.duration,
                "speed_applied": rate,
                "synthesis_rate": provider_synthesis_rate,
                "post_fit_speed": rate,
                "required_speed": 1.0,
                "speed_limit": rate,
                "used_hard_max_speed": False,
                "speech_start": timing.speech_start,
                "speech_end": timing.speech_end,
                "raw_speech_start": timing.raw_speech_start,
                "raw_speech_end": timing.raw_speech_end,
                "voice_start": item.voice_start,
                "voice_end": item.voice_end,
                "onset_error": item.lag,
                "lag": item.lag,
                "overflow_into_next_slot": max(0.0, item.voice_end - next_requested),
                "requested_delay_ms": int(round(item.requested_start * 1000.0)),
                "actual_delay_ms": item.delay_ms,
                "segment_id": index,
                "segment_cue_indexes": [item.cue_index],
                "merged": False,
            })
            segment_reports.append({
                "segment_id": index,
                "cue_indexes": [item.cue_index],
                "merged": False,
                "requested_start": item.requested_start,
                "voice_start": item.voice_start,
                "voice_end": item.voice_end,
                "lag": item.lag,
                "trimmed_dur": item.duration,
                "fitted_dur": item.duration,
                "synthesis_rate": provider_synthesis_rate,
                "post_fit_speed": rate,
            })
        lags = [item.lag for item in schedule]
        return voice_clips, {
            "enabled": True,
            "voice": voice,
            "resource_id": resource_id,
            "rate": rate,
            "synthesis_rate": provider_synthesis_rate,
            "uniform_rate": True,
            "post_fit_enabled": abs(rate - 1.0) > 1e-6,
            "post_fit_speed": rate,
            "fixed_rate_gap_seconds": fixed_rate_gap_seconds,
            "fallback": fallback,
            "short_slot_policy": short_slot_policy,
            "cue_count": len(plans),
            "voice_clip_count": len(voice_clips),
            "segment_count": len(schedule),
            "merged_segment_count": 0,
            "merged_cue_count": 0,
            "max_lag": max(lags, default=0.0),
            "mean_lag": sum(lags) / len(lags) if lags else 0.0,
            "lag_over_0_1_count": sum(lag > 0.1 for lag in lags),
            "lag_over_0_5_count": sum(lag > 0.5 for lag in lags),
            "max_onset_error": max(lags, default=0.0),
            "mean_onset_error": sum(lags) / len(lags) if lags else 0.0,
            "hard_max_speed_cue_count": 0,
            "speed_over_1_35_count": len(plans) if rate > 1.35 else 0,
            "max_speed_applied": rate,
            "compacted_text_cue_count": sum(
                report["tts_text_compacted"] for report in cue_reports
            ),
            "overlap_count": 0,
            "google_fallback_count": google_fallback_count,
            "missing_voice_count": sum(
                1 for report in cue_reports if "voice_start" not in report
            ),
            "total_synthesis_seconds": total_synthesis_seconds,
            "total_tts_pipeline_seconds": time.time() - started,
            "segments": segment_reports,
            "cues": cue_reports,
        }

    if short_slot_policy == "speech_aligned":
        timings = speech_timings_by_cue or {}
        missing_timings = [plan.cue_index for plan in plans if plan.cue_index not in timings]
        if missing_timings:
            raise ValueError(
                f"speech-aligned TTS is missing timings for cues: {missing_timings}"
            )
        gap_seconds = 0.03
        decisions: list[TtsSpeechCue] = []
        for index, plan in enumerate(plans):
            next_start = (
                timings[plans[index + 1].cue_index].speech_start
                if index + 1 < len(plans)
                else None
            )
            decisions.append(build_tts_speech_cue(
                plan,
                timings[plan.cue_index],
                trimmed_durations[index],
                video_duration,
                next_speech_start=next_start,
                max_fit_speed=max_fit_speed,
                hard_max_speed=hard_max_speed,
                gap_seconds=gap_seconds,
                tail_seconds=speech_tail_seconds,
                fit_guard_seconds=fit_guard_seconds,
                plan_index=index,
            ))
        too_fast = [
            {
                "cue_index": item.cue_index,
                "text_vi": plans[item.plan_index].display_text_vi,
                "text_tts_vi": plans[item.plan_index].text_vi,
                "target_seconds": item.target_seconds,
                "trimmed_duration": item.trimmed_duration,
                "required_speed": item.required_speed,
            }
            for item in decisions
            if item.required_speed > hard_max_speed + 1e-6
        ]
        if too_fast:
            raise ValueError(
                "speech-aligned TTS requires shorter text_tts_vi; "
                f"hard max is {hard_max_speed:.3f}x: {too_fast}"
            )

        voice_clips: list[tuple[str, int]] = []
        schedule: list[TtsSpeechCue] = []
        for index, decision in enumerate(decisions):
            fit_wav = work / f"voice_speech_{index:03d}.wav"
            fitted_duration = ffmpeg_ops.fit_audio_duration(
                str(trimmed_paths[index]),
                str(fit_wav),
                target_seconds=decision.target_seconds,
                max_speed=decision.speed_limit,
            )
            speed_applied = (
                trimmed_durations[index] / fitted_duration
                if fitted_duration > 0.0
                else 0.0
            )
            actual = replace(
                decision,
                voice_end=decision.voice_start + fitted_duration,
                fitted_duration=fitted_duration,
                speed_applied=speed_applied,
            )
            schedule.append(actual)
            voice_clips.append((str(fit_wav), actual.delay_ms))
            timing = timings[actual.cue_index]
            cue_reports[index].update({
                "fitted_dur": fitted_duration,
                "speed_applied": speed_applied,
                "required_speed": actual.required_speed,
                "speed_limit": actual.speed_limit,
                "used_hard_max_speed": actual.used_hard_max_speed,
                "speech_start": timing.speech_start,
                "speech_end": timing.speech_end,
                "raw_speech_start": timing.raw_speech_start,
                "raw_speech_end": timing.raw_speech_end,
                "voice_start": actual.voice_start,
                "voice_end": actual.voice_end,
                "onset_error": actual.voice_start - timing.speech_start,
                "speech_anchor": actual.anchor,
                "speech_target_seconds": actual.target_seconds,
                "overflow_into_next_slot": max(0.0, actual.voice_end - actual.anchor),
                "requested_delay_ms": int(round(timing.speech_start * 1000.0)),
                "actual_delay_ms": actual.delay_ms,
                "segment_id": index,
                "segment_cue_indexes": [actual.cue_index],
                "merged": False,
            })

        validate_tts_speech_schedule(
            schedule,
            video_duration,
            hard_max_speed=hard_max_speed,
            gap_seconds=gap_seconds,
            max_onset_error_seconds=max_onset_error_seconds,
        )
        onset_errors = [abs(item.voice_start - item.speech_start) for item in schedule]
        speeds = [item.speed_applied for item in schedule]
        segment_reports = [
            {
                "segment_id": index,
                "cue_indexes": [item.cue_index],
                "merged": False,
                "delay_ms": item.delay_ms,
                "voice_start": item.voice_start,
                "voice_end": item.voice_end,
                "speech_start": item.speech_start,
                "speech_end": item.speech_end,
                "onset_error": item.voice_start - item.speech_start,
                "anchor": item.anchor,
                "target_seconds": item.target_seconds,
                "trimmed_dur": item.trimmed_duration,
                "fitted_dur": item.fitted_duration,
                "speed_applied": item.speed_applied,
                "required_speed": item.required_speed,
                "speed_limit": item.speed_limit,
                "used_hard_max_speed": item.used_hard_max_speed,
            }
            for index, item in enumerate(schedule)
        ]
        return voice_clips, {
            "enabled": True,
            "voice": voice,
            "resource_id": resource_id,
            "rate": rate,
            "max_fit_speed": max_fit_speed,
            "hard_max_speed": hard_max_speed,
            "max_onset_error_seconds": max_onset_error_seconds,
            "speech_tail_seconds": speech_tail_seconds,
            "fit_guard_seconds": fit_guard_seconds,
            "speech_gap_seconds": gap_seconds,
            "fallback": fallback,
            "short_slot_policy": short_slot_policy,
            "cue_count": len(plans),
            "voice_clip_count": len(voice_clips),
            "segment_count": len(schedule),
            "merged_segment_count": 0,
            "merged_cue_count": 0,
            "max_lag": 0.0,
            "mean_lag": 0.0,
            "lag_over_0_1_count": 0,
            "max_onset_error": max(onset_errors, default=0.0),
            "mean_onset_error": (
                sum(onset_errors) / len(onset_errors) if onset_errors else 0.0
            ),
            "hard_max_speed_cue_count": sum(
                item.used_hard_max_speed for item in schedule
            ),
            "speed_over_1_35_count": sum(speed > 1.35 for speed in speeds),
            "max_speed_applied": max(speeds, default=1.0),
            "compacted_text_cue_count": sum(
                report["tts_text_compacted"] for report in cue_reports
            ),
            "overlap_count": 0,
            "google_fallback_count": google_fallback_count,
            "missing_voice_count": sum(
                1 for report in cue_reports if "voice_start" not in report
            ),
            "total_synthesis_seconds": total_synthesis_seconds,
            "total_tts_pipeline_seconds": time.time() - started,
            "segments": segment_reports,
            "cues": cue_reports,
        }

    if short_slot_policy == "cascade":
        gap_seconds = 0.03
        safety_seconds = 0.05
        last_cluster_breath_seconds = 1.0
        voice_clips: list[tuple[str, int]] = []
        schedule: list[TtsCascadeCue] = []
        for index, plan in enumerate(plans):
            decision = build_tts_cascade_cue(
                plans,
                index,
                trimmed_durations[index],
                schedule[-1].voice_end if schedule else None,
                video_duration,
                max_fit_speed=max_fit_speed,
                hard_max_speed=hard_max_speed,
                gap_seconds=gap_seconds,
                safety_seconds=safety_seconds,
                last_cluster_breath_seconds=last_cluster_breath_seconds,
            )
            fit_wav = work / f"voice_cascade_{index:03d}.wav"
            fitted_duration = ffmpeg_ops.fit_audio_duration(
                str(trimmed_paths[index]),
                str(fit_wav),
                target_seconds=decision.fit_target_seconds,
                max_speed=decision.speed_limit,
            )
            speed_applied = (
                trimmed_durations[index] / fitted_duration
                if fitted_duration > 0.0
                else 0.0
            )
            voice_end = decision.voice_start + fitted_duration
            next_slot_start = (
                plans[index + 1].original_start
                if index + 1 < len(plans)
                else video_duration
            )
            actual = replace(
                decision,
                voice_end=voice_end,
                fitted_duration=fitted_duration,
                speed_applied=speed_applied,
                overflow_into_next_slot=max(0.0, voice_end - next_slot_start),
            )
            schedule.append(actual)
            voice_clips.append((str(fit_wav), actual.delay_ms))
            cue_reports[index].update({
                "fitted_dur": fitted_duration,
                "speed_applied": speed_applied,
                "required_speed": (
                    actual.required_speed
                    if math.isfinite(actual.required_speed)
                    else None
                ),
                "speed_limit": actual.speed_limit,
                "used_hard_max_speed": actual.used_hard_max_speed,
                "voice_start": actual.voice_start,
                "voice_end": actual.voice_end,
                "lag": actual.lag,
                "cascade_anchor": actual.anchor,
                "cascade_target_seconds": actual.target_seconds,
                "overflow_into_next_slot": actual.overflow_into_next_slot,
                "requested_delay_ms": plan.delay_ms,
                "actual_delay_ms": actual.delay_ms,
                "segment_id": index,
                "segment_cue_indexes": [plan.cue_index],
                "merged": False,
            })

        validate_tts_cascade_schedule(
            schedule,
            video_duration,
            max_lag_seconds=max_lag_seconds,
            gap_seconds=gap_seconds,
        )
        lags = [item.lag for item in schedule]
        segment_reports = [
            {
                "segment_id": index,
                "cue_indexes": [item.cue_index],
                "merged": False,
                "delay_ms": item.delay_ms,
                "voice_start": item.voice_start,
                "voice_end": item.voice_end,
                "lag": item.lag,
                "anchor": item.anchor,
                "target_seconds": item.target_seconds,
                "trimmed_dur": item.trimmed_duration,
                "fitted_dur": item.fitted_duration,
                "speed_applied": item.speed_applied,
                "required_speed": (
                    item.required_speed if math.isfinite(item.required_speed) else None
                ),
                "speed_limit": item.speed_limit,
                "used_hard_max_speed": item.used_hard_max_speed,
                "overflow_into_next_slot": item.overflow_into_next_slot,
            }
            for index, item in enumerate(schedule)
        ]
        return voice_clips, {
            "enabled": True,
            "voice": voice,
            "resource_id": resource_id,
            "rate": rate,
            "max_fit_speed": max_fit_speed,
            "hard_max_speed": hard_max_speed,
            "max_lag_seconds": max_lag_seconds,
            "cascade_gap_seconds": gap_seconds,
            "cascade_safety_seconds": safety_seconds,
            "last_cluster_breath_seconds": last_cluster_breath_seconds,
            "fallback": fallback,
            "short_slot_policy": short_slot_policy,
            "cue_count": len(plans),
            "voice_clip_count": len(voice_clips),
            "segment_count": len(schedule),
            "merged_segment_count": 0,
            "merged_cue_count": 0,
            "max_lag": max(lags, default=0.0),
            "mean_lag": sum(lags) / len(lags) if lags else 0.0,
            "lag_over_0_1_count": sum(lag > 0.1 for lag in lags),
            "hard_max_speed_cue_count": sum(
                item.used_hard_max_speed for item in schedule
            ),
            "overlap_count": 0,
            "google_fallback_count": google_fallback_count,
            "missing_voice_count": sum(
                1 for report in cue_reports if "voice_start" not in report
            ),
            "total_synthesis_seconds": total_synthesis_seconds,
            "total_tts_pipeline_seconds": time.time() - started,
            "segments": segment_reports,
            "cues": cue_reports,
        }

    groups = build_tts_merge_groups(
        plans,
        trimmed_durations,
        max_fit_speed=max_fit_speed,
        policy=short_slot_policy,
    )
    voice_clips: list[tuple[str, int]] = []
    segment_reports: list[dict[str, Any]] = []
    for segment_id, group in enumerate(groups):
        source_paths = list(trimmed_paths[group.start_index : group.end_index + 1])
        cue_indexes = [
            plan.cue_index for plan in plans[group.start_index : group.end_index + 1]
        ]
        if len(source_paths) == 1:
            combined_path = source_paths[0]
            combined_duration = trimmed_durations[group.start_index]
        else:
            combined_path = work / f"voice_segment_{segment_id:03d}_concat.wav"
            combined_duration = concat_tts_wavs(source_paths, combined_path)
        fit_wav = work / f"voice_segment_{segment_id:03d}.wav"
        fitted_duration = ffmpeg_ops.fit_audio_duration(
            str(combined_path),
            str(fit_wav),
            target_seconds=group.fit_target_seconds,
            max_speed=max(1.0, max_fit_speed),
        )
        delay_ms = int(round(group.original_start * 1000.0))
        voice_end = delay_ms / 1000.0 + fitted_duration
        slot_end = group.original_start + group.slot_seconds
        if voice_end > slot_end + 0.002:
            raise ValueError(
                f"merged TTS segment {segment_id} cues {cue_indexes} overlaps: "
                f"voice_end={voice_end:.3f}, slot_end={slot_end:.3f}"
            )
        speed_applied = (
            combined_duration / fitted_duration if fitted_duration > 0.0 else 0.0
        )
        if speed_applied > max_fit_speed + 0.02:
            raise ValueError(
                f"merged TTS segment {segment_id} applied {speed_applied:.3f}x, "
                f"above max {max_fit_speed:.3f}x"
            )
        voice_clips.append((str(fit_wav), delay_ms))
        cumulative_fitted = 0.0
        for cue_position in range(group.start_index, group.end_index + 1):
            cue_fitted_duration = (
                trimmed_durations[cue_position] / speed_applied
                if speed_applied > 0.0
                else 0.0
            )
            cue_reports[cue_position].update({
                "fitted_dur": cue_fitted_duration,
                "speed_applied": speed_applied,
                "requested_delay_ms": plans[cue_position].delay_ms,
                "actual_delay_ms": int(round(
                    (group.original_start + cumulative_fitted) * 1000.0
                )),
                "segment_id": segment_id,
                "segment_cue_indexes": cue_indexes,
                "merged": len(cue_indexes) > 1,
            })
            cumulative_fitted += cue_fitted_duration
        segment_reports.append({
            "segment_id": segment_id,
            "cue_indexes": cue_indexes,
            "merged": len(cue_indexes) > 1,
            "delay_ms": delay_ms,
            "slot_seconds": group.slot_seconds,
            "fit_target_seconds": group.fit_target_seconds,
            "trimmed_dur": combined_duration,
            "fitted_dur": fitted_duration,
            "speed_applied": speed_applied,
            "required_speed": group.required_speed,
            "voice_end": voice_end,
            "slot_end": slot_end,
        })
    return voice_clips, {
        "enabled": True,
        "voice": voice,
        "resource_id": resource_id,
        "rate": rate,
        "max_fit_speed": max_fit_speed,
        "fallback": fallback,
        "short_slot_policy": short_slot_policy,
        "cue_count": len(plans),
        "voice_clip_count": len(voice_clips),
        "segment_count": len(groups),
        "merged_segment_count": sum(1 for group in groups if group.end_index > group.start_index),
        "merged_cue_count": sum(
            group.end_index - group.start_index + 1
            for group in groups
            if group.end_index > group.start_index
        ),
        "google_fallback_count": google_fallback_count,
        "missing_voice_count": sum(1 for report in cue_reports if "segment_id" not in report),
        "total_synthesis_seconds": total_synthesis_seconds,
        "total_tts_pipeline_seconds": time.time() - started,
        "segments": segment_reports,
        "cues": cue_reports,
    }


def build_tts_ducking_filtergraph(
    voice_clips: Sequence[tuple[str, int]],
    *,
    bgm_input_index: int,
    bgm_gain_db: float,
    threshold: float,
    ratio: float,
    attack_ms: float,
    release_ms: float,
) -> str:
    if not voice_clips:
        raise ValueError("ducking mix requires at least one voice clip")
    parts: list[str] = []
    labels: list[str] = []
    for input_index, (_, delay_ms) in enumerate(voice_clips, start=1):
        delay = max(0, int(delay_ms))
        label = f"v{input_index}"
        parts.append(
            f"[{input_index}:a]adelay={delay}|{delay},aresample=44100,"
            f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[{label}]"
        )
        labels.append(f"[{label}]")
    parts.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:"
        "duration=longest:dropout_transition=0[voice_pre]"
    )
    parts.append("[voice_pre]asplit=2[voice_mix][voice_key]")
    parts.append(
        f"[{bgm_input_index}:a]volume={float(bgm_gain_db):.3f}dB,"
        "aresample=44100,"
        "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[bgm]"
    )
    parts.append(
        "[bgm][voice_key]sidechaincompress="
        f"threshold={max(0.000001, float(threshold)):.6f}:"
        f"ratio={max(1.0, float(ratio)):.3f}:"
        f"attack={max(0.01, float(attack_ms)):.3f}:"
        f"release={max(0.01, float(release_ms)):.3f}[ducked]"
    )
    parts.append(
        "[ducked][voice_mix]amix=inputs=2:normalize=0:"
        "duration=longest:dropout_transition=0,alimiter=limit=0.95[aout]"
    )
    return ";".join(parts)


def mux_tts_with_ducking(
    video_path: Path,
    voice_clips: Sequence[tuple[str, int]],
    source_path: Path,
    output_path: Path,
    *,
    duration: float,
    bgm_gain_db: float,
    threshold: float,
    ratio: float,
    attack_ms: float,
    release_ms: float,
) -> None:
    args = ["ffmpeg", "-y", "-i", str(video_path)]
    for voice_path, _ in voice_clips:
        args.extend(["-i", voice_path])
    bgm_input_index = len(voice_clips) + 1
    args.extend(["-i", str(source_path)])
    graph = build_tts_ducking_filtergraph(
        voice_clips,
        bgm_input_index=bgm_input_index,
        bgm_gain_db=bgm_gain_db,
        threshold=threshold,
        ratio=ratio,
        attack_ms=attack_ms,
        release_ms=release_ms,
    )
    args.extend([
        "-filter_complex", graph,
        "-map", "0:v:0",
        "-map", "[aout]",
        "-t", f"{duration:.6f}",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "44100",
        "-ac", "2",
        "-movflags", "+faststart",
        str(output_path),
    ])
    desub.cmd(args, timeout=_ffmpeg_timeout())


def write_cover_ass(
    path: Path,
    events: Sequence[CoverEvent],
    meta: desub.VideoMeta,
    *,
    style: str,
    opacity: float,
) -> None:
    if style not in VALID_STYLES:
        raise ValueError(f"invalid cover style: {style}")
    opacity = max(0.0, min(1.0, opacity))
    cover_colour, text_colour = _ass_colour(style)
    alpha = int(round((1.0 - opacity) * 255.0))
    alpha_hex = f"{alpha:02X}"
    cover_primary = f"&H{alpha_hex}{cover_colour[-6:]}"
    font = os.environ.get("DESUB_VISUB_FONT", "DejaVu Sans").strip() or "DejaVu Sans"
    font_size = subtitle_font_size(meta)
    margin_lr = max(20, int(round(meta.width * 0.08)))
    outline = max(0.0, env_float("VISUB_COVER_TEXT_OUTLINE_PX", 2.5)) if style == "blur" else 0.0
    outline_colour = "&H00000000" if text_colour == "&H00FFFFFF" else "&H00FFFFFF"
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {meta.width}",
        f"PlayResY: {meta.height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cover,{font},{font_size},{cover_primary},{cover_primary},{cover_primary},{cover_primary},0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
        f"Style: Text,{font},{font_size},{text_colour},{text_colour},{outline_colour},&H00000000,1,0,0,0,100,100,0,0,1,{outline:.2f},0,5,{margin_lr},{margin_lr},0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for event in events:
        for text_event in event_text_events(event):
            text = desub._ass_text(  # noqa: SLF001
                text_event.text_vi,
                position_x=text_event.text_x,
                position_y=text_event.text_y,
            )
            if text_event.font_size and text_event.font_size != font_size:
                text = f"{{\\fs{text_event.font_size}}}" + text
            lines.append(
                f"Dialogue: 1,{desub._ass_time(text_event.start)},{desub._ass_time(text_event.end)},"  # noqa: SLF001
                f"Text,,0,0,0,,{text}"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ffmpeg_filter_path(path: Path) -> str:
    value = str(path.resolve()).replace("\\", "/")
    value = value.replace("'", r"\'").replace(":", r"\:").replace("[", r"\[").replace("]", r"\]")
    return value


def subtitles_filter(path: Path) -> str:
    return f"subtitles=filename='{_ffmpeg_filter_path(path)}'"


def _encode_args(output: Path) -> list[str]:
    return [
        "-c:v", "libx264",
        "-preset", os.environ.get("VISUB_COVER_PRESET", "veryfast"),
        "-crf", os.environ.get("DESUB_VISUB_OUTPUT_CRF", "18"),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output),
    ]


def _ffmpeg_timeout() -> int:
    return max(60, env_int("VISUB_COVER_FFMPEG_TIMEOUT_SECONDS", 3600))


def clamp_corner_radius(width: int, height: int, radius: int) -> int:
    return max(0, min(int(radius), max(0, min(width, height) // 2)))


def write_rounded_mask(
    path: Path,
    width: int,
    height: int,
    radius: int,
    feather_pixels: float,
) -> int:
    import cv2
    import numpy as np

    if width <= 0 or height <= 0:
        raise ValueError(f"invalid rounded mask size: {width}x{height}")
    radius = clamp_corner_radius(width, height, radius)
    mask = np.zeros((height, width), dtype=np.uint8)
    if radius == 0:
        mask[:, :] = 255
    else:
        right_center = width - radius - 1
        bottom_center = height - radius - 1
        cv2.rectangle(mask, (radius, 0), (right_center, height - 1), 255, thickness=-1)
        cv2.rectangle(mask, (0, radius), (width - 1, bottom_center), 255, thickness=-1)
        for center in (
            (radius, radius),
            (right_center, radius),
            (radius, bottom_center),
            (right_center, bottom_center),
        ):
            cv2.circle(mask, center, radius, 255, thickness=-1, lineType=cv2.LINE_AA)
    feather = max(0.0, float(feather_pixels))
    if feather > 0.0:
        mask = cv2.GaussianBlur(
            mask,
            (0, 0),
            sigmaX=feather,
            sigmaY=feather,
            borderType=cv2.BORDER_CONSTANT,
        )
        mask[height // 2, width // 2] = 255
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), mask):
        raise RuntimeError(f"failed to write rounded mask: {path}")
    return radius


def prepare_rounded_masks(
    events: Sequence[CoverEvent],
    work: Path,
    *,
    radius: int,
    feather_pixels: float,
) -> tuple[dict[tuple[int, int], Path], list[dict[str, Any]]]:
    paths: dict[tuple[int, int], Path] = {}
    report: list[dict[str, Any]] = []
    for width, height in sorted({
        (event.rect[2] - event.rect[0], event.rect[3] - event.rect[1])
        for event in events
    }):
        clamped_radius = clamp_corner_radius(width, height, radius)
        path = work / "rounded_masks" / f"mask_{width}x{height}_r{clamped_radius}_f{feather_pixels:g}.png"
        write_rounded_mask(path, width, height, radius, feather_pixels)
        paths[(width, height)] = path
        report.append({
            "width": width,
            "height": height,
            "requested_corner_radius": radius,
            "corner_radius": clamped_radius,
            "edge_feather_pixels": feather_pixels,
            "path": str(path),
        })
    return paths, report


def build_cover_filtergraph(
    events: Sequence[CoverEvent],
    ass_path: Path,
    strength: int,
    *,
    style: str,
    opacity: float,
    mask_paths: dict[tuple[int, int], Path],
) -> str:
    if not events:
        return f"[0:v]{subtitles_filter(ass_path)}[vout]"
    if style not in VALID_STYLES:
        raise ValueError(f"invalid cover style: {style}")
    if style == "blur":
        split_outputs = ["[base]", *(f"[crop{index}]" for index in range(len(events)))]
        chains = [f"[0:v]split={len(split_outputs)}{''.join(split_outputs)}"]
        current = "[base]"
    else:
        chains = []
        current = "[0:v]"
    opacity = max(0.0, min(1.0, opacity))
    for index, event in enumerate(events):
        x1, y1, x2, y2 = event.rect
        width = x2 - x1
        height = y2 - y1
        mask_path = mask_paths[(width, height)]
        chains.append(
            f"movie=filename='{_ffmpeg_filter_path(mask_path)}',format=gray,"
            f"loop=loop=-1:size=1:start=0,setpts=N/30/TB[mask{index}]"
        )
        if style == "blur":
            luma_radius = max(1, min(strength, max(1, min(width, height) // 2 - 1)))
            chroma_radius = max(1, luma_radius // 2)
            chains.append(
                f"[crop{index}]crop={width}:{height}:{x1}:{y1}:exact=1,"
                f"boxblur=luma_radius={luma_radius}:luma_power=2:"
                f"chroma_radius={chroma_radius}:chroma_power=2[cover{index}]"
            )
            chains.append(f"[cover{index}][mask{index}]alphamerge[rounded{index}]")
        else:
            colour = "white" if style == "box_white" else "black"
            chains.append(f"color=c={colour}:s={width}x{height}:r=30,format=rgb24[cover{index}]")
            alpha_filter = (
                f",colorchannelmixer=aa={opacity:.6f}" if opacity < 1.0 - 1e-9 else ""
            )
            chains.append(
                f"[cover{index}][mask{index}]alphamerge{alpha_filter}[rounded{index}]"
            )
        output = f"[overlay{index}]"
        chains.append(
            f"{current}[rounded{index}]overlay={x1}:{y1}:eof_action=pass:"
            f"enable='gte(t,{event.start:.6f})*lt(t,{event.end:.6f})'{output}"
        )
        current = output
    chains.append(f"{current}{subtitles_filter(ass_path)}[vout]")
    return ";".join(chains)


def render_cover_single(
    source: Path,
    events: Sequence[CoverEvent],
    ass_path: Path,
    output: Path,
    *,
    strength: int,
    style: str,
    opacity: float,
    mask_paths: dict[tuple[int, int], Path],
    duration: float,
    seek_start: float = 0.0,
    copy_audio: bool = True,
) -> None:
    args = ["ffmpeg", "-y"]
    if seek_start > 0:
        args.extend(["-ss", f"{seek_start:.6f}"])
    args.extend(["-i", str(source), "-t", f"{duration:.6f}"])
    args.extend([
        "-filter_complex", build_cover_filtergraph(
            events,
            ass_path,
            strength,
            style=style,
            opacity=opacity,
            mask_paths=mask_paths,
        ),
        "-map", "[vout]",
    ])
    if copy_audio:
        args.extend(["-map", "0:a?", "-c:a", "copy"])
    else:
        args.append("-an")
    args.extend(_encode_args(output))
    desub.cmd(args, timeout=_ffmpeg_timeout())


def _segment_boundaries(events: Sequence[CoverEvent], duration: float, max_events: int) -> list[tuple[float, float]]:
    if len(events) <= max_events:
        return [(0.0, duration)]
    ordered = sorted(events, key=lambda event: (event.start, event.end))
    boundaries = [0.0]
    for index in range(max_events, len(ordered), max_events):
        previous = ordered[index - 1]
        following = ordered[index]
        boundary = (previous.end + following.start) / 2.0
        boundary = min(duration, max(boundaries[-1] + 0.05, boundary))
        if boundary < duration - 0.05:
            boundaries.append(boundary)
    boundaries.append(duration)
    return [(start, end) for start, end in zip(boundaries, boundaries[1:]) if end > start + 0.01]


def _slice_events(events: Sequence[CoverEvent], start: float, end: float) -> list[CoverEvent]:
    sliced: list[CoverEvent] = []
    for event in events:
        if event.end <= start or event.start >= end:
            continue
        local_text_events = tuple(
            replace(
                text_event,
                start=max(start, text_event.start) - start,
                end=min(end, text_event.end) - start,
            )
            for text_event in event_text_events(event)
            if text_event.end > start and text_event.start < end
        )
        sliced.append(replace(
            event,
            start=max(start, event.start) - start,
            end=min(end, event.end) - start,
            cue_start=None,
            cue_end=None,
            text_vi="",
            text_events=local_text_events,
        ))
    return sliced


def render_cover_segmented(
    source: Path,
    events: Sequence[CoverEvent],
    merged_events: Sequence[CoverEvent],
    output: Path,
    meta: desub.VideoMeta,
    work: Path,
    *,
    strength: int,
    style: str,
    opacity: float,
    mask_paths: dict[tuple[int, int], Path],
    max_events: int,
) -> list[dict[str, Any]]:
    ranges = _segment_boundaries(merged_events, meta.duration, max_events)
    segments: list[Path] = []
    segment_report: list[dict[str, Any]] = []
    for index, (start, end) in enumerate(ranges):
        local_events = _slice_events(events, start, end)
        local_blurs = _slice_events(merged_events, start, end)
        local_meta = replace(meta, duration=end - start, frames=int(round((end - start) * meta.fps)))
        ass_path = work / f"cover_blur_segment_{index:03d}.ass"
        segment_path = work / f"cover_blur_segment_{index:03d}.mp4"
        write_cover_ass(ass_path, local_events, local_meta, style=style, opacity=opacity)
        render_cover_single(
            source,
            local_blurs,
            ass_path,
            segment_path,
            strength=strength,
            style=style,
            opacity=opacity,
            mask_paths=mask_paths,
            duration=end - start,
            seek_start=start,
            copy_audio=False,
        )
        segments.append(segment_path)
        segment_report.append({
            "index": index,
            "start": start,
            "end": end,
            "blur_event_count": len(local_blurs),
            "subtitle_event_count": sum(len(event_text_events(event)) for event in local_events),
        })
    joined_video = work / "cover_blur_joined_video.mp4"
    desub.concat_clips(segments, joined_video)
    desub.cmd([
        "ffmpeg", "-y",
        "-i", str(joined_video),
        "-i", str(source),
        "-t", f"{meta.duration:.6f}",
        "-map", "0:v:0",
        "-map", "1:a?",
        "-c", "copy",
        "-movflags", "+faststart",
        str(output),
    ], timeout=_ffmpeg_timeout())
    return segment_report


def stream_report(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries",
            (
                "stream=index,codec_name,codec_type,width,height,avg_frame_rate,"
                "sample_rate,channels,channel_layout,bit_rate"
            ),
            "-show_entries", "format=duration,size",
            "-of", "json",
            str(path),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return json.loads(proc.stdout)


def cluster_coverage_report(
    clusters: Sequence[dict[str, Any]],
    events: Sequence[CoverEvent],
) -> dict[str, Any]:
    cluster_reports: list[dict[str, Any]] = []
    for cluster in sorted(clusters, key=lambda item: int(item["source_index"])):
        source_index = int(cluster["source_index"])
        required_start = float(cluster["t_start"])
        required_end = float(cluster["t_end"])
        windows = sorted(
            (max(required_start, event.start), min(required_end, event.end))
            for event in events
            if event.source_cluster_index == source_index
            and event.end > required_start
            and event.start < required_end
        )
        merged: list[list[float]] = []
        for start, end in windows:
            if end <= start:
                continue
            if merged and start <= merged[-1][1] + 1e-6:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        uncovered = _interval_complement(
            required_start,
            required_end,
            [(start, end) for start, end in merged],
        )
        cluster_reports.append({
            "source_cluster_index": source_index,
            "required_start": required_start,
            "required_end": required_end,
            "cover_windows": merged,
            "uncovered_intervals": [list(interval) for interval in uncovered],
            "ok": not uncovered,
        })
    failed = [item for item in cluster_reports if not item["ok"]]
    return {
        "ok": not failed,
        "cluster_count": len(cluster_reports),
        "uncovered_cluster_count": len(failed),
        "uncovered_clusters": failed,
        "clusters": cluster_reports,
    }


def detection_timeline_coverage_report(
    mask_payload: dict[str, Any],
    events: Sequence[CoverEvent],
    meta: desub.VideoMeta,
) -> dict[str, Any]:
    """Assert every accepted subtitle detection lands inside a cover window.

    Cluster-only coverage cannot catch a valid OCR span that was accidentally
    rejected before cluster construction.  This second temporal gate compares
    cover windows against the already filtered, persistent subtitle detections.
    """
    detections = [
        item
        for item in _normalized_detections(mask_payload, meta.duration)
        if desub.box_center_in_sub_band(item["rect"], meta)
    ]
    uncovered: list[dict[str, Any]] = []
    for detection in detections:
        seconds = float(detection["t"])
        covering = [
            event.event_id
            for event in events
            if event.start - 1e-6 <= seconds <= event.end + 1e-6
        ]
        if covering:
            continue
        uncovered.append({
            "source_detection_index": detection["source_detection_index"],
            "t": seconds,
            "rect": list(detection["rect"]),
        })

    detect_fps = max(0.1, float(mask_payload.get("detect_fps") or 8.0))
    maximum_sample_gap = max(0.20, 1.75 / detect_fps)
    intervals: list[dict[str, Any]] = []
    for detection in sorted(uncovered, key=lambda item: float(item["t"])):
        seconds = float(detection["t"])
        if intervals and seconds <= float(intervals[-1]["end"]) + maximum_sample_gap:
            intervals[-1]["end"] = seconds
            intervals[-1]["detection_count"] += 1
            intervals[-1]["detection_indexes"].append(
                detection["source_detection_index"]
            )
            continue
        intervals.append({
            "start": seconds,
            "end": seconds,
            "detection_count": 1,
            "detection_indexes": [detection["source_detection_index"]],
        })
    return {
        "ok": not uncovered,
        "checked_detection_count": len(detections),
        "uncovered_detection_count": len(uncovered),
        "uncovered_interval_count": len(intervals),
        "uncovered_intervals": intervals,
        "uncovered_detections": uncovered,
    }


def cover_text_coverage_report(events: Sequence[CoverEvent]) -> dict[str, Any]:
    """Require visible Vietnamese text throughout every matched cover event."""
    event_reports: list[dict[str, Any]] = []
    for event in events:
        if event.unmatched:
            continue
        visible_windows = sorted(
            (
                max(event.start, float(text_event.start)),
                min(event.end, float(text_event.end)),
            )
            for text_event in event_text_events(event)
            if text_event.text_vi.strip()
            and text_event.end > event.start
            and text_event.start < event.end
        )
        merged: list[list[float]] = []
        for start, end in visible_windows:
            if end <= start + 1e-6:
                continue
            if merged and start <= merged[-1][1] + 1e-6:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        uncovered = _interval_complement(
            event.start,
            event.end,
            [(start, end) for start, end in merged],
        )
        event_reports.append({
            "event_id": event.event_id,
            "source_cluster_index": event.source_cluster_index,
            "cover_start": event.start,
            "cover_end": event.end,
            "visible_text_windows": merged,
            "uncovered_intervals": [list(interval) for interval in uncovered],
            "ok": not uncovered,
        })
    failed = [item for item in event_reports if not item["ok"]]
    return {
        "ok": not failed,
        "checked_event_count": len(event_reports),
        "uncovered_event_count": len(failed),
        "uncovered_interval_count": sum(
            len(item["uncovered_intervals"]) for item in failed
        ),
        "uncovered_events": failed,
        "events": event_reports,
    }


def rect_contains(outer: Rect, inner: Rect) -> bool:
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and outer[2] >= inner[2]
        and outer[3] >= inner[3]
    )


def geometry_coverage_report(
    mask_payload: dict[str, Any],
    events: Sequence[CoverEvent],
    meta: desub.VideoMeta,
    *,
    dilate_pixels: int,
    dilate_x_pixels: int | None = None,
    dilate_y_pixels: int | None = None,
    detection_guard_seconds: float = 0.15,
) -> dict[str, Any]:
    del detection_guard_seconds
    dilate_x = dilate_pixels if dilate_x_pixels is None else max(0, dilate_x_pixels)
    dilate_y = dilate_pixels if dilate_y_pixels is None else max(0, dilate_y_pixels)
    clusters = {
        int(item["source_index"]): item
        for item in _normalized_clusters(mask_payload, meta.duration)
    }
    normalized_detections = _normalized_detections(mask_payload, meta.duration)
    subtitle_lane = calibrate_subtitle_lane(normalized_detections, meta)
    active_lane_failures: dict[int, list[dict[str, Any]]] = {}
    checked_lane_detection_count = 0
    for detection in normalized_detections:
        if detection_lane_rejection_reasons(detection, subtitle_lane):
            continue
        seconds = float(detection["t"])
        candidate_cluster_indexes = {
            source_index
            for source_index, cluster in clusters.items()
            if float(cluster["t_start"]) - 1e-6
            <= seconds
            <= float(cluster["t_end"]) + 1e-6
        }
        active_events = [
            event
            for event in events
            if event.start - 1e-6 <= seconds <= event.end + 1e-6
            and event.source_cluster_index in candidate_cluster_indexes
        ]
        if not active_events:
            continue
        checked_lane_detection_count += 1
        required_rect = dilate_rect_xy(
            detection["rect"], dilate_x, dilate_y, meta.width, meta.height
        )
        if any(rect_contains(event.rect, required_rect) for event in active_events):
            continue
        event = min(
            active_events,
            key=lambda item: abs(((item.start + item.end) / 2.0) - seconds),
        )
        active_lane_failures.setdefault(event.event_id, []).append({
            "kind": "event_rect_misses_active_lane_detection",
            "event_id": event.event_id,
            "source_cluster_index": event.source_cluster_index,
            "detection_index": int(detection["source_detection_index"]),
            "detection_time": seconds,
            "detection_rect": list(detection["rect"]),
            "required_dilated_rect": list(required_rect),
            "event_rect": list(event.rect),
        })
    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    event_reports: list[dict[str, Any]] = []
    for event in events:
        event_failures: list[dict[str, Any]] = []
        for failure in active_lane_failures.get(event.event_id, []):
            failures.append(failure)
            event_failures.append(failure)
        cluster = clusters[event.source_cluster_index]
        cluster_union = union_rects(cluster["rects"])
        cluster_reference = event.cluster_reference_rect or cluster_union
        clamp_bounds = dilate_rect(cluster_reference, 40, meta.width, meta.height)
        if event.zh_rect is not None and not rect_contains(clamp_bounds, event.zh_rect):
            failure = {
                "kind": "zh_rect_outside_cluster_clamp",
                "event_id": event.event_id,
                "source_cluster_index": event.source_cluster_index,
                "event_start": event.start,
                "event_end": event.end,
                "zh_rect": list(event.zh_rect),
                "raw_cluster_union_rect": list(cluster_union),
                "cluster_reference_rect": list(cluster_reference),
                "clamp_bounds": list(clamp_bounds),
            }
            failures.append(failure)
            event_failures.append(failure)
        if event.zh_rect is not None:
            required_rect = dilate_rect_xy(
                event.zh_rect,
                dilate_x,
                dilate_y,
                meta.width,
                meta.height,
            )
            if not rect_contains(event.rect, required_rect):
                failure = {
                    "kind": "event_rect_misses_dilated_zh_rect",
                    "event_id": event.event_id,
                    "source_cluster_index": event.source_cluster_index,
                    "event_start": event.start,
                    "event_end": event.end,
                    "event_rect": list(event.rect),
                    "zh_rect": list(event.zh_rect),
                    "required_dilated_rect": list(required_rect),
                }
                failures.append(failure)
                event_failures.append(failure)
        event_warnings = [
            {"event_id": event.event_id, **dict(item)} for item in event.geometry_warnings
        ]
        warnings.extend(event_warnings)
        event_reports.append({
            "event_id": event.event_id,
            "source_cluster_index": event.source_cluster_index,
            "candidate_detection_count": event.candidate_detection_count,
            "accepted_detection_count": event.active_detection_count,
            "rejected_detection_count": event.rejected_detection_count,
            "rejected_detections": [dict(item) for item in event.rejected_detections],
            "warnings": event_warnings,
            "failure_count": len(event_failures),
            "ok": not event_failures,
        })
    return {
        "ok": not failures,
        "event_count": len(events),
        "candidate_detection_count": sum(event.candidate_detection_count for event in events),
        "checked_detection_count": sum(event.active_detection_count for event in events),
        "checked_active_lane_detection_count": checked_lane_detection_count,
        "rejected_detection_count": sum(event.rejected_detection_count for event in events),
        "warning_count": len(warnings),
        "warnings": warnings,
        "failure_count": len(failures),
        "failures": failures,
        "events": event_reports,
    }


def layout_quality_report(
    events: Sequence[CoverEvent],
    lane: SubtitleLane,
    meta: desub.VideoMeta,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    minimum_lane_support_ratio = max(
        0.0, min(1.0, env_float("VISUB_COVER_MIN_LANE_SUPPORT_RATIO", 0.60))
    )
    if (
        lane.calibration_detection_count >= 10
        and lane.support_ratio + 1e-9 < minimum_lane_support_ratio
    ):
        failures.append({
            "event_id": None,
            "source_cluster_index": None,
            "kind": "dominant_subtitle_lane_support_too_low",
            "support_detection_count": lane.support_detection_count,
            "calibration_detection_count": lane.calibration_detection_count,
            "support_ratio": lane.support_ratio,
            "minimum_support_ratio": minimum_lane_support_ratio,
        })
    maximum_zh_height = max(lane.max_height + 8.0, lane.median_height * 1.5)
    maximum_one_line_height = max(64.0, meta.height * 0.067)
    maximum_two_line_height = max(100.0, meta.height * 0.111)
    maximum_width = meta.width * 0.94
    for event in events:
        event_failures: list[dict[str, Any]] = []
        width = event.rect[2] - event.rect[0]
        height = event.rect[3] - event.rect[1]
        if event.zh_rect is not None:
            zh_height = event.zh_rect[3] - event.zh_rect[1]
            if zh_height > maximum_zh_height + 1e-6:
                event_failures.append({
                    "kind": "zh_union_too_tall",
                    "zh_height": zh_height,
                    "maximum": maximum_zh_height,
                })
        for text_event in event_text_events(event):
            if event.zh_rect is None:
                continue
            # The union bbox can be skewed by a single motion-blurred transition
            # frame even though the robust median anchor is correct.  Validate
            # against the globally calibrated subtitle lane instead of the union
            # midpoint; this still catches the old product-OCR drift.
            expected_x = lane.center_x
            expected_y = lane.center_y - max(0, text_event.line_count - 1) * (
                (text_event.font_size or subtitle_font_size(meta)) * 1.18
            ) / 2.0
            if abs(text_event.text_x - expected_x) > max(24.0, meta.width * 0.04):
                event_failures.append({
                    "kind": "text_anchor_x_misses_zh_track",
                    "text_x": text_event.text_x,
                    "expected_lane_x": expected_x,
                })
            if abs(text_event.text_y - expected_y) > max(18.0, meta.height * 0.02):
                event_failures.append({
                    "kind": "text_anchor_y_misses_zh_track",
                    "text_y": text_event.text_y,
                    "expected_y": expected_y,
                })
            maximum_height = (
                maximum_one_line_height
                if text_event.line_count <= 1
                else maximum_two_line_height
            )
            if height > maximum_height + 1e-6:
                event_failures.append({
                    "kind": "cover_rect_too_tall_for_line_count",
                    "height": height,
                    "line_count": text_event.line_count,
                    "maximum": maximum_height,
                })
        if width > maximum_width + 1e-6:
            event_failures.append({
                "kind": "cover_rect_too_wide",
                "width": width,
                "maximum": maximum_width,
            })
        for failure in event_failures:
            failures.append({
                "event_id": event.event_id,
                "source_cluster_index": event.source_cluster_index,
                "start": event.start,
                "end": event.end,
                "rect": list(event.rect),
                **failure,
            })
        rows.append({
            "event_id": event.event_id,
            "source_cluster_index": event.source_cluster_index,
            "start": event.start,
            "end": event.end,
            "rect": list(event.rect),
            "width": width,
            "height": height,
            "failures": event_failures,
            "ok": not event_failures,
        })
    return {
        "ok": not failures,
        "subtitle_lane": lane.report_dict(),
        "event_count": len(rows),
        "failure_count": len(failures),
        "failures": failures,
        "events": rows,
    }


def cover_timing_quality_report(
    clusters: Sequence[dict[str, Any]],
    events: Sequence[CoverEvent],
    *,
    pad_seconds: float,
    tolerance_seconds: float = 0.02,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for cluster in clusters:
        source_index = int(cluster["source_index"])
        cluster_events = [
            event for event in events if event.source_cluster_index == source_index
        ]
        if not cluster_events:
            continue
        cover_start = min(event.start for event in cluster_events)
        cover_end = max(event.end for event in cluster_events)
        earliest = max(0.0, float(cluster["t_start"]) - max(0.0, pad_seconds))
        latest = float(cluster["t_end"]) + max(0.0, pad_seconds)
        head_excess = max(0.0, earliest - cover_start)
        tail_excess = max(0.0, cover_end - latest)
        rows.append({
            "source_cluster_index": source_index,
            "cluster_target_start": float(cluster["t_start"]),
            "cluster_target_end": float(cluster["t_end"]),
            "first_inlier_detection": cluster.get("first_inlier_detection"),
            "last_inlier_detection": cluster.get("last_inlier_detection"),
            "cover_start": cover_start,
            "cover_end": cover_end,
            "allowed_start": earliest,
            "allowed_end": latest,
            "head_excess_seconds": head_excess,
            "tail_excess_seconds": tail_excess,
            "ok": (
                head_excess <= tolerance_seconds
                and tail_excess <= tolerance_seconds
            ),
        })
    failed = [item for item in rows if not item["ok"]]
    return {
        "ok": not failed,
        "cluster_count": len(rows),
        "failure_count": len(failed),
        "failures": failed,
        "total_head_excess_seconds": sum(
            float(item["head_excess_seconds"]) for item in rows
        ),
        "total_tail_excess_seconds": sum(
            float(item["tail_excess_seconds"]) for item in rows
        ),
        "clusters": rows,
    }


def event_rect_diff_report(events: Sequence[CoverEvent]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for event in events:
        previous = event.previous_per_event_rect
        if previous is None:
            continue
        current = event.rect
        previous_size = [previous[2] - previous[0], previous[3] - previous[1]]
        current_size = [current[2] - current[0], current[3] - current[1]]
        rows.append({
            "event_id": event.event_id,
            "source_cluster_index": event.source_cluster_index,
            "start": event.start,
            "end": event.end,
            "v4_rect": list(previous),
            "v5_rect": list(current),
            "v4_size": previous_size,
            "v5_size": current_size,
            "width_delta": current_size[0] - previous_size[0],
            "height_delta": current_size[1] - previous_size[1],
            "changed": previous != current,
            "rejected_detection_count": event.rejected_detection_count,
        })
    return {
        "baseline": "v4_filtered_per_event_with_double_padding",
        "event_count_v4": len(rows),
        "event_count_v5": len(rows),
        "changed_event_count": sum(1 for row in rows if row["changed"]),
        "events": rows,
    }


def sub_band_rect_audit(
    mask_payload: dict[str, Any],
    events: Sequence[CoverEvent],
    meta: desub.VideoMeta,
) -> dict[str, Any]:
    mask_band_top_ratio, band_top_ratio = cover_sub_band_top_ratio(mask_payload)
    band_top_y = band_top_ratio * meta.height
    clusters = {
        int(item["source_index"]): item
        for item in _normalized_clusters(mask_payload, meta.duration)
    }
    rows: list[dict[str, Any]] = []
    for event in events:
        if event.rect[1] >= band_top_y:
            continue
        raw_cluster_rect = union_rects(clusters[event.source_cluster_index]["rects"])
        cluster_reference = event.cluster_reference_rect or raw_cluster_rect
        text_above_band = (
            event.text_block_rect is not None and event.text_block_rect[1] < band_top_y
        )
        reference_above_band = cluster_reference[1] < band_top_y
        if text_above_band and not reference_above_band:
            reason = "reviewed_text_block_extends_above_sub_band"
        elif reference_above_band:
            reason = "cluster_reference_extends_above_sub_band"
        else:
            reason = "event_rect_above_sub_band_without_subtitle_or_text_support"
        justified = text_above_band or reference_above_band
        rows.append({
            "event_id": event.event_id,
            "source_cluster_index": event.source_cluster_index,
            "start": event.start,
            "end": event.end,
            "event_rect": list(event.rect),
            "zh_rect": list(event.zh_rect) if event.zh_rect is not None else None,
            "text_block_rect": (
                list(event.text_block_rect) if event.text_block_rect is not None else None
            ),
            "raw_cluster_union_rect": list(raw_cluster_rect),
            "cluster_reference_rect": list(cluster_reference),
            "justified": justified,
            "reason": reason,
        })
    unjustified = [row for row in rows if not row["justified"]]
    return {
        "ok": not unjustified,
        "mask_band_top_ratio": mask_band_top_ratio,
        "band_top_ratio": band_top_ratio,
        "band_top_y": band_top_y,
        "event_count_above_band": len(rows),
        "justified_event_count": len(rows) - len(unjustified),
        "unjustified_event_count": len(unjustified),
        "events": rows,
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def decoded_video_sha256(path: Path) -> str:
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(path),
            "-map", "0:v:0", "-an",
            "-f", "hash", "-hash", "sha256", "-",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=_ffmpeg_timeout(),
        check=True,
    )
    for line in proc.stdout.splitlines():
        if line.upper().startswith("SHA256="):
            return line.split("=", 1)[1].strip().lower()
    raise ValueError(f"ffmpeg did not return a decoded video hash for {path}")


def build_tts_delay_comparison(
    baseline_report: dict[str, Any],
    current_tts_report: dict[str, Any],
) -> dict[str, Any]:
    baseline_tts = baseline_report.get("tts", baseline_report)
    baseline_cues = {
        int(item["cue_index"]): item for item in baseline_tts.get("cues") or []
    }
    rows: list[dict[str, Any]] = []
    missing: list[int] = []
    for current in current_tts_report.get("cues") or []:
        cue_index = int(current["cue_index"])
        baseline = baseline_cues.get(cue_index)
        if baseline is None:
            missing.append(cue_index)
            continue
        requested_ms = int(current.get("requested_delay_ms", current.get("delay_ms", 0)))
        old_actual_ms = int(baseline.get("actual_delay_ms", baseline.get("delay_ms", 0)))
        new_actual_ms = int(current.get("actual_delay_ms", current.get("delay_ms", 0)))
        rows.append({
            "cue_index": cue_index,
            "text_vi": current.get("text_vi", ""),
            "requested_delay_ms": requested_ms,
            "baseline_actual_delay_ms": old_actual_ms,
            "current_actual_delay_ms": new_actual_ms,
            "baseline_onset_error_ms": old_actual_ms - requested_ms,
            "current_onset_error_ms": new_actual_ms - requested_ms,
            "current_minus_baseline_ms": new_actual_ms - old_actual_ms,
            "v5_actual_delay_ms": old_actual_ms,
            "v6_actual_delay_ms": new_actual_ms,
            "v5_drift_ms": old_actual_ms - requested_ms,
            "v6_lag_ms": new_actual_ms - requested_ms,
            "v6_minus_v5_ms": new_actual_ms - old_actual_ms,
        })
    if missing:
        raise ValueError(f"baseline TTS report is missing cue indexes: {missing}")
    return {
        "baseline_policy": baseline_tts.get("short_slot_policy"),
        "current_policy": current_tts_report.get("short_slot_policy"),
        "cue_count": len(rows),
        "max_abs_baseline_onset_error_ms": max(
            (abs(int(item["baseline_onset_error_ms"])) for item in rows), default=0
        ),
        "max_abs_current_onset_error_ms": max(
            (abs(int(item["current_onset_error_ms"])) for item in rows), default=0
        ),
        "cue_count_closer_to_current_anchor": sum(
            abs(int(item["current_onset_error_ms"]))
            < abs(int(item["baseline_onset_error_ms"]))
            for item in rows
        ),
        "max_abs_v5_drift_ms": max(
            (abs(int(item["v5_drift_ms"])) for item in rows), default=0
        ),
        "max_v6_lag_ms": max((int(item["v6_lag_ms"]) for item in rows), default=0),
        "cue_count_improved": sum(
            abs(int(item["v6_lag_ms"])) < abs(int(item["v5_drift_ms"]))
            for item in rows
        ),
        "cues": rows,
    }


def _gcs_client() -> Any:
    from google.cloud import storage

    project = os.environ.get("GCP_PROJECT_ID", desub.PROJECT_ID)
    return storage.Client(project=project)


def download_gcs(client: Any, uri: str, path: Path) -> None:
    bucket_name, blob_name = desub.parse_gs_uri(uri)
    path.parent.mkdir(parents=True, exist_ok=True)
    client.bucket(bucket_name).blob(blob_name).download_to_filename(str(path))


def upload_gcs(client: Any, path: Path, uri: str, content_type: str) -> None:
    bucket_name, blob_name = desub.parse_gs_uri(uri)
    client.bucket(bucket_name).blob(blob_name).upload_from_filename(str(path), content_type=content_type)


def _output_prefix(output_uri: str) -> str:
    bucket_name, blob_name = desub.parse_gs_uri(output_uri)
    parent, separator, _ = blob_name.rpartition("/")
    return f"gs://{bucket_name}/{parent}" if separator else f"gs://{bucket_name}"


def auto_source_id(source_uri: str, generation: str | int) -> str:
    """Return a stable, filesystem/GCS-safe id for one object generation."""
    bucket_name, blob_name = desub.parse_gs_uri(source_uri)
    stem = Path(blob_name).stem or "video"
    slug = re.sub(r"[^0-9A-Za-z._-]+", "-", stem).strip("-._") or "video"
    slug = slug[:80]
    digest = hashlib.sha256(
        f"{bucket_name}/{blob_name}#{generation}".encode("utf-8")
    ).hexdigest()[:12]
    return f"{slug}-{digest}"


def auto_artifact_uris(
    source_uri: str,
    generation: str | int,
    *,
    result_root: str = DEFAULT_RESULT_ROOT,
    pipeline_version: str = DEFAULT_PIPELINE_VERSION,
    output_uri: str = "",
    mask_uri: str = "",
    cues_uri: str = "",
    source_id: str = "",
) -> dict[str, str | bool]:
    """Resolve deterministic output artifacts while preserving explicit URIs."""
    if not result_root.startswith("gs://"):
        raise ValueError("COVER_RESULT_ROOT must be a gs:// URI")
    version = re.sub(r"[^0-9A-Za-z._-]+", "-", pipeline_version).strip("-._")
    if not version:
        raise ValueError("COVER_PIPELINE_VERSION must contain a safe name")
    resolved_source_id = str(source_id or "").strip() or auto_source_id(
        source_uri, generation
    )
    if not re.fullmatch(r"[0-9A-Za-z._-]+", resolved_source_id):
        raise ValueError("source_id must contain only safe GCS path characters")
    explicit_output = str(output_uri or "").strip()
    if explicit_output:
        if not explicit_output.startswith("gs://"):
            raise ValueError("COVER_OUTPUT_URI must be a gs:// URI")
        result_prefix = _output_prefix(explicit_output)
        resolved_output = explicit_output
    else:
        result_prefix = f"{result_root.rstrip('/')}/{version}/{resolved_source_id}"
        resolved_output = f"{result_prefix}/output.mp4"
    resolved_mask = str(mask_uri or "").strip() or f"{result_prefix}/mask.json"
    resolved_cues = str(cues_uri or "").strip() or f"{result_prefix}/visub_cues_final.json"
    for name, uri in {
        "COVER_MASK_URI": resolved_mask,
        "COVER_CUES_URI": resolved_cues,
    }.items():
        if not uri.startswith("gs://"):
            raise ValueError(f"{name} must be a gs:// URI")
    return {
        "source_id": resolved_source_id,
        "result_prefix": result_prefix,
        "output_uri": resolved_output,
        "mask_uri": resolved_mask,
        "cues_uri": resolved_cues,
        "status_uri": f"{result_prefix}/status.json",
        "auto_mask": not bool(str(mask_uri or "").strip()),
        "auto_cues": not bool(str(cues_uri or "").strip()),
    }


def normalize_douyin_url(raw_url: str) -> str:
    """Validate and canonicalize one supported Douyin video/share URL."""
    value = str(raw_url or "").strip().rstrip(".,;!?，。；！？)]}")
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if scheme not in {"http", "https"}:
        raise ValueError("COVER_DOUYIN_URL must use http or https")
    if not (
        host == "douyin.com"
        or host.endswith(".douyin.com")
        or host == "iesdouyin.com"
        or host.endswith(".iesdouyin.com")
    ):
        raise ValueError("COVER_DOUYIN_URL must point to Douyin")
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    valid_path = bool(
        (host == "v.douyin.com" and re.fullmatch(r"/[0-9A-Za-z_-]+/?", path))
        or re.fullmatch(r"/(?:video|note)/\d+/?", path)
        or re.fullmatch(r"/share/video/\d+/?", path)
    )
    if not valid_path:
        raise ValueError("COVER_DOUYIN_URL is not a supported Douyin video URL")
    canonical_path = path.rstrip("/") + "/"
    return urlunsplit(("https", host, canonical_path, "", ""))


def douyin_source_id(douyin_url: str) -> str:
    normalized = normalize_douyin_url(douyin_url)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"douyin-{digest}"


def resolve_cover_input(client: Any) -> dict[str, Any]:
    """Resolve mutually exclusive GCS/Douyin input and deterministic artifacts."""
    raw_source_uri = os.environ.get("COVER_SOURCE_URI", "").strip()
    raw_douyin_url = os.environ.get("COVER_DOUYIN_URL", "").strip()
    if bool(raw_source_uri) == bool(raw_douyin_url):
        raise ValueError(
            "exactly one of COVER_SOURCE_URI or COVER_DOUYIN_URL is required"
        )
    common = {
        "result_root": (
            os.environ.get("COVER_RESULT_ROOT", DEFAULT_RESULT_ROOT).strip()
            or DEFAULT_RESULT_ROOT
        ),
        "pipeline_version": (
            os.environ.get("COVER_PIPELINE_VERSION", DEFAULT_PIPELINE_VERSION).strip()
            or DEFAULT_PIPELINE_VERSION
        ),
        "output_uri": os.environ.get("COVER_OUTPUT_URI", "").strip(),
        "mask_uri": os.environ.get("COVER_MASK_URI", "").strip(),
        "cues_uri": os.environ.get("COVER_CUES_URI", "").strip(),
    }
    if raw_source_uri:
        if not raw_source_uri.startswith("gs://"):
            raise ValueError("COVER_SOURCE_URI must be a gs:// URI")
        generation = gcs_generation(client, raw_source_uri)
        artifacts = auto_artifact_uris(raw_source_uri, generation, **common)
        return {
            "input_kind": "gcs",
            "source_uri": raw_source_uri,
            "douyin_url": "",
            "source_generation": generation,
            "artifacts": artifacts,
        }

    douyin_url = normalize_douyin_url(raw_douyin_url)
    source_id = douyin_source_id(douyin_url)
    # This seed URI is never read; it only supplies a stable input to the common
    # artifact resolver.  The real downloaded source is staged inside the result
    # prefix below.
    seed_uri = f"gs://douyin-url-input/{source_id}/source.mp4"
    artifacts = auto_artifact_uris(
        seed_uri,
        "url-v1",
        source_id=source_id,
        **common,
    )
    return {
        "input_kind": "douyin_url",
        "source_uri": f"{artifacts['result_prefix']}/source.mp4",
        "douyin_url": douyin_url,
        "source_generation": "url-v1",
        "artifacts": artifacts,
    }


def gcs_generation(client: Any, uri: str) -> str:
    bucket_name, blob_name = desub.parse_gs_uri(uri)
    blob = client.bucket(bucket_name).blob(blob_name)
    blob.reload()
    if not blob.generation:
        raise ValueError(f"GCS object has no generation: {uri}")
    return str(blob.generation)


def gcs_exists(client: Any, uri: str) -> bool:
    bucket_name, blob_name = desub.parse_gs_uri(uri)
    return bool(client.bucket(bucket_name).blob(blob_name).exists())


def assert_full_cpu_detection(detector_summary: dict[str, Any]) -> None:
    """Reject a partial OCR timeline instead of silently rendering a bad tail."""
    if not env_bool("COVER_REQUIRE_FULL_DETECTION", True):
        return
    timed_out = sorted(
        name
        for name, info in detector_summary.items()
        if isinstance(info, dict) and bool(info.get("timed_out"))
    )
    if timed_out:
        budgets = {
            name: detector_summary[name].get("seconds")
            for name in timed_out
        }
        raise TimeoutError(
            "CPU subtitle detection did not scan the complete video timeline; "
            f"timed_out_detectors={timed_out}, elapsed_seconds={budgets}. "
            "Increase DESUB_DETECTOR_BUDGET_SECONDS and rerun; partial masks are forbidden."
        )


def default_span_height_limit(video_height: int) -> int:
    """Scale the legacy 120px span limit from its 1024px reference height."""
    return max(120, int(round(120.0 * max(1, video_height) / 1024.0)))


def upload_pipeline_status(
    client: Any,
    result_prefix: str,
    status: str,
    **fields: Any,
) -> None:
    bucket_name, blob_name = desub.parse_gs_uri(f"{result_prefix}/status.json")
    payload = {
        "status": status,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **fields,
    }
    client.bucket(bucket_name).blob(blob_name).upload_from_string(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        content_type="application/json",
    )


def build_cpu_mask_payload(
    source_path: Path,
    source_uri: str,
    meta: desub.VideoMeta,
) -> dict[str, Any]:
    """Detect subtitle clusters on CPU without entering an inpainting path."""
    detect_fps = max(
        0.5,
        env_float(
            "COVER_DETECT_FPS",
            env_float("DESUB_DETECT_FPS", 8.0),
        ),
    )
    band_top_ratio = max(
        0.0,
        min(
            0.98,
            env_float(
                "COVER_BAND_TOP_RATIO",
                env_float("DESUB_BAND_TOP_RATIO", 0.66),
            ),
        ),
    )
    crop_y = int(meta.height * band_top_ratio)
    os.environ.setdefault("DESUB_EASYOCR_GPU", "false")
    os.environ.setdefault("DESUB_COMPARE_PADDLE", "false")
    log(
        "auto_mask_sampling",
        detect_fps=detect_fps,
        duration=meta.duration,
        band_top_ratio=band_top_ratio,
        gpu=False,
    )
    frames = desub.sample_frames(source_path, meta, detect_fps)
    sample_frame_ids = [frame_id for frame_id, _seconds, _frame in frames]
    raw_detections, detector_summary = desub.detector_comparison(
        frames,
        meta,
        crop_y,
    )
    del frames
    assert_full_cpu_detection(detector_summary)
    detections, detection_filter_summary = desub.filter_detections_for_masks(
        raw_detections,
        meta,
        sample_frame_ids=sample_frame_ids,
    )
    raw_spans = desub.build_spans(
        detections,
        meta,
        dilate_px=max(0, env_int("DESUB_MASK_DILATE_PX", 10)),
    )
    subtitle_candidates, watermark_spans = desub.split_watermark_spans(
        raw_spans,
        meta,
    )
    span_height_limit = env_int(
        "DESUB_MAX_SPAN_HEIGHT_PX",
        default_span_height_limit(meta.height),
    )
    os.environ.setdefault("DESUB_MAX_SPAN_HEIGHT_PX", str(span_height_limit))
    spans, rejected_spans = desub.filter_spans_for_inpaint(
        subtitle_candidates,
        meta,
        crop_y=crop_y,
    )
    subtitle_clusters = desub.build_subtitle_clusters(spans, meta)
    watermark_cluster = desub.build_watermark_cluster(meta, watermark_spans)
    if not subtitle_clusters:
        raise ValueError(
            "CPU subtitle detection produced no subtitle_clusters; inspect detector QA"
        )
    payload = {
        "source_uri": source_uri,
        "clip_start_seconds": 0.0,
        "clip_duration_seconds": meta.duration,
        "detect_fps": detect_fps,
        "band_top_ratio": band_top_ratio,
        "span_max_height_px": span_height_limit,
        "cpu_only": True,
        "inpainting": False,
        "detectors": detector_summary,
        "detection_filter": detection_filter_summary,
        "raw_detections": [asdict(item) for item in raw_detections],
        "detections": [asdict(item) for item in detections],
        "raw_spans": [asdict(item) for item in raw_spans],
        "watermark_spans": [asdict(item) for item in watermark_spans],
        "rejected_spans": rejected_spans,
        "spans": [asdict(item) for item in spans],
        "subtitle_clusters": [
            desub.cluster_dict(item) for item in subtitle_clusters
        ],
        "watermark_cluster": (
            desub.cluster_dict(watermark_cluster) if watermark_cluster else None
        ),
    }
    log(
        "auto_mask_ready",
        raw_detection_count=len(raw_detections),
        detection_count=len(detections),
        cluster_count=len(subtitle_clusters),
    )
    return payload


def clusters_without_cues(
    mask_payload: dict[str, Any],
    cues: Sequence[dict[str, Any]],
    *,
    minimum_overlap: float = 0.12,
) -> list[int]:
    missing: list[int] = []
    for fallback_index, cluster in enumerate(
        mask_payload.get("subtitle_clusters") or []
    ):
        cluster_index = int(cluster.get("source_index", fallback_index))
        start = float(cluster.get("t_start", 0.0))
        end = float(cluster.get("t_end", 0.0))
        matched = any(
            min(end, float(cue["end"])) - max(start, float(cue["start"]))
            >= minimum_overlap
            for cue in cues
        )
        if not matched:
            missing.append(cluster_index)
    return missing


def finalize_auto_cues(cues: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    finalized: list[dict[str, Any]] = []
    for cue in cues:
        item = dict(cue)
        text_vi = " ".join(str(item.get("text_vi") or "").split())
        item["text_vi"] = text_vi
        item["text_zh"] = " ".join(str(item.get("text_zh") or "").split())
        lines = desub._balanced_subtitle_lines(text_vi)  # noqa: SLF001
        item["line_count"] = max(1, min(2, len(lines)))
        finalized.append(item)
    return finalized


def _normalized_cue_text(value: Any) -> str:
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKC", str(value or "")).casefold()
        if not character.isspace()
    )
    start = 0
    end = len(normalized)
    while start < end and unicodedata.category(normalized[start]).startswith("P"):
        start += 1
    while end > start and unicodedata.category(normalized[end - 1]).startswith("P"):
        end -= 1
    return normalized[start:end]


def merge_touching_repeated_auto_cues(
    cues: Sequence[dict[str, Any]],
    clusters: Sequence[dict[str, Any]],
    meta: desub.VideoMeta,
    *,
    max_gap_seconds: float = 0.15,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Merge only a model's frame-adjacent duplicate records.

    A wider gap can hide a real subtitle transition, so it is intentionally left for the
    duplicate audit below to reject instead of silently extending stale text.
    """
    merged: list[dict[str, Any]] = []
    report: list[dict[str, Any]] = []
    for source_index, raw in enumerate(cues):
        cue = dict(raw)
        if not merged:
            cue["auto_source_indexes"] = [source_index]
            merged.append(cue)
            continue
        previous = merged[-1]
        gap = float(cue["start"]) - float(previous["end"])
        same_text = bool(_normalized_cue_text(cue.get("text_zh"))) and (
            _normalized_cue_text(cue.get("text_zh"))
            == _normalized_cue_text(previous.get("text_zh"))
            and _normalized_cue_text(cue.get("text_vi"))
            == _normalized_cue_text(previous.get("text_vi"))
        )
        if same_text and -0.05 <= gap <= max(0.0, max_gap_seconds):
            previous_cluster = _match_cluster(previous, clusters, meta)
            current_cluster = _match_cluster(cue, clusters, meta)
            if int(previous_cluster["source_index"]) == int(current_cluster["source_index"]):
                before = [float(previous["start"]), float(previous["end"])]
                previous["start"] = min(float(previous["start"]), float(cue["start"]))
                previous["end"] = max(float(previous["end"]), float(cue["end"]))
                indexes = list(previous.get("auto_source_indexes") or [])
                indexes.append(source_index)
                previous["auto_source_indexes"] = indexes
                report.append({
                    "cluster_index": int(previous_cluster["source_index"]),
                    "source_indexes": indexes,
                    "text_zh": str(previous.get("text_zh") or ""),
                    "text_vi": str(previous.get("text_vi") or ""),
                    "gap_seconds": gap,
                    "before": before,
                    "merged_window": [float(previous["start"]), float(previous["end"])],
                })
                continue
        cue["auto_source_indexes"] = [source_index]
        merged.append(cue)
    return merged, report


def adjacent_repeated_cue_report(
    cues: Sequence[dict[str, Any]],
    clusters: Sequence[dict[str, Any]],
    meta: desub.VideoMeta,
    *,
    max_gap_seconds: float = 0.75,
) -> list[dict[str, Any]]:
    """Return same-cluster adjacent repeats that must not reach subtitle/TTS render."""
    findings: list[dict[str, Any]] = []
    for index in range(1, len(cues)):
        previous = cues[index - 1]
        current = cues[index]
        gap = float(current["start"]) - float(previous["end"])
        if gap < -0.05 or gap > max(0.0, max_gap_seconds):
            continue
        text_zh = _normalized_cue_text(current.get("text_zh"))
        if not text_zh or text_zh != _normalized_cue_text(previous.get("text_zh")):
            continue
        if _normalized_cue_text(current.get("text_vi")) != _normalized_cue_text(
            previous.get("text_vi")
        ):
            continue
        previous_cluster = _match_cluster(previous, clusters, meta)
        current_cluster = _match_cluster(current, clusters, meta)
        if int(previous_cluster["source_index"]) != int(current_cluster["source_index"]):
            continue
        findings.append({
            "previous_cue_index": index - 1,
            "cue_index": index,
            "cluster_index": int(previous_cluster["source_index"]),
            "gap_seconds": gap,
            "previous_window": [float(previous["start"]), float(previous["end"])],
            "window": [float(current["start"]), float(current["end"])],
            "text_zh": str(current.get("text_zh") or ""),
            "text_vi": str(current.get("text_vi") or ""),
        })
    return findings


def splice_repeated_cue_retry(
    cues: Sequence[dict[str, Any]],
    replacement_cues: Sequence[dict[str, Any]],
    findings: Sequence[dict[str, Any]],
    clusters: Sequence[dict[str, Any]],
    meta: desub.VideoMeta,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Replace only suspect duplicate windows while preserving other cluster cues."""
    windows: list[dict[str, Any]] = []
    for finding in findings:
        previous_index = int(finding["previous_cue_index"])
        current_index = int(finding["cue_index"])
        if previous_index < 0 or current_index >= len(cues):
            raise ValueError(f"invalid repeated cue finding indexes: {finding}")
        cluster_index = int(finding["cluster_index"])
        start = float(cues[previous_index]["start"])
        end = float(cues[current_index]["end"])
        following_index = current_index + 1
        if following_index < len(cues):
            following_cluster = _match_cluster(cues[following_index], clusters, meta)
            if int(following_cluster["source_index"]) == cluster_index:
                end = max(end, float(cues[following_index]["end"]))
        existing = next(
            (
                window
                for window in windows
                if int(window["cluster_index"]) == cluster_index
                and start <= float(window["end"]) + 0.05
                and end >= float(window["start"]) - 0.05
            ),
            None,
        )
        if existing is None:
            windows.append({
                "cluster_index": cluster_index,
                "start": start,
                "end": end,
                "finding_indexes": [[previous_index, current_index]],
            })
        else:
            existing["start"] = min(float(existing["start"]), start)
            existing["end"] = max(float(existing["end"]), end)
            existing["finding_indexes"].append([previous_index, current_index])

    def in_window(cue: dict[str, Any], window: dict[str, Any]) -> bool:
        midpoint = (float(cue["start"]) + float(cue["end"])) / 2.0
        if not (float(window["start"]) <= midpoint <= float(window["end"])):
            return False
        cluster = _match_cluster(cue, clusters, meta)
        return int(cluster["source_index"]) == int(window["cluster_index"])

    removed_indexes: set[int] = set()
    selected_replacements: list[dict[str, Any]] = []
    report: list[dict[str, Any]] = []
    for window in windows:
        original_rows = [
            (index, cue)
            for index, cue in enumerate(cues)
            if in_window(cue, window)
        ]
        replacement_rows = [
            cue for cue in replacement_cues if in_window(cue, window)
        ]
        def collapsed_zh_sequence(rows: Sequence[dict[str, Any]]) -> list[str]:
            sequence: list[str] = []
            for cue in sorted(
                rows, key=lambda item: (float(item["start"]), float(item["end"]))
            ):
                value = _normalized_cue_text(cue.get("text_zh"))
                if value and (not sequence or value != sequence[-1]):
                    sequence.append(value)
            return sequence

        original_sequence = collapsed_zh_sequence([
            cue for _, cue in original_rows
        ])
        replacement_sequence = collapsed_zh_sequence(replacement_rows)
        if replacement_sequence != original_sequence:
            raise ValueError(
                "repeated-cue targeted retry would alter subtitle meaning sequence in "
                f"cluster {window['cluster_index']} window "
                f"{float(window['start']):.3f}-{float(window['end']):.3f}: "
                f"caption sequence before {original_sequence}, "
                f"after {replacement_sequence}"
            )
        removed_indexes.update(index for index, _ in original_rows)
        selected_replacements.extend(dict(cue) for cue in replacement_rows)
        report.append({
            **window,
            "removed_cue_indexes": [index for index, _ in original_rows],
            "original_unique_caption_count": len(set(original_sequence)),
            "replacement_unique_caption_count": len(set(replacement_sequence)),
            "original_caption_sequence": original_sequence,
            "replacement_caption_sequence": replacement_sequence,
            "replacement_cue_count": len(replacement_rows),
        })
    combined = [
        dict(cue) for index, cue in enumerate(cues) if index not in removed_indexes
    ] + selected_replacements
    combined.sort(key=lambda cue: (float(cue["start"]), float(cue["end"])))
    return combined, report


def mask_payload_for_duration(
    mask_payload: dict[str, Any],
    duration: float,
) -> dict[str, Any]:
    """Limit cluster windows to a preview duration without mutating the mask."""
    clipped_clusters: list[dict[str, Any]] = []
    for cluster in mask_payload.get("subtitle_clusters") or []:
        start = max(0.0, float(cluster.get("t_start", 0.0)))
        end = min(duration, float(cluster.get("t_end", 0.0)))
        if end - start <= 0.05:
            continue
        clipped = dict(cluster)
        clipped["t_start"] = start
        clipped["t_end"] = end
        clipped["context_start"] = max(
            0.0,
            min(duration, float(cluster.get("context_start", start))),
        )
        clipped["context_end"] = max(
            clipped["context_start"],
            min(duration, float(cluster.get("context_end", end))),
        )
        clipped_clusters.append(clipped)
    return {**mask_payload, "subtitle_clusters": clipped_clusters}


def generate_auto_cues(
    client: Any,
    source_path: Path,
    source_uri: str,
    mask_payload: dict[str, Any],
    meta: desub.VideoMeta,
    work: Path,
    result_prefix: str,
) -> dict[str, Any]:
    """Generate complete, mask-gated Vietnamese cues in bounded video chunks."""
    import chunked_visub_job as cuegen

    project_id = os.environ.get("GCP_PROJECT_ID", desub.PROJECT_ID)
    region = os.environ.get("COVER_CUE_REGION", os.environ.get("VERTEX_REGION", "global"))
    model = os.environ.get(
        "COVER_CUE_MODEL",
        os.environ.get("GEMINI_MODEL", "gemini-2.5-pro"),
    )
    core_seconds = max(5.0, env_float("COVER_CUE_CHUNK_SECONDS", 30.0))
    overlap_seconds = max(0.0, env_float("COVER_CUE_OVERLAP_SECONDS", 2.0))
    timeout = max(60, env_int("COVER_CUE_TIMEOUT_SECONDS", 900))
    cue_mask_payload = mask_payload_for_duration(mask_payload, meta.duration)
    intervals = cuegen._cluster_intervals(  # noqa: SLF001
        cue_mask_payload,
        meta.width,
        meta.height,
    )
    chunk_count = int(math.ceil(meta.duration / core_seconds))
    raw_cues: list[dict[str, Any]] = []
    chunk_reports: list[dict[str, Any]] = []

    def analyze_window(
        *,
        label: str,
        segment_start: float,
        segment_end: float,
        core_start: float,
        core_end: float,
    ) -> list[dict[str, Any]]:
        segment_duration = segment_end - segment_start
        chunk = work / f"{label}.mp4"
        desub.cmd([
            "ffmpeg", "-y", "-ss", f"{segment_start:.3f}",
            "-i", str(source_path), "-t", f"{segment_duration:.3f}",
            "-map", "0:v:0", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
            str(chunk),
        ], timeout=_ffmpeg_timeout())
        chunk_uri = f"{result_prefix}/chunks/{chunk.name}"
        upload_gcs(client, chunk, chunk_uri, "video/mp4")
        local: list[dict[str, Any]] = []
        errors: list[str] = []
        for attempt in range(1, 3):
            try:
                local = cuegen._analyze_chunk(  # noqa: SLF001
                    uri=chunk_uri,
                    duration=segment_duration,
                    project_id=project_id,
                    region=region,
                    model=model,
                    timeout=timeout,
                    known_present=True,
                )
                if local:
                    break
                errors.append(f"attempt {attempt}: empty lines")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
        accepted: list[dict[str, Any]] = []
        for cue in local:
            absolute = dict(cue)
            absolute["start"] = float(cue["start"]) + segment_start
            absolute["end"] = float(cue["end"]) + segment_start
            midpoint = (float(absolute["start"]) + float(absolute["end"])) / 2.0
            if core_start <= midpoint < core_end or (
                core_end >= meta.duration and core_start <= midpoint <= core_end
            ):
                accepted.append(absolute)
        chunk_reports.append({
            "label": label,
            "segment_start": segment_start,
            "segment_end": segment_end,
            "core_start": core_start,
            "core_end": core_end,
            "model_cues": len(local),
            "accepted_cues": len(accepted),
            "errors": errors,
            "uri": chunk_uri,
        })
        return accepted

    for index in range(chunk_count):
        core_start = index * core_seconds
        core_end = min(meta.duration, core_start + core_seconds)
        known_present = any(
            min(core_end, interval["end"]) - max(core_start, interval["start"])
            >= 0.12
            for interval in intervals
        )
        if not known_present:
            continue
        segment_start = max(0.0, core_start - overlap_seconds)
        segment_end = min(meta.duration, core_end + overlap_seconds)
        raw_cues.extend(analyze_window(
            label=f"chunk_{index:03d}",
            segment_start=segment_start,
            segment_end=segment_end,
            core_start=core_start,
            core_end=core_end,
        ))

    gated, rejected = cuegen._gate_to_detected_subtitles(  # noqa: SLF001
        raw_cues,
        intervals,
        meta.duration,
    )
    missing = clusters_without_cues(cue_mask_payload, gated)
    clusters = list(cue_mask_payload.get("subtitle_clusters") or [])
    for source_index in missing:
        cluster = next(
            (
                value
                for fallback_index, value in enumerate(clusters)
                if int(value.get("source_index", fallback_index)) == source_index
            ),
            None,
        )
        if cluster is None:
            continue
        cluster_start = max(0.0, float(cluster["t_start"]) - overlap_seconds)
        cluster_end = min(meta.duration, float(cluster["t_end"]) + overlap_seconds)
        raw_cues.extend(analyze_window(
            label=f"retry_cluster_{source_index:03d}",
            segment_start=cluster_start,
            segment_end=cluster_end,
            core_start=float(cluster["t_start"]),
            core_end=float(cluster["t_end"]),
        ))
    gated, rejected_after_retry = cuegen._gate_to_detected_subtitles(  # noqa: SLF001
        raw_cues,
        intervals,
        meta.duration,
    )
    rejected.extend(rejected_after_retry)
    finalized = finalize_auto_cues(gated)
    normalized_clusters = _normalized_clusters(cue_mask_payload, meta.duration)
    finalized, repeated_cue_merges = merge_touching_repeated_auto_cues(
        finalized,
        normalized_clusters,
        meta,
        max_gap_seconds=max(
            0.0, env_float("COVER_AUTO_REPEAT_TOUCH_GAP_SECONDS", 0.15)
        ),
    )
    repeated_cue_findings = adjacent_repeated_cue_report(
        finalized,
        normalized_clusters,
        meta,
        max_gap_seconds=max(
            0.0, env_float("COVER_AUTO_REPEAT_AUDIT_GAP_SECONDS", 0.75)
        ),
    )
    initial_repeated_cue_findings = list(repeated_cue_findings)
    initial_repeated_cue_merges = list(repeated_cue_merges)
    repeated_cue_retry_cluster_indexes: list[int] = []
    repeated_cue_retry_splices: list[dict[str, Any]] = []
    repeated_cue_retry_error: str | None = None
    if repeated_cue_findings and env_bool("COVER_AUTO_RETRY_REPEATED_CUES", True):
        repeated_cue_retry_cluster_indexes = sorted({
            int(item["cluster_index"]) for item in repeated_cue_findings
        })
        retry_clusters = [
            cluster
            for cluster in normalized_clusters
            if int(cluster["source_index"]) in repeated_cue_retry_cluster_indexes
        ]
        replacement_cues: list[dict[str, Any]] = []
        for cluster in retry_clusters:
            cluster_index = int(cluster["source_index"])
            segment_start = max(
                0.0, float(cluster["t_start"]) - overlap_seconds
            )
            segment_end = min(
                meta.duration, float(cluster["t_end"]) + overlap_seconds
            )
            replacement_cues.extend(analyze_window(
                label=f"retry_repeat_cluster_{cluster_index:03d}",
                segment_start=segment_start,
                segment_end=segment_end,
                core_start=float(cluster["t_start"]),
                core_end=float(cluster["t_end"]),
            ))
        gated_replacements, rejected_after_repeat_retry = cuegen._gate_to_detected_subtitles(  # noqa: SLF001
            replacement_cues,
            intervals,
            meta.duration,
        )
        rejected.extend(rejected_after_repeat_retry)
        finalized_replacements = finalize_auto_cues(gated_replacements)
        try:
            spliced, repeated_cue_retry_splices = splice_repeated_cue_retry(
                finalized,
                finalized_replacements,
                repeated_cue_findings,
                normalized_clusters,
                meta,
            )
        except ValueError as exc:
            repeated_cue_retry_error = str(exc)
        else:
            gated_spliced, rejected_after_splice = cuegen._gate_to_detected_subtitles(  # noqa: SLF001
                spliced,
                intervals,
                meta.duration,
            )
            rejected.extend(rejected_after_splice)
            finalized = finalize_auto_cues(gated_spliced)
            finalized, repeated_cue_merges = merge_touching_repeated_auto_cues(
                finalized,
                normalized_clusters,
                meta,
                max_gap_seconds=max(
                    0.0, env_float("COVER_AUTO_REPEAT_TOUCH_GAP_SECONDS", 0.15)
                ),
            )
            repeated_cue_findings = adjacent_repeated_cue_report(
                finalized,
                normalized_clusters,
                meta,
                max_gap_seconds=max(
                    0.0, env_float("COVER_AUTO_REPEAT_AUDIT_GAP_SECONDS", 0.75)
                ),
            )
    missing_after_retry = clusters_without_cues(cue_mask_payload, finalized)
    payload = {
        "auto_generated": True,
        "source_uri": source_uri,
        "mask_uri": f"{result_prefix}/mask.json",
        "model": model,
        "region": region,
        "chunk_core_seconds": core_seconds,
        "chunk_overlap_seconds": overlap_seconds,
        "chunks": chunk_reports,
        "raw_cue_count": len(raw_cues),
        "cue_count": len(finalized),
        "repeated_cue_merge_count": len(repeated_cue_merges),
        "repeated_cue_merges": repeated_cue_merges,
        "initial_repeated_cue_merge_count": len(initial_repeated_cue_merges),
        "initial_repeated_cue_merges": initial_repeated_cue_merges,
        "initial_repeated_cue_finding_count": len(initial_repeated_cue_findings),
        "initial_repeated_cue_findings": initial_repeated_cue_findings,
        "repeated_cue_retry_cluster_indexes": repeated_cue_retry_cluster_indexes,
        "repeated_cue_retry_splices": repeated_cue_retry_splices,
        "repeated_cue_retry_error": repeated_cue_retry_error,
        "repeated_cue_finding_count": len(repeated_cue_findings),
        "repeated_cue_findings": repeated_cue_findings,
        "missing_cluster_indexes": missing_after_retry,
        "rejected": rejected,
        "cues": finalized,
    }
    draft_path = work / "visub_cues_draft.json"
    desub.write_json(draft_path, payload)
    upload_gcs(
        client,
        draft_path,
        f"{result_prefix}/visub_cues_draft.json",
        "application/json",
    )
    if repeated_cue_findings or repeated_cue_retry_error:
        cue_quality_path = work / "cue_quality_report.json"
        desub.write_json(cue_quality_path, {
            "ok": False,
            "stage": "automatic_cue_generation",
            "initial_adjacent_repeated_cue_count": len(
                initial_repeated_cue_findings
            ),
            "initial_adjacent_repeated_cues": initial_repeated_cue_findings,
            "retry_cluster_indexes": repeated_cue_retry_cluster_indexes,
            "retry_splices": repeated_cue_retry_splices,
            "retry_error": repeated_cue_retry_error,
            "adjacent_repeated_cue_count": len(repeated_cue_findings),
            "adjacent_repeated_cues": repeated_cue_findings,
            "message": "Refused unsafe or unresolved duplicate-speech retry.",
        })
        upload_gcs(
            client,
            cue_quality_path,
            f"{result_prefix}/cue_quality_report.json",
            "application/json",
        )
    if not finalized:
        raise ValueError("automatic Gemini analysis produced no mask-gated cues")
    if missing_after_retry and env_bool("COVER_AUTO_REQUIRE_ALL_CLUSTERS", True):
        raise ValueError(
            "automatic Gemini analysis missed subtitle clusters after retry: "
            + ",".join(str(value) for value in missing_after_retry)
        )
    if repeated_cue_retry_error:
        raise ValueError(repeated_cue_retry_error)
    if repeated_cue_findings:
        raise ValueError(
            "automatic Gemini analysis left adjacent repeated subtitle cues; "
            "refusing to synthesize duplicate TTS: "
            + ", ".join(
                f"{item['previous_cue_index']}/{item['cue_index']}"
                for item in repeated_cue_findings
            )
        )
    log(
        "auto_cues_ready",
        cue_count=len(finalized),
        missing_cluster_indexes=missing_after_retry,
        model=model,
    )
    return payload


def extract_comparison_frame(
    source: Path,
    output: Path,
    destination: Path,
    seconds: float,
    meta: desub.VideoMeta,
) -> None:
    crop_width = min(720, meta.width)
    crop_y = min(760, max(0, meta.height - 1))
    crop_height = max(1, meta.height - crop_y)
    panel_width = min(360, meta.width)
    destination.parent.mkdir(parents=True, exist_ok=True)
    graph = (
        f"[0:v]split=2[source_full_in][source_band_in];"
        f"[1:v]split=2[output_full_in][output_band_in];"
        f"[source_full_in]scale={panel_width}:-2[source_full];"
        f"[output_full_in]scale={panel_width}:-2[output_full];"
        f"[source_band_in]crop={crop_width}:{crop_height}:0:{crop_y},"
        f"scale={panel_width}:-2[source_band];"
        f"[output_band_in]crop={crop_width}:{crop_height}:0:{crop_y},"
        f"scale={panel_width}:-2[output_band];"
        "[source_full][output_full]hstack=inputs=2[full_pair];"
        "[source_band][output_band]hstack=inputs=2[band_pair];"
        "[full_pair][band_pair]vstack=inputs=2[qa]"
    )
    desub.cmd([
        "ffmpeg", "-y",
        "-ss", f"{seconds:.6f}", "-i", str(source),
        "-ss", f"{seconds:.6f}", "-i", str(output),
        "-filter_complex", graph,
        "-map", "[qa]",
        "-frames:v", "1",
        "-update", "1",
        str(destination),
    ], timeout=_ffmpeg_timeout())


def last_testable_cluster_post_time(
    events: Sequence[CoverEvent],
    duration: float,
    offset_seconds: float = 0.2,
) -> tuple[int, float] | None:
    cluster_cover_ends: dict[int, float] = {}
    for event in events:
        cluster_cover_ends[event.source_cluster_index] = max(
            cluster_cover_ends.get(event.source_cluster_index, 0.0),
            event.end,
        )
    testable_clusters = [
        (source_index, cover_end + offset_seconds)
        for source_index, cover_end in cluster_cover_ends.items()
        if cover_end + offset_seconds < duration
    ]
    return max(testable_clusters, key=lambda item: item[1]) if testable_clusters else None


def safe_qa_frame_time(seconds: float, meta: desub.VideoMeta) -> float:
    frame_interval = 1.0 / max(1.0, float(meta.fps))
    frame_based_end = (
        max(0.0, (float(meta.frames) - 1.0) / max(1.0, float(meta.fps)))
        if meta.frames > 0 else meta.duration - frame_interval
    )
    last_safe_time = max(
        0.0,
        min(frame_based_end, meta.duration - frame_interval) - 0.001,
    )
    return min(last_safe_time, max(0.0, float(seconds)))


def extract_and_upload_qa(
    client: Any,
    source: Path,
    output: Path,
    events: Sequence[CoverEvent],
    orphans: Sequence[dict[str, Any]],
    clusters: Sequence[dict[str, Any]],
    meta: desub.VideoMeta,
    work: Path,
    output_prefix: str,
) -> list[dict[str, Any]]:
    qa_dir = work / "qa"
    requests: list[dict[str, Any]] = []
    covered_orphan_indexes: set[int] = set()
    for event in events:
        inset = min(0.03, max(0.0, (event.end - event.start) / 4.0))
        phases = (
            ("head", min(event.end, event.start + inset)),
            ("mid", (event.start + event.end) / 2.0),
            ("tail", max(event.start, event.end - inset)),
        )
        for phase, seconds in phases:
            requests.append({
                "name": f"event_{event.event_id:03d}_{phase}",
                "seconds": seconds,
                "event_id": event.event_id,
                "source_cluster_index": event.source_cluster_index,
                "reason": f"every_event_{phase}",
            })
        if event.unmatched:
            covered_orphan_indexes.add(event.source_cluster_index)
    for orphan in orphans:
        source_index = int(orphan["source_cluster_index"])
        if source_index in covered_orphan_indexes:
            continue
        requests.append({
            "name": f"unmatched_cluster_{source_index:03d}",
            "seconds": (float(orphan["source_cluster_start"]) + float(orphan["source_cluster_end"])) / 2.0,
            "event_id": None,
            "source_cluster_index": source_index,
            "reason": "unmatched_cluster",
        })
    for seconds in (26.5, 36.7, 45.0, 54.3):
        if seconds < meta.duration:
            requests.append({
                "name": f"regression_{seconds:.1f}".replace(".", "_"),
                "seconds": seconds,
                "event_id": None,
                "source_cluster_index": None,
                "reason": "known_previous_leak",
            })
    for seconds in (0.46, 2.30, 17.10, 19.80, 31.00):
        if seconds < meta.duration:
            requests.append({
                "name": f"compact_box_{seconds:.2f}".replace(".", "_"),
                "seconds": seconds,
                "event_id": None,
                "source_cluster_index": None,
                "reason": "compact_box",
            })
    for seconds in (22.8, 25.0, 34.5, 63.5, 96.5, 101.5):
        if seconds < meta.duration:
            requests.append({
                "name": f"detection_filter_{seconds:.1f}".replace(".", "_"),
                "seconds": seconds,
                "event_id": None,
                "source_cluster_index": None,
                "reason": "detection_filter_regression",
            })
    for seconds in (0.9, 4.5, 17.5, 22.3, 26.9, 30.5, 39.0):
        if seconds < meta.duration:
            requests.append({
                "name": f"no_blank_text_{seconds:.1f}".replace(".", "_"),
                "seconds": seconds,
                "event_id": None,
                "source_cluster_index": None,
                "reason": "no_blur_without_vietnamese_text",
            })
    for cluster in clusters:
        source_index = int(cluster["source_index"])
        cluster_start = float(cluster["t_start"])
        cluster_end = float(cluster["t_end"])
        head = min(cluster_end, cluster_start + 0.05)
        tail = max(cluster_start, cluster_end - 0.05)
        requests.extend([
            {
                "name": f"cluster_{source_index:03d}_head",
                "seconds": head,
                "event_id": None,
                "source_cluster_index": source_index,
                "reason": "cluster_head",
            },
            {
                "name": f"cluster_{source_index:03d}_tail",
                "seconds": tail,
                "event_id": None,
                "source_cluster_index": source_index,
                "reason": "cluster_tail",
            },
        ])
        cluster_events = [
            event
            for event in events
            if event.source_cluster_index == source_index
        ]
        if cluster_events:
            cover_start = min(event.start for event in cluster_events)
            cover_end = max(event.end for event in cluster_events)
            if cover_start >= 0.03:
                requests.append({
                    "name": f"cluster_{source_index:03d}_pre_cover",
                    "seconds": cover_start - 0.03,
                    "event_id": None,
                    "source_cluster_index": source_index,
                    "reason": "pre_cover_boundary",
                })
            if cover_end + 0.03 < meta.duration:
                requests.append({
                    "name": f"cluster_{source_index:03d}_post_cover",
                    "seconds": cover_end + 0.03,
                    "event_id": None,
                    "source_cluster_index": source_index,
                    "reason": "post_cover_boundary",
                })
    uploaded: list[dict[str, Any]] = []
    for request in requests:
        requested_seconds = float(request["seconds"])
        seconds = safe_qa_frame_time(requested_seconds, meta)
        path = qa_dir / f"{request['name']}_{seconds:.3f}.png"
        extract_comparison_frame(source, output, path, seconds, meta)
        if not path.exists():
            seconds = safe_qa_frame_time(
                seconds - max(0.10, 3.0 / max(1.0, meta.fps)), meta
            )
            path = qa_dir / f"{request['name']}_{seconds:.3f}.png"
            extract_comparison_frame(source, output, path, seconds, meta)
        if not path.exists():
            raise FileNotFoundError(
                f"QA frame could not be decoded at {requested_seconds:.6f}s "
                f"or fallback {seconds:.6f}s"
            )
        uri = f"{output_prefix}/qa/{path.name}"
        upload_gcs(client, path, uri, "image/png")
        uploaded.append({
            **request,
            "requested_seconds": requested_seconds,
            "seconds": seconds,
            "time_clamped": abs(seconds - requested_seconds) > 1e-6,
            "uri": uri,
        })
    return uploaded


def _parameter_report(
    *,
    style: str,
    unmatched_mode: str,
    rect_mode: str,
    fill_mode: str,
    dilate_pixels: int,
    dilate_x_pixels: int,
    dilate_y_pixels: int,
    pad_seconds: float,
    opacity: float,
    blur_strength: int,
    blur_max_events: int,
    detection_guard_seconds: float,
    text_padding_pixels: int,
    text_padding_x_pixels: int,
    text_padding_y_pixels: int,
    corner_radius_pixels: int,
    edge_feather_pixels: float,
    max_seconds: float,
    tts_enabled: bool,
    tts_voice: str,
    tts_resource_id: str,
    tts_rate: float,
    tts_max_fit_speed: float,
    tts_hard_max_speed: float,
    tts_max_lag_seconds: float,
    tts_fallback: str,
    tts_short_slot_policy: str,
    tts_baseline_report_uri: str,
    visual_baseline_output_uri: str,
    visual_baseline_reuse: bool,
    bgm_gain_db: float,
) -> dict[str, Any]:
    return {
        "VISUB_COVER_STYLE": style,
        "VISUB_COVER_UNMATCHED": unmatched_mode,
        "VISUB_COVER_RECT_MODE": rect_mode,
        "VISUB_COVER_FILL": fill_mode,
        "VISUB_COVER_DILATE_PX": dilate_pixels,
        "VISUB_COVER_DILATE_X_PX": dilate_x_pixels,
        "VISUB_COVER_DILATE_Y_PX": dilate_y_pixels,
        "VISUB_COVER_PAD_SECONDS": pad_seconds,
        "VISUB_COVER_OPACITY": opacity,
        "VISUB_COVER_BLUR_STRENGTH": blur_strength,
        "VISUB_COVER_BLUR_MAX_EVENTS": blur_max_events,
        "VISUB_COVER_DETECTION_GUARD_SECONDS": detection_guard_seconds,
        "VISUB_COVER_TEXT_PAD_PX": text_padding_pixels,
        "VISUB_COVER_TEXT_PAD_X_PX": text_padding_x_pixels,
        "VISUB_COVER_TEXT_PAD_Y_PX": text_padding_y_pixels,
        "VISUB_COVER_CORNER_RADIUS_PX": corner_radius_pixels,
        "VISUB_COVER_EDGE_FEATHER_PX": edge_feather_pixels,
        "VISUB_COVER_MAX_SECONDS": max_seconds,
        "DESUB_VISUB_OUTPUT_CRF": os.environ.get("DESUB_VISUB_OUTPUT_CRF", "18"),
        "DESUB_VISUB_FONT": os.environ.get("DESUB_VISUB_FONT", "DejaVu Sans"),
        "DESUB_VISUB_FONT_SIZE_RATIO": env_float("DESUB_VISUB_FONT_SIZE_RATIO", 0.040),
        "DESUB_VISUB_WRAP_CHARS": env_int("DESUB_VISUB_WRAP_CHARS", 24),
        "VISUB_COVER_TEXT_OUTLINE_PX": env_float("VISUB_COVER_TEXT_OUTLINE_PX", 2.5),
        "VISUB_TTS_ENABLED": tts_enabled,
        "VISUB_TTS_VOICE": tts_voice,
        "VISUB_TTS_RESOURCE_ID": tts_resource_id,
        "VISUB_TTS_RATE": tts_rate,
        "VISUB_TTS_MAX_FIT_SPEED": tts_max_fit_speed,
        "VISUB_TTS_HARD_MAX_SPEED": tts_hard_max_speed,
        "VISUB_TTS_MAX_LAG_SECONDS": tts_max_lag_seconds,
        "VISUB_TTS_FALLBACK": tts_fallback,
        "VISUB_TTS_SHORT_SLOT_POLICY": tts_short_slot_policy,
        "VISUB_TTS_BASELINE_REPORT_URI": tts_baseline_report_uri,
        "VISUB_VISUAL_BASELINE_OUTPUT_URI": visual_baseline_output_uri,
        "VISUB_VISUAL_BASELINE_REUSE": visual_baseline_reuse,
        "VISUB_BGM_GAIN_DB": bgm_gain_db,
    }


def main() -> int:
    started = time.time()
    client = _gcs_client()
    cover_input = resolve_cover_input(client)
    input_kind = str(cover_input["input_kind"])
    source_uri = str(cover_input["source_uri"])
    douyin_url = str(cover_input["douyin_url"])
    source_generation = str(cover_input["source_generation"])
    artifacts = dict(cover_input["artifacts"])
    cues_uri = str(artifacts["cues_uri"])
    mask_uri = str(artifacts["mask_uri"])
    output_uri = str(artifacts["output_uri"])
    output_prefix = str(artifacts["result_prefix"])
    auto_mask = bool(artifacts["auto_mask"])
    auto_cues = bool(artifacts["auto_cues"])
    auto_resume = env_bool("COVER_AUTO_RESUME", True)
    upload_pipeline_status(
        client,
        output_prefix,
        "validating",
        input_kind=input_kind,
        source_uri=source_uri,
        douyin_url=douyin_url,
        source_generation=source_generation,
        source_id=artifacts["source_id"],
        output_uri=output_uri,
        mask_uri=mask_uri,
        cues_uri=cues_uri,
        cpu_only=True,
    )

    style = parse_style()
    unmatched_mode = parse_unmatched_mode()
    rect_mode = parse_rect_mode()
    fill_mode = parse_fill_mode()
    tts_subtitle_timing = parse_tts_subtitle_timing()
    dilate_x_pixels, dilate_y_pixels, dilate_pixels = axis_padding_from_env(
        "VISUB_COVER_DILATE_X_PX",
        "VISUB_COVER_DILATE_Y_PX",
        "VISUB_COVER_DILATE_PX",
        default_x=10,
        default_y=4,
    )
    pad_seconds = max(0.0, env_float("VISUB_COVER_PAD_SECONDS", 0.12))
    opacity = max(0.0, min(1.0, env_float("VISUB_COVER_OPACITY", 1.0)))
    blur_strength = max(1, env_int("VISUB_COVER_BLUR_STRENGTH", 20))
    blur_max_events = max(10, env_int("VISUB_COVER_BLUR_MAX_EVENTS", 100))
    detection_guard_seconds = max(0.0, env_float("VISUB_COVER_DETECTION_GUARD_SECONDS", 0.15))
    text_padding_x_pixels, text_padding_y_pixels, text_padding_pixels = axis_padding_from_env(
        "VISUB_COVER_TEXT_PAD_X_PX",
        "VISUB_COVER_TEXT_PAD_Y_PX",
        "VISUB_COVER_TEXT_PAD_PX",
        default_x=12,
        default_y=4,
    )
    corner_radius_pixels = max(0, env_int("VISUB_COVER_CORNER_RADIUS_PX", 20))
    edge_feather_pixels = max(0.0, env_float("VISUB_COVER_EDGE_FEATHER_PX", 3.0))
    max_seconds = max(0.0, env_float("VISUB_COVER_MAX_SECONDS", 0.0))
    tts_enabled = env_bool("VISUB_TTS_ENABLED", False)
    tts_voice = os.environ.get("VISUB_TTS_VOICE", "BV075_streaming").strip() or "BV075_streaming"
    tts_resource_id = (
        os.environ.get("VISUB_TTS_RESOURCE_ID", "7102355803792740865").strip()
        or "7102355803792740865"
    )
    tts_rate = max(0.1, env_float("VISUB_TTS_RATE", 1.0))
    tts_max_fit_speed = max(1.0, env_float("VISUB_TTS_MAX_FIT_SPEED", 1.25))
    tts_hard_max_speed = max(
        tts_max_fit_speed,
        env_float("VISUB_TTS_HARD_MAX_SPEED", 1.40),
    )
    tts_max_lag_seconds = max(0.0, env_float("VISUB_TTS_MAX_LAG_SECONDS", 0.6))
    tts_fallback = os.environ.get("VISUB_TTS_FALLBACK", "google").strip().lower() or "google"
    tts_short_slot_policy = (
        os.environ.get("VISUB_TTS_SHORT_SLOT_POLICY", "fixed_rate_narration").strip().lower()
        or "fixed_rate_narration"
    )
    tts_align_speech = env_bool(
        "VISUB_TTS_ALIGN_SPEECH",
        tts_short_slot_policy in {"speech_aligned", "fixed_rate_narration"},
    )
    tts_speech_timings_uri = os.environ.get(
        "VISUB_TTS_SPEECH_TIMINGS_URI", ""
    ).strip()
    tts_alignment_output_uri = os.environ.get(
        "VISUB_TTS_ALIGNMENT_OUTPUT_URI", ""
    ).strip()
    tts_text_overrides_uri = os.environ.get(
        "VISUB_TTS_TEXT_OVERRIDES_URI", ""
    ).strip()
    tts_align_region = (
        os.environ.get("VISUB_TTS_ALIGN_REGION", "us-central1").strip()
        or "us-central1"
    )
    tts_align_model = (
        os.environ.get("VISUB_TTS_ALIGN_MODEL", "gemini-2.5-flash").strip()
        or "gemini-2.5-flash"
    )
    tts_align_attempts = max(1, env_int("VISUB_TTS_ALIGN_ATTEMPTS", 3))
    tts_align_max_early = max(
        0.0, env_float("VISUB_TTS_ALIGN_MAX_EARLY_SECONDS", 0.30)
    )
    tts_align_max_late = max(
        0.0, env_float("VISUB_TTS_ALIGN_MAX_LATE_SECONDS", 0.25)
    )
    tts_speech_tail_seconds = max(
        0.0, env_float("VISUB_TTS_SPEECH_TAIL_SECONDS", 0.20)
    )
    tts_fit_guard_seconds = max(
        0.0, env_float("VISUB_TTS_FIT_GUARD_SECONDS", 0.015)
    )
    tts_fixed_rate_gap_seconds = max(
        0.0, env_float("VISUB_TTS_FIXED_GAP_SECONDS", 0.05)
    )
    tts_max_onset_error_seconds = max(
        0.0, env_float("VISUB_TTS_MAX_ONSET_ERROR_SECONDS", 0.03)
    )
    tts_prepare_only = env_bool("VISUB_TTS_PREPARE_ONLY", False)
    tts_baseline_report_uri = os.environ.get(
        "VISUB_TTS_BASELINE_REPORT_URI", ""
    ).strip()
    visual_baseline_output_uri = os.environ.get(
        "VISUB_VISUAL_BASELINE_OUTPUT_URI", ""
    ).strip()
    visual_baseline_reuse = env_bool("VISUB_VISUAL_BASELINE_REUSE", False)
    for name, uri in {
        "VISUB_TTS_BASELINE_REPORT_URI": tts_baseline_report_uri,
        "VISUB_VISUAL_BASELINE_OUTPUT_URI": visual_baseline_output_uri,
        "VISUB_TTS_SPEECH_TIMINGS_URI": tts_speech_timings_uri,
        "VISUB_TTS_ALIGNMENT_OUTPUT_URI": tts_alignment_output_uri,
        "VISUB_TTS_TEXT_OVERRIDES_URI": tts_text_overrides_uri,
    }.items():
        if uri and not uri.startswith("gs://"):
            raise ValueError(f"{name} must be empty or a gs:// URI")
    if visual_baseline_reuse and not visual_baseline_output_uri:
        raise ValueError(
            "VISUB_VISUAL_BASELINE_REUSE requires VISUB_VISUAL_BASELINE_OUTPUT_URI"
        )
    if visual_baseline_reuse and tts_short_slot_policy == "fixed_rate_narration":
        raise ValueError(
            "fixed_rate_narration retimes visible Vietnamese subtitles and cannot "
            "reuse a previously rendered visual baseline"
        )
    bgm_gain_db = env_float("VISUB_BGM_GAIN_DB", -20.0)
    tts_duck_original = env_bool(
        "VISUB_TTS_DUCK_ORIGINAL",
        tts_short_slot_policy == "speech_aligned",
    )
    tts_duck_threshold = max(
        0.000001, env_float("VISUB_TTS_DUCK_THRESHOLD", 0.02)
    )
    tts_duck_ratio = max(1.0, env_float("VISUB_TTS_DUCK_RATIO", 10.0))
    tts_duck_attack_ms = max(0.01, env_float("VISUB_TTS_DUCK_ATTACK_MS", 8.0))
    tts_duck_release_ms = max(0.01, env_float("VISUB_TTS_DUCK_RELEASE_MS", 250.0))

    work_root = Path(os.environ.get("WORK_DIR", str(DEFAULT_WORK_ROOT)))
    work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="cover-visub-", dir=work_root))
    source_path = work / "source.mp4"
    cues_path = work / "visub_cues_final.json"
    mask_path = work / "mask.json"
    ass_path = work / "cover_visub.ass"
    output_path = work / "cover_visub.mp4"
    report_path = work / "cover_report.json"

    log(
        "download",
        input_kind=input_kind,
        source_uri=source_uri,
        douyin_url=douyin_url,
        cues_uri=cues_uri,
        mask_uri=mask_uri,
        output_uri=output_uri,
        auto_mask=auto_mask,
        auto_cues=auto_cues,
        work=str(work),
    )
    source_info: dict[str, Any]
    if douyin_url:
        reuse_source = auto_resume and gcs_exists(client, source_uri)
        upload_pipeline_status(
            client,
            output_prefix,
            "downloading" if not reuse_source else "loading_source",
            input_kind=input_kind,
            source_uri=source_uri,
            douyin_url=douyin_url,
            resumed=reuse_source,
        )
        if reuse_source:
            download_gcs(client, source_uri, source_path)
            source_info = {"source": "gcs_resume", "uri": source_uri}
        else:
            source_info = desub.download_source_video(
                source_uri="",
                douyin_url=douyin_url,
                out_path=source_path,
            )
            upload_gcs(client, source_path, source_uri, "video/mp4")
        source_generation = gcs_generation(client, source_uri)
    else:
        download_gcs(client, source_uri, source_path)
        source_info = {"source": "gcs", "uri": source_uri}
    source_meta = desub.ffprobe(source_path)
    target_duration = min(source_meta.duration, max_seconds) if max_seconds > 0 else source_meta.duration
    meta = replace(
        source_meta,
        duration=target_duration,
        frames=int(round(target_duration * source_meta.fps)),
    )

    reuse_mask = auto_mask and auto_resume and gcs_exists(client, mask_uri)
    if not auto_mask or reuse_mask:
        upload_pipeline_status(
            client,
            output_prefix,
            "loading_mask",
            source_uri=source_uri,
            mask_uri=mask_uri,
            resumed=reuse_mask,
        )
        download_gcs(client, mask_uri, mask_path)
        mask_payload = json.loads(mask_path.read_text(encoding="utf-8"))
    else:
        upload_pipeline_status(
            client,
            output_prefix,
            "detecting",
            source_uri=source_uri,
            detect_fps=env_float("COVER_DETECT_FPS", 8.0),
            cpu_only=True,
        )
        mask_payload = build_cpu_mask_payload(
            source_path,
            source_uri,
            meta,
        )
        desub.write_json(mask_path, mask_payload)
        upload_gcs(client, mask_path, mask_uri, "application/json")

    reuse_cues = auto_cues and auto_resume and gcs_exists(client, cues_uri)
    if not auto_cues or reuse_cues:
        upload_pipeline_status(
            client,
            output_prefix,
            "loading_cues",
            source_uri=source_uri,
            cues_uri=cues_uri,
            resumed=reuse_cues,
        )
        download_gcs(client, cues_uri, cues_path)
        cue_payload = json.loads(cues_path.read_text(encoding="utf-8"))
    else:
        upload_pipeline_status(
            client,
            output_prefix,
            "translating",
            source_uri=source_uri,
            mask_uri=mask_uri,
            cue_model=os.environ.get(
                "COVER_CUE_MODEL",
                os.environ.get("GEMINI_MODEL", "gemini-2.5-pro"),
            ),
        )
        cue_payload = generate_auto_cues(
            client,
            source_path,
            source_uri,
            mask_payload,
            meta,
            work,
            output_prefix,
        )
        desub.write_json(cues_path, cue_payload)
        upload_gcs(client, cues_path, cues_uri, "application/json")

    cues = normalize_cues(cue_payload, target_duration)
    cue_quality_clusters = _normalized_clusters(mask_payload, target_duration)
    repeated_cue_findings = adjacent_repeated_cue_report(
        cues,
        cue_quality_clusters,
        meta,
        max_gap_seconds=max(
            0.0, env_float("COVER_REPEAT_AUDIT_GAP_SECONDS", 0.75)
        ),
    )
    cue_quality = {
        "ok": not repeated_cue_findings,
        "adjacent_repeated_cue_count": len(repeated_cue_findings),
        "adjacent_repeated_cues": repeated_cue_findings,
        "auto_repeated_cue_merge_count": int(
            cue_payload.get("repeated_cue_merge_count", 0)
            if isinstance(cue_payload, dict)
            else 0
        ),
        "auto_repeated_cue_merges": (
            list(cue_payload.get("repeated_cue_merges") or [])
            if isinstance(cue_payload, dict)
            else []
        ),
        "auto_repeat_retry_cluster_indexes": (
            list(cue_payload.get("repeated_cue_retry_cluster_indexes") or [])
            if isinstance(cue_payload, dict)
            else []
        ),
        "auto_repeat_retry_splices": (
            list(cue_payload.get("repeated_cue_retry_splices") or [])
            if isinstance(cue_payload, dict)
            else []
        ),
        "initial_auto_repeated_cue_merge_count": int(
            cue_payload.get("initial_repeated_cue_merge_count", 0)
            if isinstance(cue_payload, dict)
            else 0
        ),
        "initial_auto_repeated_cue_merges": (
            list(cue_payload.get("initial_repeated_cue_merges") or [])
            if isinstance(cue_payload, dict)
            else []
        ),
        "initial_auto_repeated_cue_finding_count": int(
            cue_payload.get("initial_repeated_cue_finding_count", 0)
            if isinstance(cue_payload, dict)
            else 0
        ),
        "initial_auto_repeated_cue_findings": (
            list(cue_payload.get("initial_repeated_cue_findings") or [])
            if isinstance(cue_payload, dict)
            else []
        ),
        "reviewed_duplicate_fix": (
            cue_payload.get("reviewed_duplicate_fix")
            if isinstance(cue_payload, dict)
            else None
        ),
    }
    if repeated_cue_findings and env_bool("COVER_REJECT_REPEATED_CUES", True):
        cue_quality_path = work / "cue_quality_report.json"
        desub.write_json(cue_quality_path, cue_quality)
        upload_gcs(
            client,
            cue_quality_path,
            f"{output_prefix}/cue_quality_report.json",
            "application/json",
        )
        raise ValueError(
            "adjacent repeated subtitle cues would synthesize duplicate speech: "
            + ", ".join(
                f"{item['previous_cue_index']}/{item['cue_index']}"
                for item in repeated_cue_findings
            )
        )
    tts_text_by_cue = {
        int(cue["source_cue_index"]): " ".join(str(cue["text_tts_vi"]).split())
        for cue in cues
        if str(cue.get("text_tts_vi") or "").strip()
    }
    if tts_text_overrides_uri:
        overrides_path = work / "tts_text_overrides.json"
        download_gcs(client, tts_text_overrides_uri, overrides_path)
        tts_text_by_cue.update(tts_text_overrides_from_payload(json.loads(
            overrides_path.read_text(encoding="utf-8")
        )))
    if tts_short_slot_policy == "fixed_rate_narration":
        # The narration policy always speaks the complete reviewed subtitle.
        # Compact text fields/override files belong only to slot-fitting modes.
        tts_text_by_cue = {}

    speech_timings: list[SpeechTiming] = []
    speech_alignment_path = work / "speech_alignment.json"
    if tts_enabled and tts_short_slot_policy in {
        "speech_aligned",
        "fixed_rate_narration",
    }:
        if tts_speech_timings_uri:
            download_gcs(client, tts_speech_timings_uri, speech_alignment_path)
            speech_timings = speech_timings_from_payload(
                json.loads(speech_alignment_path.read_text(encoding="utf-8")),
                cues,
                target_duration,
                max_early_seconds=tts_align_max_early,
                max_late_seconds=tts_align_max_late,
            )
            log(
                "speech_alignment_loaded",
                uri=tts_speech_timings_uri,
                cue_count=len(speech_timings),
            )
        elif tts_align_speech:
            log(
                "speech_alignment_start",
                cue_count=len(cues),
                model=tts_align_model,
                region=tts_align_region,
            )
            speech_timings = generate_speech_timings_with_gemini(
                source_uri,
                cues,
                target_duration,
                project_id=os.environ.get("GCP_PROJECT_ID", desub.PROJECT_ID),
                region=tts_align_region,
                model=tts_align_model,
                attempts=tts_align_attempts,
                max_early_seconds=tts_align_max_early,
                max_late_seconds=tts_align_max_late,
            )
            desub.write_json(speech_alignment_path, speech_timing_payload(
                speech_timings,
                source_uri=source_uri,
                model=tts_align_model,
                region=tts_align_region,
            ))
            log("speech_alignment_ready", cue_count=len(speech_timings))
        else:
            raise ValueError(
                f"{tts_short_slot_policy} policy requires VISUB_TTS_SPEECH_TIMINGS_URI "
                "or VISUB_TTS_ALIGN_SPEECH=true"
            )
    events, matches, orphans = build_cover_events(
        cues,
        mask_payload,
        meta,
        dilate_pixels=dilate_pixels,
        dilate_x_pixels=dilate_x_pixels,
        dilate_y_pixels=dilate_y_pixels,
        pad_seconds=pad_seconds,
        unmatched_mode=unmatched_mode,
        rect_mode=rect_mode,
        fill_mode=fill_mode,
        detection_guard_seconds=detection_guard_seconds,
        text_padding_pixels=text_padding_pixels,
        text_padding_x_pixels=text_padding_x_pixels,
        text_padding_y_pixels=text_padding_y_pixels,
    )
    clusters, _, subtitle_lane, cluster_timing = effective_subtitle_clusters(
        mask_payload, meta
    )
    coverage = cluster_coverage_report(clusters, events)
    detection_timeline_coverage = detection_timeline_coverage_report(
        mask_payload,
        events,
        meta,
    )
    geometry = geometry_coverage_report(
        mask_payload,
        events,
        meta,
        dilate_pixels=dilate_pixels,
        dilate_x_pixels=dilate_x_pixels,
        dilate_y_pixels=dilate_y_pixels,
        detection_guard_seconds=detection_guard_seconds,
    )
    layout_quality = layout_quality_report(events, subtitle_lane, meta)
    timing_quality = cover_timing_quality_report(
        clusters,
        events,
        pad_seconds=pad_seconds,
    )
    rect_diff = event_rect_diff_report(events)
    band_audit = sub_band_rect_audit(mask_payload, events, meta)
    output_prefix = _output_prefix(output_uri)
    parameters = _parameter_report(
        style=style,
        unmatched_mode=unmatched_mode,
        rect_mode=rect_mode,
        fill_mode=fill_mode,
        dilate_pixels=dilate_pixels,
        dilate_x_pixels=dilate_x_pixels,
        dilate_y_pixels=dilate_y_pixels,
        pad_seconds=pad_seconds,
        opacity=opacity,
        blur_strength=blur_strength,
        blur_max_events=blur_max_events,
        detection_guard_seconds=detection_guard_seconds,
        text_padding_pixels=text_padding_pixels,
        text_padding_x_pixels=text_padding_x_pixels,
        text_padding_y_pixels=text_padding_y_pixels,
        corner_radius_pixels=corner_radius_pixels,
        edge_feather_pixels=edge_feather_pixels,
        max_seconds=max_seconds,
        tts_enabled=tts_enabled,
        tts_voice=tts_voice,
        tts_resource_id=tts_resource_id,
        tts_rate=tts_rate,
        tts_max_fit_speed=tts_max_fit_speed,
        tts_hard_max_speed=tts_hard_max_speed,
        tts_max_lag_seconds=tts_max_lag_seconds,
        tts_fallback=tts_fallback,
        tts_short_slot_policy=tts_short_slot_policy,
        tts_baseline_report_uri=tts_baseline_report_uri,
        visual_baseline_output_uri=visual_baseline_output_uri,
        visual_baseline_reuse=visual_baseline_reuse,
        bgm_gain_db=bgm_gain_db,
    )
    parameters.update({
        "COVER_PIPELINE_VERSION": os.environ.get(
            "COVER_PIPELINE_VERSION", DEFAULT_PIPELINE_VERSION
        ),
        "COVER_RESULT_ROOT": os.environ.get(
            "COVER_RESULT_ROOT", DEFAULT_RESULT_ROOT
        ),
        "COVER_AUTO_RESUME": auto_resume,
        "COVER_AUTO_MASK": auto_mask,
        "COVER_AUTO_CUES": auto_cues,
        "COVER_SOURCE_GENERATION": source_generation,
        "COVER_REQUIRE_FULL_DETECTION": env_bool(
            "COVER_REQUIRE_FULL_DETECTION", True
        ),
        "COVER_DETECT_FPS": env_float("COVER_DETECT_FPS", 8.0),
        "DESUB_DETECTOR_BUDGET_SECONDS": env_float(
            "DESUB_DETECTOR_BUDGET_SECONDS", 600.0
        ),
        "DESUB_MAX_SPAN_HEIGHT_PX": env_int(
            "DESUB_MAX_SPAN_HEIGHT_PX",
            default_span_height_limit(meta.height),
        ),
        "COVER_CUE_MODEL": os.environ.get(
            "COVER_CUE_MODEL", os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")
        ),
        "VISUB_TTS_ALIGN_SPEECH": tts_align_speech,
        "VISUB_TTS_SPEECH_TIMINGS_URI": tts_speech_timings_uri,
        "VISUB_TTS_ALIGNMENT_OUTPUT_URI": tts_alignment_output_uri,
        "VISUB_TTS_TEXT_OVERRIDES_URI": tts_text_overrides_uri,
        "VISUB_TTS_ALIGN_REGION": tts_align_region,
        "VISUB_TTS_ALIGN_MODEL": tts_align_model,
        "VISUB_TTS_ALIGN_ATTEMPTS": tts_align_attempts,
        "VISUB_TTS_ALIGN_MAX_EARLY_SECONDS": tts_align_max_early,
        "VISUB_TTS_ALIGN_MAX_LATE_SECONDS": tts_align_max_late,
        "VISUB_TTS_SPEECH_TAIL_SECONDS": tts_speech_tail_seconds,
        "VISUB_TTS_FIT_GUARD_SECONDS": tts_fit_guard_seconds,
        "VISUB_TTS_FIXED_GAP_SECONDS": tts_fixed_rate_gap_seconds,
        "VISUB_TTS_SUBTITLE_TIMING": tts_subtitle_timing,
        "VISUB_TTS_MAX_ONSET_ERROR_SECONDS": tts_max_onset_error_seconds,
        "VISUB_TTS_PREPARE_ONLY": tts_prepare_only,
        "VISUB_TTS_DUCK_ORIGINAL": tts_duck_original,
        "VISUB_TTS_DUCK_THRESHOLD": tts_duck_threshold,
        "VISUB_TTS_DUCK_RATIO": tts_duck_ratio,
        "VISUB_TTS_DUCK_ATTACK_MS": tts_duck_attack_ms,
        "VISUB_TTS_DUCK_RELEASE_MS": tts_duck_release_ms,
        "VISUB_COVER_OCR_SIMILARITY_THRESHOLD": env_float(
            "VISUB_COVER_OCR_SIMILARITY_THRESHOLD", 0.42
        ),
        "VISUB_COVER_MAX_BOUNDARY_SNAP_SECONDS": env_float(
            "VISUB_COVER_MAX_BOUNDARY_SNAP_SECONDS", 0.30
        ),
        "VISUB_COVER_MAX_OCR_TRANSITION_GAP_SECONDS": env_float(
            "VISUB_COVER_MAX_OCR_TRANSITION_GAP_SECONDS", 0.25
        ),
        "VISUB_COVER_MAX_TEXT_WIDTH_RATIO": env_float(
            "VISUB_COVER_MAX_TEXT_WIDTH_RATIO", 0.88
        ),
        "VISUB_COVER_MIN_FONT_SIZE_RATIO": env_float(
            "VISUB_COVER_MIN_FONT_SIZE_RATIO", 0.030
        ),
        "VISUB_COVER_MIN_LANE_SUPPORT_RATIO": env_float(
            "VISUB_COVER_MIN_LANE_SUPPORT_RATIO", 0.60
        ),
        "tts_text_override_count": len(tts_text_by_cue),
    })
    if (
        not coverage["ok"]
        or not detection_timeline_coverage["ok"]
        or not geometry["ok"]
        or not band_audit["ok"]
        or not layout_quality["ok"]
        or not timing_quality["ok"]
    ):
        report_uri = f"{output_prefix}/cover_report.json"
        errors: list[str] = []
        if not coverage["ok"]:
            errors.append("cluster coverage assertion failed")
        if not detection_timeline_coverage["ok"]:
            errors.append("detection timeline coverage assertion failed")
        if not geometry["ok"]:
            errors.append("event geometry assertion failed")
        if not band_audit["ok"]:
            errors.append("sub-band rect audit failed")
        if not layout_quality["ok"]:
            errors.append("visual layout compactness assertion failed")
        if not timing_quality["ok"]:
            errors.append("cover timing excess assertion failed")
        failure_report = {
            "ok": False,
            "error": "; ".join(errors),
            "source_uri": source_uri,
            "cues_uri": cues_uri,
            "mask_uri": mask_uri,
            "output_uri": output_uri,
            "report_uri": report_uri,
            "style": style,
            "parameters": parameters,
            "coverage": coverage,
            "detection_timeline_coverage": detection_timeline_coverage,
            "geometry": geometry,
            "layout_quality": layout_quality,
            "timing_quality": timing_quality,
            "cluster_timing": cluster_timing,
            "sub_band_rect_audit": band_audit,
            "v4_v5_event_rect_diff": rect_diff,
            "events": [event.report_dict() for event in events],
            "elapsed_seconds": time.time() - started,
        }
        desub.write_json(report_path, failure_report)
        upload_gcs(client, report_path, report_uri, "application/json")
        temporal_failures = [
            {
                "source_cluster_index": item["source_cluster_index"],
                "uncovered_intervals": item["uncovered_intervals"],
            }
            for item in coverage["uncovered_clusters"]
        ]
        raise ValueError(
            f"cover assertions failed: temporal={temporal_failures}, "
            f"detection_timeline={detection_timeline_coverage['uncovered_intervals']}, "
            f"geometry={geometry['failures']}, sub_band={band_audit}, "
            f"layout={layout_quality['failures']}, timing={timing_quality['failures']}"
        )

    speech_alignment_uri = ""
    if speech_timings:
        if not speech_alignment_path.exists():
            desub.write_json(speech_alignment_path, speech_timing_payload(
                speech_timings,
                source_uri=source_uri,
                model=tts_align_model,
                region=tts_align_region,
            ))
        speech_alignment_uri = (
            tts_alignment_output_uri
            or f"{output_prefix}/speech_alignment.json"
        )
        upload_gcs(
            client,
            speech_alignment_path,
            speech_alignment_uri,
            "application/json",
        )
    if tts_prepare_only:
        if not speech_timings:
            raise ValueError(
                "VISUB_TTS_PREPARE_ONLY requires speech-aligned TTS timings"
            )
        prepare_report = {
            "ok": True,
            "mode": "tts_prepare_only",
            "source_uri": source_uri,
            "cues_uri": cues_uri,
            "mask_uri": mask_uri,
            "output_uri": output_uri,
            "report_uri": f"{output_prefix}/cover_report.json",
            "speech_alignment_uri": speech_alignment_uri,
            "speech_alignment": [asdict(item) for item in speech_timings],
            "tts_text_by_cue": {
                str(index): text for index, text in sorted(tts_text_by_cue.items())
            },
            "parameters": parameters,
            "coverage": coverage,
            "detection_timeline_coverage": detection_timeline_coverage,
            "geometry": geometry,
            "layout_quality": layout_quality,
            "timing_quality": timing_quality,
            "cluster_timing": cluster_timing,
            "sub_band_rect_audit": band_audit,
            "elapsed_seconds": time.time() - started,
        }
        desub.write_json(report_path, prepare_report)
        upload_gcs(
            client,
            report_path,
            prepare_report["report_uri"],
            "application/json",
        )
        log(
            "tts_prepare_complete",
            speech_alignment_uri=speech_alignment_uri,
            cue_count=len(speech_timings),
        )
        return 0
    if orphans:
        log(
            "unmatched_clusters",
            count=len(orphans),
            mode=unmatched_mode,
            source_cluster_indexes=[item["source_cluster_index"] for item in orphans],
        )
    log("events_ready", style=style, event_count=len(events), cue_count=len(cues), orphan_count=len(orphans))
    if geometry["rejected_detection_count"] or geometry["warning_count"]:
        log(
            "detections_filtered",
            rejected_detection_count=geometry["rejected_detection_count"],
            geometry_warning_count=geometry["warning_count"],
        )

    tts_report: dict[str, Any] = {
        "enabled": False,
        "cue_count": 0,
        "voice_clip_count": 0,
        "google_fallback_count": 0,
        "missing_voice_count": 0,
        "cues": [],
    }
    voice_clips: list[tuple[str, int]] = []
    if tts_enabled and tts_short_slot_policy == "fixed_rate_narration":
        upload_pipeline_status(
            client,
            output_prefix,
            "synthesizing",
            source_uri=source_uri,
            cue_count=len(cues),
            voice=tts_voice,
            rate=tts_rate,
        )
        log(
            "tts_start",
            voice=tts_voice,
            resource_id=tts_resource_id,
            rate=tts_rate,
            fallback=tts_fallback,
            short_slot_policy=tts_short_slot_policy,
        )
        voice_clips, tts_report = synthesize_tts_voice_clips(
            events,
            work,
            target_duration,
            voice=tts_voice,
            resource_id=tts_resource_id,
            rate=tts_rate,
            max_fit_speed=tts_max_fit_speed,
            hard_max_speed=tts_hard_max_speed,
            max_lag_seconds=tts_max_lag_seconds,
            fallback=tts_fallback,
            short_slot_policy=tts_short_slot_policy,
            tts_text_by_cue=tts_text_by_cue,
            speech_timings_by_cue={
                item.cue_index: item for item in speech_timings
            },
            speech_tail_seconds=tts_speech_tail_seconds,
            fit_guard_seconds=tts_fit_guard_seconds,
            max_onset_error_seconds=tts_max_onset_error_seconds,
            fixed_rate_gap_seconds=tts_fixed_rate_gap_seconds,
        )
        if tts_subtitle_timing == "voice":
            events = retime_cover_text_to_voice(events, tts_report["cues"])
            tts_report["subtitle_timing_policy"] = "voice_start_end"
        else:
            # Blur follows the detected Chinese cluster. Keep Vietnamese text on
            # the already tiled cluster window so delayed fixed-rate narration
            # cannot leave a floating blur rectangle without a subtitle.
            tts_report["subtitle_timing_policy"] = "cluster_tiled_source"
        tts_report["source_video_timeline_changed"] = False
        log(
            "fixed_rate_subtitle_timing",
            cue_count=tts_report["cue_count"],
            max_lag=tts_report["max_lag"],
            rate=tts_rate,
            subtitle_timing_policy=tts_report["subtitle_timing_policy"],
        )

    text_coverage = cover_text_coverage_report(events)
    if fill_mode == "extend_text" and not text_coverage["ok"]:
        failure_report = {
            "ok": False,
            "error": "matched cover event contains blur-only intervals",
            "source_uri": source_uri,
            "cues_uri": cues_uri,
            "mask_uri": mask_uri,
            "output_uri": output_uri,
            "report_uri": f"{output_prefix}/cover_report.json",
            "style": style,
            "parameters": parameters,
            "coverage": coverage,
            "detection_timeline_coverage": detection_timeline_coverage,
            "text_coverage": text_coverage,
            "events": [event.report_dict() for event in events],
            "tts": tts_report,
            "elapsed_seconds": time.time() - started,
        }
        desub.write_json(report_path, failure_report)
        upload_gcs(
            client,
            report_path,
            f"{output_prefix}/cover_report.json",
            "application/json",
        )
        raise ValueError(
            "blur-only subtitle assertion failed: "
            f"{text_coverage['uncovered_events']}"
        )

    segment_report: list[dict[str, Any]] = []
    if rect_mode == "per_event":
        render_events = list(events)
    else:
        render_events = merge_blur_events(
            events,
            gap_seconds=max(0.0, env_float("VISUB_COVER_BLUR_MERGE_GAP_SECONDS", 0.20)),
            iou_threshold=max(0.0, min(1.0, env_float("VISUB_COVER_BLUR_MERGE_IOU", 0.82))),
            near_pixels=max(0, env_int("VISUB_COVER_BLUR_RECT_NEAR_PX", 12)),
        )
    mask_paths, rounded_mask_report = prepare_rounded_masks(
        render_events,
        work,
        radius=corner_radius_pixels,
        feather_pixels=edge_feather_pixels,
    )
    write_cover_ass(ass_path, events, meta, style=style, opacity=opacity)
    upload_pipeline_status(
        client,
        output_prefix,
        "rendering",
        source_uri=source_uri,
        event_count=len(render_events),
        style=style,
        tts_enabled=tts_enabled,
    )
    log(
        "cover_events_ready",
        before=len(events),
        after=len(render_events),
        rect_mode=rect_mode,
        rounded_mask_count=len(mask_paths),
    )
    rendered_video_path = work / "cover_visub_rendered.mp4" if tts_enabled else output_path
    baseline_output_path: Path | None = None
    if visual_baseline_reuse:
        baseline_output_path = work / "visual_baseline_v5.mp4"
        download_gcs(client, visual_baseline_output_uri, baseline_output_path)
        if tts_enabled:
            rendered_video_path = baseline_output_path
        else:
            shutil.copy2(baseline_output_path, output_path)
        log(
            "visual_baseline_reused",
            baseline_output_uri=visual_baseline_output_uri,
            target_duration=target_duration,
        )
    elif len(render_events) <= blur_max_events:
        render_cover_single(
            source_path,
            render_events,
            ass_path,
            rendered_video_path,
            strength=blur_strength,
            style=style,
            opacity=opacity,
            mask_paths=mask_paths,
            duration=target_duration,
        )
    else:
        log("segmented_render", render_event_count=len(render_events), limit=blur_max_events)
        segment_report = render_cover_segmented(
            source_path,
            events,
            render_events,
            rendered_video_path,
            meta,
            work,
            strength=blur_strength,
            style=style,
            opacity=opacity,
            mask_paths=mask_paths,
            max_events=blur_max_events,
        )

    if tts_enabled:
        if not voice_clips:
            log(
                "tts_start",
                voice=tts_voice,
                resource_id=tts_resource_id,
                rate=tts_rate,
                fallback=tts_fallback,
                short_slot_policy=tts_short_slot_policy,
            )
            voice_clips, tts_report = synthesize_tts_voice_clips(
                events,
                work,
                target_duration,
                voice=tts_voice,
                resource_id=tts_resource_id,
                rate=tts_rate,
                max_fit_speed=tts_max_fit_speed,
                hard_max_speed=tts_hard_max_speed,
                max_lag_seconds=tts_max_lag_seconds,
                fallback=tts_fallback,
                short_slot_policy=tts_short_slot_policy,
                tts_text_by_cue=tts_text_by_cue,
                speech_timings_by_cue={
                    item.cue_index: item for item in speech_timings
                },
                speech_tail_seconds=tts_speech_tail_seconds,
                fit_guard_seconds=tts_fit_guard_seconds,
                max_onset_error_seconds=tts_max_onset_error_seconds,
                fixed_rate_gap_seconds=tts_fixed_rate_gap_seconds,
            )
        tts_report["speech_alignment_uri"] = speech_alignment_uri
        if tts_report["missing_voice_count"]:
            raise ValueError(
                f"TTS is missing {tts_report['missing_voice_count']} reviewed cue clips"
            )
        if tts_baseline_report_uri:
            baseline_report_path = work / "tts_baseline_report.json"
            download_gcs(client, tts_baseline_report_uri, baseline_report_path)
            baseline_report = json.loads(
                baseline_report_path.read_text(encoding="utf-8")
            )
            delay_comparison = build_tts_delay_comparison(
                baseline_report,
                tts_report,
            )
            tts_report["baseline_delay_comparison"] = delay_comparison
            tts_report["v5_v6_delay_comparison"] = delay_comparison
        if tts_duck_original:
            mux_tts_with_ducking(
                rendered_video_path,
                voice_clips,
                source_path,
                output_path,
                duration=target_duration,
                bgm_gain_db=bgm_gain_db,
                threshold=tts_duck_threshold,
                ratio=tts_duck_ratio,
                attack_ms=tts_duck_attack_ms,
                release_ms=tts_duck_release_ms,
            )
        else:
            ffmpeg_ops.mux_synced(
                str(rendered_video_path),
                voice_clips,
                str(source_path),
                str(output_path),
                speed=1.0,
                bgm_gain_db=bgm_gain_db,
                final_dur=target_duration,
            )
        tts_report["audio_ducking"] = {
            "enabled": tts_duck_original,
            "bgm_gain_db": bgm_gain_db,
            "threshold": tts_duck_threshold,
            "ratio": tts_duck_ratio,
            "attack_ms": tts_duck_attack_ms,
            "release_ms": tts_duck_release_ms,
        }
        log(
            "tts_mixed",
            cue_count=tts_report["cue_count"],
            google_fallback_count=tts_report["google_fallback_count"],
            total_synthesis_seconds=tts_report["total_synthesis_seconds"],
        )

    upload_pipeline_status(
        client,
        output_prefix,
        "verifying",
        source_uri=source_uri,
        output_uri=output_uri,
    )
    output_probe = stream_report(output_path)
    output_duration = float(output_probe.get("format", {}).get("duration") or 0.0)
    stream_types = {stream.get("codec_type") for stream in output_probe.get("streams") or []}
    duration_delta = abs(output_duration - target_duration)
    if duration_delta > 0.15:
        raise ValueError(f"duration changed by more than 0.15s: {target_duration} -> {output_duration}")
    if not {"video", "audio"}.issubset(stream_types):
        raise ValueError(f"final output is missing a stream: {sorted(stream_types)}")
    if tts_enabled:
        audio_stream = next(
            stream
            for stream in output_probe.get("streams") or []
            if stream.get("codec_type") == "audio"
        )
        audio_signature = (
            audio_stream.get("codec_name"),
            str(audio_stream.get("sample_rate") or ""),
            int(audio_stream.get("channels") or 0),
        )
        if audio_signature != ("aac", "44100", 2):
            raise ValueError(
                "TTS output audio must be AAC 44100Hz stereo, got "
                f"codec={audio_signature[0]}, sample_rate={audio_signature[1]}, "
                f"channels={audio_signature[2]}"
            )
    if tts_enabled and tts_short_slot_policy == "fixed_rate_narration":
        video_stream = next(
            stream
            for stream in output_probe.get("streams") or []
            if stream.get("codec_type") == "video"
        )
        output_frame_count = int(
            video_stream.get("nb_frames")
            or round(output_duration * meta.fps)
        )
        if abs(output_frame_count - meta.frames) > 1:
            raise ValueError(
                "fixed-rate narration changed the source video timeline: "
                f"frames={meta.frames} -> {output_frame_count}"
            )
        tts_report["video_timeline"] = {
            "changed": False,
            "source_frame_count": meta.frames,
            "output_frame_count": output_frame_count,
            "source_fps": meta.fps,
            "output_r_frame_rate": video_stream.get("r_frame_rate"),
            "duration_delta_seconds": duration_delta,
        }

    visual_baseline_report: dict[str, Any] = {
        "enabled": False,
        "matches": None,
    }
    if visual_baseline_output_uri:
        if baseline_output_path is None:
            baseline_output_path = work / "visual_baseline_v5.mp4"
            download_gcs(client, visual_baseline_output_uri, baseline_output_path)
        baseline_video_hash = decoded_video_sha256(baseline_output_path)
        current_video_hash = decoded_video_sha256(output_path)
        visual_baseline_report = {
            "enabled": True,
            "baseline_output_uri": visual_baseline_output_uri,
            "hash_kind": "ffmpeg_decoded_video_sha256",
            "baseline_video_sha256": baseline_video_hash,
            "current_video_sha256": current_video_hash,
            "matches": baseline_video_hash == current_video_hash,
            "reused_for_video": visual_baseline_reuse,
        }
        if not visual_baseline_report["matches"]:
            raise ValueError(
                "v6 decoded video differs from v5 visual baseline: "
                f"{baseline_video_hash} != {current_video_hash}"
            )

    log("upload_output", output_uri=output_uri, bytes=output_path.stat().st_size)
    upload_gcs(client, output_path, output_uri, "video/mp4")
    qa = extract_and_upload_qa(
        client,
        source_path,
        output_path,
        events,
        orphans,
        clusters,
        meta,
        work,
        output_prefix,
    )
    filled_intervals = [
        {"source_cluster_index": event.source_cluster_index, **dict(interval)}
        for event in events
        for interval in event.filled_intervals
    ]
    report = {
        "ok": True,
        "input_kind": input_kind,
        "douyin_url": douyin_url,
        "source_uri": source_uri,
        "source_info": source_info,
        "source_generation": source_generation,
        "source_id": artifacts["source_id"],
        "result_prefix": output_prefix,
        "cues_uri": cues_uri,
        "mask_uri": mask_uri,
        "output_uri": output_uri,
        "report_uri": f"{output_prefix}/cover_report.json",
        "style": style,
        "parameters": parameters,
        "source_duration": source_meta.duration,
        "target_duration": target_duration,
        "cue_count": len(cues),
        "cue_quality": cue_quality,
        "cluster_count": len(clusters),
        "event_count": len(events),
        "text_event_count": sum(len(event_text_events(event)) for event in events),
        "filled_interval_count": len(filled_intervals),
        "filled_intervals": filled_intervals,
        "matched_cluster_count": len({event.source_cluster_index for event in events if not event.unmatched}),
        "unmatched_cluster_count": len(orphans),
        "unmatched_clusters": orphans,
        "events": [event.report_dict() for event in events],
        "cue_matches": matches,
        "coverage": coverage,
        "detection_timeline_coverage": detection_timeline_coverage,
        "text_coverage": text_coverage,
        "geometry": geometry,
        "layout_quality": layout_quality,
        "timing_quality": timing_quality,
        "cluster_timing": cluster_timing,
        "sub_band_rect_audit": band_audit,
        "v4_v5_event_rect_diff": rect_diff,
        "tts": tts_report,
        "visual_baseline": visual_baseline_report,
        "render_event_count": len(render_events),
        "blur_event_count_after_merge": len(render_events) if style == "blur" else None,
        "rounded_masks": rounded_mask_report,
        "segments": segment_report,
        "qa": qa,
        "output_probe": output_probe,
        "output_duration": output_duration,
        "duration_delta_seconds": duration_delta,
        "output_sha256": file_sha256(output_path),
        "elapsed_seconds": time.time() - started,
    }
    desub.write_json(report_path, report)
    upload_gcs(client, report_path, report["report_uri"], "application/json")
    upload_pipeline_status(
        client,
        output_prefix,
        "completed",
        input_kind=input_kind,
        source_uri=source_uri,
        douyin_url=douyin_url,
        output_uri=output_uri,
        report_uri=report["report_uri"],
        mask_uri=mask_uri,
        cues_uri=cues_uri,
        cue_count=len(cues),
        cluster_count=len(clusters),
        unmatched_cluster_count=len(orphans),
        elapsed_seconds=report["elapsed_seconds"],
    )
    log(
        "completed",
        output_uri=output_uri,
        report_uri=report["report_uri"],
        event_count=len(events),
        unmatched_cluster_count=len(orphans),
        elapsed_seconds=report["elapsed_seconds"],
    )
    return 0


def entrypoint() -> int:
    try:
        return main()
    except Exception as exc:
        log("failed", error_type=type(exc).__name__, error=str(exc))
        try:
            client = _gcs_client()
            cover_input = resolve_cover_input(client)
            artifacts = dict(cover_input["artifacts"])
            upload_pipeline_status(
                client,
                str(artifacts["result_prefix"]),
                "failed",
                input_kind=cover_input["input_kind"],
                source_uri=cover_input["source_uri"],
                douyin_url=cover_input["douyin_url"],
                error_type=type(exc).__name__,
                error=str(exc),
            )
        except Exception as status_exc:  # noqa: BLE001
            log(
                "failed_status_upload",
                error_type=type(status_exc).__name__,
                error=str(status_exc),
            )
        raise


if __name__ == "__main__":
    raise SystemExit(entrypoint())
