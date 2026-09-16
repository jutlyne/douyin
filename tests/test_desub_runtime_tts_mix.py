from __future__ import annotations

import json
import unittest
from pathlib import Path

from integrated_desub_contracts.validation import validate_tts_timing_evidence
from integrated_desub_runtime.tts_mix import (
    CAPCUT_PROSODY_RATE,
    CAPCUT_PROSODY_RATE_TEXT,
    CAPCUT_RESOURCE_ID,
    CAPCUT_VOICE,
    REQUIRED_STEMS,
    CueScheduleUnfit,
    CueTimingInput,
    RuntimeContractError,
    build_capcut_direct_request,
    build_mix_manifest_structure,
    plan_tail_adjustment,
    schedule_cue,
    schedule_cues,
    synthesize_capcut_direct,
)


ROOT = Path(__file__).resolve().parents[1]


def _sha(label: str) -> str:
    return (label.encode("utf-8").hex() + "0" * 64)[:64]


def _stem_evidence(
    *,
    clean_samples: int,
    video_samples: int,
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for name in REQUIRED_STEMS:
        result[name] = {
            "sha256": _sha(name),
            "sample_count": (
                clean_samples if name == "clean_input" else video_samples
            ),
        }
    return result


class CapCutDirectTests(unittest.TestCase):
    def test_request_freezes_projection_rate_voice_and_ssml(self) -> None:
        request = build_capcut_direct_request('Đúng & "vui"')
        projection = request["semantic_projection"]
        self.assertEqual(projection["provider"], "capcut-private")
        self.assertEqual(projection["voice"], CAPCUT_VOICE)
        self.assertEqual(projection["resource_id"], CAPCUT_RESOURCE_ID)
        self.assertEqual(
            projection["prosody_rate"],
            CAPCUT_PROSODY_RATE_TEXT,
        )
        self.assertIn(
            '<prosody rate="1.5000">Đúng &amp; &quot;vui&quot;</prosody>',
            request["ssml"],
        )
        self.assertIsNone(request["normalized_request_sha256"])
        self.assertEqual(
            request["hash_status"],
            "blocked_pending_vetted_rfc8785",
        )
        self.assertFalse(
            request["execution_constraints"]["post_atempo_allowed"]
        )
        self.assertFalse(
            request["execution_constraints"]["provider_fallback_allowed"]
        )

        policy = json.loads(
            (
                ROOT
                / "integrated_desub_contracts"
                / "policies"
                / "v1"
                / "hash_preimage_contracts.json"
            ).read_text(encoding="utf-8")
        )
        ordered = policy["contracts"][
            "normalized_capcut_tts_request_sha256"
        ]["ordered_semantic_fields"]
        self.assertEqual(list(projection), ordered)

    def test_request_ssml_matches_existing_adapter_without_network(self) -> None:
        from container_short.steps import capcut_tts

        text = "Đúng & vui."
        request = build_capcut_direct_request(text)
        (parts, _) = capcut_tts._tts_new_request(
            text,
            CAPCUT_VOICE,
            CAPCUT_RESOURCE_ID,
            CAPCUT_PROSODY_RATE,
            dict(capcut_tts.DEFAULT_DEVICE),
        )
        url, _, body_text = parts
        body = json.loads(body_text)
        provider_payload = json.loads(body["tasks"][0]["payload"])
        projection = request["semantic_projection"]
        self.assertTrue(
            url.startswith(
                projection["base_url"] + projection["create_path"] + "?"
            )
        )
        self.assertEqual(
            body["tasks"][0]["req_key"],
            projection["request_key"],
        )
        self.assertEqual(
            body["tasks"][0]["task_version"],
            projection["task_version"],
        )
        self.assertEqual(
            provider_payload["audio_format"],
            projection["audio_format"],
        )
        self.assertEqual(provider_payload["ssml"], request["ssml"])

    def test_request_rejects_empty_non_nfc_and_xml_control(self) -> None:
        for text in ("", "e\u0301", "ok\x00bad"):
            with self.subTest(text=repr(text)):
                with self.assertRaises(RuntimeContractError):
                    build_capcut_direct_request(text)

    def test_adapter_is_called_once_at_direct_provider_rate(self) -> None:
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def fake_adapter(*args: object, **kwargs: object) -> float:
            calls.append((args, kwargs))
            return 1.25

        result = synthesize_capcut_direct(
            "Một câu vui.",
            "voice.mp3",
            adapter=fake_adapter,
            device={"device_id": "test"},
            poll_timeout=17,
        )
        self.assertEqual(result, 1.25)
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(args, ("Một câu vui.", "voice.mp3"))
        self.assertEqual(kwargs["voice"], CAPCUT_VOICE)
        self.assertEqual(kwargs["resource_id"], CAPCUT_RESOURCE_ID)
        self.assertEqual(kwargs["rate"], CAPCUT_PROSODY_RATE)
        self.assertEqual(kwargs["poll_timeout"], 17)


class CueSchedulingTests(unittest.TestCase):
    def test_single_cue_schedule_matches_frozen_validator(self) -> None:
        schedule = schedule_cue(
            CueTimingInput(
                cue_id="cue-000000",
                cue_index=0,
                speech_anchor_sample=10_000,
                decoded_sample_count=1_200,
                detected_onset_sample=100,
                detected_offset_sample_inclusive=1_000,
            ),
            video_sample_count=50_000,
        )
        self.assertEqual(schedule.target_actual_onset_sample, 10_000)
        self.assertEqual(schedule.placement_sample, 9_900)
        self.assertEqual(schedule.actual_onset_sample, 10_000)
        self.assertEqual(schedule.actual_offset_sample_inclusive, 10_900)
        self.assertTrue(schedule.fits)

        attempt = {
            "attempt": {
                "job_id": "desub-" + "1" * 32,
                "attempt_id": "att-" + "2" * 32,
                "attempt_seq": 1,
                "fence_digest": "3" * 64,
            },
            "cue_id": "cue-000000",
            "cue_index": 0,
            "candidate_index": 0,
            "translation_candidate_sha256": "4" * 64,
            "provider": "capcut-private",
            "voice": "BV075_streaming",
            "prosody_rate": "1.5000",
            "normalized_request_sha256": "5" * 64,
            "provider_response_id": "response-1",
            "outcome": "succeeded",
            "returned_media": {
                "uri": "gs://fixture/voice.mp3",
                "generation": "1",
                "size_bytes": 10,
                "sha256": "6" * 64,
                "content_type": "audio/mpeg",
            },
            "decoded_pcm": {
                "uri": "gs://fixture/voice.f32le",
                "generation": "2",
                "size_bytes": 20,
                "sha256": "7" * 64,
                "content_type": "application/octet-stream",
            },
            "silence_detector": {
                "sample_rate_hz": 44_100,
                "decoded_sample_count": 1_200,
                "window_samples": 882,
                "hop_samples": 441,
                "tail_window_padding": "right_zero_pad",
                "rms_denominator_samples": 882,
                "active_threshold_dbfs": -45,
                "active_threshold_linear": 0.005623413251903491,
                "consecutive_windows": 2,
                "active_runs": [
                    {
                        "onset_sample": 100,
                        "offset_sample_inclusive": 1_000,
                    }
                ],
                "onset_sample": 100,
                "offset_sample_inclusive": 1_000,
                "implementation_sha256": "8" * 64,
            },
            "schedule": schedule.contract_dict(),
        }
        result = validate_tts_timing_evidence(attempt, selected=True)
        self.assertTrue(result["selectable"])
        self.assertEqual(result["actual_offset_sample_inclusive"], 10_900)

    def test_sequence_uses_actual_previous_offset_and_exact_gap(self) -> None:
        schedules = schedule_cues(
            [
                CueTimingInput(
                    "cue-000000",
                    0,
                    10_000,
                    1_200,
                    100,
                    1_000,
                ),
                CueTimingInput(
                    "cue-000001",
                    1,
                    11_000,
                    1_100,
                    200,
                    900,
                ),
            ],
            video_sample_count=50_000,
        )
        self.assertEqual(
            schedules[1].previous_actual_offset_sample_inclusive,
            schedules[0].actual_offset_sample_inclusive,
        )
        self.assertEqual(schedules[1].previous_gap_samples, 2_205)
        self.assertEqual(schedules[1].overlap_samples, 0)
        self.assertEqual(schedules[1].lag_samples, 2_105)

    def test_sequence_stops_with_preserved_unfit_schedule(self) -> None:
        cues = [
            CueTimingInput(
                "cue-000000",
                0,
                9_500,
                2_000,
                100,
                1_900,
            )
        ]
        with self.assertRaises(CueScheduleUnfit) as raised:
            schedule_cues(cues, video_sample_count=10_000)
        self.assertFalse(raised.exception.schedule.fits)
        self.assertGreaterEqual(
            raised.exception.schedule.actual_offset_sample_inclusive,
            10_000,
        )


class TailAndMixManifestTests(unittest.TestCase):
    def test_tail_adjustment_is_exact_and_mutually_exclusive(self) -> None:
        padded = plan_tail_adjustment(9_000, 10_000)
        self.assertEqual(padded.pad_samples, 1_000)
        self.assertEqual(padded.trim_samples, 0)
        trimmed = plan_tail_adjustment(10_500, 10_000)
        self.assertEqual(trimmed.pad_samples, 0)
        self.assertEqual(trimmed.trim_samples, 500)

    def test_mix_manifest_has_exact_tail_and_named_stems(self) -> None:
        video_samples = 50_000
        clean_samples = 48_500
        schedules = schedule_cues(
            [
                CueTimingInput(
                    "cue-000000",
                    0,
                    10_000,
                    1_200,
                    100,
                    1_000,
                ),
                CueTimingInput(
                    "cue-000001",
                    1,
                    11_000,
                    1_100,
                    200,
                    900,
                ),
            ],
            video_sample_count=video_samples,
        )
        manifest = build_mix_manifest_structure(
            video_sample_count=video_samples,
            clean_input_sample_count=clean_samples,
            schedules=schedules,
            stem_pcm=_stem_evidence(
                clean_samples=clean_samples,
                video_samples=video_samples,
            ),
        )
        self.assertFalse(manifest["authorization_ready"])
        self.assertEqual(
            set(manifest["stem_pcm"]),
            set(REQUIRED_STEMS),
        )
        self.assertEqual(
            manifest["tail_adjustments"]["clean_input_to_bed"][
                "pad_samples"
            ],
            1_500,
        )
        placed_end = max(
            item.placed_clip_end_sample_exclusive for item in schedules
        )
        self.assertEqual(
            manifest["tail_adjustments"]["placed_voice_to_bus"][
                "pad_samples"
            ],
            video_samples - placed_end,
        )
        self.assertEqual(
            manifest["output_audio"]["decoded_sample_count"],
            video_samples,
        )
        self.assertEqual(
            manifest["tts_identity"]["prosody_rate"],
            "1.5000",
        )
        self.assertTrue(manifest["unresolved_runtime_bindings"])

    def test_mix_manifest_rejects_missing_or_wrong_length_stem(self) -> None:
        schedule = schedule_cue(
            CueTimingInput(
                "cue-000000",
                0,
                1_000,
                500,
                10,
                400,
            ),
            video_sample_count=10_000,
        )
        stems = _stem_evidence(
            clean_samples=9_000,
            video_samples=10_000,
        )
        del stems["premaster"]
        with self.assertRaisesRegex(
            RuntimeContractError,
            "five named stems",
        ):
            build_mix_manifest_structure(
                video_sample_count=10_000,
                clean_input_sample_count=9_000,
                schedules=[schedule],
                stem_pcm=stems,
            )

        stems = _stem_evidence(
            clean_samples=9_000,
            video_samples=10_000,
        )
        stems["voice"]["sample_count"] = 9_999
        with self.assertRaisesRegex(
            RuntimeContractError,
            "must equal 10000",
        ):
            build_mix_manifest_structure(
                video_sample_count=10_000,
                clean_input_sample_count=9_000,
                schedules=[schedule],
                stem_pcm=stems,
            )


if __name__ == "__main__":
    unittest.main()
