import os
import tempfile
import unittest

from container_short.steps.ffmpeg_ops import (
    scheduled_voice_start,
    shift_dialogue_timeline,
    split_subtitle_phrases,
    stabilize_speech_timings,
    subtitle_lines_for_voice,
    write_ass_subtitles,
)
from container_short.steps.gemini_script import DialogueLine


class SequentialSubtitleTest(unittest.TestCase):
    def test_splits_vietnamese_by_punctuation(self):
        self.assertEqual(
            split_subtitle_phrases(
                "Chào bạn, tôi tên là Hà Nhân, tôi sống ở Trung Quốc."
            ),
            [
                "Chào bạn",
                "tôi tên là Hà Nhân",
                "tôi sống ở Trung Quốc",
            ],
        )

    def test_writes_one_phrase_per_sequential_event(self):
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "subtitles.ass")
            count = write_ass_subtitles(
                [
                    DialogueLine(
                        start=0,
                        end=6,
                        text_vi=(
                            "Chào bạn, tôi tên là Hà Nhân, "
                            "tôi sống ở Trung Quốc."
                        ),
                    )
                ],
                path,
                speed=1,
                video_dur=6,
                exact_timing=True,
                sequential_punctuation=True,
            )
            with open(path, encoding="utf-8") as handle:
                content = handle.read()

        self.assertEqual(count, 3)
        dialogue = [
            line for line in content.splitlines() if line.startswith("Dialogue:")
        ]
        self.assertEqual(len(dialogue), 3)
        self.assertNotIn("\\N", "\n".join(dialogue))
        self.assertTrue(all("{\\fs36}" in line for line in dialogue))
        self.assertIn("Chào bạn", dialogue[0])
        self.assertIn("tôi tên là Hà Nhân", dialogue[1])
        self.assertIn("tôi sống ở Trung Quốc", dialogue[2])

    def test_uses_rendered_voice_duration_instead_of_chinese_window(self):
        lines = [
            DialogueLine(
                start=42.2,
                end=44.4,
                text_vi=(
                    "Lần này! Ta muốn tất cả những kẻ tham gia "
                    "phải chôn cùng chúng!"
                ),
            )
        ]
        synced = subtitle_lines_for_voice(
            lines,
            [{
                "actual_voice_start": 42.2,
                "fitted_duration": 2.758,
            }],
            speed=1,
            video_dur=45,
        )
        self.assertEqual(synced[0].start, 42.2)
        self.assertAlmostEqual(synced[0].end, 44.958)

    def test_negative_offset_moves_tts_and_subtitle_earlier(self):
        lines = [
            DialogueLine(start=2.16, end=3.0, text_vi="Tuân lệnh")
        ]
        shift_dialogue_timeline(
            lines,
            offset_seconds=-0.25,
            speed=1,
        )
        self.assertAlmostEqual(lines[0].start, 1.91)
        self.assertAlmostEqual(lines[0].end, 2.75)

    def test_dynamic_timing_only_clamps_outlier_cues(self):
        class Cue:
            def __init__(self, start):
                self.start = start

        stabilized = stabilize_speech_timings(
            [Cue(0.0), Cue(2.0), Cue(14.0)],
            [
                (0.9, 1.4),
                (2.06, 2.5),
                (13.5, 14.1),
            ],
        )
        self.assertAlmostEqual(stabilized[0][0], 0.15)
        self.assertAlmostEqual(stabilized[1][0], 2.06)
        self.assertAlmostEqual(stabilized[2][0], 13.75)
        self.assertAlmostEqual(stabilized[0][1] - stabilized[0][0], 0.5)

    def test_aligned_voice_does_not_inherit_previous_overflow(self):
        self.assertEqual(
            scheduled_voice_start(
                7.47,
                7.687,
                align_dub_to_speech=True,
            ),
            7.47,
        )


if __name__ == "__main__":
    unittest.main()
