from __future__ import annotations

import copy
import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from integrated_desub_contracts.candidate_validation import (
    validate_candidate_tts_family_structure,
    validate_tts_attempt_chain_structure,
    validate_translation_candidate,
    validate_translation_preflight_approval_structure,
    verify_stored_json_bundle,
)
from integrated_desub_contracts.validation import ContractValidationError


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _timestamp(minutes: int) -> str:
    value = datetime(2026, 7, 23, tzinfo=timezone.utc) + timedelta(minutes=minutes)
    return value.isoformat().replace("+00:00", "Z")


def _bundle(document: dict[str, Any], label: str) -> dict[str, Any]:
    object_bytes = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        "object_ref": {
            "uri": f"gs://desub-contract-test/{label}.json",
            "generation": str(int.from_bytes(hashlib.sha256(label.encode()).digest()[:4], "big") + 1),
            "size_bytes": len(object_bytes),
            "sha256": hashlib.sha256(object_bytes).hexdigest(),
            "content_type": "application/json",
        },
        "document": document,
        "object_bytes": object_bytes,
    }


def _object_ref(label: str, content_type: str) -> dict[str, Any]:
    return {
        "uri": f"gs://desub-contract-test/{label}",
        "generation": str(int.from_bytes(hashlib.sha256(label.encode()).digest()[:4], "big") + 1),
        "size_bytes": 100 + len(label),
        "sha256": _sha(label),
        "content_type": content_type,
    }


ATTEMPT = {
    "job_id": f"desub-{'a' * 32}",
    "attempt_id": f"att-{'b' * 32}",
    "attempt_seq": 1,
    "fence_digest": _sha("fence"),
}
TRANSLATION_POLICY_SHA = _sha("translation-policy")
SEMANTIC_POLICY_SHA = _sha("semantic-policy")
STYLE_POLICY_SHA = _sha("style-policy")


def _model(kind: str) -> dict[str, Any]:
    return {
        "provider": f"{kind}-provider",
        "service_identity": f"{kind}-service",
        "model_id": f"{kind}-model",
        "model_version": "2026-07-23",
        "endpoint_region": "asia-southeast1",
        "adapter_sha256": _sha(f"{kind}-adapter"),
        "response_id": f"{kind}-response",
    }


def _source_bundle() -> dict[str, Any]:
    return _bundle(
        {
            "schema_version": "1",
            "attempt": copy.deepcopy(ATTEMPT),
            "cue_id": "cue-000000",
            "cue_index": 0,
            "source_sha256": _sha("source-video"),
            "source_audio": {
                "sample_rate_hz": 44100,
                "speech_start_sample": 10000,
            },
            "created_at": _timestamp(0),
        },
        "source-cue",
    )


def _candidate_bundle(
    source: dict[str, Any],
    index: int = 0,
    *,
    created_minute: int | None = None,
) -> dict[str, Any]:
    return _bundle(
        {
            "schema_version": "1",
            "attempt": copy.deepcopy(ATTEMPT),
            "cue_id": "cue-000000",
            "cue_index": 0,
            "source_cue_record": copy.deepcopy(source["object_ref"]),
            "source_cue_record_sha256": source["object_ref"]["sha256"],
            "candidate_index": index,
            "reason": "initial" if index == 0 else "compact_unfit",
            "text_vi_raw": f"Ứng viên số {index}",
            "text_vi_nfc": f"Ứng viên số {index}",
            "generator": _model("generator"),
            "generator_prompt_sha256": _sha("generator-prompt"),
            "candidate_sha256": _sha(f"candidate-semantic-{index}"),
            "created_at": _timestamp(
                created_minute if created_minute is not None else 1 + index * 5
            ),
        },
        f"candidate-{index}",
    )


def _verdict_bundle(
    source: dict[str, Any],
    candidate: dict[str, Any],
    *,
    role: str,
    created_minute: int,
) -> dict[str, Any]:
    kind = "semantic" if role == "translation_semantic_preflight" else "style"
    policy_sha = SEMANTIC_POLICY_SHA if kind == "semantic" else STYLE_POLICY_SHA
    return _bundle(
        {
            "schema_version": "1",
            "role": role,
            "attempt": copy.deepcopy(ATTEMPT),
            "policy": {
                "name": f"{kind}_preflight_policy",
                "version": "1.0.0",
                "sha256": policy_sha,
            },
            "model": _model(kind),
            "bindings": {
                "source_sha256": source["document"]["source_sha256"],
                "clean_sha256": _sha("clean"),
                "source_cue_manifest_sha256": _sha("source-manifest"),
                "source_cue_collection_root": _sha("collection-root"),
                "policy_sha256": policy_sha,
                "cue_id": candidate["document"]["cue_id"],
                "source_cue_record_sha256": source["object_ref"]["sha256"],
                "translation_attempt_sha256": candidate["object_ref"]["sha256"],
                "candidate_sha256": candidate["document"]["candidate_sha256"],
            },
            "decision": "pass",
            "findings": [],
            "created_at": _timestamp(created_minute),
        },
        f"{kind}-verdict-{candidate['document']['candidate_index']}",
    )


def _approval_bundle(
    source: dict[str, Any],
    candidate: dict[str, Any],
    semantic: dict[str, Any],
    style: dict[str, Any],
    *,
    issued_minute: int,
    expires_minute: int | None = None,
) -> dict[str, Any]:
    index = candidate["document"]["candidate_index"]
    return _bundle(
        {
            "payload": {
                "schema_version": "1",
                "translation_preflight_approval_id": f"tpa-{index:032x}",
                "issued_at": _timestamp(issued_minute),
                "expires_at": _timestamp(
                    expires_minute
                    if expires_minute is not None
                    else issued_minute + 60
                ),
                "attempt": copy.deepcopy(ATTEMPT),
                "cue_id": "cue-000000",
                "cue_index": 0,
                "source_cue_record": copy.deepcopy(source["object_ref"]),
                "source_cue_record_sha256": source["object_ref"]["sha256"],
                "translation_candidate": copy.deepcopy(candidate["object_ref"]),
                "translation_candidate_object_sha256": candidate["object_ref"][
                    "sha256"
                ],
                "candidate_index": index,
                "candidate_sha256": candidate["document"]["candidate_sha256"],
                "translation_policy_sha256": TRANSLATION_POLICY_SHA,
                "semantic_policy_sha256": SEMANTIC_POLICY_SHA,
                "style_policy_sha256": STYLE_POLICY_SHA,
                "semantic_verdict": {
                    "role": "translation_semantic_preflight",
                    "object": copy.deepcopy(semantic["object_ref"]),
                    "decision": "pass",
                },
                "style_verdict": {
                    "role": "translation_style_preflight",
                    "object": copy.deepcopy(style["object_ref"]),
                    "decision": "pass",
                },
                "decision": "pass",
                "controller_signer_principal": (
                    "serviceAccount:qa-controller@desub.iam.gserviceaccount.com"
                ),
            },
            "signature": {
                "algorithm": "EC_SIGN_P256_SHA256",
                "kms_key_version": (
                    "projects/desub/locations/asia-southeast1/keyRings/qa/"
                    "cryptoKeys/approval/cryptoKeyVersions/1"
                ),
                "public_key_sha256": _sha("public-key"),
                "signed_digest": _sha(f"approval-payload-{index}"),
                "signature_b64": "c2ln",
            },
        },
        f"approval-{index}",
    )


def _active_detector() -> dict[str, Any]:
    return {
        "sample_rate_hz": 44100,
        "decoded_sample_count": 5000,
        "window_samples": 882,
        "hop_samples": 441,
        "tail_window_padding": "right_zero_pad",
        "rms_denominator_samples": 882,
        "active_threshold_dbfs": -45,
        "active_threshold_linear": 0.005623413251903491,
        "consecutive_windows": 2,
        "active_runs": [
            {"onset_sample": 100, "offset_sample_inclusive": 1000}
        ],
        "onset_sample": 100,
        "offset_sample_inclusive": 1000,
        "implementation_sha256": _sha("silence-detector"),
    }


def _inactive_detector() -> dict[str, Any]:
    result = _active_detector()
    result.update(
        {
            "active_runs": [],
            "onset_sample": None,
            "offset_sample_inclusive": None,
        }
    )
    return result


def _schedule(*, fits: bool) -> dict[str, Any]:
    return {
        "speech_anchor_sample": 10000,
        "previous_actual_offset_sample_inclusive": None,
        "target_actual_onset_sample": 10000,
        "video_sample_count": 20000 if fits else 10500,
        "placement_sample": 9900,
        "actual_onset_sample": 10000,
        "actual_offset_sample_inclusive": 10900,
        "lag_samples": 0,
        "previous_gap_samples": None,
        "overlap_samples": 0,
        "fits": fits,
    }


def _tts_bundle(
    candidate: dict[str, Any],
    approval: dict[str, Any],
    *,
    outcome: str = "succeeded",
    created_minute: int = 5,
) -> dict[str, Any]:
    index = candidate["document"]["candidate_index"]
    document: dict[str, Any] = {
        "schema_version": "1",
        "attempt": copy.deepcopy(ATTEMPT),
        "cue_id": "cue-000000",
        "cue_index": 0,
        "candidate_index": index,
        "translation_candidate": copy.deepcopy(candidate["object_ref"]),
        "translation_candidate_object_sha256": candidate["object_ref"]["sha256"],
        "translation_candidate_sha256": candidate["document"]["candidate_sha256"],
        "translation_preflight_approval": copy.deepcopy(approval["object_ref"]),
        "translation_preflight_approval_sha256": approval["object_ref"]["sha256"],
        "provider": "capcut-private",
        "voice": "BV075_streaming",
        "prosody_rate": "1.5000",
        "normalized_request_sha256": _sha(f"tts-request-{index}"),
        "provider_response_id": f"tts-response-{index}",
        "outcome": outcome,
        "created_at": _timestamp(created_minute),
    }
    if outcome == "provider_failed":
        document["provider_response_id"] = None
        document["error_code"] = "TTS_PROVIDER_FAILED"
    elif outcome == "media_invalid":
        document["error_code"] = "TTS_MEDIA_INVALID"
        document["returned_media"] = _object_ref(
            f"returned-{index}.mp3", "audio/mpeg"
        )
    elif outcome == "no_active_speech":
        document["error_code"] = "TTS_MEDIA_INVALID"
        document["returned_media"] = _object_ref(
            f"returned-{index}.mp3", "audio/mpeg"
        )
        document["decoded_pcm"] = _object_ref(
            f"decoded-{index}.f32le", "application/octet-stream"
        )
        document["silence_detector"] = _inactive_detector()
    elif outcome in {"succeeded", "schedule_unfit"}:
        document["returned_media"] = _object_ref(
            f"returned-{index}.mp3", "audio/mpeg"
        )
        document["decoded_pcm"] = _object_ref(
            f"decoded-{index}.f32le", "application/octet-stream"
        )
        document["silence_detector"] = _active_detector()
        document["schedule"] = _schedule(fits=outcome == "succeeded")
        if outcome == "schedule_unfit":
            document["error_code"] = (
                "DUB_CUE_UNFIT" if index == 2 else "DUB_SCHEDULE_INVALID"
            )
    return _bundle(document, f"tts-{index}-{outcome}")


def _chain(
    index: int = 0,
    *,
    candidate_minute: int | None = None,
    verdict_start_minute: int | None = None,
    approval_minute: int | None = None,
    tts_minute: int | None = None,
    outcome: str = "succeeded",
) -> dict[str, Any]:
    source = _source_bundle()
    candidate_minute = (
        candidate_minute if candidate_minute is not None else 1 + index * 5
    )
    verdict_start_minute = (
        verdict_start_minute
        if verdict_start_minute is not None
        else candidate_minute + 1
    )
    approval_minute = (
        approval_minute
        if approval_minute is not None
        else verdict_start_minute + 2
    )
    tts_minute = tts_minute if tts_minute is not None else approval_minute + 1
    candidate = _candidate_bundle(
        source,
        index,
        created_minute=candidate_minute,
    )
    semantic = _verdict_bundle(
        source,
        candidate,
        role="translation_semantic_preflight",
        created_minute=verdict_start_minute,
    )
    style = _verdict_bundle(
        source,
        candidate,
        role="translation_style_preflight",
        created_minute=verdict_start_minute + 1,
    )
    approval = _approval_bundle(
        source,
        candidate,
        semantic,
        style,
        issued_minute=approval_minute,
    )
    tts = _tts_bundle(
        candidate,
        approval,
        outcome=outcome,
        created_minute=tts_minute,
    )
    return {
        "source": source,
        "candidate": candidate,
        "semantic": semantic,
        "style": style,
        "approval": approval,
        "tts": tts,
    }


def _approval_kwargs(chain: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_bundle_value": chain["candidate"],
        "source_cue_bundle_value": chain["source"],
        "semantic_verdict_bundle_value": chain["semantic"],
        "style_verdict_bundle_value": chain["style"],
        "expected_translation_policy_sha256": TRANSLATION_POLICY_SHA,
        "expected_semantic_policy_sha256": SEMANTIC_POLICY_SHA,
        "expected_style_policy_sha256": STYLE_POLICY_SHA,
        "signature_verified": True,
    }


def _tts_kwargs(chain: dict[str, Any]) -> dict[str, Any]:
    result = _approval_kwargs(chain)
    result["approval_bundle_value"] = chain["approval"]
    return result


class StoredBundleAndCandidateTests(unittest.TestCase):
    def test_exact_bundle_checks_size_sha_and_decoded_json(self) -> None:
        bundle = _bundle({"answer": 42}, "simple")
        result = verify_stored_json_bundle(bundle)
        self.assertEqual(result["document"], {"answer": 42})

        cases = []
        wrong_size = copy.deepcopy(bundle)
        wrong_size["object_ref"]["size_bytes"] += 1
        cases.append(wrong_size)
        wrong_sha = copy.deepcopy(bundle)
        wrong_sha["object_ref"]["sha256"] = _sha("wrong")
        cases.append(wrong_sha)
        wrong_document = copy.deepcopy(bundle)
        wrong_document["document"]["answer"] = 43
        cases.append(wrong_document)
        for value in cases:
            with self.subTest(value=value["object_ref"]):
                with self.assertRaises(ContractValidationError):
                    verify_stored_json_bundle(value)

    def test_bundle_rejects_duplicate_keys(self) -> None:
        object_bytes = b'{"value":1,"value":2}'
        bundle = {
            "object_ref": {
                "uri": "gs://desub-contract-test/duplicate.json",
                "generation": "1",
                "size_bytes": len(object_bytes),
                "sha256": hashlib.sha256(object_bytes).hexdigest(),
                "content_type": "application/json",
            },
            "document": {"value": 2},
            "object_bytes": object_bytes,
        }
        with self.assertRaisesRegex(ContractValidationError, "duplicate"):
            verify_stored_json_bundle(bundle)

    def test_bundle_rejects_python_bool_integer_equality_alias(self) -> None:
        bundle = _bundle({"value": 1}, "typed-json")
        bundle["document"]["value"] = True
        with self.assertRaisesRegex(ContractValidationError, "decoded document"):
            verify_stored_json_bundle(bundle)

    def test_candidate_cross_binds_source_attempt_cue_nfc_and_object_ref(self) -> None:
        chain = _chain()
        result = validate_translation_candidate(
            chain["candidate"],
            source_cue_bundle_value=chain["source"],
        )
        self.assertEqual(result["candidate_index"], 0)
        self.assertEqual(
            result["candidate_object_sha256"],
            chain["candidate"]["object_ref"]["sha256"],
        )

        tampered_docs = []
        wrong_source = copy.deepcopy(chain["candidate"]["document"])
        wrong_source["source_cue_record"]["generation"] = "999"
        tampered_docs.append(wrong_source)
        wrong_attempt = copy.deepcopy(chain["candidate"]["document"])
        wrong_attempt["attempt"]["attempt_seq"] = 2
        tampered_docs.append(wrong_attempt)
        wrong_nfc = copy.deepcopy(chain["candidate"]["document"])
        wrong_nfc["text_vi_nfc"] = "khác"
        tampered_docs.append(wrong_nfc)
        for index, document in enumerate(tampered_docs):
            with self.subTest(index=index):
                with self.assertRaises(ContractValidationError):
                    validate_translation_candidate(
                        _bundle(document, f"tampered-candidate-{index}"),
                        source_cue_bundle_value=chain["source"],
                    )


class ApprovalTests(unittest.TestCase):
    def test_approval_cross_binds_transitive_evidence_and_requires_signature(self) -> None:
        chain = _chain()
        result = validate_translation_preflight_approval_structure(
            chain["approval"],
            **_approval_kwargs(chain),
        )
        self.assertEqual(result["payload"]["decision"], "pass")
        self.assertEqual(
            result["candidate_context"]["candidate_object_sha256"],
            chain["candidate"]["object_ref"]["sha256"],
        )

        kwargs = _approval_kwargs(chain)
        kwargs["signature_verified"] = False
        with self.assertRaisesRegex(ContractValidationError, "signature_verified"):
            validate_translation_preflight_approval_structure(
                chain["approval"], **kwargs
            )

    def test_approval_rejects_self_verification_binding_and_policy_tamper(self) -> None:
        chain = _chain()
        semantic_doc = copy.deepcopy(chain["semantic"]["document"])
        semantic_doc["model"] = copy.deepcopy(
            chain["candidate"]["document"]["generator"]
        )
        tampered_semantic = _bundle(semantic_doc, "self-verifying-semantic")
        approval_doc = copy.deepcopy(chain["approval"]["document"])
        approval_doc["payload"]["semantic_verdict"]["object"] = copy.deepcopy(
            tampered_semantic["object_ref"]
        )
        tampered_approval = _bundle(approval_doc, "self-verifying-approval")
        kwargs = _approval_kwargs(chain)
        kwargs["semantic_verdict_bundle_value"] = tampered_semantic
        with self.assertRaisesRegex(ContractValidationError, "identities"):
            validate_translation_preflight_approval_structure(
                tampered_approval, **kwargs
            )

        with self.assertRaisesRegex(ContractValidationError, "policy"):
            validate_translation_preflight_approval_structure(
                chain["approval"],
                **{
                    **_approval_kwargs(chain),
                    "expected_semantic_policy_sha256": _sha("other-policy"),
                },
            )

    def test_approval_rejects_verdict_candidate_tamper_and_long_window(self) -> None:
        chain = _chain()
        verdict_doc = copy.deepcopy(chain["semantic"]["document"])
        verdict_doc["bindings"]["candidate_sha256"] = _sha("other-candidate")
        verdict = _bundle(verdict_doc, "wrong-candidate-verdict")
        approval_doc = copy.deepcopy(chain["approval"]["document"])
        approval_doc["payload"]["semantic_verdict"]["object"] = copy.deepcopy(
            verdict["object_ref"]
        )
        approval = _bundle(approval_doc, "wrong-candidate-approval")
        kwargs = _approval_kwargs(chain)
        kwargs["semantic_verdict_bundle_value"] = verdict
        with self.assertRaisesRegex(ContractValidationError, "candidate SHA"):
            validate_translation_preflight_approval_structure(
                approval, **kwargs
            )

        long_doc = copy.deepcopy(chain["approval"]["document"])
        long_doc["payload"]["expires_at"] = (
            datetime(2026, 8, 1, tzinfo=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        with self.assertRaisesRegex(ContractValidationError, "seven days"):
            validate_translation_preflight_approval_structure(
                _bundle(long_doc, "long-approval"),
                **_approval_kwargs(chain),
            )


class AuthorizedTtsTests(unittest.TestCase):
    def test_success_cross_binds_candidate_approval_and_timing(self) -> None:
        chain = _chain()
        result = validate_tts_attempt_chain_structure(
            chain["tts"],
            **_tts_kwargs(chain),
        )
        self.assertEqual(result["outcome"], "succeeded")
        self.assertEqual(result["timing"]["actual_offset_sample_inclusive"], 10900)

        document = copy.deepcopy(chain["tts"]["document"])
        document["translation_candidate_object_sha256"] = _sha("other-object")
        with self.assertRaisesRegex(ContractValidationError, "object SHA"):
            validate_tts_attempt_chain_structure(
                _bundle(document, "wrong-tts-candidate"),
                **_tts_kwargs(chain),
            )

    def test_tts_rejects_expired_approval_and_timing_tamper(self) -> None:
        chain = _chain()
        expired_tts_doc = copy.deepcopy(chain["tts"]["document"])
        expired_tts_doc["created_at"] = _timestamp(100)
        with self.assertRaisesRegex(ContractValidationError, "approval window"):
            validate_tts_attempt_chain_structure(
                _bundle(expired_tts_doc, "expired-tts"),
                **_tts_kwargs(chain),
            )

        timing_doc = copy.deepcopy(chain["tts"]["document"])
        timing_doc["schedule"]["actual_offset_sample_inclusive"] += 1
        with self.assertRaisesRegex(ContractValidationError, "offset equation"):
            validate_tts_attempt_chain_structure(
                _bundle(timing_doc, "timing-tamper"),
                **_tts_kwargs(chain),
            )

        wrong_anchor = copy.deepcopy(chain["tts"]["document"])
        wrong_anchor["schedule"]["speech_anchor_sample"] += 1
        with self.assertRaisesRegex(ContractValidationError, "source cue"):
            validate_tts_attempt_chain_structure(
                _bundle(wrong_anchor, "wrong-source-anchor"),
                **_tts_kwargs(chain),
            )

    def test_outcome_matrix_accepts_each_frozen_failure_shape(self) -> None:
        for outcome in (
            "provider_failed",
            "media_invalid",
            "no_active_speech",
            "schedule_unfit",
        ):
            with self.subTest(outcome=outcome):
                chain = _chain(outcome=outcome)
                result = validate_tts_attempt_chain_structure(
                    chain["tts"],
                    **_tts_kwargs(chain),
                )
                self.assertEqual(result["outcome"], outcome)

        chain = _chain()
        mismatch_doc = copy.deepcopy(chain["tts"]["document"])
        mismatch_doc["outcome"] = "media_invalid"
        mismatch_doc["error_code"] = "TTS_VOICE_RATE_MISMATCH"
        mismatch_doc.pop("schedule")
        result = validate_tts_attempt_chain_structure(
            _bundle(mismatch_doc, "voice-rate-mismatch"),
            **_tts_kwargs(chain),
        )
        self.assertEqual(result["outcome"], "media_invalid")

    def test_outcome_matrix_rejects_forbidden_evidence_and_error(self) -> None:
        provider_chain = _chain(outcome="provider_failed")
        provider_doc = copy.deepcopy(provider_chain["tts"]["document"])
        provider_doc["returned_media"] = _object_ref("should-not-exist.mp3", "audio/mpeg")
        with self.assertRaisesRegex(ContractValidationError, "media evidence"):
            validate_tts_attempt_chain_structure(
                _bundle(provider_doc, "provider-with-media"),
                **_tts_kwargs(provider_chain),
            )

        media_chain = _chain(outcome="media_invalid")
        media_doc = copy.deepcopy(media_chain["tts"]["document"])
        media_doc["decoded_pcm"] = _object_ref(
            "should-not-decode.f32le", "application/octet-stream"
        )
        with self.assertRaisesRegex(ContractValidationError, "decoded"):
            validate_tts_attempt_chain_structure(
                _bundle(media_doc, "undecodable-with-pcm"),
                **_tts_kwargs(media_chain),
            )

        unfit_chain = _chain(outcome="schedule_unfit")
        unfit_doc = copy.deepcopy(unfit_chain["tts"]["document"])
        unfit_doc["error_code"] = "DUB_CUE_UNFIT"
        with self.assertRaisesRegex(ContractValidationError, "failure code"):
            validate_tts_attempt_chain_structure(
                _bundle(unfit_doc, "early-cue-unfit"),
                **_tts_kwargs(unfit_chain),
            )


class CandidateFamilyTests(unittest.TestCase):
    def _three_candidate_family(
        self,
        final_outcome: str = "succeeded",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        chains = [
            _chain(
                0,
                candidate_minute=1,
                verdict_start_minute=2,
                approval_minute=4,
                tts_minute=5,
                outcome="schedule_unfit",
            ),
            _chain(
                1,
                candidate_minute=6,
                verdict_start_minute=7,
                approval_minute=9,
                tts_minute=10,
                outcome="schedule_unfit",
            ),
            _chain(
                2,
                candidate_minute=11,
                verdict_start_minute=12,
                approval_minute=14,
                tts_minute=15,
                outcome=final_outcome,
            ),
        ]
        candidates = [item["candidate"] for item in chains]
        evidence = [
            {
                "approval_bundle": item["approval"],
                "semantic_verdict_bundle": item["semantic"],
                "style_verdict_bundle": item["style"],
                "signature_verified": True,
            }
            for item in chains
        ]
        tts = [item["tts"] for item in chains]
        return candidates, evidence, tts

    def _validate(
        self,
        candidates: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
        tts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return validate_candidate_tts_family_structure(
            source_cue_bundle_value=_source_bundle(),
            candidate_bundle_values=candidates,
            preflight_evidence_values=evidence,
            tts_bundle_values=tts,
            expected_translation_policy_sha256=TRANSLATION_POLICY_SHA,
            expected_semantic_policy_sha256=SEMANTIC_POLICY_SHA,
            expected_style_policy_sha256=STYLE_POLICY_SHA,
        )

    def test_family_allows_two_unfit_retries_then_success(self) -> None:
        candidates, evidence, tts = self._three_candidate_family()
        result = self._validate(candidates, evidence, tts)
        self.assertEqual(result["candidate_count"], 3)
        self.assertEqual(result["terminal_outcome"], "succeeded")

    def test_family_allows_only_final_candidate_two_cue_unfit(self) -> None:
        candidates, evidence, tts = self._three_candidate_family(
            final_outcome="schedule_unfit"
        )
        result = self._validate(candidates, evidence, tts)
        self.assertEqual(result["terminal_error_code"], "DUB_CUE_UNFIT")

        chain = _chain(outcome="schedule_unfit")
        with self.assertRaisesRegex(ContractValidationError, "exhaust"):
            self._validate(
                [chain["candidate"]],
                [
                    {
                        "approval_bundle": chain["approval"],
                        "semantic_verdict_bundle": chain["semantic"],
                        "style_verdict_bundle": chain["style"],
                        "signature_verified": True,
                    }
                ],
                [chain["tts"]],
            )

    def test_family_rejects_continuation_after_terminal_and_missing_one_to_one(self) -> None:
        candidates, evidence, tts = self._three_candidate_family()
        terminal_first_doc = copy.deepcopy(tts[0]["document"])
        terminal_first_doc["outcome"] = "succeeded"
        terminal_first_doc.pop("error_code")
        terminal_first_doc["schedule"] = _schedule(fits=True)
        tts[0] = _bundle(terminal_first_doc, "terminal-first")
        with self.assertRaisesRegex(ContractValidationError, "terminal"):
            self._validate(candidates, evidence, tts)

        candidates, evidence, tts = self._three_candidate_family()
        with self.assertRaisesRegex(ContractValidationError, "exactly one"):
            self._validate(candidates, evidence, tts[:-1])

    def test_family_rejects_noncontiguous_candidates(self) -> None:
        candidates, evidence, tts = self._three_candidate_family()
        with self.assertRaisesRegex(ContractValidationError, "contiguous"):
            self._validate(
                [candidates[0], candidates[2]],
                [evidence[0], evidence[2]],
                [tts[0], tts[2]],
            )


if __name__ == "__main__":
    unittest.main()
