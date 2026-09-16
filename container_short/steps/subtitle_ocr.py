"""Detect burned-in Chinese subtitle cues from a short video clip."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from difflib import SequenceMatcher

from . import ffmpeg_ops


@dataclass
class SubtitleCue:
    start: float
    end: float
    text_zh: str


def _normalize(text: str) -> str:
    chars = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff，。！？、：；“”‘’…]+", text)
    return "".join(chars).strip("，。！？、：；")


def _similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a in b or b in a:
        return min(len(a), len(b)) >= 3
    return SequenceMatcher(None, a, b).ratio() >= 0.62


def detect_subtitle_cues(
    video_path: str,
    work_dir: str,
    *,
    fps: float = 4.0,
    crop_top_ratio: float = 0.58,
    min_duration: float = 0.35,
) -> list[SubtitleCue]:
    """OCR the lower part of the clip and group repeated text into timed cues."""
    import pytesseract  # type: ignore
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps  # type: ignore

    frames_dir = os.path.join(work_dir, "subtitle_ocr_frames")
    if os.path.isdir(frames_dir):
        shutil.rmtree(frames_dir)
    os.makedirs(frames_dir, exist_ok=True)

    pattern = os.path.join(frames_dir, "%06d.png")
    vf = (
        f"fps={fps},"
        f"crop=iw:ih*{1.0 - crop_top_ratio:.4f}:0:ih*{crop_top_ratio:.4f},"
        "scale=iw*1.5:ih*1.5"
    )
    cmd = [
        ffmpeg_ops.ffmpeg_bin(), "-y", "-i", video_path,
        "-vf", vf, "-an", pattern,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ffmpeg_ops.FFmpegError(f"extract OCR frames failed: {proc.stderr[-1200:]}")

    observations: list[tuple[float, str]] = []
    for index, name in enumerate(sorted(os.listdir(frames_dir))):
        if not name.lower().endswith(".png"):
            continue
        path = os.path.join(frames_dir, name)
        with Image.open(path) as image:
            gray = ImageOps.grayscale(image)
            gray = ImageEnhance.Contrast(gray).enhance(2.2)
            gray = gray.filter(ImageFilter.SHARPEN)
            text = pytesseract.image_to_string(
                gray,
                lang="chi_sim",
                config="--psm 6",
            )
        observations.append((index / fps, _normalize(text)))

    cues: list[SubtitleCue] = []
    active_text = ""
    active_start = 0.0
    last_seen = 0.0
    blank_count = 0

    def close_active(end: float) -> None:
        nonlocal active_text, active_start
        if active_text and end - active_start >= min_duration:
            cues.append(SubtitleCue(active_start, max(active_start + min_duration, end), active_text))
        active_text = ""

    for timestamp, text in observations:
        if not text:
            blank_count += 1
            if active_text and blank_count >= 2:
                close_active(last_seen + 1.0 / fps)
            continue

        blank_count = 0
        if not active_text:
            active_text = text
            active_start = timestamp
        elif _similar(active_text, text):
            if len(text) > len(active_text):
                active_text = text
        else:
            close_active(timestamp)
            active_text = text
            active_start = timestamp
        last_seen = timestamp

    if active_text:
        close_active(last_seen + 1.0 / fps)

    # Merge tiny OCR splits that are effectively the same subtitle.
    merged: list[SubtitleCue] = []
    for cue in cues:
        if merged and cue.start - merged[-1].end <= 0.35 and _similar(merged[-1].text_zh, cue.text_zh):
            merged[-1].end = cue.end
            if len(cue.text_zh) > len(merged[-1].text_zh):
                merged[-1].text_zh = cue.text_zh
        else:
            merged.append(cue)
    return merged
