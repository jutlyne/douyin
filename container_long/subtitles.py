from __future__ import annotations

from typing import Iterable, TextIO

from container_long.text_cleanup import normalize_long_vi_text
from container_long.utils import srt_time


def write_vietnamese_srt(
    handle: TextIO,
    entries: Iterable[tuple[float, float, str]],
) -> int:
    count = 0
    for start, end, text_vi in entries:
        text = normalize_long_vi_text(text_vi)
        if end <= start or not text:
            continue
        count += 1
        handle.write(
            f"{count}\n"
            f"{srt_time(start)} --> {srt_time(end)}\n"
            f"{text}\n\n"
        )
    return count
