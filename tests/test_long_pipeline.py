import unittest

from container_long.models import LongDialogueLine
from container_long.pipeline import _assert_no_large_output_gaps
from container_long.utils import (
    chunk_ranges,
    dialogue_gap_ranges,
    merge_dialogue_lines,
    non_overlapping_dialogue_lines,
    prefer_visual_dialogue_lines,
    readable_short_subtitle_lines,
    replace_tail_dialogue_lines,
    select_chunk_lines,
    srt_time,
    suppress_tail_repeated_dialogue_lines,
    voice_synced_dialogue_lines,
)
from container_short.steps.ffmpeg_ops import FFmpegError, voice_clip_fit_target
from container_long.editing import (
    build_ad_detection_prompt,
    keep_segments,
    normalize_ad_spans,
    normalize_cut_spans,
    shift_chapter_lines,
    shift_subtitle_lines,
    to_seconds,
)
from long_job_runner.payload import parse_cut_spans


class ChunkRangesTest(unittest.TestCase):
    def test_single_chunk(self):
        self.assertEqual(chunk_ranges(300, 360), [(0, 300)])

    def test_multiple_chunks(self):
        self.assertEqual(
            chunk_ranges(901, 360),
            [
                (0.0, 450.5),
                (450.5, 901),
            ],
        )

    def test_balances_longer_source_without_six_minute_cut(self):
        self.assertEqual(chunk_ranges(900, 360), [(0.0, 450.0), (450.0, 900)])

    def test_keeps_seven_minute_source_in_one_chunk(self):
        self.assertEqual(chunk_ranges(420, 360), [(0.0, 420)])

    def test_rejects_empty_video(self):
        with self.assertRaises(ValueError):
            chunk_ranges(0, 360)

    def test_srt_time_supports_long_video(self):
        self.assertEqual(srt_time(7_501.25), "02:05:01,250")

    def test_padded_analysis_does_not_duplicate_boundary_lines(self):
        selected = select_chunk_lines(
            [
                LongDialogueLine(1, 4, "A", "A"),
                LongDialogueLine(7, 11, "B", "B"),
            ],
            analysis_start=357,
            chunk_start=360,
            chunk_end=720,
        )
        self.assertEqual([line.text_vi for line in selected], ["B"])
        self.assertEqual(selected[0].start, 4)

    def test_detects_large_internal_dialogue_gap(self):
        gaps = dialogue_gap_ranges(
            [
                LongDialogueLine(20, 24, "A", "A"),
                LongDialogueLine(10, 12, "B", "B"),
                LongDialogueLine(60, 62, "C", "C"),
            ],
            min_gap=20,
        )
        self.assertEqual(gaps, [(24, 60)])

    def test_output_gap_without_source_dialogue_warns_only(self):
        _assert_no_large_output_gaps(
            [
                LongDialogueLine(10, 12, "A", "A"),
                LongDialogueLine(50, 52, "B", "B"),
            ],
            index=2,
            source_lines=[
                LongDialogueLine(10, 12, "A", "A"),
                LongDialogueLine(50, 52, "B", "B"),
            ],
        )

    def test_output_gap_with_source_dialogue_still_fails(self):
        with self.assertRaisesRegex(RuntimeError, "uncovered subtitle/dub gap"):
            _assert_no_large_output_gaps(
                [
                    LongDialogueLine(10, 12, "A", "A"),
                    LongDialogueLine(50, 52, "B", "B"),
                ],
                index=2,
                source_lines=[
                    LongDialogueLine(10, 12, "A", "A"),
                    LongDialogueLine(24, 26, "missing", "missing"),
                    LongDialogueLine(50, 52, "B", "B"),
                ],
            )

    def test_merge_dialogue_lines_dedupes_near_identical_repeats(self):
        merged = merge_dialogue_lines(
            [
                LongDialogueLine(10.0, 12.0, "A", "hello"),
                LongDialogueLine(10.2, 12.1, "A", "hello"),
                LongDialogueLine(13.0, 14.0, "B", "world"),
            ]
        )
        self.assertEqual([line.text_vi for line in merged], ["hello", "world"])

    def test_merge_dialogue_lines_filters_visual_watermark_noise(self):
        merged = merge_dialogue_lines(
            [
                LongDialogueLine(10.0, 11.0, "@foo", "@bar"),
                LongDialogueLine(12.0, 13.0, "故事纯属虚构", "Câu chuyện hoàn toàn là hư cấu"),
                LongDialogueLine(14.0, 15.0, "A", "real dialogue"),
            ]
        )
        self.assertEqual([line.text_vi for line in merged], ["real dialogue"])

    def test_merge_dialogue_lines_dedupes_same_zh_overlap(self):
        merged = merge_dialogue_lines(
            [
                LongDialogueLine(20.0, 22.0, "先生何故抱着一本空白的书看", "text one"),
                LongDialogueLine(20.1, 22.4, "先生何故抱着一本空白的书看", "text one better"),
            ]
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].text_vi, "text one better")

    def test_merge_dialogue_lines_dedupes_contained_tail_overlap(self):
        merged = merge_dialogue_lines(
            [
                LongDialogueLine(
                    103.5,
                    106.0,
                    "我已让弟子收拾出了一间空房 先生且随我来吧",
                    "Ta đã cho đệ tử dọn phòng, tiên sinh hãy đi theo ta.",
                ),
                LongDialogueLine(
                    105.5,
                    106.8,
                    "先生且随我来吧",
                    "Tiên sinh hãy theo ta đến đây.",
                ),
            ]
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].text_vi, "Ta đã cho đệ tử dọn phòng, tiên sinh hãy đi theo ta.")

    def test_merge_dialogue_lines_dedupes_semantic_tail_overlap(self):
        merged = merge_dialogue_lines(
            [
                LongDialogueLine(
                    10.0,
                    12.0,
                    "",
                    "Tuy co huong khoi, cung duc duoc kim than",
                ),
                LongDialogueLine(11.5, 12.2, "", "cung duc kim than"),
                LongDialogueLine(
                    13.0,
                    14.0,
                    "",
                    "nhung cuoi cung van khong the roi khoi noi nay",
                ),
            ]
        )

        self.assertEqual(
            [line.text_vi for line in merged],
            [
                "Tuy co huong khoi, cung duc duoc kim than",
                "nhung cuoi cung van khong the roi khoi noi nay",
            ],
        )

    def test_prefer_visual_dialogue_lines_replaces_same_time_guesses(self):
        refreshed = prefer_visual_dialogue_lines(
            [
                LongDialogueLine(10.0, 15.0, "A", "wrong merged text"),
                LongDialogueLine(20.0, 21.0, "B", "kept text"),
            ],
            [
                LongDialogueLine(10.0, 11.3, "V1", "visual first"),
                LongDialogueLine(11.4, 14.8, "V2", "visual second"),
            ],
        )

        self.assertEqual(
            [line.text_vi for line in refreshed],
            ["visual first", "visual second", "kept text"],
        )

    def test_prefer_visual_dialogue_lines_drops_bracketed_guess(self):
        refreshed = prefer_visual_dialogue_lines(
            [
                LongDialogueLine(10.2, 11.9, "wrong", "wrong inserted text"),
            ],
            [
                LongDialogueLine(8.0, 10.0, "V1", "visual before"),
                LongDialogueLine(12.1, 14.0, "V2", "visual after"),
            ],
        )

        self.assertEqual(
            [line.text_vi for line in refreshed],
            ["visual before", "visual after"],
        )
    def test_non_overlapping_dialogue_lines_clamps_distinct_overlap(self):
        cleaned = non_overlapping_dialogue_lines(
            [
                LongDialogueLine(20.0, 21.2, "", "Dung lai, nha mon co lenh"),
                LongDialogueLine(
                    21.0,
                    22.0,
                    "",
                    "Bat cu ai cung khong duoc den gan mieu Thanh Hoang",
                ),
            ]
        )

        self.assertEqual(len(cleaned), 2)
        self.assertLessEqual(cleaned[0].end + 0.05, cleaned[1].start)
        self.assertGreaterEqual(cleaned[0].end - cleaned[0].start, 0.2)

    def test_voice_synced_lines_use_actual_tts_duration(self):
        synced = voice_synced_dialogue_lines(
            [LongDialogueLine(58.4, 104.9, "A", "long line")],
            [{"index": 0, "actual_voice_start": 58.4, "fitted_duration": 6.5}],
            video_dur=220,
        )
        self.assertEqual(len(synced), 1)
        self.assertAlmostEqual(synced[0].start, 58.4)
        self.assertAlmostEqual(synced[0].end, 64.9)

    def test_voice_synced_lines_can_extend_into_padded_tail(self):
        synced = voice_synced_dialogue_lines(
            [LongDialogueLine(98.0, 100.0, "A", "tail line")],
            [{"index": 0, "actual_voice_start": 98.0, "fitted_duration": 3.0}],
            video_dur=101.2,
        )
        self.assertEqual(len(synced), 1)
        self.assertAlmostEqual(synced[0].start, 98.0)
        self.assertAlmostEqual(synced[0].end, 101.0)

    def test_voice_synced_keeps_short_echo_line_sharing_words(self):
        # Regression batch -178 (~6:02): câu echo dùng chung từ với câu trước
        # bị dedup drop SAU khi đã TTS → tiếng đọc mà không có sub.
        synced = voice_synced_dialogue_lines(
            [
                LongDialogueLine(
                    51.5,
                    54.6,
                    "出坊之时, 城隍大人有一事托我与先生商量",
                    "Lúc ra khỏi phường, Thành Hoàng đại nhân có một việc "
                    "nhờ ta thương lượng với tiên sinh.",
                ),
                LongDialogueLine(
                    55.7,
                    58.2,
                    "托你跟我商量?",
                    "Nhờ ngươi thương lượng với ta?",
                ),
            ],
            [
                {"index": 0, "actual_voice_start": 51.5, "fitted_duration": 3.77},
                {"index": 1, "actual_voice_start": 55.73, "fitted_duration": 2.4},
            ],
            video_dur=101.7,
        )

        self.assertEqual(len(synced), 2)
        self.assertAlmostEqual(synced[1].start, 55.73)
        self.assertAlmostEqual(synced[1].end, 58.13)

    def test_non_overlapping_voiced_mode_never_removes_lines(self):
        cleaned = non_overlapping_dialogue_lines(
            [
                LongDialogueLine(
                    51.5,
                    55.27,
                    "A",
                    "Nhờ ta thương lượng với tiên sinh.",
                ),
                LongDialogueLine(
                    55.73,
                    58.13,
                    "B",
                    "Nhờ ngươi thương lượng với ta?",
                ),
                LongDialogueLine(58.3, 58.45, "C", "Ừm"),
            ],
            dedup=False,
            drop_short=False,
        )

        self.assertEqual(len(cleaned), 3)
        self.assertLessEqual(cleaned[0].end + 0.05, cleaned[1].start)

    def test_readable_short_lines_extend_kept_tiny_voiced_cue(self):
        cleaned = readable_short_subtitle_lines(
            [
                LongDialogueLine(58.3, 58.45, "C", "Ừm"),
                LongDialogueLine(60.0, 62.0, "D", "Câu tiếp theo"),
            ],
            video_dur=70.0,
            dedup=False,
            drop_short=False,
        )

        self.assertEqual(len(cleaned), 2)
        self.assertAlmostEqual(cleaned[0].end, 59.05)

    def test_suppresses_repeated_long_line_at_chunk_tail(self):
        cleaned = suppress_tail_repeated_dialogue_lines(
            [
                LongDialogueLine(
                    58.6,
                    60.8,
                    "same long chinese sentence",
                    "Khong ngo ruou nay mang ve lai co cong dung nhu vay",
                ),
                LongDialogueLine(97.2, 97.8, "xian sheng", "Tien sinh"),
                LongDialogueLine(
                    98.8,
                    101.4,
                    "same long chinese sentence",
                    "Khong ngo ruou nay mang ve lai co cong dung nhu vay",
                ),
            ],
            chunk_duration=101.7,
        )

        self.assertEqual([line.start for line in cleaned], [58.6, 97.2])

    def test_readable_short_subtitle_lines_extends_tiny_cues(self):
        cleaned = readable_short_subtitle_lines(
            [
                LongDialogueLine(58.3, 58.72, "dui", "Dung vay"),
                LongDialogueLine(59.1, 61.7, "next", "Thanh Hoang da biet"),
            ],
            video_dur=70.0,
        )

        self.assertAlmostEqual(cleaned[0].end, 59.05)
        self.assertLessEqual(cleaned[0].end + 0.05, cleaned[1].start)

    def test_replace_tail_lines_keeps_earlier_context_and_refreshes_tail(self):
        refreshed = replace_tail_dialogue_lines(
            [
                LongDialogueLine(60.9, 67.2, "A", "Cần phải thay đổi"),
                LongDialogueLine(101.0, 107.3, "A", "Cần phải thay đổi"),
                LongDialogueLine(108.2, 110.0, "B", "Huyền Hoàng hiểu ý"),
            ],
            [
                LongDialogueLine(101.0, 101.7, "C", "Đi nghỉ đi"),
                LongDialogueLine(102.1, 105.4, "D", "Ta đã bảo đệ tử dọn phòng"),
                LongDialogueLine(105.9, 108.2, "E", "Không cần đâu"),
            ],
            tail_start=96.1,
        )

        self.assertEqual(
            [line.text_vi for line in refreshed],
            [
                "Cần phải thay đổi",
                "Đi nghỉ đi",
                "Ta đã bảo đệ tử dọn phòng",
                "Không cần đâu",
            ],
        )

    def test_configured_end_microfit_handles_small_long_overflow(self):
        with self.assertRaises(FFmpegError):
            voice_clip_fit_target(
                delay_ms=218_000,
                duration=2.693,
                final_dur=220.276,
            )

        self.assertAlmostEqual(
            voice_clip_fit_target(
                delay_ms=218_000,
                duration=2.693,
                final_dur=220.276,
                max_microfit_speed=1.2,
            ),
            2.276,
            places=3,
        )


class AdReviewEditingTest(unittest.TestCase):
    def test_to_seconds_parses_hms_and_numeric(self):
        self.assertEqual(to_seconds("01:29:52"), 5392.0)
        self.assertEqual(to_seconds("10:30"), 630.0)
        self.assertEqual(to_seconds("45"), 45.0)
        self.assertEqual(to_seconds(12.5), 12.5)
        self.assertAlmostEqual(to_seconds("00:00:05,500"), 5.5)

    def test_normalize_cut_spans_sorts_and_merges(self):
        self.assertEqual(
            normalize_cut_spans([(30, 40), (10, 20), (15, 25)]),
            [(10.0, 25.0), (30.0, 40.0)],
        )

    def test_normalize_cut_spans_clamps_to_duration(self):
        self.assertEqual(
            normalize_cut_spans([(-5, 10), (45, 999)], duration=50),
            [(0.0, 10.0), (45.0, 50.0)],
        )

    def test_keep_segments_is_complement(self):
        self.assertEqual(
            keep_segments([(10, 25), (30, 40)], 50),
            [(0.0, 10.0), (25.0, 30.0), (40.0, 50.0)],
        )

    def test_keep_segments_leading_cut(self):
        self.assertEqual(keep_segments([(0, 10)], 30), [(10.0, 30.0)])

    def test_shift_subtitle_drops_fully_covered_and_shifts_rest(self):
        shifted = shift_subtitle_lines(
            [(10, 15, "a"), (22, 28, "ad"), (40, 45, "b")],
            [(20, 30)],
        )
        self.assertEqual(shifted, [(10.0, 15.0, "a"), (30.0, 35.0, "b")])

    def test_shift_subtitle_clamps_line_straddling_cut_start(self):
        shifted = shift_subtitle_lines([(25, 35, "x")], [(20, 30)])
        self.assertEqual(shifted, [(20.0, 25.0, "x")])

    def test_shift_subtitle_shrinks_line_with_interior_cut(self):
        shifted = shift_subtitle_lines([(10, 60, "y")], [(20, 30)])
        self.assertEqual(shifted, [(10.0, 50.0, "y")])

    def test_shift_subtitle_handles_multiple_cuts(self):
        shifted = shift_subtitle_lines([(15, 45, "z")], [(20, 25), (35, 40)])
        self.assertEqual(shifted, [(15.0, 35.0, "z")])

    def test_shift_chapters_moves_and_drops_into_cut(self):
        shifted = shift_chapter_lines(
            ["00:00:00 A", "00:00:20 B", "00:00:50 C"],
            [(15, 30)],
        )
        self.assertEqual(
            shifted, ["00:00:00 A", "00:00:15 B", "00:00:35 C"]
        )

    def test_parse_cut_spans_accepts_hms_pairs(self):
        spans, error = parse_cut_spans([["01:29:52", "01:30:10"]])
        self.assertEqual(error, "")
        self.assertEqual(spans, [[5392.0, 5410.0]])

    def test_parse_cut_spans_rejects_empty(self):
        spans, error = parse_cut_spans([])
        self.assertEqual(spans, [])
        self.assertTrue(error)

    def test_parse_cut_spans_rejects_backwards_span(self):
        spans, error = parse_cut_spans([["00:10", "00:05"]])
        self.assertEqual(spans, [])
        self.assertIn("greater than start", error)

    def test_build_ad_detection_prompt_lists_timeline(self):
        prompt = build_ad_detection_prompt(
            [{"start": 5392, "end": 5410, "text_vi": "Doi dien thoai cu"}]
        )
        self.assertIn("01:29:52", prompt)
        self.assertIn("Doi dien thoai cu", prompt)
        self.assertIn("spans=[]", prompt)

    def test_normalize_ad_spans_clamps_and_sorts(self):
        spans = normalize_ad_spans([
            {"start": 30, "end": 20},  # dropped: end <= start
            {"start": 5, "end": 8, "confidence": 2.0, "reason_vi": "  x  "},
            {"start": -5, "end": 10},
        ])
        self.assertEqual(
            [(span["start"], span["end"]) for span in spans],
            [(0.0, 10.0), (5.0, 8.0)],
        )
        self.assertEqual(spans[1]["confidence"], 1.0)
        self.assertEqual(spans[1]["reason_vi"], "x")


if __name__ == "__main__":
    unittest.main()
