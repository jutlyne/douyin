#!/usr/bin/env python3
"""Apply reviewed timing and layout corrections to chunked Vietnamese cues."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


WRAP_CHARS = 24
TWO_LINE_Y_SHIFT = 24.0 / 1280.0
VIDEO_DURATION = 190.966667


def balanced_lines(value: str) -> list[str]:
    text = " ".join(str(value or "").split())
    if len(text) <= WRAP_CHARS or " " not in text:
        return [text]

    words = text.split()
    candidates: list[tuple[int, int, str, str]] = []
    for index in range(1, len(words)):
        left = " ".join(words[:index])
        right = " ".join(words[index:])
        overflow = max(0, len(left) - WRAP_CHARS) + max(0, len(right) - WRAP_CHARS)
        candidates.append((overflow, abs(len(left) - len(right)), left, right))
    _, _, left, right = min(candidates)
    return [left, right]


def updated(cue: dict[str, Any], **changes: Any) -> dict[str, Any]:
    result = dict(cue)
    result.update(changes)
    return result


def replacement(cue: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return updated(cue, **changes)


def finalize(source: dict[str, Any]) -> dict[str, Any]:
    cues = source.get("cues") or []
    if len(cues) != 80:
        raise ValueError(f"Expected 80 v5 cues, got {len(cues)}")

    result: list[dict[str, Any]] = []
    for index, cue in enumerate(cues):
        if index in {5, 37, 47, 49}:
            continue

        if index == 4:
            result.append(replacement(
                cue,
                start=4.6,
                end=7.6,
                text_zh="\u6709\u554a\u4f60\u6709\u663e\u793a\u5668\u5417",
                text_vi="C\u00f3 ch\u1ee9, b\u1ea1n c\u00f3 m\u00e0n h\u00ecnh ch\u01b0a?",
            ))
        elif index == 14:
            result.append(updated(cue, start=17.85, end=19.5))
        elif index == 15:
            result.append(updated(cue, start=19.5, end=20.25))
        elif index == 38:
            result.append(updated(cue, start=92.2, end=93.3))
        elif index == 39:
            result.append(updated(cue, center_y=0.73515625))
        elif index == 40:
            result.append(updated(cue, start=101.25, end=102.5, center_y=0.73515625))
        elif index == 41:
            result.append(updated(cue, start=102.5, end=103.2, center_y=0.73515625))
        elif index == 42:
            result.append(updated(cue, start=105.75, end=106.9))
        elif index == 45:
            result.append(updated(cue, end=109.75))
        elif index == 46:
            result.append(replacement(
                cue,
                start=109.75,
                end=111.25,
                text_zh="\u9ed1\u8272\u7684\u5427\u8010\u810f\u4e00\u70b9",
                text_vi="M\u00e0u \u0111en \u0111i, \u0111\u1ee1 b\u1ea9n h\u01a1n.",
            ))
        elif index == 48:
            result.append(replacement(
                cue,
                start=111.25,
                end=112.25,
                text_zh="\u597d\u6ca1\u95ee\u9898",
                text_vi="\u0110\u01b0\u1ee3c, kh\u00f4ng v\u1ea5n \u0111\u1ec1.",
            ))
        elif index == 50:
            result.append(updated(cue, start=112.25))
        elif index == 57:
            result.append(updated(cue, start=128.75, end=129.75))
        elif index == 74:
            result.extend([
                replacement(
                    cue,
                    start=170.0,
                    end=170.5,
                    text_zh="\u4f60\u653e\u8fd9\u5427",
                    text_vi="Em \u0111\u1ec3 \u0111\u00e2y nh\u00e9.",
                ),
                replacement(
                    cue,
                    start=170.5,
                    end=171.5,
                    text_zh="\u6211\u7ed9\u4f60\u6253\u5305\u597d",
                    text_vi="Anh \u0111\u00f3ng g\u00f3i cho.",
                ),
            ])
        elif index == 75:
            result.append(updated(cue, start=171.5, end=172.1, text_vi="V\u00e2ng."))
        elif index == 77:
            result.append(updated(cue, end=187.0, text_vi="OK, mang v\u1ec1 nh\u00e9."))
        elif index == 78:
            result.append(updated(
                cue,
                start=187.0,
                end=190.05,
                text_vi="\u0110\u00e3 giao th\u00e0nh c\u00f4ng m\u1ed9t b\u1ed9 m\u00e1y x\u1ecbn. T\u1ea1m bi\u1ec7t!",
            ))
        elif index == 79:
            result.append(updated(
                cue,
                start=190.2,
                end=VIDEO_DURATION,
                text_vi="T\u1ea1m bi\u1ec7t!",
            ))
        else:
            result.append(dict(cue))

    previous_end = 0.0
    for index, cue in enumerate(result):
        start = float(cue["start"])
        end = float(cue["end"])
        if start < previous_end - 0.001:
            raise ValueError(f"Cue {index} overlaps its predecessor: {start} < {previous_end}")
        if end <= start + 0.1:
            raise ValueError(f"Cue {index} is too short: {start}-{end}")
        if not str(cue.get("text_vi") or "").strip():
            raise ValueError(f"Cue {index} has no Vietnamese text")

        lines = balanced_lines(str(cue["text_vi"]))
        if len(lines) > 2:
            raise ValueError(f"Cue {index} wraps to more than two lines")
        if len(lines) == 2:
            cue["center_y"] = min(0.80, float(cue["center_y"]) + TWO_LINE_Y_SHIFT)
        cue["line_count"] = len(lines)
        cue["reviewed_index"] = index
        previous_end = end

    output = dict(source)
    output["parent_cue_object"] = "visub_cues_chunked_v5.json"
    output["cue_count"] = len(result)
    output["cues"] = result
    output["finalization"] = {
        "version": "v7-reviewed",
        "max_lines": 2,
        "wrap_chars": WRAP_CHARS,
        "two_line_y_shift_ratio": TWO_LINE_Y_SHIFT,
        "dropped_source_indexes": [37],
        "merged_source_indexes": [[4, 5], [46, 47], [48, 49]],
        "manual_split_source_indexes": [74],
        "vertical_realign_source_indexes": [39, 40, 41],
    }
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    source = json.loads(args.input.read_text(encoding="utf-8"))
    output = finalize(source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    line_counts = {1: 0, 2: 0}
    for cue in output["cues"]:
        line_counts[int(cue["line_count"])] += 1
    print(json.dumps({
        "cue_count": output["cue_count"],
        "one_line": line_counts[1],
        "two_line": line_counts[2],
        "first_start": output["cues"][0]["start"],
        "last_end": output["cues"][-1]["end"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
