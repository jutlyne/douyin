from __future__ import annotations

import re
from typing import Any

from container_long.models import LongDialogueLine


_NAME_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    (r"\bTôm\s+Trường\s+Sinh\b", "Hà Trường Sinh"),
    (r"\bTôm\s+tiên\s+sinh\b", "Hà tiên sinh"),
    (r"\bTôm\s+mỗ\b", "Hà mỗ"),
    (r"\bHạ\s+Trường\s+Sinh\b", "Hà Trường Sinh"),
    (r"\bHạ\s+tiên\s+sinh\b", "Hà tiên sinh"),
    (r"\bHạ\s+mỗ\b", "Hà mỗ"),
)


def normalize_long_vi_text(value: str) -> str:
    text = str(value or "").strip()
    for pattern, replacement in _NAME_REPLACEMENTS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def normalize_long_dialogue_line(line: LongDialogueLine) -> LongDialogueLine:
    return LongDialogueLine(
        start=line.start,
        end=line.end,
        text_zh=line.text_zh,
        text_vi=normalize_long_vi_text(line.text_vi),
    )


def normalize_long_dialogue_lines(
    lines: list[LongDialogueLine],
) -> list[LongDialogueLine]:
    return [normalize_long_dialogue_line(line) for line in lines]


def normalize_long_text_value(value: Any) -> Any:
    if isinstance(value, str):
        return normalize_long_vi_text(value)
    if isinstance(value, list):
        return [normalize_long_text_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: normalize_long_text_value(item)
            for key, item in value.items()
        }
    return value
