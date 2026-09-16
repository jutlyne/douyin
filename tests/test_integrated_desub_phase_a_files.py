from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path
from typing import Any

import integrated_desub_contracts as public_contracts
from integrated_desub_contracts.enums import (
    ERROR_CODE_SET,
    ERROR_SEMANTICS,
    ERROR_STAGE_SET,
    LAUNCH_STATE_SET,
    STATE_SET,
)


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "integrated_desub_contracts"
SCHEMAS = CONTRACT / "schemas" / "v1"
POLICIES = CONTRACT / "policies" / "v1"
FIXTURES = CONTRACT / "fixtures" / "v1"
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain a JSON object")
    return value


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _resolve_pointer(document: Any, fragment: str) -> Any:
    if fragment in ("", "#"):
        return document
    if not fragment.startswith("#/"):
        raise AssertionError(f"unsupported local JSON pointer: {fragment}")
    current = document
    for raw in fragment[2:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        current = current[int(token)] if isinstance(current, list) else current[token]
    return current


class SchemaCatalogTests(unittest.TestCase):
    def test_required_schemas_parse_have_unique_ids_and_local_refs_exist(self) -> None:
        required = {
            "common.schema.json",
            "taxonomy.schema.json",
            "create_job_request.schema.json",
            "create_job_response.schema.json",
            "job_status.schema.json",
            "callback_event.schema.json",
            "job_state_record.schema.json",
            "idempotency_record.schema.json",
            "source_cue_record.schema.json",
            "source_cue_manifest.schema.json",
            "clean_manifest.schema.json",
            "machine_report.schema.json",
            "qa_packet.schema.json",
            "verifier_verdict.schema.json",
            "clean_approval.schema.json",
            "clean_gate_index.schema.json",
            "dub_work_item.schema.json",
            "translation_attempt.schema.json",
            "translation_preflight_approval.schema.json",
            "tts_attempt.schema.json",
            "cue_ledger.schema.json",
            "dub_manifest.schema.json",
            "release_approval.schema.json",
            "release_index.schema.json",
        }
        files = sorted(SCHEMAS.glob("*.json"))
        self.assertTrue(required <= {path.name for path in files})
        ids: set[str] = set()
        for path in files:
            document = _json(path)
            self.assertEqual(
                document.get("$schema"),
                "https://json-schema.org/draft/2020-12/schema",
                path.name,
            )
            schema_id = document.get("$id")
            self.assertIsInstance(schema_id, str, path.name)
            self.assertNotIn(schema_id, ids, path.name)
            ids.add(schema_id)
            for node in _walk(document):
                reference = node.get("$ref")
                if not isinstance(reference, str) or reference.startswith("#"):
                    continue
                target, separator, raw_fragment = reference.partition("#")
                target_path = SCHEMAS / target
                self.assertTrue(target_path.is_file(), f"{path.name}: {target}")
                target_document = _json(target_path)
                _resolve_pointer(target_document, f"#{raw_fragment}" if separator else "")

    def test_structured_top_level_schemas_reject_unknown_fields(self) -> None:
        exempt = {"common.schema.json", "error.schema.json", "taxonomy.schema.json"}
        for path in SCHEMAS.glob("*.json"):
            if path.name in exempt:
                continue
            document = _json(path)
            self.assertEqual(document.get("type"), "object", path.name)
            self.assertIs(document.get("additionalProperties"), False, path.name)

    def test_schema_taxonomies_match_python_contract(self) -> None:
        definitions = _json(SCHEMAS / "taxonomy.schema.json")["$defs"]
        self.assertEqual(set(definitions["state"]["enum"]), STATE_SET)
        self.assertEqual(set(definitions["launchState"]["enum"]), LAUNCH_STATE_SET)
        self.assertEqual(set(definitions["errorStage"]["enum"]), ERROR_STAGE_SET)
        self.assertEqual(set(definitions["errorCode"]["enum"]), ERROR_CODE_SET)

    def test_callback_schema_freezes_exact_event_state_pairs(self) -> None:
        schema = _json(SCHEMAS / "callback_event.schema.json")
        pairs: set[tuple[str, str]] = set()
        for branch in schema["oneOf"]:
            properties = branch["allOf"][1]["properties"]
            pairs.add(
                (
                    properties["event"]["const"],
                    properties["state"]["const"],
                )
            )
        expected_states = {
            "completed",
            "clean_qa_failed",
            "translation_failed",
            "tts_failed",
            "scheduling_failed",
            "subtitle_failed",
            "dub_qa_failed",
            "failed",
            "cancelled",
        }
        self.assertEqual(
            pairs,
            {(f"desub.{state}", state) for state in expected_states},
        )

    def test_job_state_persists_the_single_clean_repair_budget(self) -> None:
        schema = _json(SCHEMAS / "job_state_record.schema.json")
        self.assertIn("clean_repair_attempts_used", schema["required"])
        counter = schema["properties"]["clean_repair_attempts_used"]
        self.assertEqual(counter, {"type": "integer", "minimum": 0, "maximum": 1})

    def test_idempotency_schema_names_exact_stored_bytes_contract(self) -> None:
        schema = _json(SCHEMAS / "idempotency_record.schema.json")
        self.assertIn("request_bytes_contract", schema["required"])
        contract = schema["properties"]["request_bytes_contract"]
        self.assertEqual(contract["const"], "stored_exact_utf8_json_v1")
        self.assertIn("does not claim", contract["$comment"])


class FrozenPolicyTests(unittest.TestCase):
    def test_error_semantics_policy_matches_executable_map_exactly(self) -> None:
        policy = _json(POLICIES / "error_semantics_policy.json")
        entries = policy["semantics_by_code"]
        self.assertEqual(set(entries), ERROR_CODE_SET)
        self.assertEqual(set(ERROR_SEMANTICS), ERROR_CODE_SET)
        for code, semantics in ERROR_SEMANTICS.items():
            expected = {
                "family": semantics["family"],
                "allowed_stages": list(semantics["allowed_stages"]),
                "retryable": semantics["retryable"],
                "public_http_status": semantics["public_http_status"],
                "terminal_state_by_stage": dict(
                    semantics["terminal_state_by_stage"]
                ),
                "context_keys": sorted(semantics["context_keys"]),
            }
            self.assertEqual(entries[code], expected, code)

    def test_input_output_scope_is_exact(self) -> None:
        policy = _json(POLICIES / "input_contract.json")
        self.assertEqual(policy["input"]["request_count"], 1)
        self.assertEqual(policy["input"]["kind"], "douyin_video_url")
        self.assertEqual(policy["input"]["max_bytes"], 1024**3)
        self.assertEqual(policy["subtitle_target"]["y_min_inclusive"], 0.55)
        self.assertEqual(policy["subtitle_target"]["y_max_inclusive"], 0.90)
        self.assertEqual(
            policy["subtitle_target"]["membership_coordinate"],
            "caption_baseline_y_normalized",
        )
        self.assertFalse(
            policy["subtitle_target"]["bbox_intersection_or_containment_is_membership"]
        )
        self.assertFalse(policy["outputs"]["internal_clean"]["public"])
        self.assertEqual(policy["outputs"]["user_artifact"]["kind"], "dubbed")
        self.assertTrue(set(policy["reject_codes"].values()) <= ERROR_CODE_SET)

    def test_dub_rate_timing_mix_and_slo_are_frozen(self) -> None:
        dub = _json(POLICIES / "dub_qa_policy.json")
        mix = _json(POLICIES / "dub_mix_policy.json")
        ops = _json(POLICIES / "retention_capacity_policy.json")
        self.assertEqual(dub["translation"]["max_compact_retries"], 2)
        self.assertEqual(dub["tts"]["voice"], "BV075_streaming")
        self.assertEqual(dub["tts"]["ssml_prosody_rate"], "1.5000")
        self.assertFalse(dub["tts"]["post_atempo_allowed"])
        self.assertEqual(dub["schedule"]["minimum_gap_samples"], 2205)
        self.assertEqual(dub["schedule"]["maximum_lag_samples"], 26460)
        self.assertEqual(mix["gains_db"]["bed_pre_duck"], "-20.000")
        self.assertEqual(mix["sidechaincompress"]["threshold"], "0.020000")
        self.assertEqual(ops["capacity"]["max_active_executions"], 2)
        self.assertEqual(ops["capacity"]["max_queued_jobs"], 10)
        self.assertEqual(ops["slo"]["execution_minutes_max"], 60)
        self.assertEqual(ops["slo"]["hard_timeout_minutes"], 90)

    def test_machine_metric_names_match_executable_aggregate_contract(self) -> None:
        from integrated_desub_contracts.validation import REQUIRED_MACHINE_METRIC_NAMES

        clean = _json(POLICIES / "clean_qa_policy.json")
        dub = _json(POLICIES / "dub_qa_policy.json")
        self.assertEqual(
            set(clean["required_machine_metric_names"]),
            set(REQUIRED_MACHINE_METRIC_NAMES["clean"]),
        )
        self.assertEqual(
            set(dub["required_machine_metric_names"]),
            set(REQUIRED_MACHINE_METRIC_NAMES["dub"]),
        )
        self.assertIn(
            "vietsub_style_sync", dub["required_machine_metric_names"]
        )
        self.assertIn(
            "mix_graph_stem_integrity",
            dub["required_machine_metric_names"],
        )

    def test_dub_manifest_and_release_use_named_stems_and_closed_metadata(self) -> None:
        manifest = _json(SCHEMAS / "dub_manifest.schema.json")
        release = _json(SCHEMAS / "release_approval.schema.json")
        required_stems = {
            "clean_input",
            "bed",
            "ducked_bed",
            "voice",
            "premaster",
        }
        manifest_stems = manifest["properties"]["stem_pcm"]
        release_stems = release["properties"]["payload"]["properties"][
            "stem_pcm"
        ]
        self.assertEqual(set(manifest_stems["required"]), required_stems)
        self.assertEqual(set(release_stems["required"]), required_stems)
        self.assertIs(manifest_stems["additionalProperties"], False)
        self.assertIs(release_stems["additionalProperties"], False)
        self.assertTrue(
            {"output_video", "output_audio", "subtitle_render"}
            <= set(manifest["required"])
        )
        for name in ("output_video", "output_audio", "subtitle_render"):
            self.assertIs(
                manifest["properties"][name]["additionalProperties"],
                False,
                name,
            )

    def test_unproven_bindings_remain_explicitly_blocked(self) -> None:
        for name in (
            "clean_qa_policy.json",
            "dub_qa_policy.json",
            "dub_mix_policy.json",
            "vietsub_style_policy.json",
            "model_role_policy.json",
            "kms_allowlist.json",
        ):
            policy = _json(POLICIES / name)
            self.assertTrue(policy["status"].startswith("blocked"), name)
            self.assertTrue(policy["unresolved_gate_a_bindings"], name)

    def test_kms_allowlist_is_intentionally_empty(self) -> None:
        policy = _json(POLICIES / "kms_allowlist.json")
        self.assertEqual(policy["status"], "blocked_empty_allowlist")
        self.assertEqual(policy["algorithm"], "EC_SIGN_P256_SHA256")
        self.assertEqual(policy["approved_key_versions"], [])
        self.assertEqual(policy["approved_public_key_sha256"], [])

    def test_hash_preimages_exclude_ephemeral_capcut_identity(self) -> None:
        policy = _json(POLICIES / "hash_preimage_contracts.json")
        tts = policy["contracts"]["normalized_capcut_tts_request_sha256"]
        self.assertIn("prosody_rate", tts["ordered_semantic_fields"])
        self.assertIn("text_vi_nfc", tts["ordered_semantic_fields"])
        self.assertIn("device_identity", tts["excluded_ephemeral_fields"])
        self.assertIn("request_signature", tts["excluded_ephemeral_fields"])

    def test_source_cue_root_preimage_exclusions_match_builder(self) -> None:
        policy = _json(POLICIES / "source_cue_collection_root_contract.json")
        self.assertIn("attempt", policy["excluded_manifest_fields"])

    def test_schema_format_assertions_are_mandatory(self) -> None:
        readme = (SCHEMAS / "README.md").read_text(encoding="utf-8")
        self.assertIn("`format`", readme)
        self.assertIn("assertions enabled", readme)
        self.assertIn("annotation-only", readme)

    def test_qa_runtime_roles_are_all_unbound_until_proven(self) -> None:
        policy = _json(POLICIES / "qa_runtime_bindings.json")
        self.assertEqual(policy["status"], "blocked_no_approved_production_runtime")
        self.assertEqual(policy["required_processing_jurisdiction"], "Singapore")
        self.assertEqual(
            policy["candidate_endpoint_identifiers_not_approved"]["google_vertex"],
            "asia-southeast1",
        )
        self.assertEqual(
            policy["candidate_endpoint_identifiers_not_approved"]["openai_regional_storage"],
            "sg.api.openai.com",
        )
        self.assertTrue(policy["roles"])
        self.assertTrue(all(binding is None for binding in policy["roles"].values()))

    def test_audio_video_role_requires_direct_modalities(self) -> None:
        policy = _json(POLICIES / "model_role_policy.json")
        self.assertEqual(
            policy["required_input_modalities_by_role"]["dub_audio_video"],
            ["json", "audio", "video"],
        )
        self.assertIn("direct input modality", policy["direct_modality_claim_rule"])
        self.assertIn(
            "audio_video_capable_runtime_for_dub_audio_video_or_approved_contract_revision",
            policy["unresolved_gate_a_bindings"],
        )


class GoldenAndCanaryTests(unittest.TestCase):
    def test_golden_utf8_hex_and_sha_are_self_consistent(self) -> None:
        fixture = _json(FIXTURES / "jcs_golden_vectors.json")
        for vector in fixture["vectors"]:
            data = vector["canonical_utf8"].encode("utf-8")
            self.assertEqual(data.hex(), vector["canonical_utf8_hex"], vector["name"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), vector["sha256"], vector["name"])

        root = _json(FIXTURES / "source_cue_root_golden.json")
        canonical = root["canonical_utf8"].encode("utf-8")
        self.assertEqual(json.loads(root["canonical_utf8"]), root["preimage"])
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), root["sha256"])

    def test_canary_suite_cannot_be_misreported_as_ready(self) -> None:
        suite = _json(FIXTURES / "canary_fixture_suite.json")
        self.assertEqual(suite["status"], "blocked_incomplete_fixture_evidence")
        self.assertEqual(len(suite["fixtures"]), 3)
        self.assertTrue(all(item["status"].startswith("blocked") for item in suite["fixtures"]))
        self.assertTrue(suite["suite_level_blockers"])

    def test_cloud_run_template_is_not_misreported_as_executed(self) -> None:
        record = _json(FIXTURES / "cloud_run_validate_only_template.json")
        self.assertEqual(record["status"], "template_only_not_executed")
        self.assertIs(record["request"]["validateOnly"], True)
        self.assertIsNone(record["observed_execution_name"])
        names = {
            item["name"]
            for item in record["request"]["overrides"]["containerOverrides"][0]["env"]
        }
        self.assertEqual(
            names,
            {"DESUB_LAUNCH_TOKEN", "DESUB_JOB_ID", "DESUB_ATTEMPT_ID", "DESUB_ATTEMPT_SEQ"},
        )

    def test_present_local_fixture_hashes_match_manifest(self) -> None:
        suite = _json(FIXTURES / "canary_fixture_suite.json")
        for fixture in suite["fixtures"]:
            relative = fixture.get("local_path")
            if not relative:
                continue
            path = ROOT / relative
            if not path.is_file():
                continue
            expected = (fixture.get("source_object") or fixture.get("local_file"))["sha256"]
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(digest, expected, fixture["fixture_id"])


class GateStatusTests(unittest.TestCase):
    def test_structural_helpers_are_not_public_authorization_apis(self) -> None:
        unsafe_names = {
            "validate_authorized_dub_work_item",
            "validate_authorized_tts_attempt",
            "validate_candidate_tts_family",
            "validate_candidate_tts_family_structure",
            "validate_clean_authorization_bundles",
            "validate_clean_authorization_structure",
            "validate_clean_evidence_bindings_structure",
            "validate_clean_gate_index_structure",
            "validate_cue_ledger_structure",
            "validate_dub_work_item_authority_structure",
            "validate_dub_work_item_structure",
            "validate_release_bundle_structure",
            "validate_release_evidence_structure",
            "validate_release_index_structure",
            "validate_translation_preflight_approval",
            "validate_translation_preflight_approval_structure",
            "validate_tts_attempt_chain_structure",
        }
        self.assertTrue(
            unsafe_names.isdisjoint(set(public_contracts.__all__))
        )
        self.assertTrue(
            all(
                not hasattr(public_contracts, name)
                for name in unsafe_names
            )
        )

    def test_gate_is_fail_closed_and_blocks_phase_b(self) -> None:
        status = _json(CONTRACT / "phase_a_gate_status.json")
        self.assertEqual(status["gate"], "A")
        self.assertEqual(status["status"], "blocked")
        self.assertIs(status["fail_closed"], True)
        self.assertIn("start_phase_b", status["prohibited_while_blocked"])
        self.assertGreaterEqual(len(status["blockers"]), 7)


class ContractCatalogTests(unittest.TestCase):
    def test_catalog_hashes_exact_phase_a_files(self) -> None:
        catalog = _json(CONTRACT / "contract_catalog.json")
        self.assertEqual(catalog["phase"], "A")
        self.assertEqual(catalog["status"], "blocked_not_approved")
        self.assertEqual(
            catalog["hash_contract"], "sha256_of_exact_raw_file_bytes"
        )
        entries = catalog["entries"]
        paths = [entry["path"] for entry in entries]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertNotIn(
            "integrated_desub_contracts/contract_catalog.json", paths
        )
        expected = {
            ".gitattributes",
            "docs/DESUB_CLOUD_RUN_DRY_RUN.md",
            "docs/DESUB_DEPENDENCY_SECURITY_GATE.md",
            "docs/DESUB_KMS_RUNBOOK.md",
            "docs/DESUB_QA_RUNTIME_DECISION.md",
            "docs/N8N_CLEAN_DESUB_PLAN.md",
            "docs/DESUB_V1_CONTRACT_ADR.md",
        }
        expected.update(
            path.relative_to(ROOT).as_posix()
            for path in CONTRACT.rglob("*")
            if path.is_file()
            and path.name != "contract_catalog.json"
            and "__pycache__" not in path.parts
            and path.suffix != ".pyc"
        )
        expected.update(
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "tests").glob(
                "test_integrated_desub_*.py"
            )
        )
        self.assertEqual(set(paths), expected)
        for entry in entries:
            self.assertRegex(entry["sha256"], SHA_RE)
            self.assertNotIn("\\", entry["path"])
            path = ROOT / Path(entry["path"])
            self.assertTrue(path.is_file(), entry["path"])
            data = path.read_bytes()
            self.assertEqual(len(data), entry["size_bytes"], entry["path"])
            self.assertEqual(
                hashlib.sha256(data).hexdigest(),
                entry["sha256"],
                entry["path"],
            )


if __name__ == "__main__":
    unittest.main()
