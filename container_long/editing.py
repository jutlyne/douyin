"""Pure helpers for the human-in-the-loop ad-cut flow.

These functions take the final subtitle/chapter timeline and a list of cut
spans (in seconds) and return the shifted timeline after those spans are
removed from the video. No ffmpeg or GCS access lives here so the logic stays
unit-testable in isolation. See ``review_job.py`` for the job that applies the
resulting cuts to the assembled ``final-long.*`` artifacts.
"""

from __future__ import annotations

import re

from container_long.text_cleanup import normalize_long_vi_text

_EPS = 1e-6
_CHAPTER_RE = re.compile(r"^\s*(\d+):(\d{2}):(\d{2})\s+(.+?)\s*$")

_AD_PROMPT_TEMPLATE = """You are reviewing the Vietnamese subtitle track of a \
long compilation video assembled from short Douyin clips. Some clips contain \
PROMOTIONAL / ADVERTISEMENT segments that are NOT part of the story: \
second-hand phone trade-in ("thu cu doi moi", Aihuishou, Guazi), loans/credit \
("vay tien"), brand promos (Huawei/iPhone welfare), shopping/discount \
calls-to-action, "link o bio", or subscribe/channel promos.

Identify every contiguous advertisement segment. For each one return start and \
end in SECONDS (absolute, matching the (t=...) values shown), a short \
Vietnamese reason, and a confidence between 0 and 1.

Rules:
- Only flag genuine ads/promotions, NOT normal story dialogue that merely \
mentions money or objects.
- Merge adjacent advertisement lines into a single span.
- start/end must stay within the subtitle timeline and end must be > start.
- If there are no advertisements, return spans=[].

Subtitle lines (absolute time):
{timeline}
"""


def to_seconds(value) -> float:
    """Parse ``HH:MM:SS`` / ``MM:SS`` / ``SS`` / numeric into float seconds."""
    if isinstance(value, bool):  # avoid True -> 1.0 surprises
        raise ValueError(f"invalid time value: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().replace(",", ".")
    if not text:
        raise ValueError("empty time value")
    if ":" in text:
        parts = text.split(":")
        if len(parts) > 3:
            raise ValueError(f"invalid time value: {value!r}")
        total = 0.0
        for part in parts:
            total = total * 60.0 + float(part)
        return total
    return float(text)


def chapter_time_str(seconds: float) -> str:
    """Format seconds as ``HH:MM:SS`` (matches assembler chapter format)."""
    total = max(0, int(float(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def hms(seconds: float) -> str:
    """Alias for :func:`chapter_time_str` used when reporting ad candidates."""
    return chapter_time_str(seconds)


def parse_cut_span(item) -> list[float]:
    """Coerce one span (``[start, end]`` / ``{start, end}``) into ``[s, e]`` secs."""
    if isinstance(item, dict):
        start = item.get("start")
        end = item.get("end")
    elif isinstance(item, (list, tuple)) and len(item) == 2:
        start, end = item
    else:
        raise ValueError(f"invalid cut span: {item!r}")
    return [to_seconds(start), to_seconds(end)]


def parse_cut_spans(items) -> list[list[float]]:
    """Parse a list of spans; skips empty/degenerate spans, keeps order."""
    spans: list[list[float]] = []
    for item in items or []:
        start, end = parse_cut_span(item)
        if end - start > _EPS:
            spans.append([start, end])
    return spans


def normalize_cut_spans(spans, *, duration=None) -> list[tuple[float, float]]:
    """Clamp, sort and merge overlapping cut spans into disjoint ``(s, e)``."""
    cleaned: list[tuple[float, float]] = []
    for pair in spans:
        start, end = float(pair[0]), float(pair[1])
        start = max(0.0, start)
        if duration is not None:
            limit = float(duration)
            start = min(start, limit)
            end = min(end, limit)
        if end - start > _EPS:
            cleaned.append((start, end))
    cleaned.sort()
    merged: list[tuple[float, float]] = []
    for start, end in cleaned:
        if merged and start <= merged[-1][1] + _EPS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def keep_segments(cut_spans, duration) -> list[tuple[float, float]]:
    """Return the segments to keep (complement of cut spans) within [0, dur]."""
    duration = float(duration)
    spans = normalize_cut_spans(cut_spans, duration=duration)
    kept: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in spans:
        if start - cursor > 1e-3:
            kept.append((cursor, start))
        cursor = max(cursor, end)
    if duration - cursor > 1e-3:
        kept.append((cursor, duration))
    return kept


def removed_before(t: float, cut_spans) -> float:
    """Total cut duration strictly before time ``t`` (spans must be normalized)."""
    t = float(t)
    total = 0.0
    for start, end in cut_spans:
        if end <= t:
            total += end - start
        elif start < t:  # start < t < end
            total += t - start
    return total


def shift_subtitle_lines(lines, cut_spans, *, duration=None):
    """Shift ``(start, end, text)`` lines after removing ``cut_spans``.

    - lines fully inside a cut span are dropped;
    - lines straddling a boundary are clamped to the surviving side;
    - all surviving times are pulled back by the cut duration before them.
    """
    spans = normalize_cut_spans(cut_spans, duration=duration)
    if not spans:
        return [(float(s), float(e), t) for s, e, t in lines]

    result: list[tuple[float, float, str]] = []
    for start, end, text in lines:
        start = float(start)
        end = float(end)
        if end <= start:
            continue
        # Fully covered by a single cut span -> the line disappears.
        if any(s <= start and end <= e for s, e in spans):
            continue
        new_start, new_end = start, end
        for s, e in spans:
            if s <= new_start < e:  # starts inside a cut -> jump to its end
                new_start = e
            if s < new_end <= e:  # ends inside a cut -> pull back to its start
                new_end = s
        if new_end <= new_start:
            continue
        shifted_start = new_start - removed_before(new_start, spans)
        shifted_end = new_end - removed_before(new_end, spans)
        if shifted_end - shifted_start > 1e-3:
            result.append((shifted_start, shifted_end, text))
    return result


def build_ad_detection_prompt(subtitles) -> str:
    """Render the ad-detection prompt from the structured subtitle timeline."""
    lines = [
        item
        for item in (subtitles or [])
        if str(item.get("text_vi") or "").strip()
    ]
    timeline = "\n".join(
        f"[{hms(item['start'])} - {hms(item['end'])}] "
        f"(t={float(item['start']):.1f}-{float(item['end']):.1f}) "
        f"{str(item.get('text_vi') or '').strip()}"
        for item in lines
    )
    return _AD_PROMPT_TEMPLATE.format(timeline=timeline)


def normalize_ad_spans(raw_spans) -> list[dict]:
    """Clamp/sort raw ad spans (dicts with start/end/reason_vi/confidence)."""
    spans: list[dict] = []
    for span in raw_spans or []:
        start = max(0.0, float(span.get("start", 0.0)))
        end = float(span.get("end", 0.0))
        if end <= start:
            continue
        spans.append({
            "start": start,
            "end": end,
            "reason_vi": normalize_long_vi_text(str(span.get("reason_vi") or "")),
            "confidence": max(0.0, min(1.0, float(span.get("confidence") or 0.0))),
        })
    spans.sort(key=lambda item: item["start"])
    return spans


def parse_chapter_line(line: str):
    """Return ``(seconds, title)`` for an ``HH:MM:SS Title`` line, else None."""
    match = _CHAPTER_RE.match(str(line or ""))
    if not match:
        return None
    hours, minutes, secs, title = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(secs), title


def shift_chapter_lines(chapter_lines, cut_spans, *, duration=None):
    """Shift ``HH:MM:SS Title`` chapter lines after removing ``cut_spans``."""
    spans = normalize_cut_spans(cut_spans, duration=duration)
    if not spans:
        return list(chapter_lines)

    shifted: list[str] = []
    for line in chapter_lines:
        parsed = parse_chapter_line(line)
        if parsed is None:
            shifted.append(line)
            continue
        seconds, title = parsed
        for s, e in spans:
            if s <= seconds < e:  # chapter starts inside a cut -> move to its end
                seconds = e
        new_seconds = max(0.0, seconds - removed_before(seconds, spans))
        shifted.append(f"{chapter_time_str(new_seconds)} {title}")
    return shifted
