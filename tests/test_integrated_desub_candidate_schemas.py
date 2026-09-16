from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "integrated_desub_contracts" / "schemas" / "v1"


def _load(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def _outcome_branch(schema: dict, outcome: str) -> dict:
    matches = [
        branch
        for branch in schema["oneOf"]
        if branch.get("properties", {}).get("outcome", {}).get("const") == outcome
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected one static branch for outcome={outcome!r}")
    return matches[0]


class CandidateAndTtsSchemaTests(unittest.TestCase):
    def test_translation_candidate_is_pure_create_only_generator_output(self) -> None:
        schema = _load("translation_attempt.schema.json")
        required = set(schema["required"])
        properties = set(schema["properties"])

        self.assertTrue(
            {
                "source_cue_record",
                "source_cue_record_sha256",
                "candidate_sha256",
                "generator",
                "generator_prompt_sha256",
            }.issubset(required)
        )
        self.assertNotIn("semantic_preflight", properties)
        self.assertNotIn("style_preflight", properties)
        self.assertNotIn("translation_preflight_approval", properties)
        self.assertFalse(schema["additionalProperties"])

    def test_preflight_approval_binds_candidate_cue_verdicts_and_policies(self) -> None:
        schema = _load("translation_preflight_approval.schema.json")
        payload = schema["properties"]["payload"]
        required = set(payload["required"])
        self.assertTrue(
            {
                "attempt",
                "cue_id",
                "cue_index",
                "source_cue_record",
                "source_cue_record_sha256",
                "translation_candidate",
                "translation_candidate_object_sha256",
                "candidate_index",
                "candidate_sha256",
                "translation_policy_sha256",
                "semantic_policy_sha256",
                "style_policy_sha256",
                "semantic_verdict",
                "style_verdict",
                "decision",
            }.issubset(required)
        )
        self.assertEqual(payload["properties"]["decision"]["const"], "pass")
        self.assertEqual(
            schema["$defs"]["semanticVerdict"]["allOf"][1]["properties"]["role"]["const"],
            "translation_semantic_preflight",
        )
        self.assertEqual(
            schema["$defs"]["styleVerdict"]["allOf"][1]["properties"]["role"]["const"],
            "translation_style_preflight",
        )
        self.assertEqual(
            schema["properties"]["signature"]["$ref"],
            "common.schema.json#/$defs/kmsSignature",
        )

    def test_tts_requires_candidate_and_controller_approval_before_synthesis(self) -> None:
        schema = _load("tts_attempt.schema.json")
        required = set(schema["required"])
        self.assertTrue(
            {
                "translation_candidate",
                "translation_candidate_object_sha256",
                "translation_candidate_sha256",
                "translation_preflight_approval",
                "translation_preflight_approval_sha256",
            }.issubset(required)
        )
        self.assertIn("issued_at <= created_at < approval.expires_at", schema["$comment"])

    def test_tts_provider_failure_has_exact_error_and_no_media_evidence(self) -> None:
        schema = _load("tts_attempt.schema.json")
        branch = _outcome_branch(schema, "provider_failed")
        self.assertEqual(
            branch["properties"]["error_code"]["const"], "TTS_PROVIDER_FAILED"
        )
        forbidden = {
            next(iter(item["required"]))
            for item in schema["$defs"]["noMediaEvidence"]["not"]["anyOf"]
        }
        self.assertEqual(
            forbidden,
            {"returned_media", "decoded_pcm", "silence_detector", "schedule"},
        )

    def test_tts_media_failure_branches_are_non_contradictory(self) -> None:
        schema = _load("tts_attempt.schema.json")
        branch = _outcome_branch(schema, "media_invalid")
        by_error = {
            sub["properties"]["error_code"]["const"]: sub
            for sub in branch["oneOf"]
        }
        self.assertEqual(
            set(by_error), {"TTS_MEDIA_INVALID", "TTS_VOICE_RATE_MISMATCH"}
        )
        self.assertIn(
            {"$ref": "#/$defs/noDecodedEvidence"},
            by_error["TTS_MEDIA_INVALID"]["allOf"],
        )
        mismatch = by_error["TTS_VOICE_RATE_MISMATCH"]
        self.assertTrue(
            {"decoded_pcm", "silence_detector"}.issubset(set(mismatch["required"]))
        )
        self.assertEqual(mismatch["not"]["required"], ["schedule"])

    def test_tts_no_speech_schedule_failure_and_success_are_exact(self) -> None:
        schema = _load("tts_attempt.schema.json")

        no_speech = _outcome_branch(schema, "no_active_speech")
        self.assertEqual(
            no_speech["properties"]["error_code"]["const"], "TTS_MEDIA_INVALID"
        )
        self.assertEqual(
            no_speech["properties"]["silence_detector"]["$ref"],
            "#/$defs/inactiveSilenceDetector",
        )
        self.assertEqual(no_speech["not"]["required"], ["schedule"])

        schedule = _outcome_branch(schema, "schedule_unfit")
        self.assertEqual(
            schedule["properties"]["schedule"]["allOf"][1]["properties"]["fits"]["const"],
            False,
        )
        mapping = {
            tuple(sub["properties"]["candidate_index"].get("enum", []))
            or (sub["properties"]["candidate_index"]["const"],):
            sub["properties"]["error_code"]["const"]
            for sub in schedule["oneOf"]
        }
        self.assertEqual(mapping[(0, 1)], "DUB_SCHEDULE_INVALID")
        self.assertEqual(mapping[(2,)], "DUB_CUE_UNFIT")

        success = _outcome_branch(schema, "succeeded")
        self.assertEqual(
            success["properties"]["schedule"]["allOf"][1]["properties"]["fits"]["const"],
            True,
        )
        self.assertEqual(success["not"]["required"], ["error_code"])
        self.assertTrue(
            {"returned_media", "decoded_pcm", "silence_detector", "schedule"}.issubset(
                set(success["required"])
            )
        )

    def test_ledger_and_release_preserve_preflight_approval_chain(self) -> None:
        ledger = _load("cue_ledger.schema.json")
        entry = ledger["properties"]["cues"]["items"]
        self.assertTrue(
            {
                "translation_preflight_approval",
                "translation_preflight_approval_sha256",
            }.issubset(set(entry["required"]))
        )

        release = _load("release_approval.schema.json")
        payload = release["properties"]["payload"]
        self.assertIn("translation_preflight_approvals", payload["required"])
        self.assertIn("translation_preflight_approvals", payload["properties"])
        self.assertNotIn("translation_preflight_verdicts", payload["properties"])

    def test_readme_declares_cross_document_family_validation(self) -> None:
        readme = (SCHEMAS / "README.md").read_text(encoding="utf-8")
        for phrase in (
            "translation candidate",
            "translation_preflight_approval",
            "TTS attempt",
            "release approval",
            "semantic-validator requirements",
        ):
            self.assertIn(phrase, readme)

    def test_plan_declares_immutable_preflight_artifact_chain(self) -> None:
        plan = (ROOT / "docs" / "N8N_CLEAN_DESUB_PLAN.md").read_text(
            encoding="utf-8"
        )
        for phrase in (
            "pure create-only generator candidate",
            "translation_preflight_approval",
            "ObjectRef/SHA",
            "preflight approvals/TTS attempts/ledger",
        ):
            self.assertIn(phrase, plan)


if __name__ == "__main__":
    unittest.main()
