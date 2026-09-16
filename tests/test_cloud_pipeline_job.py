from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from experiments.desub import prototype
from integrated_desub_runtime.cloud_pipeline_job import (
    balanced_subtitle_lines,
    build_drawtext_filtergraph,
    build_drawtext_measure_filtergraph,
    build_mix_filtergraph,
    build_schedule,
    measure_drawtext_widths,
    parse_rect,
    parse_drawtext_width_markers,
    require_cuda_runtime,
    schedule_to_subtitle_cues,
    subtitle_render_style,
    validate_tts_identity,
    validate_clean_engine_report,
    validate_cues,
    validate_resume_clean_config,
    validate_subtitle_widths,
    validate_vertex_qa_payload,
    video_sample_frame_ids,
)


class IntegratedCloudPipelineTests(unittest.TestCase):
    def test_resume_clean_config_is_all_or_nothing_and_exact(self) -> None:
        self.assertIsNone(
            validate_resume_clean_config(uri="", generation="", sha256="")
        )
        config = validate_resume_clean_config(
            uri="gs://bucket/attempts/0001/clean/clean.mp4",
            generation="1785127671732968",
            sha256="a" * 64,
        )
        self.assertEqual(
            config,
            {
                "uri": "gs://bucket/attempts/0001/clean/clean.mp4",
                "generation": "1785127671732968",
                "sha256": "a" * 64,
            },
        )
        with self.assertRaisesRegex(ValueError, "URI, exact generation"):
            validate_resume_clean_config(
                uri="gs://bucket/clean.mp4",
                generation="",
                sha256="a" * 64,
            )
        with self.assertRaisesRegex(ValueError, "positive integer"):
            validate_resume_clean_config(
                uri="gs://bucket/clean.mp4",
                generation="-1",
                sha256="a" * 64,
            )
        with self.assertRaisesRegex(ValueError, "64 lowercase hex"):
            validate_resume_clean_config(
                uri="gs://bucket/clean.mp4",
                generation="1",
                sha256="not-a-sha",
            )

    def test_generated_inpaint_runner_persists_cuda_evidence_last(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = Path(tmp) / "mask_inpaint_runner.py"
            prototype.write_mask_inpaint_runner(runner)
            source = runner.read_text(encoding="utf-8")

        device_position = source.index(
            'mask_stats["device_type"] = str(device.type)'
        )
        write_position = source.index(
            "Path(args.mask_stats_json).write_text"
        )
        self.assertLess(device_position, write_position)
        self.assertEqual(
            source.count("Path(args.mask_stats_json).write_text"),
            1,
        )

    def test_tts_identity_is_exact_and_rejects_overrides(self) -> None:
        identity = validate_tts_identity(
            voice="BV075_streaming",
            resource_id="7102355803792740865",
            rate=1.5,
        )
        self.assertEqual(identity["provider_rate"], "1.5000")
        with self.assertRaisesRegex(ValueError, "voice must be exactly"):
            validate_tts_identity(
                voice="BV074_streaming",
                resource_id="7102355803792740865",
                rate=1.5,
            )
        with self.assertRaisesRegex(ValueError, "resource id must be exactly"):
            validate_tts_identity(
                voice="BV075_streaming",
                resource_id="wrong",
                rate=1.5,
            )

    def test_video_sample_frame_ids_are_bounded_and_nearest_frame(self) -> None:
        frame_ids = video_sample_frame_ids(
            frame_count=31,
            source_fps=30.0,
            sample_fps=8.0,
        )
        self.assertEqual(frame_ids, [0, 4, 8, 11, 15, 19, 22, 26, 30])
        self.assertEqual(len(frame_ids), len(set(frame_ids)))
        with self.assertRaisesRegex(ValueError, r"in \(0, 24\]"):
            video_sample_frame_ids(
                frame_count=31,
                source_fps=30.0,
                sample_fps=25.0,
            )

    def test_cue_validation_preserves_exact_cardinality(self) -> None:
        cues = validate_cues(
            {
                "cues": [
                    {
                        "reviewed_index": 0,
                        "start": 0.0,
                        "end": 0.8,
                        "text_zh": "你好",
                        "text_vi": "Xin chào.",
                    },
                    {
                        "reviewed_index": 1,
                        "start": 1.0,
                        "end": 2.0,
                        "text_zh": "可以",
                        "text_vi": "Được luôn.",
                    },
                ]
            },
            duration=3.0,
        )
        self.assertEqual(len(cues), 2)
        self.assertEqual([cue["reviewed_index"] for cue in cues], [0, 1])

    def test_fixed_rate_schedule_is_ordered_and_retimes_subtitles(self) -> None:
        cues = [
            {"start": 0.0, "end": 0.8, "text_vi": "Một"},
            {"start": 0.8, "end": 1.2, "text_vi": "Hai"},
        ]
        schedule = build_schedule(
            cues,
            [0.75, 0.70],
            video_duration=2.0,
            gap_seconds=0.05,
        )
        self.assertAlmostEqual(schedule[0]["start"], 0.0)
        self.assertAlmostEqual(schedule[1]["start"], 0.8)
        self.assertLessEqual(schedule[0]["end"] + 0.05, schedule[1]["start"])
        self.assertEqual(schedule[0]["start_sample"], 0)
        self.assertEqual(schedule[1]["start_sample"], 35_280)
        retimed = schedule_to_subtitle_cues(cues, schedule)
        self.assertEqual(len(retimed), len(cues))
        self.assertAlmostEqual(retimed[1]["end"], 1.5)
        self.assertEqual(retimed[0]["center_y"], 0.735)

    def test_fixed_rate_schedule_fails_when_tail_cannot_fit(self) -> None:
        with self.assertRaisesRegex(ValueError, "exceeds the video duration"):
            build_schedule(
                [{"start": 0.8, "end": 0.9}],
                [0.5],
                video_duration=1.0,
                gap_seconds=0.05,
            )

    def test_fixed_rate_schedule_reports_worst_lag_cue(self) -> None:
        with self.assertRaisesRegex(ValueError, r"at cue 1$"):
            build_schedule(
                [
                    {"start": 0.0, "end": 0.1},
                    {"start": 0.1, "end": 0.2},
                ],
                [1.0, 0.1],
                video_duration=2.0,
                gap_seconds=0.05,
                max_lag_seconds=0.6,
            )

    def test_mix_graph_pads_and_trims_both_buses_and_final_audio(self) -> None:
        graph = build_mix_filtergraph(
            [0, 1_000],
            bgm_input_index=3,
            duration=10.0,
            bgm_gain_db=-20.0,
        )
        self.assertEqual(graph.count("apad,atrim=0:10.000000"), 3)
        self.assertIn("adelay=1000S|1000S", graph)
        self.assertLess(
            graph.index("aresample=44100"),
            graph.index("adelay=1000S|1000S"),
        )
        self.assertIn("[3:a]volume=-20.000dB", graph)
        self.assertIn("alimiter=limit=0.95", graph)

    def test_clean_rect_is_strict(self) -> None:
        self.assertEqual(parse_rect("115;870;605;1015"), (115, 870, 605, 1015))
        with self.assertRaises(ValueError):
            parse_rect("115,870,605")

    def test_drawtext_renderer_is_one_to_one_and_safe_for_percent_text(self) -> None:
        cues = [
            {
                "start": 0.0,
                "end": 0.8,
                "center_y": 0.735,
                "text_vi": "Tăng 20% FPS.",
            },
            {
                "start": 1.0,
                "end": 1.8,
                "center_y": 0.735,
                "text_vi": "Được luôn.",
            },
        ]
        root = Path.cwd()
        graph = build_drawtext_filtergraph(
            cues,
            text_paths=[root / "tmp/cue_0.txt", root / "tmp/cue_1.txt"],
            font_path=root / "app/fonts/DejaVuSans-Bold.ttf",
            height=1280,
        )
        self.assertEqual(graph.count("drawtext="), 2)
        self.assertIn("expansion=none", graph)
        self.assertIn("enable='between(t,0.000000,0.800000)'", graph)
        self.assertTrue(graph.endswith("format=yuv420p[vsub]"))

    def test_balanced_subtitle_lines_never_drops_words(self) -> None:
        text = "Con card thần thánh tôi đang dùng."
        lines = balanced_subtitle_lines(text, max_chars=24)
        self.assertEqual(" ".join(lines), text)
        self.assertLessEqual(len(lines), 2)

    def test_balanced_subtitle_lines_wraps_known_clipped_cues(self) -> None:
        self.assertEqual(
            balanced_subtitle_lines(
                "Có máy chơi game không?",
                max_chars=20,
            ),
            ["Có máy chơi", "game không?"],
        )
        self.assertEqual(
            balanced_subtitle_lines(
                "6750 GRE vượt ngân sách.",
                max_chars=20,
            ),
            ["6750 GRE vượt", "ngân sách."],
        )

    def test_drawtext_measure_graph_uses_render_style_and_unique_markers(
        self,
    ) -> None:
        root = Path.cwd()
        graph = build_drawtext_measure_filtergraph(
            text_paths=[root / "tmp/cue_0.txt", root / "tmp/cue_1.txt"],
            font_path=root / "app/fonts/DejaVuSans-Bold.ttf",
            height=1280,
        )
        self.assertIn("expansion=none", graph)
        self.assertIn("fontsize=51", graph)
        self.assertIn("borderw=2.500", graph)
        self.assertIn("line_spacing=6", graph)
        self.assertIn("print(1000000+text_w)", graph)
        self.assertIn("print(1010000+text_w)", graph)
        self.assertTrue(graph.endswith("null[measure_out]"))

    def test_drawtext_width_marker_parser_is_fail_closed(self) -> None:
        output = "\n".join(
            [
                "[Eval @ 0x1] 1000749.000000",
                "[Eval @ 0x2] 1010320.500000",
            ]
        )
        self.assertEqual(
            parse_drawtext_width_markers(output, expected_count=2),
            [749.0, 320.5],
        )
        with self.assertRaisesRegex(ValueError, "missing"):
            parse_drawtext_width_markers(
                "[Eval @ 0x1] 1000749.000000",
                expected_count=2,
            )
        with self.assertRaisesRegex(ValueError, "conflicting"):
            parse_drawtext_width_markers(
                "\n".join(
                    [
                        "[Eval @ 0x1] 1000749.000000",
                        "[Eval @ 0x1] 1000750.000000",
                    ]
                ),
                expected_count=1,
            )
        with self.assertRaisesRegex(ValueError, "missing"):
            parse_drawtext_width_markers(
                "[Eval @ 0x1] 1012000.000000",
                expected_count=1,
            )

    def test_drawtext_width_measurement_maps_one_frame_output(self) -> None:
        class FakeDesub:
            def __init__(self) -> None:
                self.args: list[str] = []
                self.timeout: int | None = None

            def cmd(
                self,
                args: list[str],
                *,
                timeout: int | None = None,
            ) -> object:
                self.args = args
                self.timeout = timeout
                return type(
                    "Proc",
                    (),
                    {"stdout": "[Eval @ 0x1] 1000400.000000"},
                )()

        root = Path.cwd()
        fake = FakeDesub()
        self.assertEqual(
            measure_drawtext_widths(
                desub=fake,
                text_paths=[root / "tmp/cue_0.txt"],
                font_path=root / "app/fonts/DejaVuSans-Bold.ttf",
                width=720,
                height=1280,
            ),
            [400.0],
        )
        self.assertIn("-nostdin", fake.args)
        self.assertIn("-xerror", fake.args)
        self.assertIn("[measure_out]", fake.args)
        self.assertEqual(fake.args[-3:], ["-f", "null", "-"])
        self.assertEqual(fake.timeout, 120)

    def test_subtitle_width_gate_includes_border_and_margin(self) -> None:
        report = validate_subtitle_widths(
            [667.0],
            frame_width=720,
            border_width=2.5,
            safe_margin=24.0,
        )
        self.assertTrue(report["pass"])
        self.assertEqual(report["max_rendered_width_pixels"], 672.0)
        with self.assertRaisesRegex(ValueError, "cue 0"):
            validate_subtitle_widths(
                [667.001],
                frame_width=720,
                border_width=2.5,
                safe_margin=24.0,
            )
        with self.assertRaisesRegex(ValueError, "cue 0"):
            validate_subtitle_widths(
                [749.0],
                frame_width=720,
                border_width=2.5,
                safe_margin=24.0,
            )

    def test_subtitle_style_rejects_invalid_numeric_environment(self) -> None:
        with patch.dict(
            "os.environ",
            {"DESUB_VISUB_FONT_SIZE_RATIO": "nan"},
        ):
            with self.assertRaisesRegex(ValueError, "font-size ratio"):
                subtitle_render_style(1280)
        with patch.dict(
            "os.environ",
            {"INTEGRATED_VISUB_BORDER_WIDTH": "-1"},
        ):
            with self.assertRaisesRegex(ValueError, "border width"):
                subtitle_render_style(1280)
        with self.assertRaisesRegex(ValueError, "safe margin"):
            validate_subtitle_widths(
                [100.0],
                frame_width=720,
                border_width=2.5,
                safe_margin=float("nan"),
            )

    def test_cuda_preflight_rejects_cpu_fallback(self) -> None:
        class FakeCuda:
            @staticmethod
            def is_available() -> bool:
                return False

        class FakeTorch:
            cuda = FakeCuda()
            version = type("Version", (), {"cuda": None})()
            __version__ = "2.13.0+cu126"

        with self.assertRaisesRegex(ValueError, "CPU fallback is forbidden"):
            require_cuda_runtime(FakeTorch())

    def test_cuda_preflight_accepts_one_cuda_126_device(self) -> None:
        class FakeCuda:
            @staticmethod
            def is_available() -> bool:
                return True

            @staticmethod
            def device_count() -> int:
                return 1

            @staticmethod
            def get_device_name(_index: int) -> str:
                return "NVIDIA L4"

            @staticmethod
            def get_device_capability(_index: int) -> tuple[int, int]:
                return (8, 9)

        class FakeTorch:
            cuda = FakeCuda()
            version = type("Version", (), {"cuda": "12.6"})()
            __version__ = "2.13.0+cu126"

        report = require_cuda_runtime(FakeTorch())
        self.assertTrue(report["pass"])
        self.assertEqual(report["device_name"], "NVIDIA L4")

    def test_vertex_verdict_is_strictly_typed_and_all_gates_pass(self) -> None:
        payload = {
            "overall_pass": True,
            "chinese_dialogue_subtitle_residuals": [],
            "inpainting_artifacts": [],
            "vietnamese_voice_present": True,
            "vietnamese_voice_consistent": True,
            "vietnamese_subtitles_present": True,
            "voice_subtitle_sync_pass": True,
            "audio_tail_pass": True,
            "notes": [],
        }
        self.assertTrue(validate_vertex_qa_payload(payload))
        payload["overall_pass"] = "false"
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            validate_vertex_qa_payload(payload)

    def test_vertex_cannot_pass_with_a_residual_or_artifact(self) -> None:
        payload = {
            "overall_pass": True,
            "chinese_dialogue_subtitle_residuals": [
                {"timestamp_seconds": 1.0, "description": "residual"}
            ],
            "inpainting_artifacts": [],
            "vietnamese_voice_present": True,
            "vietnamese_voice_consistent": True,
            "vietnamese_subtitles_present": True,
            "voice_subtitle_sync_pass": True,
            "audio_tail_pass": True,
            "notes": [],
        }
        self.assertFalse(validate_vertex_qa_payload(payload))

    def test_clean_engine_requires_timed_full_box_for_every_cue(self) -> None:
        report = {
            "cluster_count": 2,
            "cluster_results": [
                {
                    "mask_kind": "stroke",
                    "clusters": [{"start": 0.0}, {"start": 1.0}],
                    "mask_stats": {
                        "device_type": "cuda",
                        "cuda_required": True,
                        "processed_frames": 60,
                        "static_mask": {
                            "static_mask_enabled": True,
                            "static_region_count": 2,
                            "fallback_static_regions": 2,
                            "prepass_frames": 60,
                            "input_frames": 60,
                        },
                    },
                }
            ],
        }
        checks = validate_clean_engine_report(report, expected_cluster_count=2)
        self.assertTrue(checks["pass"])
        report["cluster_results"][0]["mask_stats"]["static_mask"][
            "fallback_static_regions"
        ] = 1
        with self.assertRaisesRegex(ValueError, "every timed region"):
            validate_clean_engine_report(report, expected_cluster_count=2)

    def test_clean_engine_rejects_child_cpu_fallback(self) -> None:
        report = {
            "cluster_count": 1,
            "cluster_results": [
                {
                    "mask_kind": "stroke",
                    "clusters": [{"start": 0.0}],
                    "mask_stats": {
                        "device_type": "cpu",
                        "cuda_required": True,
                        "processed_frames": 30,
                        "static_mask": {
                            "static_mask_enabled": True,
                            "static_region_count": 1,
                            "fallback_static_regions": 1,
                            "prepass_frames": 30,
                            "input_frames": 30,
                        },
                    },
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "fail-closed on CUDA"):
            validate_clean_engine_report(report, expected_cluster_count=1)


if __name__ == "__main__":
    unittest.main()
