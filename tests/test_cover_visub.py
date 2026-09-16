from __future__ import annotations

import sys
import tempfile
import unittest
import importlib.util
import os
from dataclasses import replace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
DESUB_EXPERIMENT = ROOT / "experiments" / "desub"
sys.path.insert(0, str(DESUB_EXPERIMENT))

import cover_visub_job as cover  # noqa: E402
import prototype as desub  # noqa: E402


class AutoPipelineTests(unittest.TestCase):
    def test_douyin_url_input_builds_deterministic_staging_artifacts(self) -> None:
        raw = "http://v.douyin.com/omX_s0jMfi0/?tracking=ignored"
        normalized = cover.normalize_douyin_url(raw)
        self.assertEqual(normalized, "https://v.douyin.com/omX_s0jMfi0/")
        with mock.patch.dict(
            os.environ,
            {
                "COVER_SOURCE_URI": "",
                "COVER_DOUYIN_URL": raw,
                "COVER_RESULT_ROOT": "gs://media/desub/cover-visub",
                "COVER_PIPELINE_VERSION": "v9",
                "COVER_OUTPUT_URI": "",
                "COVER_MASK_URI": "",
                "COVER_CUES_URI": "",
            },
        ):
            resolved = cover.resolve_cover_input(None)
        source_id = cover.douyin_source_id(normalized)
        prefix = f"gs://media/desub/cover-visub/v9/{source_id}"
        self.assertEqual(resolved["input_kind"], "douyin_url")
        self.assertEqual(resolved["douyin_url"], normalized)
        self.assertEqual(resolved["source_uri"], f"{prefix}/source.mp4")
        self.assertEqual(resolved["artifacts"]["output_uri"], f"{prefix}/output.mp4")

    def test_cover_input_requires_exactly_one_source(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"COVER_SOURCE_URI": "", "COVER_DOUYIN_URL": ""},
        ):
            with self.assertRaisesRegex(ValueError, "exactly one"):
                cover.resolve_cover_input(None)
        with mock.patch.dict(
            os.environ,
            {
                "COVER_SOURCE_URI": "gs://media/source.mp4",
                "COVER_DOUYIN_URL": "https://v.douyin.com/abc/",
            },
        ):
            with self.assertRaisesRegex(ValueError, "exactly one"):
                cover.resolve_cover_input(None)

    def test_douyin_url_rejects_non_douyin_and_non_video_paths(self) -> None:
        for value in (
            "https://example.com/video/123/",
            "https://www.douyin.com/user/123/",
            "file:///tmp/source.mp4",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    cover.normalize_douyin_url(value)

    def test_auto_source_id_is_stable_per_gcs_generation(self) -> None:
        uri = "gs://media/incoming/My new video.mp4"
        first = cover.auto_source_id(uri, "123")
        self.assertEqual(first, cover.auto_source_id(uri, 123))
        self.assertRegex(first, r"^My-new-video-[0-9a-f]{12}$")
        self.assertNotEqual(first, cover.auto_source_id(uri, "124"))

    def test_auto_artifact_uris_build_deterministic_result_prefix(self) -> None:
        artifacts = cover.auto_artifact_uris(
            "gs://media/incoming/source.mp4",
            "456",
            result_root="gs://media/desub/cover-visub",
            pipeline_version="v2",
        )
        prefix = str(artifacts["result_prefix"])
        self.assertRegex(
            prefix,
            r"^gs://media/desub/cover-visub/v2/source-[0-9a-f]{12}$",
        )
        self.assertEqual(artifacts["output_uri"], f"{prefix}/output.mp4")
        self.assertEqual(artifacts["mask_uri"], f"{prefix}/mask.json")
        self.assertEqual(
            artifacts["cues_uri"],
            f"{prefix}/visub_cues_final.json",
        )
        self.assertEqual(artifacts["status_uri"], f"{prefix}/status.json")
        self.assertTrue(artifacts["auto_mask"])
        self.assertTrue(artifacts["auto_cues"])

    def test_auto_artifact_uris_preserves_explicit_lab_artifacts(self) -> None:
        artifacts = cover.auto_artifact_uris(
            "gs://media/incoming/source.mp4",
            "456",
            output_uri="gs://media/manual/run/output.mp4",
            mask_uri="gs://media/input/mask.json",
            cues_uri="gs://media/input/cues.json",
        )
        self.assertEqual(artifacts["result_prefix"], "gs://media/manual/run")
        self.assertEqual(
            artifacts["output_uri"],
            "gs://media/manual/run/output.mp4",
        )
        self.assertEqual(artifacts["mask_uri"], "gs://media/input/mask.json")
        self.assertEqual(artifacts["cues_uri"], "gs://media/input/cues.json")
        self.assertFalse(artifacts["auto_mask"])
        self.assertFalse(artifacts["auto_cues"])

    def test_clusters_without_cues_reports_only_missing_cluster(self) -> None:
        mask = {
            "subtitle_clusters": [
                {"source_index": 4, "t_start": 1.0, "t_end": 2.0},
                {"source_index": 9, "t_start": 4.0, "t_end": 5.0},
            ]
        }
        cues = [{"start": 1.1, "end": 1.9, "text_vi": "Mot"}]
        self.assertEqual(cover.clusters_without_cues(mask, cues), [9])

    def test_finalize_auto_cues_normalizes_text_and_line_count(self) -> None:
        finalized = cover.finalize_auto_cues([
            {
                "start": 0.0,
                "end": 1.0,
                "text_zh": "  你   好 ",
                "text_vi": "  Xin   chào  ",
            }
        ])
        self.assertEqual(finalized[0]["text_zh"], "你 好")
        self.assertEqual(finalized[0]["text_vi"], "Xin chào")
        self.assertEqual(finalized[0]["line_count"], 1)

    def test_touching_auto_duplicate_is_merged_once(self) -> None:
        meta = desub.VideoMeta(
            width=200, height=100, fps=25.0, frames=125, duration=5.0
        )
        clusters = [{
            "source_index": 7,
            "t_start": 1.0,
            "t_end": 4.0,
            "rects": [(30, 70, 170, 90)],
        }]
        cues = [
            {
                "start": 1.0,
                "end": 2.0,
                "text_zh": "same caption",
                "text_vi": "Cung mot cau.",
                "center_y": 0.8,
            },
            {
                "start": 2.0,
                "end": 3.0,
                "text_zh": "same caption",
                "text_vi": "Cung mot cau.",
                "center_y": 0.8,
            },
        ]
        merged, report = cover.merge_touching_repeated_auto_cues(
            cues, clusters, meta
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual((merged[0]["start"], merged[0]["end"]), (1.0, 3.0))
        self.assertEqual(merged[0]["auto_source_indexes"], [0, 1])
        self.assertEqual(len(report), 1)

    def test_gapped_duplicate_is_flagged_instead_of_extended_over_new_frames(self) -> None:
        meta = desub.VideoMeta(
            width=200, height=100, fps=25.0, frames=125, duration=5.0
        )
        clusters = [{
            "source_index": 3,
            "t_start": 1.0,
            "t_end": 4.0,
            "rects": [(30, 70, 170, 90)],
        }]
        cues = [
            {
                "start": 1.0,
                "end": 2.0,
                "text_zh": "stale caption",
                "text_vi": "Cau cu.",
                "center_y": 0.8,
            },
            {
                "start": 2.5,
                "end": 3.0,
                "text_zh": "stale caption",
                "text_vi": "Cau cu.",
                "center_y": 0.8,
            },
        ]
        merged, merge_report = cover.merge_touching_repeated_auto_cues(
            cues, clusters, meta
        )
        self.assertEqual(len(merged), 2)
        self.assertEqual(merge_report, [])
        findings = cover.adjacent_repeated_cue_report(merged, clusters, meta)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["gap_seconds"], 0.5)

    def test_same_text_in_different_clusters_is_not_reported_as_duplicate(self) -> None:
        meta = desub.VideoMeta(
            width=200, height=100, fps=25.0, frames=125, duration=5.0
        )
        clusters = [
            {
                "source_index": 1,
                "t_start": 0.5,
                "t_end": 1.5,
                "rects": [(30, 70, 170, 90)],
            },
            {
                "source_index": 2,
                "t_start": 2.0,
                "t_end": 3.0,
                "rects": [(30, 70, 170, 90)],
            },
        ]
        cues = [
            {
                "start": 0.5,
                "end": 1.5,
                "text_zh": "repeat later",
                "text_vi": "Lap lai sau.",
                "center_y": 0.8,
            },
            {
                "start": 2.0,
                "end": 3.0,
                "text_zh": "repeat later",
                "text_vi": "Lap lai sau.",
                "center_y": 0.8,
            },
        ]
        self.assertEqual(
            cover.adjacent_repeated_cue_report(cues, clusters, meta), []
        )

    def test_duplicate_normalization_handles_full_width_punctuation(self) -> None:
        meta = desub.VideoMeta(
            width=200, height=100, fps=25.0, frames=125, duration=5.0
        )
        clusters = [{
            "source_index": 8,
            "t_start": 1.0,
            "t_end": 4.0,
            "rects": [(30, 70, 170, 90)],
        }]
        cues = [
            {
                "start": 1.0,
                "end": 2.0,
                "text_zh": "ABC，123！",
                "text_vi": "Cung mot cau!",
                "center_y": 0.8,
            },
            {
                "start": 2.0,
                "end": 3.0,
                "text_zh": "abc,123.",
                "text_vi": "Cung mot cau.",
                "center_y": 0.8,
            },
        ]
        merged, report = cover.merge_touching_repeated_auto_cues(
            cues, clusters, meta
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(report), 1)

    def test_duplicate_normalization_preserves_internal_decimal_separator(self) -> None:
        self.assertNotEqual(
            cover._normalized_cue_text("1.5G"),
            cover._normalized_cue_text("15G"),
        )

    def test_retry_splice_rejects_loss_of_a_nonduplicate_caption(self) -> None:
        meta = desub.VideoMeta(
            width=200, height=100, fps=25.0, frames=150, duration=6.0
        )
        clusters = [{
            "source_index": 19,
            "t_start": 1.0,
            "t_end": 4.2,
            "rects": [(30, 70, 170, 90)],
        }]
        cues = [
            {"start": 1.0, "end": 2.0, "text_zh": "A", "text_vi": "Mot", "center_y": 0.8},
            {"start": 2.5, "end": 3.0, "text_zh": "A", "text_vi": "Mot", "center_y": 0.8},
            {"start": 3.0, "end": 4.0, "text_zh": "B", "text_vi": "Hai", "center_y": 0.8},
        ]
        findings = cover.adjacent_repeated_cue_report(cues, clusters, meta)
        replacements = [
            {"start": 1.0, "end": 2.4, "text_zh": "A", "text_vi": "Mot", "center_y": 0.8},
        ]
        with self.assertRaisesRegex(ValueError, "would alter subtitle meaning sequence"):
            cover.splice_repeated_cue_retry(
                cues, replacements, findings, clusters, meta
            )

    def test_retry_splice_replaces_only_suspect_window_and_keeps_other_cues(self) -> None:
        meta = desub.VideoMeta(
            width=200, height=100, fps=25.0, frames=200, duration=8.0
        )
        clusters = [
            {
                "source_index": 19,
                "t_start": 1.0,
                "t_end": 4.2,
                "rects": [(30, 70, 170, 90)],
            },
            {
                "source_index": 20,
                "t_start": 5.0,
                "t_end": 6.0,
                "rects": [(30, 70, 170, 90)],
            },
        ]
        cues = [
            {"start": 1.0, "end": 2.0, "text_zh": "A", "text_vi": "Mot", "center_y": 0.8},
            {"start": 2.5, "end": 3.0, "text_zh": "A", "text_vi": "Mot", "center_y": 0.8},
            {"start": 3.0, "end": 4.0, "text_zh": "B", "text_vi": "Hai", "center_y": 0.8},
            {"start": 5.0, "end": 6.0, "text_zh": "C", "text_vi": "Ba", "center_y": 0.8},
        ]
        findings = cover.adjacent_repeated_cue_report(cues, clusters, meta)
        replacements = [
            {"start": 1.0, "end": 2.4, "text_zh": "A", "text_vi": "Mot", "center_y": 0.8},
            {"start": 2.4, "end": 4.0, "text_zh": "B", "text_vi": "Hai", "center_y": 0.8},
        ]
        spliced, report = cover.splice_repeated_cue_retry(
            cues, replacements, findings, clusters, meta
        )
        self.assertEqual([cue["text_zh"] for cue in spliced], ["A", "B", "C"])
        self.assertEqual(report[0]["original_unique_caption_count"], 2)
        self.assertEqual(report[0]["replacement_unique_caption_count"], 2)

    def test_retry_splice_rejects_extra_reordered_or_nonadjacent_repeat(self) -> None:
        meta = desub.VideoMeta(
            width=200, height=100, fps=25.0, frames=150, duration=6.0
        )
        clusters = [{
            "source_index": 19,
            "t_start": 1.0,
            "t_end": 4.2,
            "rects": [(30, 70, 170, 90)],
        }]
        cues = [
            {"start": 1.0, "end": 2.0, "text_zh": "A", "text_vi": "Mot", "center_y": 0.8},
            {"start": 2.5, "end": 3.0, "text_zh": "A", "text_vi": "Mot", "center_y": 0.8},
            {"start": 3.0, "end": 4.0, "text_zh": "B", "text_vi": "Hai", "center_y": 0.8},
        ]
        findings = cover.adjacent_repeated_cue_report(cues, clusters, meta)
        invalid_sequences = {
            "extra": [("A", 1.0, 2.0), ("B", 2.0, 3.0), ("C", 3.0, 4.0)],
            "reordered": [("B", 1.0, 2.5), ("A", 2.5, 4.0)],
            "nonadjacent_repeat": [
                ("A", 1.0, 2.0),
                ("B", 2.0, 3.0),
                ("A", 3.0, 4.0),
            ],
        }
        for label, sequence in invalid_sequences.items():
            with self.subTest(label=label):
                replacements = [
                    {
                        "start": start,
                        "end": end,
                        "text_zh": text,
                        "text_vi": text,
                        "center_y": 0.8,
                    }
                    for text, start, end in sequence
                ]
                with self.assertRaisesRegex(
                    ValueError, "would alter subtitle meaning sequence"
                ):
                    cover.splice_repeated_cue_retry(
                        cues, replacements, findings, clusters, meta
                    )

    def test_preview_duration_drops_later_clusters_and_clips_boundary(self) -> None:
        mask = {
            "subtitle_clusters": [
                {
                    "source_index": 1,
                    "t_start": 39.5,
                    "t_end": 42.0,
                    "context_start": 39.0,
                    "context_end": 42.5,
                },
                {
                    "source_index": 2,
                    "t_start": 45.9,
                    "t_end": 48.0,
                    "context_start": 45.4,
                    "context_end": 48.5,
                },
            ]
        }
        clipped = cover.mask_payload_for_duration(mask, 41.0)
        self.assertEqual(len(clipped["subtitle_clusters"]), 1)
        self.assertEqual(clipped["subtitle_clusters"][0]["t_end"], 41.0)
        self.assertEqual(clipped["subtitle_clusters"][0]["context_end"], 41.0)
        self.assertEqual(mask["subtitle_clusters"][0]["t_end"], 42.0)

    def test_partial_cpu_detector_timeline_is_rejected(self) -> None:
        summary = {
            "easyocr_craft": {
                "ok": True,
                "timed_out": True,
                "seconds": 1800.5,
            },
            "paddle_det": {"ok": True, "skipped": True},
        }
        with self.assertRaisesRegex(TimeoutError, "partial masks are forbidden"):
            cover.assert_full_cpu_detection(summary)

    def test_complete_cpu_detector_timeline_is_accepted(self) -> None:
        cover.assert_full_cpu_detection({
            "easyocr_craft": {
                "ok": True,
                "timed_out": False,
                "seconds": 2500.0,
            }
        })

    def test_span_height_limit_scales_for_1280px_video(self) -> None:
        self.assertEqual(cover.default_span_height_limit(1024), 120)
        self.assertEqual(cover.default_span_height_limit(1280), 150)

    def test_detection_timeline_coverage_rejects_unclustered_tail(self) -> None:
        meta = desub.VideoMeta(
            width=200,
            height=100,
            fps=25.0,
            frames=100,
            duration=4.0,
        )
        mask = {
            "detect_fps": 8.0,
            "detections": [
                {"t": 1.5, "x1": 60, "y1": 70, "x2": 120, "y2": 82},
                {"t": 2.5, "x1": 60, "y1": 70, "x2": 120, "y2": 82},
            ],
        }
        event = cover.CoverEvent(0, 1.0, 2.0, (50, 65, 130, 90), 0, 1.0, 2.0)
        report = cover.detection_timeline_coverage_report(mask, [event], meta)
        self.assertFalse(report["ok"])
        self.assertEqual(report["checked_detection_count"], 2)
        self.assertEqual(report["uncovered_detection_count"], 1)
        self.assertEqual(report["uncovered_intervals"][0]["start"], 2.5)

        repaired = replace(event, end=3.0)
        repaired_report = cover.detection_timeline_coverage_report(
            mask,
            [repaired],
            meta,
        )
        self.assertTrue(repaired_report["ok"])


class CoverGeometryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.meta = desub.VideoMeta(width=200, height=100, fps=25.0, frames=250, duration=10.0)

    def test_union_dilate_and_expand_for_vietnamese_text(self) -> None:
        rects = [(50, 50, 80, 60), (75, 48, 100, 62)]
        without_text, lines = cover.cover_rect_for_cluster(rects, "", self.meta, 10)
        self.assertEqual(without_text, (40, 38, 110, 72))
        self.assertEqual(lines, [])

        with mock.patch.dict("os.environ", {"DESUB_VISUB_WRAP_CHARS": "12"}, clear=False):
            expanded, wrapped = cover.cover_rect_for_cluster(
                rects,
                "Dong phu de Viet dai hon rat nhieu",
                self.meta,
                10,
            )
            text_width, text_height, _ = cover.estimate_text_size(
                "Dong phu de Viet dai hon rat nhieu",
                self.meta,
            )
        self.assertEqual(len(wrapped), 2)
        self.assertLessEqual(expanded[0], without_text[0])
        self.assertLessEqual(expanded[1], without_text[1])
        self.assertGreaterEqual(expanded[2], without_text[2])
        self.assertGreaterEqual(expanded[3], without_text[3])
        self.assertGreaterEqual(expanded[2] - expanded[0], min(self.meta.width, text_width))
        self.assertGreaterEqual(expanded[3] - expanded[1], min(self.meta.height, text_height))

    def test_cluster_union_rect_is_fixed_and_text_timing_tiles_cluster(self) -> None:
        cues = [
            {
                "start": 1.2,
                "end": 2.0,
                "text_vi": "Cau mot",
                "text_zh": "one",
                "center_x": 0.35,
                "center_y": 0.55,
                "line_count": 1,
                "source_cue_index": 10,
            },
            {
                "start": 3.0,
                "end": 4.0,
                "text_vi": "Cau hai dai hon",
                "text_zh": "two",
                "center_x": 0.65,
                "center_y": 0.55,
                "line_count": 1,
                "source_cue_index": 11,
            },
        ]
        mask = {
            "subtitle_clusters": [
                {"t_start": 1.0, "t_end": 5.0, "rects": [[50, 45, 90, 60], [85, 43, 120, 62]]}
            ]
        }
        events, matches, orphans = cover.build_cover_events(
            cues,
            mask,
            self.meta,
            rect_mode="cluster_union",
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(orphans, [])
        event = events[0]
        self.assertEqual((event.start, event.end), (0.88, 5.12))
        self.assertEqual(len(event.text_events), 2)
        self.assertEqual((event.text_events[0].start, event.text_events[0].end), (0.88, 3.0))
        self.assertEqual((event.text_events[1].start, event.text_events[1].end), (3.0, 5.12))
        self.assertEqual(event.text_events[0].end, event.text_events[1].start)
        self.assertEqual({item["kind"] for item in event.filled_intervals}, {"head", "gap", "tail"})
        self.assertEqual(matches[0]["rect"], list(event.rect))
        self.assertLessEqual(event.rect[0], 40)
        self.assertGreaterEqual(event.rect[2], 130)

    def test_fill_blur_only_keeps_text_gap_but_cover_stays_full(self) -> None:
        cues = [
            {"start": 1.2, "end": 2.0, "text_vi": "Mot", "center_y": 0.5, "line_count": 1},
            {"start": 3.0, "end": 4.0, "text_vi": "Hai", "center_y": 0.5, "line_count": 1},
        ]
        mask = {
            "subtitle_clusters": [
                {"t_start": 1.0, "t_end": 5.0, "rects": [[50, 45, 150, 65]]}
            ]
        }
        events, _, _ = cover.build_cover_events(
            cues,
            mask,
            self.meta,
            rect_mode="cluster_union",
            fill_mode="blur_only",
        )
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].start, events[0].end), (0.88, 5.12))
        self.assertEqual(
            [(item.start, item.end) for item in events[0].text_events],
            [(1.2, 2.0), (3.0, 4.0)],
        )
        self.assertEqual(events[0].filled_intervals, ())

    def test_pad_is_clamped_to_midpoint_of_adjacent_cluster_gap(self) -> None:
        cues = [
            {"start": 1.1, "end": 1.9, "text_vi": "Mot", "center_y": 0.5, "line_count": 1},
            {"start": 2.2, "end": 2.9, "text_vi": "Hai", "center_y": 0.5, "line_count": 1},
        ]
        mask = {
            "subtitle_clusters": [
                {"t_start": 1.0, "t_end": 2.0, "rects": [[30, 45, 90, 65]]},
                {"t_start": 2.1, "t_end": 3.0, "rects": [[110, 45, 170, 65]]},
            ]
        }
        events, _, _ = cover.build_cover_events(cues, mask, self.meta, pad_seconds=0.12)
        by_cluster = {event.source_cluster_index: event for event in events}
        self.assertAlmostEqual(by_cluster[0].end, 2.05)
        self.assertAlmostEqual(by_cluster[1].start, 2.05)
        self.assertLessEqual(by_cluster[0].end, by_cluster[1].start)

    def test_unmatched_cluster_is_a_blank_cover_event_by_default(self) -> None:
        cues = [{"start": 1.0, "end": 2.0, "text_vi": "Mot", "center_y": 0.5, "line_count": 1}]
        mask = {
            "subtitle_clusters": [
                {"t_start": 1.0, "t_end": 2.0, "rects": [[50, 45, 100, 60]]},
                {"t_start": 7.0, "t_end": 8.0, "rects": [[60, 50, 120, 65]]},
            ]
        }
        events, _, orphans = cover.build_cover_events(cues, mask, self.meta)
        self.assertEqual(len(orphans), 1)
        orphan_event = next(event for event in events if event.unmatched)
        self.assertEqual((orphan_event.start, orphan_event.end), (6.88, 8.12))
        self.assertEqual(orphan_event.text_events, ())

    def test_coverage_report_identifies_exact_uncovered_interval(self) -> None:
        clusters = [
            {"source_index": 7, "t_start": 1.0, "t_end": 3.0, "rects": ((1, 1, 2, 2),)}
        ]
        event = cover.CoverEvent(0, 1.0, 2.5, (1, 1, 2, 2), 7, 1.0, 3.0)
        report = cover.cluster_coverage_report(clusters, [event])
        self.assertFalse(report["ok"])
        self.assertEqual(report["uncovered_clusters"][0]["uncovered_intervals"], [[2.5, 3.0]])

    def test_per_event_rect_unions_active_detection_and_text_block(self) -> None:
        cues = [
            {
                "start": 1.1,
                "end": 1.9,
                "text_vi": "OK",
                "center_x": 0.5,
                "center_y": 0.78,
                "line_count": 1,
            }
        ]
        detection = {"t": 1.5, "x1": 70, "y1": 70, "x2": 90, "y2": 82}
        mask = {
            "band_top_ratio": 0.66,
            "detections": [detection],
            "subtitle_clusters": [
                {"t_start": 1.0, "t_end": 2.0, "rects": [[30, 65, 170, 92]]}
            ],
        }
        events, _, _ = cover.build_cover_events(
            cues,
            mask,
            self.meta,
            dilate_pixels=10,
            dilate_x_pixels=10,
            dilate_y_pixels=4,
            text_padding_pixels=12,
            text_padding_x_pixels=12,
            text_padding_y_pixels=4,
        )
        self.assertEqual(len(events), 1)
        event = events[0]
        text_rect = cover.text_block_rect_for_event(event.text_events[0], self.meta, 12, 4)
        expected = cover.union_rects(
            [
                cover.dilate_rect_xy(
                    (70, 70, 90, 82),
                    10,
                    4,
                    self.meta.width,
                    self.meta.height,
                ),
                text_rect,
            ]
        )
        self.assertEqual(event.rect, expected)
        self.assertEqual(event.zh_rect, (70, 70, 90, 82))
        self.assertEqual(event.text_block_rect, text_rect)
        self.assertEqual(event.active_detection_count, 1)
        self.assertFalse(event.detection_fallback)
        self.assertGreaterEqual(
            event.cluster_union_rect[2] - event.cluster_union_rect[0],
            event.rect[2] - event.rect[0],
        )

        geometry = cover.geometry_coverage_report(
            mask,
            events,
            self.meta,
            dilate_pixels=10,
            dilate_x_pixels=10,
            dilate_y_pixels=4,
        )
        self.assertTrue(geometry["ok"])
        bad_event = replace(event, rect=(80, 50, 100, 70))
        bad_geometry = cover.geometry_coverage_report(
            mask,
            [bad_event],
            self.meta,
            dilate_pixels=10,
            dilate_x_pixels=10,
            dilate_y_pixels=4,
        )
        self.assertFalse(bad_geometry["ok"])
        self.assertGreaterEqual(bad_geometry["failure_count"], 1)
        self.assertIn(
            "event_rect_misses_active_lane_detection",
            {item["kind"] for item in bad_geometry["failures"]},
        )

    def test_one_line_rect_has_no_double_vertical_padding_at_51px_font(self) -> None:
        meta = desub.VideoMeta(
            width=720,
            height=1280,
            fps=30.0,
            frames=300,
            duration=10.0,
        )
        text_event = cover.TextEvent(
            cue_index=0,
            start=0.0,
            end=1.0,
            original_start=0.1,
            original_end=0.9,
            text_vi="Đúng vậy",
            text_zh="对",
            line_count=1,
            text_x=360,
            text_y=940,
        )
        geometry = cover.per_event_cover_rect(
            {"source_index": 0, "rects": ((280, 910, 440, 971),)},
            [{"source_detection_index": 0, "t": 0.5, "rect": (280, 910, 440, 971)}],
            text_event,
            0.0,
            1.0,
            meta,
            dilate_pixels=10,
            dilate_x_pixels=10,
            dilate_y_pixels=4,
            text_padding_pixels=12,
            text_padding_x_pixels=12,
            text_padding_y_pixels=4,
            band_top_ratio=0.66,
        )
        expected = cover.union_rects([
            cover.dilate_rect_xy((280, 910, 440, 971), 10, 4, 720, 1280),
            cover.text_block_rect_for_event(text_event, meta, 12, 4),
        ])
        self.assertEqual(geometry.rect, expected)
        self.assertLess(geometry.rect[3] - geometry.rect[1], 85)

    def test_text_position_disconnected_from_cluster_is_anchored_to_sub_band(self) -> None:
        cues = [{
            "start": 1.1,
            "end": 1.9,
            "text_vi": "Khong duoc",
            "center_x": 0.5,
            "center_y": 0.20,
            "line_count": 1,
            "source_cue_index": 9,
        }]
        mask = {
            "band_top_ratio": 0.66,
            "subtitle_clusters": [
                {"t_start": 1.0, "t_end": 2.0, "rects": [[60, 72, 140, 92]]}
            ],
        }
        events, _, _ = cover.build_cover_events(
            cues,
            mask,
            self.meta,
            dilate_x_pixels=10,
            dilate_y_pixels=4,
            text_padding_x_pixels=12,
            text_padding_y_pixels=4,
        )
        event = events[0]
        self.assertEqual(event.text_events[0].text_y, 82)
        self.assertGreaterEqual(event.text_block_rect[1], 60)
        warning = next(
            item
            for item in event.geometry_warnings
            if item["kind"] == "text_position_anchored_to_selected_subtitle_track"
        )
        self.assertEqual(warning["original_text_y"], 20)
        self.assertEqual(warning["anchored_text_y"], 82)

    def test_detection_outside_sub_band_is_rejected_and_falls_back(self) -> None:
        cluster = {"source_index": 0, "rects": ((50, 70, 100, 90),)}
        detections = [
            {"source_detection_index": 7, "t": 1.5, "rect": (60, 30, 80, 45)}
        ]
        with mock.patch.object(
            desub,
            "box_center_in_sub_band",
            wraps=desub.box_center_in_sub_band,
        ) as band_helper:
            geometry = cover.per_event_cover_rect(
                cluster,
                detections,
                None,
                1.0,
                2.0,
                self.meta,
                dilate_pixels=10,
                band_top_ratio=0.66,
            )
        band_helper.assert_called_once_with((60, 30, 80, 45), self.meta)
        self.assertTrue(geometry.fallback)
        self.assertEqual(geometry.zh_rect, (50, 70, 100, 90))
        self.assertEqual(geometry.accepted_detection_count, 0)
        self.assertEqual(geometry.rejected_detections[0]["reasons"], ["center_out_sub_band"])

    def test_detection_without_cluster_intersection_is_rejected(self) -> None:
        cluster = {"source_index": 0, "rects": ((50, 70, 100, 90),)}
        detections = [
            {"source_detection_index": 8, "t": 1.5, "rect": (130, 70, 160, 85)}
        ]
        geometry = cover.per_event_cover_rect(
            cluster,
            detections,
            None,
            1.0,
            2.0,
            self.meta,
            dilate_pixels=10,
            band_top_ratio=0.66,
        )
        self.assertTrue(geometry.fallback)
        self.assertEqual(geometry.zh_rect, (50, 70, 100, 90))
        self.assertIn("no_cluster_intersection", geometry.rejected_detections[0]["reasons"])

    def test_cluster_reference_ignores_disconnected_product_text_above_subtitle(self) -> None:
        cluster = {
            "source_index": 0,
            "rects": (
                (55, 45, 95, 58),
                (45, 72, 115, 92),
            ),
        }
        detections = [
            {"source_detection_index": 10, "t": 1.5, "rect": (55, 45, 95, 58)},
            {"source_detection_index": 11, "t": 1.5, "rect": (55, 74, 105, 90)},
        ]
        geometry = cover.per_event_cover_rect(
            cluster,
            detections,
            None,
            1.0,
            2.0,
            self.meta,
            dilate_pixels=2,
            band_top_ratio=0.40,
            cluster_intersection_margin_pixels=4,
        )
        self.assertEqual(geometry.cluster_reference_rect, (45, 72, 115, 92))
        self.assertEqual(geometry.zh_rect, (55, 74, 105, 90))
        self.assertEqual(geometry.accepted_detection_count, 1)
        self.assertEqual(len(geometry.rejected_detections), 1)
        self.assertIn("no_cluster_intersection", geometry.rejected_detections[0]["reasons"])

    def test_zh_rect_is_clamped_to_cluster_union_plus_40px(self) -> None:
        cluster = {"source_index": 0, "rects": ((50, 70, 100, 90),)}
        detections = [
            {"source_detection_index": 9, "t": 1.5, "rect": (120, 70, 180, 85)}
        ]
        geometry = cover.per_event_cover_rect(
            cluster,
            detections,
            None,
            1.0,
            2.0,
            self.meta,
            dilate_pixels=10,
            band_top_ratio=0.66,
        )
        self.assertFalse(geometry.fallback)
        self.assertEqual(geometry.zh_rect, (120, 70, 140, 85))
        self.assertEqual(geometry.warnings[0]["kind"], "zh_rect_clamped_to_cluster_margin")

    def test_dominant_lane_rejects_product_ocr_below_and_off_axis(self) -> None:
        meta = desub.VideoMeta(
            width=720,
            height=1280,
            fps=30.0,
            frames=300,
            duration=10.0,
        )
        detections = [
            {
                "source_detection_index": index,
                "t": 1.0 + index * 0.1,
                "rect": (300, 910, 420, 970),
                "text": "够了",
            }
            for index in range(20)
        ]
        detections.extend([
            {
                "source_detection_index": 20,
                "t": 1.5,
                "rect": (315, 1010, 405, 1088),
                "text": "K系列",
            },
            {
                "source_detection_index": 21,
                "t": 1.6,
                "rect": (90, 912, 190, 970),
                "text": "RoHS CE",
            },
        ])
        lane = cover.calibrate_subtitle_lane(detections, meta)
        self.assertEqual(cover.detection_lane_rejection_reasons(detections[0], lane), [])
        self.assertIn(
            "outside_dominant_subtitle_lane_y",
            cover.detection_lane_rejection_reasons(detections[20], lane),
        )
        self.assertIn(
            "outside_dominant_subtitle_lane_x",
            cover.detection_lane_rejection_reasons(detections[21], lane),
        )

    def test_effective_cluster_geometry_uses_inliers_but_preserves_mask_timing(self) -> None:
        meta = desub.VideoMeta(
            width=720,
            height=1280,
            fps=30.0,
            frames=300,
            duration=10.0,
        )
        mask = {
            "subtitle_clusters": [{
                "t_start": 1.0,
                "t_end": 5.0,
                "rects": [[300, 910, 420, 970]],
            }],
            "detections": [
                {"t": 1.5, "x1": 300, "y1": 910, "x2": 420, "y2": 970, "text": "A"},
                {"t": 4.5, "x1": 300, "y1": 910, "x2": 420, "y2": 970, "text": "A"},
            ],
        }
        clusters, _, _, report = cover.effective_subtitle_clusters(mask, meta)
        self.assertEqual((clusters[0]["t_start"], clusters[0]["t_end"]), (1.0, 5.0))
        self.assertAlmostEqual(
            report["total_trimmed_head_seconds"]
            + report["total_trimmed_tail_seconds"],
            0.0,
        )
        self.assertEqual(
            report["clusters"][0]["mask_context_after_last_detection_seconds"],
            0.5,
        )

    def test_mask_tail_is_covered_even_after_last_ocr_detection(self) -> None:
        meta = desub.VideoMeta(
            width=720,
            height=1280,
            fps=30.0,
            frames=900,
            duration=30.0,
        )
        mask = {
            "subtitle_clusters": [{
                "t_start": 22.3,
                "t_end": 27.033,
                "rects": [[300, 910, 420, 970]],
            }],
            "detections": [
                {"t": 22.8, "x1": 300, "y1": 910, "x2": 420, "y2": 970, "text": "H61"},
                {"t": 26.533, "x1": 300, "y1": 910, "x2": 420, "y2": 970, "text": "H61"},
            ],
        }
        cues = [{
            "start": 22.8,
            "end": 26.2,
            "text_vi": "Lấy một cái H61 đi.",
            "text_zh": "H61",
            "center_x": 0.5,
            "center_y": 0.735,
        }]
        events, _, _ = cover.build_cover_events(cues, mask, meta, pad_seconds=0.12)
        self.assertLessEqual(events[0].start, 22.18)
        self.assertGreaterEqual(events[-1].end, 27.153)
        self.assertTrue(cover.cluster_coverage_report(
            cover.effective_subtitle_clusters(mask, meta)[0], events
        )["ok"])

    def test_no_ocr_match_selects_lane_track_not_parallel_product_track(self) -> None:
        lane = cover.SubtitleLane(360, 940, 60, 90, 45, 42, 75, 8)
        detections = []
        for index, seconds in enumerate((1.1, 1.2, 1.3)):
            detections.extend([
                {
                    "source_detection_index": index * 2,
                    "t": seconds,
                    "rect": (300, 910, 420, 970),
                    "text": "ocr-noise",
                },
                {
                    "source_detection_index": index * 2 + 1,
                    "t": seconds,
                    "rect": (390, 910, 450, 970),
                    "text": "PRODUCT",
                },
            ])
        event = cover.TextEvent(
            0, 1.0, 1.5, 1.0, 1.5, "Phụ đề", "不匹配", 1, 360, 940, 51
        )
        selected, report = cover.select_event_detection_track(
            detections,
            event,
            lane,
            selection_guard_seconds=0.2,
            similarity_threshold=0.42,
        )
        self.assertEqual(report["selection"], "lane_fallback")
        self.assertEqual(
            {item["source_detection_index"] for item in selected},
            {0, 2, 4},
        )

    def test_ocr_track_selects_matching_caption_and_anchors_text(self) -> None:
        meta = desub.VideoMeta(
            width=720,
            height=1280,
            fps=30.0,
            frames=300,
            duration=10.0,
        )
        lane = cover.SubtitleLane(360, 940, 60, 90, 45, 42, 75, 20)
        cluster = {
            "source_index": 0,
            "t_start": 1.0,
            "t_end": 3.0,
            "rects": ((300, 910, 420, 970),),
        }
        detections = [
            {
                "source_detection_index": 0,
                "t": 1.9,
                "rect": (260, 910, 460, 970),
                "text": "很多",
            },
            {
                "source_detection_index": 1,
                "t": 2.2,
                "rect": (318, 912, 402, 968),
                "text": "够了",
            },
        ]
        text = cover.TextEvent(
            cue_index=7,
            start=2.0,
            end=3.0,
            original_start=2.0,
            original_end=2.8,
            text_vi="Đủ rồi.",
            text_zh="够了",
            line_count=1,
            text_x=500,
            text_y=1020,
            font_size=51,
        )
        geometry = cover.per_event_cover_rect(
            cluster,
            detections,
            text,
            2.0,
            3.0,
            meta,
            dilate_pixels=10,
            dilate_x_pixels=10,
            dilate_y_pixels=4,
            text_padding_x_pixels=12,
            text_padding_y_pixels=4,
            subtitle_lane=lane,
        )
        self.assertEqual(geometry.zh_rect, (318, 912, 402, 968))
        self.assertEqual((geometry.text_event.text_x, geometry.text_event.text_y), (360, 940))
        self.assertEqual(geometry.accepted_detection_count, 1)
        self.assertIn(
            "ocr_track_mismatch",
            next(
                item for item in geometry.rejected_detections
                if item["source_detection_index"] == 0
            )["reasons"],
        )

    def test_text_boundary_snaps_to_midpoint_between_ocr_tracks(self) -> None:
        lane = cover.SubtitleLane(100, 80, 20, 50, 20, 10, 30, 4)
        cluster = {"source_index": 0, "t_start": 1.0, "t_end": 4.0}
        events = [
            cover.TextEvent(0, 1.0, 3.0, 1.0, 2.4, "Mot", "甲", 1, 100, 80),
            cover.TextEvent(1, 3.0, 4.0, 3.0, 4.0, "Hai", "乙", 1, 100, 80),
        ]
        detections = [
            {"source_detection_index": 0, "t": 2.9, "rect": (80, 70, 120, 90), "text": "甲"},
            {"source_detection_index": 1, "t": 3.1, "rect": (80, 70, 120, 90), "text": "乙"},
        ]
        adjusted, warnings = cover.snap_text_event_boundaries_to_detections(
            events,
            cluster,
            detections,
            lane,
        )
        self.assertEqual((adjusted[0].end, adjusted[1].start), (3.0, 3.0))
        self.assertEqual(len(warnings), 1)

    def test_text_boundary_keeps_reviewed_time_when_ocr_support_gap_is_large(self) -> None:
        lane = cover.SubtitleLane(100, 80, 20, 50, 20, 10, 30, 4)
        cluster = {"source_index": 0, "t_start": 1.0, "t_end": 4.0}
        events = [
            cover.TextEvent(0, 1.0, 3.0, 1.0, 2.4, "Mot", "甲", 1, 100, 80),
            cover.TextEvent(1, 3.0, 4.0, 3.0, 4.0, "Hai", "乙", 1, 100, 80),
        ]
        detections = [
            {"source_detection_index": 0, "t": 2.5, "rect": (80, 70, 120, 90), "text": "甲"},
            {"source_detection_index": 1, "t": 3.5, "rect": (80, 70, 120, 90), "text": "乙"},
        ]
        adjusted, warnings = cover.snap_text_event_boundaries_to_detections(
            events,
            cluster,
            detections,
            lane,
        )
        self.assertEqual((adjusted[0].end, adjusted[1].start), (3.0, 3.0))
        self.assertEqual(warnings, [])

    def test_dynamic_font_fit_keeps_long_caption_inside_safe_width(self) -> None:
        meta = desub.VideoMeta(
            width=720,
            height=1280,
            fps=30.0,
            frames=300,
            duration=10.0,
        )
        text = "Câu phụ đề Việt Nam này rất dài và cần tự co chữ để không sát mép"
        font_size = cover.fitted_subtitle_font_size(text, meta, padding_x_pixels=12)
        width, _, _ = cover.estimate_text_block_size(
            text,
            meta,
            12,
            4,
            font_size=font_size,
        )
        self.assertLessEqual(width, round(meta.width * 0.88))
        self.assertLess(font_size, cover.subtitle_font_size(meta))

    def test_layout_fails_closed_when_subtitle_lane_is_not_dominant(self) -> None:
        lane = cover.SubtitleLane(
            360, 940, 60, 90, 45, 42, 75, 100,
            support_detection_count=40,
            support_ratio=0.40,
        )
        meta = desub.VideoMeta(720, 1280, 30.0, 300, 10.0)
        report = cover.layout_quality_report([], lane, meta)
        self.assertFalse(report["ok"])
        self.assertEqual(
            report["failures"][0]["kind"],
            "dominant_subtitle_lane_support_too_low",
        )

    def test_corner_radius_is_clamped_to_half_short_edge(self) -> None:
        self.assertEqual(cover.clamp_corner_radius(100, 30, 20), 15)
        self.assertEqual(cover.clamp_corner_radius(100, 30, -5), 0)

    def test_post_cover_qa_uses_end_of_cluster_not_an_intermediate_event(self) -> None:
        events = [
            cover.CoverEvent(0, 180.0, 183.0, (1, 1, 2, 2), 29, 180.0, 183.0),
            cover.CoverEvent(1, 184.0, 190.2, (1, 1, 2, 2), 30, 184.0, 191.0),
            cover.CoverEvent(2, 190.2, 191.0, (1, 1, 2, 2), 30, 184.0, 191.0),
        ]
        self.assertEqual(cover.last_testable_cluster_post_time(events, 191.0), (29, 183.2))

    def test_qa_frame_time_stays_before_last_decodable_frame(self) -> None:
        meta = desub.VideoMeta(720, 1280, 30.0, 5729, 190.966667)
        self.assertAlmostEqual(
            cover.safe_qa_frame_time(190.937, meta),
            (5728 / 30.0) - 0.001,
            places=6,
        )
        self.assertEqual(cover.safe_qa_frame_time(-1.0, meta), 0.0)

    @unittest.skipUnless(
        importlib.util.find_spec("numpy") and importlib.util.find_spec("cv2"),
        "numpy/cv2 are provided by the render image, not the local host",
    )
    def test_rounded_mask_has_transparent_corner_and_opaque_center(self) -> None:
        import cv2

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "rounded.png"
            radius = cover.write_rounded_mask(path, 100, 60, 20, 0)
            mask = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        self.assertEqual(radius, 20)
        self.assertEqual(int(mask[0, 0]), 0)
        self.assertEqual(int(mask[30, 50]), 255)


class TtsTimingTests(unittest.TestCase):
    @staticmethod
    def text_event(
        cue_index: int,
        tiled_start: float,
        original_start: float,
        original_end: float,
    ) -> cover.TextEvent:
        return cover.TextEvent(
            cue_index=cue_index,
            start=tiled_start,
            end=original_end,
            original_start=original_start,
            original_end=original_end,
            text_vi=f"Câu {cue_index}",
            text_zh="",
            line_count=1,
            text_x=100,
            text_y=80,
        )

    def test_slot_fit_and_delay_use_original_cue_start_without_overlap(self) -> None:
        plans = cover.build_tts_cue_plans(
            [
                self.text_event(0, 0.80, 1.00, 1.60),
                self.text_event(1, 2.60, 3.00, 3.70),
            ],
            5.0,
        )
        self.assertEqual([plan.delay_ms for plan in plans], [1000, 3000])
        self.assertEqual([plan.slot_seconds for plan in plans], [2.0, 2.0])
        self.assertEqual([plan.fit_target_seconds for plan in plans], [1.95, 1.95])
        cover.validate_tts_fits(plans, [1.90, 1.95])

    def test_uniform_audio_speed_uses_valid_atempo_chain(self) -> None:
        self.assertEqual(cover.atempo_filter_chain(1.5), "atempo=1.500000")
        self.assertEqual(
            cover.atempo_filter_chain(3.0),
            "atempo=2.000000,atempo=1.500000",
        )
        self.assertEqual(
            cover.atempo_filter_chain(0.25),
            "atempo=0.500000,atempo=0.500000",
        )
        with self.assertRaisesRegex(ValueError, "must be positive"):
            cover.atempo_filter_chain(0.0)

    def test_fixed_rate_narration_ripples_audio_without_scaling(self) -> None:
        text_events = [
            self.text_event(0, 0.80, 1.00, 1.60),
            self.text_event(1, 1.80, 2.00, 2.50),
        ]
        plans = cover.build_tts_cue_plans(text_events, 5.0)
        timings = {
            0: cover.SpeechTiming(0, 1.0, 1.6, 1.0, 1.6, 1.0, 1.6, "", ""),
            1: cover.SpeechTiming(1, 2.0, 2.5, 2.0, 2.5, 2.0, 2.5, "", ""),
        }
        schedule = cover.build_tts_fixed_rate_schedule(
            plans,
            [1.20, 0.50],
            timings,
            5.0,
            gap_seconds=0.08,
        )
        self.assertAlmostEqual(schedule[0].voice_start, 1.0)
        self.assertAlmostEqual(schedule[0].voice_end, 2.2)
        self.assertAlmostEqual(schedule[1].voice_start, 2.28)
        self.assertAlmostEqual(schedule[1].lag, 0.28)
        self.assertAlmostEqual(schedule[1].duration, 0.50)

    def test_fixed_rate_subtitle_follows_voice_but_cover_timing_is_unchanged(self) -> None:
        text_event = self.text_event(4, 0.80, 1.00, 1.60)
        event = cover.CoverEvent(
            event_id=0,
            start=0.70,
            end=1.80,
            rect=(10, 20, 200, 100),
            source_cluster_index=3,
            source_cluster_start=0.75,
            source_cluster_end=1.75,
            text_events=(text_event,),
        )
        updated = cover.retime_cover_text_to_voice(
            [event],
            [{"cue_index": 4, "voice_start": 1.25, "voice_end": 2.10}],
        )[0]
        self.assertEqual((updated.start, updated.end), (0.70, 1.80))
        self.assertEqual(updated.rect, event.rect)
        self.assertEqual(
            (updated.text_events[0].start, updated.text_events[0].end),
            (1.25, 2.10),
        )

    def test_cluster_tiled_subtitle_is_default_for_fixed_rate_voice(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(cover.parse_tts_subtitle_timing(), "cluster")

    def test_text_coverage_detects_blur_only_gap_after_voice_retime(self) -> None:
        text_event = self.text_event(4, 0.70, 1.00, 1.80)
        event = cover.CoverEvent(
            event_id=0,
            start=0.70,
            end=1.80,
            rect=(10, 20, 200, 100),
            source_cluster_index=3,
            source_cluster_start=0.75,
            source_cluster_end=1.75,
            text_events=(text_event,),
        )
        source_report = cover.cover_text_coverage_report([event])
        self.assertTrue(source_report["ok"])

        retimed = cover.retime_cover_text_to_voice(
            [event],
            [{"cue_index": 4, "voice_start": 1.25, "voice_end": 2.10}],
        )
        voice_report = cover.cover_text_coverage_report(retimed)
        self.assertFalse(voice_report["ok"])
        self.assertEqual(voice_report["uncovered_event_count"], 1)
        self.assertEqual(
            voice_report["uncovered_events"][0]["uncovered_intervals"],
            [[0.70, 1.25]],
        )

    def test_fixed_rate_narration_rejects_voice_past_video_end(self) -> None:
        plans = cover.build_tts_cue_plans(
            [self.text_event(0, 0.80, 1.00, 1.60)],
            2.0,
        )
        timings = {
            0: cover.SpeechTiming(0, 1.0, 1.6, 1.0, 1.6, 1.0, 1.6, "", ""),
        }
        with self.assertRaisesRegex(ValueError, "video_end"):
            cover.build_tts_fixed_rate_schedule(
                plans,
                [1.20],
                timings,
                2.0,
            )

    def test_fit_validation_rejects_voice_overlapping_next_cue(self) -> None:
        plans = cover.build_tts_cue_plans(
            [
                self.text_event(0, 0.80, 1.00, 1.60),
                self.text_event(1, 2.60, 3.00, 3.70),
            ],
            5.0,
        )
        with self.assertRaisesRegex(ValueError, "overlaps the next cue"):
            cover.validate_tts_fits(plans, [2.10, 1.0])

    def test_merge_short_slots_extends_group_until_voice_fits(self) -> None:
        plans = cover.build_tts_cue_plans(
            [
                self.text_event(0, 0.80, 1.00, 1.60),
                self.text_event(1, 2.60, 3.00, 3.70),
            ],
            5.0,
        )
        groups = cover.build_tts_merge_groups(
            plans,
            [3.20, 0.80],
            max_fit_speed=1.60,
            policy="merge_short_slots",
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual((groups[0].start_index, groups[0].end_index), (0, 1))
        self.assertAlmostEqual(groups[0].slot_seconds, 4.0)
        self.assertLessEqual(groups[0].required_speed, 1.60)

    def test_fail_policy_rejects_a_short_slot_instead_of_merging(self) -> None:
        plans = cover.build_tts_cue_plans(
            [
                self.text_event(0, 0.80, 1.00, 1.60),
                self.text_event(1, 2.60, 3.00, 3.70),
            ],
            5.0,
        )
        with self.assertRaisesRegex(ValueError, "above VISUB_TTS_MAX_FIT_SPEED"):
            cover.build_tts_merge_groups(
                plans,
                [3.20, 0.80],
                max_fit_speed=1.60,
                policy="fail",
            )

    def test_short_final_cue_merges_backwards_with_previous_group(self) -> None:
        plans = cover.build_tts_cue_plans(
            [
                self.text_event(0, 0.80, 1.00, 1.60),
                self.text_event(1, 2.60, 3.00, 3.70),
            ],
            5.0,
        )
        groups = cover.build_tts_merge_groups(
            plans,
            [0.50, 3.20],
            max_fit_speed=1.60,
            policy="merge_short_slots",
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual((groups[0].start_index, groups[0].end_index), (0, 1))
        self.assertLessEqual(groups[0].required_speed, 1.60)

    def test_cascade_overflow_moves_following_cue_by_previous_end_plus_gap(self) -> None:
        plans = cover.build_tts_cue_plans(
            [
                self.text_event(0, 0.9, 1.0, 1.5),
                self.text_event(1, 1.9, 2.0, 2.5),
            ],
            5.0,
            cluster_indexes_by_cue={0: 4, 1: 4},
        )
        schedule = cover.build_tts_cascade_schedule(
            plans,
            [2.20, 0.50],
            5.0,
            max_fit_speed=1.60,
            hard_max_speed=2.0,
            max_lag_seconds=0.60,
        )
        self.assertAlmostEqual(schedule[0].voice_end, 2.10, places=3)
        self.assertAlmostEqual(schedule[1].voice_start, 2.13, places=3)
        self.assertAlmostEqual(schedule[1].lag, 0.13, places=3)
        self.assertGreater(schedule[0].overflow_into_next_slot, 0.09)

    def test_cascade_rejects_lag_over_configured_limit(self) -> None:
        plans = cover.build_tts_cue_plans(
            [
                self.text_event(0, 0.9, 1.0, 1.5),
                self.text_event(1, 1.9, 2.0, 2.5),
            ],
            5.0,
            cluster_indexes_by_cue={0: 4, 1: 4},
        )
        with self.assertRaisesRegex(ValueError, "max lag exceeds"):
            cover.build_tts_cascade_schedule(
                plans,
                [2.20, 0.50],
                5.0,
                max_fit_speed=1.60,
                hard_max_speed=2.0,
                max_lag_seconds=0.10,
            )

    def test_last_cue_in_cluster_gets_one_second_breath_tail(self) -> None:
        plans = cover.build_tts_cue_plans(
            [
                self.text_event(0, 0.9, 1.0, 1.5),
                self.text_event(1, 3.9, 4.0, 4.5),
            ],
            6.0,
            cluster_indexes_by_cue={0: 4, 1: 5},
        )
        self.assertAlmostEqual(cover.tts_cascade_anchor(plans, 0, 6.0), 2.5)
        schedule = cover.build_tts_cascade_schedule(
            plans,
            [1.0, 1.0],
            6.0,
            max_fit_speed=1.60,
            hard_max_speed=2.0,
            max_lag_seconds=0.60,
        )
        self.assertAlmostEqual(schedule[0].anchor, 2.5)
        self.assertAlmostEqual(schedule[0].target_seconds, 1.45)

    def test_tts_text_override_is_separate_from_display_text(self) -> None:
        plans = cover.build_tts_cue_plans(
            [self.text_event(4, 0.9, 1.0, 2.0)],
            3.0,
            tts_text_by_cue={4: "Cau doc ngan."},
        )
        self.assertEqual(plans[0].display_text_vi, "Câu 4")
        self.assertEqual(plans[0].text_vi, "Cau doc ngan.")

    def test_speech_alignment_clamps_noisy_start_around_visual_cue(self) -> None:
        cues = [{
            "source_cue_index": 7,
            "start": 5.0,
            "end": 6.0,
            "text_zh": "zh",
            "text_vi": "vi",
        }]
        timings = cover.stabilize_speech_timings(
            cues,
            [(3.0, 4.0)],
            10.0,
            max_early_seconds=0.30,
            max_late_seconds=0.25,
        )
        self.assertAlmostEqual(timings[0].speech_start, 4.70)
        self.assertAlmostEqual(timings[0].speech_end, 5.70)

    def test_speech_aligned_cue_uses_audio_anchor_and_natural_speed_cap(self) -> None:
        plan = cover.build_tts_cue_plans(
            [self.text_event(2, 0.9, 1.0, 2.0)],
            4.0,
        )[0]
        timing = cover.SpeechTiming(
            cue_index=2,
            visual_start=1.0,
            visual_end=2.0,
            speech_start=1.12,
            speech_end=2.02,
            raw_speech_start=1.12,
            raw_speech_end=2.02,
            text_zh="zh",
            text_vi="vi",
        )
        decision = cover.build_tts_speech_cue(
            plan,
            timing,
            1.10,
            4.0,
            next_speech_start=2.40,
            max_fit_speed=1.25,
            hard_max_speed=1.40,
        )
        self.assertAlmostEqual(decision.voice_start, 1.12, places=3)
        self.assertAlmostEqual(decision.anchor, 2.12, places=3)
        self.assertLessEqual(decision.speed_applied, 1.25)

    def test_speech_aligned_fit_guard_reserves_encoder_rounding_margin(self) -> None:
        plan = cover.build_tts_cue_plans(
            [self.text_event(2, 0.9, 1.0, 2.0)],
            4.0,
        )[0]
        timing = cover.SpeechTiming(
            2, 1.0, 2.0, 1.0, 2.0, 1.0, 2.0, "zh", "vi"
        )
        decision = cover.build_tts_speech_cue(
            plan,
            timing,
            1.0,
            4.0,
            next_speech_start=2.20,
            max_fit_speed=1.25,
            hard_max_speed=1.40,
            fit_guard_seconds=0.015,
        )
        self.assertAlmostEqual(decision.anchor, 2.10)
        self.assertAlmostEqual(decision.target_seconds, 1.085)

    def test_speech_schedule_rejects_text_that_needs_more_than_hard_cap(self) -> None:
        plan = cover.build_tts_cue_plans(
            [self.text_event(2, 0.9, 1.0, 1.5)],
            3.0,
        )[0]
        timing = cover.SpeechTiming(2, 1.0, 1.5, 1.0, 1.45, 1.0, 1.45, "zh", "vi")
        decision = cover.build_tts_speech_cue(
            plan,
            timing,
            1.20,
            3.0,
            next_speech_start=1.60,
            max_fit_speed=1.25,
            hard_max_speed=1.40,
        )
        self.assertGreater(decision.required_speed, 1.40)
        with self.assertRaisesRegex(ValueError, "schedule assertions failed"):
            cover.validate_tts_speech_schedule(
                [decision],
                3.0,
                hard_max_speed=1.40,
            )

    def test_ducking_filter_uses_voice_as_sidechain_key(self) -> None:
        graph = cover.build_tts_ducking_filtergraph(
            [("a.wav", 1000), ("b.wav", 2500)],
            bgm_input_index=3,
            bgm_gain_db=-20.0,
            threshold=0.02,
            ratio=10.0,
            attack_ms=8.0,
            release_ms=250.0,
        )
        self.assertIn("[voice_pre]asplit=2[voice_mix][voice_key]", graph)
        self.assertIn("[bgm][voice_key]sidechaincompress=", graph)
        self.assertIn("[ducked][voice_mix]amix=inputs=2", graph)


class BlurMergeTests(unittest.TestCase):
    @staticmethod
    def event(event_id: int, cluster_index: int, start: float, end: float, rect: cover.Rect) -> cover.CoverEvent:
        return cover.CoverEvent(event_id, start, end, rect, cluster_index, start, end)

    def test_merge_only_near_duplicate_events_from_same_cluster(self) -> None:
        events = [
            self.event(0, 4, 1.0, 2.0, (10, 20, 110, 60)),
            self.event(1, 4, 2.1, 3.0, (12, 19, 112, 61)),
            self.event(2, 5, 3.1, 4.0, (12, 19, 112, 61)),
        ]
        merged = cover.merge_blur_events(events, gap_seconds=0.20, near_pixels=4)
        self.assertEqual(len(merged), 2)
        self.assertEqual((merged[0].start, merged[0].end), (1.0, 3.0))
        self.assertEqual(merged[0].rect, (10, 19, 112, 61))
        self.assertEqual((merged[1].start, merged[1].end), (3.1, 4.0))


class AssTests(unittest.TestCase):
    def test_box_ass_contains_text_only_because_cover_uses_rounded_overlay(self) -> None:
        meta = desub.VideoMeta(width=720, height=1280, fps=30.0, frames=300, duration=10.0)
        text_event = cover.TextEvent(
            cue_index=4,
            start=0.88,
            end=2.12,
            original_start=1.0,
            original_end=2.0,
            text_vi="Phu de Viet",
            text_zh="",
            line_count=1,
            text_x=360,
            text_y=930,
        )
        event = cover.CoverEvent(
            event_id=0,
            start=0.88,
            end=2.12,
            rect=(100, 850, 620, 1010),
            source_cluster_index=3,
            source_cluster_start=1.0,
            source_cluster_end=2.0,
            text_events=(text_event,),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cover.ass"
            cover.write_cover_ass(path, [event], meta, style="box_black", opacity=1.0)
            content = path.read_text(encoding="utf-8")
        dialogue_lines = [line for line in content.splitlines() if line.startswith("Dialogue:")]
        self.assertEqual(len(dialogue_lines), 1)
        self.assertTrue(dialogue_lines[0].startswith("Dialogue: 1"))
        self.assertIn(r"{\an5\pos(360,930)}Phu de Viet", dialogue_lines[0])

    def test_filtergraph_applies_mask_before_rounded_overlay(self) -> None:
        meta = desub.VideoMeta(width=720, height=1280, fps=30.0, frames=300, duration=10.0)
        event = cover.CoverEvent(0, 1.0, 2.0, (100, 850, 300, 960), 3, 1.0, 2.0)
        with tempfile.TemporaryDirectory() as temp_dir:
            ass_path = Path(temp_dir) / "text.ass"
            mask_path = Path(temp_dir) / "mask.png"
            graph = cover.build_cover_filtergraph(
                [event],
                ass_path,
                20,
                style="blur",
                opacity=1.0,
                mask_paths={(200, 110): mask_path},
            )
            box_graph = cover.build_cover_filtergraph(
                [event],
                ass_path,
                20,
                style="box_white",
                opacity=0.8,
                mask_paths={(200, 110): mask_path},
            )
        self.assertIn("movie=filename=", graph)
        self.assertIn("[cover0][mask0]alphamerge[rounded0]", graph)
        self.assertIn("[base][rounded0]overlay=100:850", graph)
        self.assertIn("gte(t,1.000000)*lt(t,2.000000)", graph)
        self.assertIn("color=c=white:s=200x110", box_graph)
        self.assertIn("alphamerge,colorchannelmixer=aa=0.800000", box_graph)
        self.assertIn("[0:v][rounded0]overlay=100:850", box_graph)


if __name__ == "__main__":
    unittest.main()
