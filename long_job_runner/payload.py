from __future__ import annotations


def as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def to_seconds(value) -> float:
    """Parse ``HH:MM:SS`` / ``MM:SS`` / ``SS`` / numeric into float seconds."""
    if isinstance(value, bool):
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


def parse_cut_spans(raw_spans) -> tuple[list[list[float]], str]:
    """Normalize incoming cut spans into ``[[start, end], ...]`` in seconds.

    Accepts each span as ``["01:29:52", "01:30:10"]``, ``[start, end]`` seconds,
    or ``{"start": ..., "end": ...}``. Returns (spans, error_message).
    """
    if not isinstance(raw_spans, list) or not raw_spans:
        return [], "spans must be a non-empty list"
    spans: list[list[float]] = []
    for index, item in enumerate(raw_spans):
        if isinstance(item, dict):
            start, end = item.get("start"), item.get("end")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            start, end = item
        else:
            return [], f"spans[{index}] must be [start, end] or {{start, end}}"
        try:
            start_s, end_s = to_seconds(start), to_seconds(end)
        except (ValueError, TypeError) as exc:
            return [], f"spans[{index}] has an invalid time: {exc}"
        if end_s <= start_s:
            return [], f"spans[{index}] end must be greater than start"
        spans.append([start_s, end_s])
    return spans, ""


def parse_videos(raw_videos, *, force_refresh_all: bool) -> tuple[list[dict], str]:
    if not isinstance(raw_videos, list) or not 1 <= len(raw_videos) <= 30:
        return [], "videos must contain between 1 and 30 items"

    videos: list[dict] = []
    for index, item in enumerate(raw_videos):
        force_refresh = force_refresh_all
        if isinstance(item, str):
            url = item.strip()
        elif isinstance(item, dict):
            url = str(item.get("douyin_url") or "").strip()
            force_refresh = force_refresh or as_bool(item.get("force_refresh"))
        else:
            return [], f"videos[{index}] must be a string or object"
        if not url.startswith(("http://", "https://")):
            return [], f"videos[{index}] has an invalid douyin_url"
        entry = {"douyin_url": url}
        if force_refresh:
            entry["force_refresh"] = True
        videos.append(entry)
    return videos, ""
