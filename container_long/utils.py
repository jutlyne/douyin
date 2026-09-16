from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import replace
from difflib import SequenceMatcher

from container_long.models import LongDialogueLine

_TOKEN_STOPWORDS = {
    "a",
    "ai",
    "anh",
    "ba",
    "boi",
    "cai",
    "cac",
    "cho",
    "co",
    "con",
    "cua",
    "da",
    "de",
    "den",
    "duoc",
    "gi",
    "la",
    "lai",
    "ma",
    "mot",
    "nay",
    "nen",
    "nhung",
    "ong",
    "qua",
    "ra",
    "roi",
    "ta",
    "thi",
    "toi",
    "trong",
    "va",
    "ve",
    "vi",
}


def _fold_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    return re.sub(r"[^\w]+", "", without_marks, flags=re.UNICODE)


def _fold_words(value: str) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    folded = re.sub(r"[^\w]+", " ", without_marks, flags=re.UNICODE)
    return [
        word
        for word in folded.split()
        if len(word) >= 2 and word not in _TOKEN_STOPWORDS
    ]


def _is_noise_line(line: LongDialogueLine) -> bool:
    text_vi = line.text_vi.strip()
    text_zh = line.text_zh.strip()
    folded_vi = _fold_text(text_vi)
    if text_vi.startswith("@") or text_zh.startswith("@"):
        return True
    if "故事" in text_zh and "虚构" in text_zh:
        return True
    return "cauchuyen" in folded_vi and "hucau" in folded_vi


def _overlap_ratio(a: LongDialogueLine, b: LongDialogueLine) -> float:
    overlap = min(a.end, b.end) - max(a.start, b.start)
    if overlap <= 0:
        return 0.0
    shortest = min(a.end - a.start, b.end - b.start)
    if shortest <= 0:
        return 0.0
    return overlap / shortest


def _time_conflicts(
    existing: LongDialogueLine,
    candidate: LongDialogueLine,
    *,
    min_overlap: float = 0.35,
    edge_tolerance: float = 0.25,
) -> bool:
    if _overlap_ratio(existing, candidate) >= min_overlap:
        return True
    existing_midpoint = (existing.start + existing.end) / 2.0
    candidate_midpoint = (candidate.start + candidate.end) / 2.0
    return (
        candidate.start - edge_tolerance
        <= existing_midpoint
        <= candidate.end + edge_tolerance
    ) or (
        existing.start - edge_tolerance
        <= candidate_midpoint
        <= existing.end + edge_tolerance
    )


def _bracketed_by_visual_lines(
    existing: LongDialogueLine,
    visual_lines: list[LongDialogueLine],
    *,
    max_edge_gap: float = 0.75,
) -> bool:
    previous = [line for line in visual_lines if line.end <= existing.start]
    following = [line for line in visual_lines if line.start >= existing.end]
    if not previous or not following:
        return False
    prev = max(previous, key=lambda line: line.end)
    nxt = min(following, key=lambda line: line.start)
    return (
        existing.start - prev.end <= max_edge_gap
        and nxt.start - existing.end <= max_edge_gap
    )

def _same_or_contained(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    return len(shorter) >= 6 and shorter in longer


def _similar_text(left: str, right: str) -> bool:
    if not left or not right:
        return False
    shorter, _ = sorted((left, right), key=len)
    if len(shorter) < 12:
        return False
    return SequenceMatcher(None, left, right).ratio() >= 0.86


def _similar_token_subset(left: str, right: str) -> bool:
    left_words = set(_fold_words(left))
    right_words = set(_fold_words(right))
    if not left_words or not right_words:
        return False
    shorter, longer = sorted((left_words, right_words), key=len)
    if len(shorter) < 3:
        return False
    common = len(shorter & longer)
    return common >= 3 and common / len(shorter) >= 0.75


def _is_duplicate_line(existing: LongDialogueLine, candidate: LongDialogueLine) -> bool:
    existing_vi = _fold_text(existing.text_vi)
    candidate_vi = _fold_text(candidate.text_vi)
    existing_zh = _fold_text(existing.text_zh)
    candidate_zh = _fold_text(candidate.text_zh)
    same_or_contained = (
        _same_or_contained(existing_vi, candidate_vi)
        or _same_or_contained(existing_zh, candidate_zh)
    )
    similar_text = _similar_text(existing_vi, candidate_vi) or _similar_text(
        existing_zh, candidate_zh
    )
    similar_tokens = _similar_token_subset(
        existing.text_vi, candidate.text_vi
    ) or _similar_token_subset(existing.text_zh, candidate.text_zh)

    same_time = (
        abs(existing.start - candidate.start) <= 0.5
        and abs(existing.end - candidate.end) <= 0.5
    )
    overlapping = _overlap_ratio(existing, candidate) >= 0.6
    soft_overlapping = _overlap_ratio(existing, candidate) >= 0.35
    nearby_touching = (
        candidate.start <= existing.end + 0.75
        and candidate.end >= existing.start - 0.25
    )
    if same_or_contained:
        return same_time or overlapping or nearby_touching
    if similar_text:
        return same_time or overlapping
    if similar_tokens:
        return same_time or soft_overlapping or nearby_touching
    return False


def _prefer_replacement(existing: LongDialogueLine, candidate: LongDialogueLine) -> bool:
    if len(candidate.text_vi.strip()) > len(existing.text_vi.strip()) + 2:
        return True
    return len(_fold_text(candidate.text_zh)) > len(_fold_text(existing.text_zh))


def chunk_ranges(
    duration: float,
    chunk_seconds: int,
    *,
    min_chunk_seconds: int = 240,
    max_chunk_seconds: int = 480,
) -> list[tuple[float, float]]:
    """Split a video into balanced chunks around the requested target size."""
    if duration <= 0:
        raise ValueError("duration must be > 0")
    target = max(60, int(chunk_seconds))
    minimum = max(60, int(min_chunk_seconds))
    maximum = max(minimum, int(max_chunk_seconds))
    target = min(maximum, max(minimum, target))

    # Prefer fewer, larger chunks to reduce Gemini calls while respecting the
    # configured upper bound.
    count = max(1, math.ceil(duration / maximum))
    while (
        count > 1
        and duration / count < minimum
        and duration / (count - 1) <= maximum
    ):
        count -= 1

    size = duration / count
    return [
        (index * size, duration if index == count - 1 else (index + 1) * size)
        for index in range(count)
    ]


def srt_time(seconds: float) -> str:
    milliseconds = max(0, int(round(float(seconds) * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def select_chunk_lines(
    lines: list[LongDialogueLine],
    *,
    analysis_start: float,
    chunk_start: float,
    chunk_end: float,
) -> list[LongDialogueLine]:
    """Map padded-analysis timings into one non-overlapping output chunk."""
    chunk_duration = chunk_end - chunk_start
    selected: list[LongDialogueLine] = []
    for line in lines:
        absolute_start = analysis_start + line.start
        absolute_end = analysis_start + line.end
        midpoint = (absolute_start + absolute_end) / 2
        if midpoint < chunk_start or midpoint >= chunk_end:
            continue
        start = max(0.0, absolute_start - chunk_start)
        end = min(chunk_duration, absolute_end - chunk_start)
        if end <= start:
            continue
        selected.append(
            LongDialogueLine(
                start=start,
                end=end,
                text_zh=line.text_zh,
                text_vi=line.text_vi,
            )
        )
    return selected


def merge_dialogue_lines(lines: list[LongDialogueLine]) -> list[LongDialogueLine]:
    """Return stable time-ordered dialogue lines without near-identical repeats."""
    ordered = sorted(lines, key=lambda item: (item.start, item.end, item.text_vi))
    merged: list[LongDialogueLine] = []
    for line in ordered:
        if line.end <= line.start:
            continue
        if not (line.text_vi or line.text_zh).strip():
            continue
        if _is_noise_line(line):
            continue
        duplicate = False
        for prev_index in range(max(0, len(merged) - 6), len(merged)):
            prev = merged[prev_index]
            if _is_duplicate_line(prev, line):
                if _prefer_replacement(prev, line):
                    merged[prev_index] = line
                duplicate = True
                break
        if not duplicate:
            merged.append(line)
    return merged


def non_overlapping_dialogue_lines(
    lines: list[LongDialogueLine],
    *,
    min_gap: float = 0.05,
    min_duration: float = 0.2,
    dedup: bool = True,
    drop_short: bool = True,
) -> list[LongDialogueLine]:
    """Return de-duped dialogue lines with adjacent subtitle times unclashed.

    ``dedup=False``/``drop_short=False`` is the voiced-output mode: every line
    already has a TTS clip mixed into the audio, so no line may be removed —
    a removed subtitle leaves dubbed speech with nothing on screen.
    """
    if dedup:
        ordered = merge_dialogue_lines(lines)
    else:
        ordered = sorted(
            (
                line
                for line in lines
                if line.end > line.start and line.text_vi.strip()
            ),
            key=lambda item: (item.start, item.end),
        )
    if len(ordered) < 2:
        return ordered

    cleaned: list[LongDialogueLine] = []
    for line in ordered:
        current = line
        if cleaned:
            prev = cleaned[-1]
            required_start = prev.end + min_gap
            if current.start < required_start:
                trimmed_prev_end = max(
                    prev.start + min_duration,
                    min(prev.end, current.start - min_gap),
                )
                if trimmed_prev_end < prev.end:
                    cleaned[-1] = replace(prev, end=trimmed_prev_end)
                    prev = cleaned[-1]
                    required_start = prev.end + min_gap
                if current.start < required_start:
                    shifted_start = min(
                        current.end - min_duration,
                        required_start,
                    )
                    current = replace(current, start=max(0.0, shifted_start))
        if current.end - current.start < min_duration:
            if drop_short:
                continue
            if current.end <= current.start:
                continue
        cleaned.append(current)
    return cleaned


def suppress_tail_repeated_dialogue_lines(
    lines: list[LongDialogueLine],
    *,
    chunk_duration: float,
    tail_seconds: float = 8.0,
    min_repeat_gap: float = 20.0,
    min_text_chars: int = 18,
) -> list[LongDialogueLine]:
    """Drop long repeated lines that reappear near a chunk boundary.

    Gemini occasionally assigns an earlier subtitle to the final seconds of a
    chunk during retry/visual refresh. Short callouts such as "Tien sinh" are
    allowed to repeat, but a long sentence repeated near the tail is usually a
    hallucinated boundary line and creates wrong dubbed audio.
    """
    ordered = merge_dialogue_lines(lines)
    if not ordered:
        return []

    tail_start = max(0.0, float(chunk_duration) - max(0.0, tail_seconds))
    kept: list[LongDialogueLine] = []
    for line in ordered:
        folded_vi = _fold_text(line.text_vi)
        folded_zh = _fold_text(line.text_zh)
        comparable_len = max(len(folded_vi), len(folded_zh))
        midpoint = (line.start + line.end) / 2.0
        if midpoint >= tail_start and comparable_len >= min_text_chars:
            repeated = False
            for prev in kept:
                if line.start - prev.end < min_repeat_gap:
                    continue
                same_text = (
                    _same_or_contained(_fold_text(prev.text_vi), folded_vi)
                    or _same_or_contained(_fold_text(prev.text_zh), folded_zh)
                    or _similar_text(_fold_text(prev.text_vi), folded_vi)
                    or _similar_text(_fold_text(prev.text_zh), folded_zh)
                )
                if same_text:
                    repeated = True
                    break
            if repeated:
                continue
        kept.append(line)
    return kept


def readable_short_subtitle_lines(
    lines: list[LongDialogueLine],
    *,
    video_dur: float,
    min_duration: float = 0.75,
    min_gap: float = 0.05,
    dedup: bool = True,
    drop_short: bool = True,
) -> list[LongDialogueLine]:
    """Extend very short subtitle cues when there is room before the next cue."""
    ordered = non_overlapping_dialogue_lines(
        lines,
        min_gap=min_gap,
        dedup=dedup,
        drop_short=drop_short,
    )
    adjusted: list[LongDialogueLine] = []
    for index, line in enumerate(ordered):
        next_start = (
            ordered[index + 1].start
            if index + 1 < len(ordered)
            else float(video_dur)
        )
        target_end = min(
            float(video_dur),
            max(float(line.end), float(line.start) + max(0.0, min_duration)),
            max(float(line.end), float(next_start) - max(0.0, min_gap)),
        )
        if target_end > line.end:
            line = replace(line, end=target_end)
        adjusted.append(line)
    return adjusted

def replace_tail_dialogue_lines(
    lines: list[LongDialogueLine],
    candidates: list[LongDialogueLine],
    *,
    tail_start: float,
    lead_seconds: float = 0.2,
) -> list[LongDialogueLine]:
    """Replace a suspect tail region with freshly analyzed tail dialogue."""
    fresh_tail = merge_dialogue_lines(candidates)
    if not fresh_tail:
        return merge_dialogue_lines(lines)

    cutoff = float(tail_start) + max(0.0, float(lead_seconds))
    keep_before_tail = [
        line
        for line in lines
        if (line.start + line.end) / 2.0 < cutoff
    ]
    return merge_dialogue_lines(keep_before_tail + fresh_tail)


def prefer_visual_dialogue_lines(
    lines: list[LongDialogueLine],
    visual_lines: list[LongDialogueLine],
) -> list[LongDialogueLine]:
    """Prefer visible subtitles/captions over existing text in the same time span."""
    fresh_visual = merge_dialogue_lines(visual_lines)
    if not fresh_visual:
        return merge_dialogue_lines(lines)

    kept = [
        line
        for line in merge_dialogue_lines(lines)
        if not any(_time_conflicts(line, visual) for visual in fresh_visual)
        and not _bracketed_by_visual_lines(line, fresh_visual)
    ]
    return merge_dialogue_lines(kept + fresh_visual)


def dialogue_gap_ranges(
    lines: list[LongDialogueLine],
    *,
    min_gap: float,
) -> list[tuple[float, float]]:
    """Find large internal gaps between covered dialogue ranges."""
    ordered = merge_dialogue_lines(lines)
    if len(ordered) < 2:
        return []

    gaps: list[tuple[float, float]] = []
    covered_end = ordered[0].end
    for line in ordered[1:]:
        if line.start - covered_end > min_gap:
            gaps.append((covered_end, line.start))
        covered_end = max(covered_end, line.end)
    return gaps


def voice_synced_dialogue_lines(
    lines: list[LongDialogueLine],
    voice_sync: list[dict],
    *,
    video_dur: float,
) -> list[LongDialogueLine]:
    sync_by_index = {
        int(item.get("index", idx)): item for idx, item in enumerate(voice_sync)
    }
    synced: list[LongDialogueLine] = []
    for index, line in enumerate(lines):
        timing = sync_by_index.get(index)
        if timing:
            start = float(timing["actual_voice_start"])
            end = start + float(timing["fitted_duration"])
        else:
            start = float(line.start)
            end = float(line.end)
        start = max(0.0, min(float(video_dur), start))
        end = max(start, min(float(video_dur), end))
        if end <= start or not line.text_vi.strip():
            continue
        synced.append(
            LongDialogueLine(
                start=start,
                end=end,
                text_zh=line.text_zh,
                text_vi=line.text_vi,
            )
        )
    # Every synced line already has its TTS clip mixed into the audio, so
    # dedup here would leave dubbed speech without a subtitle. Input lines
    # were deduped before synthesis; only re-order after the time shift.
    return sorted(synced, key=lambda item: (item.start, item.end))
