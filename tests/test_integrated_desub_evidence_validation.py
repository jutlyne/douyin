from __future__ import annotations

import copy
import hashlib
import json
import unittest
from typing import Any

from integrated_desub_contracts.evidence_validation import (
    validate_clean_authorization_structure,
    validate_dub_work_item_authority_structure,
    validate_qa_packet_bundles,
    validate_release_evidence_structure,
    validate_stored_bundle,
)
from integrated_desub_contracts.validation import (
    REQUIRED_MACHINE_METRIC_NAMES,
    ContractValidationError,
)


NOW = "2026-07-23T00:00:00Z"
LATER = "2026-07-24T00:00:00Z"


def _sha(value: str | bytes) -> str:
    data = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _attempt() -> dict[str, object]:
    return {
        "job_id": "desub-0123456789abcdef0123456789abcdef",
        "attempt_id": "att-0123456789abcdef0123456789abcdef",
        "attempt_seq": 1,
        "fence_digest": _sha("fence"),
    }


def _policy(name: str) -> dict[str, str]:
    return {
        "name": name,
        "version": "1.0.0",
        "sha256": _sha(name),
    }


def _model(role: str) -> dict[str, object]:
    return {
        "provider": "openai",
        "service_identity": (
            "serviceAccount:qa@desub.iam.gserviceaccount.com"
        ),
        "model_id": role,
        "model_version": "2026-07-23",
        "endpoint_region": "global",
        "adapter_sha256": _sha(f"{role}-adapter"),
        "response_id": f"response-{role}",
    }


def _signature() -> dict[str, str]:
    return {
        "algorithm": "EC_SIGN_P256_SHA256",
        "kms_key_version": (
            "projects/desub/locations/global/keyRings/release/"
            "cryptoKeys/approval/cryptoKeyVersions/1"
        ),
        "public_key_sha256": _sha("public-key"),
        "signed_digest": _sha("signed-payload"),
        "signature_b64": "YWJj",
    }


def _placeholder_ref(
    label: str,
    *,
    content_type: str = "application/json",
    digest: str | None = None,
) -> dict[str, object]:
    return {
        "uri": f"gs://desub-media/integrated/{label}",
        "generation": "123456789",
        "size_bytes": 128,
        "sha256": digest or _sha(label),
        "content_type": content_type,
    }


def _json_bundle(label: str, document: dict[str, Any]) -> dict[str, Any]:
    object_bytes = _json_bytes(document)
    return {
        "object_ref": {
            "uri": f"gs://desub-media/evidence/{label}",
            "generation": "123456789",
            "size_bytes": len(object_bytes),
            "sha256": _sha(object_bytes),
            "content_type": "application/json",
        },
        "document": copy.deepcopy(document),
        "object_bytes": object_bytes,
    }


def _binary_bundle(
    label: str,
    object_bytes: bytes,
    *,
    content_type: str = "video/mp4",
) -> dict[str, Any]:
    return {
        "object_ref": {
            "uri": f"gs://desub-media/evidence/{label}",
            "generation": "123456789",
            "size_bytes": len(object_bytes),
            "sha256": _sha(object_bytes),
            "content_type": content_type,
        },
        "object_bytes": object_bytes,
    }


def _repack_json_bundle(
    bundle: dict[str, Any], document: dict[str, Any]
) -> dict[str, Any]:
    result = copy.deepcopy(bundle)
    object_bytes = _json_bytes(document)
    result["document"] = copy.deepcopy(document)
    result["object_bytes"] = object_bytes
    result["object_ref"]["size_bytes"] = len(object_bytes)
    result["object_ref"]["sha256"] = _sha(object_bytes)
    return result


def _report(
    gate: str,
    policy: dict[str, str],
    bindings: dict[str, str],
) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "gate": gate,
        "attempt": _attempt(),
        "policy": copy.deepcopy(policy),
        "bindings": copy.deepcopy(bindings),
        "decision": "pass",
        "metrics": [
            {
                "name": name,
                "decision": "pass",
                "value": 0,
                "unit": "count",
                "denominator": 1,
                "evidence": [_placeholder_ref(f"{gate}-{name}.json")],
            }
            for name in sorted(REQUIRED_MACHINE_METRIC_NAMES[gate])
        ],
        "implementation": {
            "image_digest": f"sha256:{_sha(f'{gate}-image')}",
            "code_sha256": _sha(f"{gate}-code"),
            "metric_decode_sha256": _sha(f"{gate}-decode"),
        },
        "started_at": NOW,
        "finished_at": NOW,
    }


def _clean_fixture() -> dict[str, Any]:
    clean_policy = _policy("clean_qa_policy")
    source = _placeholder_ref(
        "source.mp4", content_type="video/mp4", digest=_sha("source")
    )
    clean = _placeholder_ref(
        "clean.mp4", content_type="video/mp4", digest=_sha("clean")
    )
    manifest = _placeholder_ref(
        "source-cue-manifest.json", digest=_sha("source-cue-manifest")
    )
    bindings = {
        "source_sha256": source["sha256"],
        "clean_sha256": clean["sha256"],
        "source_cue_manifest_sha256": manifest["sha256"],
        "source_cue_collection_root": _sha("source-cue-root"),
        "clean_policy_sha256": clean_policy["sha256"],
    }
    report_bundle = _json_bundle(
        "clean-machine-report.json",
        _report("clean", clean_policy, bindings),
    )
    verdict_bundles: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for role in ("clean_a", "clean_b", "clean_c", "clean_final_sol"):
        model = _model(role)
        verdict_bindings = {
            "source_sha256": bindings["source_sha256"],
            "clean_sha256": bindings["clean_sha256"],
            "source_cue_manifest_sha256": bindings[
                "source_cue_manifest_sha256"
            ],
            "source_cue_collection_root": bindings[
                "source_cue_collection_root"
            ],
            "policy_sha256": bindings["clean_policy_sha256"],
        }
        verdict = {
            "schema_version": "1",
            "role": role,
            "attempt": _attempt(),
            "policy": copy.deepcopy(clean_policy),
            "model": copy.deepcopy(model),
            "bindings": verdict_bindings,
            "decision": "pass",
            "findings": [],
            "created_at": NOW,
        }
        verdict_bundle = _json_bundle(f"{role}.json", verdict)
        verdict_bundles.append(verdict_bundle)
        summaries.append(
            {
                "role": role,
                "object": copy.deepcopy(verdict_bundle["object_ref"]),
                "model": copy.deepcopy(model),
                "bindings": copy.deepcopy(bindings),
            }
        )
    approval_document = {
        "payload": {
            "schema_version": "1",
            "clean_approval_id": (
                "cap-0123456789abcdef0123456789abcdef"
            ),
            "issued_at": NOW,
            "expires_at": LATER,
            "attempt": _attempt(),
            "source": source,
            "clean": clean,
            "clean_manifest": _placeholder_ref("clean-manifest.json"),
            "clean_contract_sha256": _sha("clean-contract"),
            "source_cue_manifest": manifest,
            "source_cue_collection_root": bindings[
                "source_cue_collection_root"
            ],
            "extractor_aligner_policy_sha256": _sha(
                "extractor-aligner-policy"
            ),
            "clean_policy": clean_policy,
            "clean_machine_report": copy.deepcopy(
                report_bundle["object_ref"]
            ),
            "clean_verdicts": summaries,
            "controller_signer_principal": (
                "serviceAccount:controller@desub.iam.gserviceaccount.com"
            ),
        },
        "signature": _signature(),
    }
    approval_bundle = _json_bundle(
        "clean-approval.json", approval_document
    )
    index_document = {
        "schema_version": "1",
        "attempt": _attempt(),
        "clean_approval_id": approval_document["payload"][
            "clean_approval_id"
        ],
        "clean_approval": copy.deepcopy(approval_bundle["object_ref"]),
        "source_sha256": bindings["source_sha256"],
        "clean_sha256": bindings["clean_sha256"],
        "source_cue_manifest_sha256": bindings[
            "source_cue_manifest_sha256"
        ],
        "source_cue_collection_root": bindings[
            "source_cue_collection_root"
        ],
        "clean_policy_sha256": bindings["clean_policy_sha256"],
        "state_version": 7,
        "activated_at": NOW,
    }
    index_bundle = _json_bundle("clean-gate-index.json", index_document)
    return {
        "bindings": bindings,
        "report_bundle": report_bundle,
        "verdict_bundles": verdict_bundles,
        "approval_bundle": approval_bundle,
        "index_bundle": index_bundle,
        "evidence": [report_bundle, *verdict_bundles],
    }


def _repack_clean_authority(fixture: dict[str, Any]) -> None:
    approval_document = fixture["approval_bundle"]["document"]
    fixture["approval_bundle"] = _repack_json_bundle(
        fixture["approval_bundle"], approval_document
    )
    index_document = fixture["index_bundle"]["document"]
    index_document["clean_approval"] = copy.deepcopy(
        fixture["approval_bundle"]["object_ref"]
    )
    fixture["index_bundle"] = _repack_json_bundle(
        fixture["index_bundle"], index_document
    )


def _authorized_dub_fixture() -> dict[str, Any]:
    fixture = _clean_fixture()
    approval_payload = fixture["approval_bundle"]["document"]["payload"]
    cue_refs = {
        "record": _placeholder_ref("cue-000000.json"),
        "crop": _placeholder_ref(
            "cue-000000.png", content_type="image/png"
        ),
        "pcm_slice": _placeholder_ref(
            "cue-000000.wav", content_type="audio/wav"
        ),
    }
    manifest_bundle = _json_bundle(
        "source-cue-manifest.json",
        {
            "schema_version": "1",
            "attempt": _attempt(),
            "source": copy.deepcopy(approval_payload["source"]),
            "collection_root": approval_payload[
                "source_cue_collection_root"
            ],
            "cues": [
                {
                    "cue_id": "cue-000000",
                    **copy.deepcopy(cue_refs),
                }
            ],
        },
    )
    manifest_sha = manifest_bundle["object_ref"]["sha256"]
    fixture["bindings"]["source_cue_manifest_sha256"] = manifest_sha
    approval_payload["source_cue_manifest"] = copy.deepcopy(
        manifest_bundle["object_ref"]
    )

    report_document = fixture["report_bundle"]["document"]
    report_document["bindings"]["source_cue_manifest_sha256"] = manifest_sha
    fixture["report_bundle"] = _repack_json_bundle(
        fixture["report_bundle"], report_document
    )
    approval_payload["clean_machine_report"] = copy.deepcopy(
        fixture["report_bundle"]["object_ref"]
    )

    updated_verdicts: list[dict[str, Any]] = []
    for offset, verdict_bundle in enumerate(fixture["verdict_bundles"]):
        verdict_document = verdict_bundle["document"]
        verdict_document["bindings"][
            "source_cue_manifest_sha256"
        ] = manifest_sha
        updated = _repack_json_bundle(verdict_bundle, verdict_document)
        updated_verdicts.append(updated)
        summary = approval_payload["clean_verdicts"][offset]
        summary["object"] = copy.deepcopy(updated["object_ref"])
        summary["bindings"]["source_cue_manifest_sha256"] = manifest_sha
    fixture["verdict_bundles"] = updated_verdicts
    fixture["evidence"] = [fixture["report_bundle"], *updated_verdicts]

    fixture["index_bundle"]["document"][
        "source_cue_manifest_sha256"
    ] = manifest_sha
    _repack_clean_authority(fixture)

    work_item = {
        "schema_version": "1",
        "attempt": _attempt(),
        "clean_approval_id": approval_payload["clean_approval_id"],
        "clean_approval": copy.deepcopy(
            fixture["approval_bundle"]["object_ref"]
        ),
        "clean_gate_index": copy.deepcopy(
            fixture["index_bundle"]["object_ref"]
        ),
        "clean": copy.deepcopy(approval_payload["clean"]),
        "clean_manifest": copy.deepcopy(
            approval_payload["clean_manifest"]
        ),
        "source_cue_manifest": copy.deepcopy(
            manifest_bundle["object_ref"]
        ),
        "source_cue_evidence": [
            copy.deepcopy(cue_refs[field])
            for field in ("record", "crop", "pcm_slice")
        ],
        "clean_policy_sha256": approval_payload["clean_policy"]["sha256"],
        "expires_at": LATER,
    }
    return {
        "clean_fixture": fixture,
        "manifest_bundle": manifest_bundle,
        "work_item": work_item,
    }


def _release_fixture() -> dict[str, Any]:
    clean_fixture = _clean_fixture()
    clean_payload = clean_fixture["approval_bundle"]["document"]["payload"]
    ledger_bundle = _json_bundle(
        "cue-ledger.json",
        {
            "schema_version": "1",
            "attempt": _attempt(),
            "cue_count": 1,
            "cues": [{"cue_id": "cue-000000"}],
            "created_at": NOW,
        },
    )
    dubbed_bundle = _binary_bundle(
        "dubbed.mp4", b"\x00\x00\x00\x18ftypmp42dubbed-video"
    )
    clean_policy = copy.deepcopy(clean_payload["clean_policy"])
    dub_policy = _policy("dub_qa_policy")
    release_bindings = {
        "source_sha256": clean_payload["source"]["sha256"],
        "clean_sha256": clean_payload["clean"]["sha256"],
        "source_cue_manifest_sha256": clean_payload[
            "source_cue_manifest"
        ]["sha256"],
        "source_cue_collection_root": clean_payload[
            "source_cue_collection_root"
        ],
        "clean_policy_sha256": clean_policy["sha256"],
        "cue_ledger_sha256": ledger_bundle["object_ref"]["sha256"],
        "dubbed_sha256": dubbed_bundle["object_ref"]["sha256"],
        "dub_policy_sha256": dub_policy["sha256"],
    }
    dub_report_bundle = _json_bundle(
        "dub-machine-report.json",
        _report("dub", dub_policy, release_bindings),
    )
    final_bundles: list[dict[str, Any]] = []
    final_summaries: list[dict[str, Any]] = []
    for role in (
        "translation_semantics",
        "translation_style",
        "dub_audio_video",
        "dub_final_sol",
    ):
        model = _model(role)
        verdict_bindings = {
            "source_sha256": release_bindings["source_sha256"],
            "clean_sha256": release_bindings["clean_sha256"],
            "source_cue_manifest_sha256": release_bindings[
                "source_cue_manifest_sha256"
            ],
            "source_cue_collection_root": release_bindings[
                "source_cue_collection_root"
            ],
            "policy_sha256": release_bindings["dub_policy_sha256"],
            "cue_ledger_sha256": release_bindings[
                "cue_ledger_sha256"
            ],
            "dubbed_sha256": release_bindings["dubbed_sha256"],
            "machine_report_sha256": dub_report_bundle["object_ref"][
                "sha256"
            ],
        }
        verdict = {
            "schema_version": "1",
            "role": role,
            "attempt": _attempt(),
            "policy": copy.deepcopy(dub_policy),
            "model": copy.deepcopy(model),
            "bindings": verdict_bindings,
            "decision": "pass",
            "findings": [],
            "created_at": NOW,
        }
        verdict_bundle = _json_bundle(f"{role}.json", verdict)
        final_bundles.append(verdict_bundle)
        final_summaries.append(
            {
                "role": role,
                "object": copy.deepcopy(verdict_bundle["object_ref"]),
                "model": copy.deepcopy(model),
                "bindings": copy.deepcopy(release_bindings),
            }
        )
    preflight_bundle = _json_bundle(
        "translation-preflight-approval.json",
        {
            "payload": {
                "schema_version": "1",
                "translation_preflight_approval_id": (
                    "tpa-0123456789abcdef0123456789abcdef"
                ),
                "decision": "pass",
            },
            "signature": _signature(),
        },
    )
    release_document = {
        "payload": {
            "schema_version": "1",
            "release_approval_id": (
                "rap-0123456789abcdef0123456789abcdef"
            ),
            "issued_at": NOW,
            "expires_at": LATER,
            "attempt": _attempt(),
            "clean_approval_id": clean_payload["clean_approval_id"],
            "clean_approval": copy.deepcopy(
                clean_fixture["approval_bundle"]["object_ref"]
            ),
            "source": copy.deepcopy(clean_payload["source"]),
            "clean": copy.deepcopy(clean_payload["clean"]),
            "clean_manifest": copy.deepcopy(clean_payload["clean_manifest"]),
            "source_cue_manifest": copy.deepcopy(
                clean_payload["source_cue_manifest"]
            ),
            "source_cue_collection_root": clean_payload[
                "source_cue_collection_root"
            ],
            "translation_attempts": [
                _placeholder_ref("translation-0.json")
            ],
            "translation_preflight_approvals": [
                copy.deepcopy(preflight_bundle["object_ref"])
            ],
            "tts_attempts": [_placeholder_ref("tts-0.json")],
            "cue_ledger": copy.deepcopy(ledger_bundle["object_ref"]),
            "dubbed": copy.deepcopy(dubbed_bundle["object_ref"]),
            "dub_manifest": _placeholder_ref("dub-manifest.json"),
            "no_vietsub_control": _placeholder_ref(
                "no-vietsub.mp4", content_type="video/mp4"
            ),
            "vietsub_alpha_mask": _placeholder_ref(
                "vietsub-mask.png", content_type="image/png"
            ),
            "mix_report": _placeholder_ref("mix-report.json"),
            "stem_pcm": {
                name: _placeholder_ref(
                    f"stem-{name}.wav", content_type="audio/wav"
                )
                for name in (
                    "clean_input",
                    "bed",
                    "ducked_bed",
                    "voice",
                    "premaster",
                )
            },
            "clean_contract_sha256": clean_payload[
                "clean_contract_sha256"
            ],
            "dub_contract_sha256": _sha("dub-contract"),
            "render_policy_sha256": _sha("render-policy"),
            "style_policy_sha256": _sha("style-policy"),
            "mix_policy_sha256": _sha("mix-policy"),
            "clean_policy": clean_policy,
            "dub_policy": dub_policy,
            "clean_machine_report": copy.deepcopy(
                clean_fixture["report_bundle"]["object_ref"]
            ),
            "dub_machine_report": copy.deepcopy(
                dub_report_bundle["object_ref"]
            ),
            "final_verdicts": final_summaries,
            "controller_signer_principal": (
                "serviceAccount:controller@desub.iam.gserviceaccount.com"
            ),
        },
        "signature": _signature(),
    }
    release_bundle = _json_bundle(
        "release-approval.json", release_document
    )
    index_document = {
        "schema_version": "1",
        "attempt": _attempt(),
        "release_approval_id": release_document["payload"][
            "release_approval_id"
        ],
        "release_approval": copy.deepcopy(release_bundle["object_ref"]),
        "clean_approval_id": clean_payload["clean_approval_id"],
        "clean_sha256": release_bindings["clean_sha256"],
        "source_cue_manifest_sha256": release_bindings[
            "source_cue_manifest_sha256"
        ],
        "cue_ledger_sha256": release_bindings["cue_ledger_sha256"],
        "dubbed_sha256": release_bindings["dubbed_sha256"],
        "clean_policy_sha256": release_bindings[
            "clean_policy_sha256"
        ],
        "dub_policy_sha256": release_bindings["dub_policy_sha256"],
        "state_version": 19,
        "activated_at": NOW,
    }
    index_bundle = _json_bundle("release-index.json", index_document)
    return {
        "clean_fixture": clean_fixture,
        "release_bindings": release_bindings,
        "ledger_bundle": ledger_bundle,
        "dubbed_bundle": dubbed_bundle,
        "dub_report_bundle": dub_report_bundle,
        "final_bundles": final_bundles,
        "preflight_bundle": preflight_bundle,
        "release_bundle": release_bundle,
        "index_bundle": index_bundle,
        "evidence": [
            clean_fixture["report_bundle"],
            dub_report_bundle,
            ledger_bundle,
            dubbed_bundle,
            *final_bundles,
        ],
    }


def _repack_release_authority(fixture: dict[str, Any]) -> None:
    release_document = fixture["release_bundle"]["document"]
    fixture["release_bundle"] = _repack_json_bundle(
        fixture["release_bundle"], release_document
    )
    index_document = fixture["index_bundle"]["document"]
    index_document["release_approval"] = copy.deepcopy(
        fixture["release_bundle"]["object_ref"]
    )
    fixture["index_bundle"] = _repack_json_bundle(
        fixture["index_bundle"], index_document
    )


def _qa_fixture() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source_sha = _sha("full-source-video")
    source_document = {
        "schema_version": "1",
        "attempt": _attempt(),
        "cue_id": "cue-000000",
        "cue_index": 0,
        "source_sha256": source_sha,
    }
    source_bundle = _json_bundle(
        "source-cue-record.json", source_document
    )
    candidate_sha = _sha("candidate-semantic-projection")
    candidate_document = {
        "schema_version": "1",
        "attempt": _attempt(),
        "cue_id": "cue-000000",
        "cue_index": 0,
        "source_cue_record": copy.deepcopy(source_bundle["object_ref"]),
        "source_cue_record_sha256": source_bundle["object_ref"]["sha256"],
        "candidate_index": 0,
        "candidate_sha256": candidate_sha,
    }
    candidate_bundle = _json_bundle(
        "translation-candidate.json", candidate_document
    )
    policy = _policy("translation_semantic_preflight")
    packet = {
        "schema_version": "1",
        "packet_id": "qap-0123456789abcdef0123456789abcdef",
        "role": "translation_semantic_preflight",
        "attempt": _attempt(),
        "policy": policy,
        "bindings": {
            "source_sha256": source_sha,
            "clean_sha256": _sha("clean"),
            "source_cue_manifest_sha256": _sha("manifest"),
            "source_cue_collection_root": _sha("cue-root"),
            "policy_sha256": policy["sha256"],
            "cue_id": "cue-000000",
            "source_cue_record_sha256": source_bundle["object_ref"][
                "sha256"
            ],
            "translation_attempt_sha256": candidate_bundle["object_ref"][
                "sha256"
            ],
            "candidate_sha256": candidate_sha,
        },
        "full_source_video_included": False,
        "declared_total_bytes": (
            source_bundle["object_ref"]["size_bytes"]
            + candidate_bundle["object_ref"]["size_bytes"]
        ),
        "evidence": [
            {
                "kind": "source_cue_record",
                "cue_id": "cue-000000",
                "object": copy.deepcopy(source_bundle["object_ref"]),
            },
            {
                "kind": "translation_candidate",
                "cue_id": "cue-000000",
                "object": copy.deepcopy(candidate_bundle["object_ref"]),
            },
        ],
        "created_at": NOW,
        "expires_at": LATER,
    }
    return packet, [source_bundle, candidate_bundle]


class StoredBundleAndQaTests(unittest.TestCase):
    def test_stored_bundle_checks_size_sha_and_exact_json(self) -> None:
        bundle = _json_bundle("document.json", {"a": 1, "b": [True, None]})
        ref, document = validate_stored_bundle(bundle)
        self.assertEqual(ref, bundle["object_ref"])
        self.assertEqual(document, bundle["document"])

        wrong_size = copy.deepcopy(bundle)
        wrong_size["object_ref"]["size_bytes"] += 1
        with self.assertRaises(ContractValidationError):
            validate_stored_bundle(wrong_size)

        wrong_document = copy.deepcopy(bundle)
        wrong_document["document"]["a"] = 2
        with self.assertRaises(ContractValidationError):
            validate_stored_bundle(wrong_document)

    def test_translation_preflight_packet_resolves_exact_bytes(self) -> None:
        packet, bundles = _qa_fixture()
        result = validate_qa_packet_bundles(packet, bundles)
        self.assertEqual(result["evidence_count"], 2)
        self.assertEqual(
            result["declared_total_bytes"], packet["declared_total_bytes"]
        )

    def test_qa_rejects_ttl_swapped_objects_and_wrong_semantic_sha(self) -> None:
        packet, bundles = _qa_fixture()
        too_long = copy.deepcopy(packet)
        too_long["expires_at"] = "2026-07-26T00:00:01Z"
        with self.assertRaises(ContractValidationError):
            validate_qa_packet_bundles(too_long, bundles)

        swapped = copy.deepcopy(packet)
        swapped["evidence"][0]["object"], swapped["evidence"][1]["object"] = (
            swapped["evidence"][1]["object"],
            swapped["evidence"][0]["object"],
        )
        with self.assertRaises(ContractValidationError):
            validate_qa_packet_bundles(swapped, bundles)

        wrong_sha = copy.deepcopy(packet)
        wrong_sha["bindings"]["candidate_sha256"] = _sha("other-candidate")
        with self.assertRaises(ContractValidationError):
            validate_qa_packet_bundles(wrong_sha, bundles)

    def test_qa_rejects_missing_bytes_and_full_source(self) -> None:
        packet, bundles = _qa_fixture()
        with self.assertRaises(ContractValidationError):
            validate_qa_packet_bundles(packet, bundles[:1])

        full_source = copy.deepcopy(packet)
        full_source["full_source_video_included"] = True
        with self.assertRaises(ContractValidationError):
            validate_qa_packet_bundles(full_source, bundles)


class CleanAuthorizationTests(unittest.TestCase):
    def test_clean_authorization_resolves_report_and_four_verdicts(self) -> None:
        fixture = _clean_fixture()
        result = validate_clean_authorization_structure(
            fixture["approval_bundle"],
            fixture["index_bundle"],
            fixture["evidence"],
            signature_verified=True,
        )
        self.assertEqual(
            result["clean_sha256"], fixture["bindings"]["clean_sha256"]
        )

    def test_clean_rejects_swapped_or_failing_verdict(self) -> None:
        swapped = _clean_fixture()
        summaries = swapped["approval_bundle"]["document"]["payload"][
            "clean_verdicts"
        ]
        summaries[0]["object"], summaries[1]["object"] = (
            summaries[1]["object"],
            summaries[0]["object"],
        )
        _repack_clean_authority(swapped)
        with self.assertRaises(ContractValidationError):
            validate_clean_authorization_structure(
                swapped["approval_bundle"],
                swapped["index_bundle"],
                swapped["evidence"],
                signature_verified=True,
            )

        failing = _clean_fixture()
        failed_bundle = copy.deepcopy(failing["verdict_bundles"][0])
        failed_document = failed_bundle["document"]
        failed_document["decision"] = "fail"
        failed_bundle = _repack_json_bundle(failed_bundle, failed_document)
        failing["verdict_bundles"][0] = failed_bundle
        failing["evidence"][1] = failed_bundle
        failing["approval_bundle"]["document"]["payload"]["clean_verdicts"][
            0
        ]["object"] = copy.deepcopy(failed_bundle["object_ref"])
        _repack_clean_authority(failing)
        with self.assertRaises(ContractValidationError):
            validate_clean_authorization_structure(
                failing["approval_bundle"],
                failing["index_bundle"],
                failing["evidence"],
                signature_verified=True,
            )

    def test_clean_rejects_wrong_index_ref_and_signature_flag(self) -> None:
        fixture = _clean_fixture()
        wrong_index = copy.deepcopy(fixture["index_bundle"])
        wrong_index_document = wrong_index["document"]
        wrong_index_document["clean_approval"] = _placeholder_ref(
            "wrong-clean-approval.json"
        )
        wrong_index = _repack_json_bundle(
            wrong_index, wrong_index_document
        )
        with self.assertRaises(ContractValidationError):
            validate_clean_authorization_structure(
                fixture["approval_bundle"],
                wrong_index,
                fixture["evidence"],
                signature_verified=True,
            )
        with self.assertRaises(ContractValidationError):
            validate_clean_authorization_structure(
                fixture["approval_bundle"],
                fixture["index_bundle"],
                fixture["evidence"],
                signature_verified=False,
            )

    def test_dub_work_item_requires_full_signed_clean_authority(self) -> None:
        fixture = _authorized_dub_fixture()
        clean = fixture["clean_fixture"]
        result = validate_dub_work_item_authority_structure(
            fixture["work_item"],
            clean_approval_bundle_value=clean["approval_bundle"],
            clean_gate_index_bundle_value=clean["index_bundle"],
            source_cue_manifest_bundle_value=fixture["manifest_bundle"],
            clean_authorization_evidence_bundle_values=clean["evidence"],
            clean_signature_verified=True,
        )
        self.assertEqual(
            result["clean_sha256"], clean["bindings"]["clean_sha256"]
        )

        with self.assertRaises(ContractValidationError):
            validate_dub_work_item_authority_structure(
                fixture["work_item"],
                clean_approval_bundle_value=clean["approval_bundle"],
                clean_gate_index_bundle_value=clean["index_bundle"],
                source_cue_manifest_bundle_value=fixture[
                    "manifest_bundle"
                ],
                clean_authorization_evidence_bundle_values=clean[
                    "evidence"
                ],
                clean_signature_verified=False,
            )


class ReleaseEvidenceStructureTests(unittest.TestCase):
    def _validate(self, fixture: dict[str, Any]) -> dict[str, Any]:
        preflight_ref = fixture["preflight_bundle"]["object_ref"]
        return validate_release_evidence_structure(
            fixture["clean_fixture"]["approval_bundle"],
            fixture["release_bundle"],
            fixture["index_bundle"],
            fixture["evidence"],
            clean_signature_verified=True,
            release_signature_verified=True,
            translation_preflight_bundle_values=[
                fixture["preflight_bundle"]
            ],
            translation_preflight_signature_verified={
                preflight_ref["sha256"]: True
            },
        )

    def test_release_resolves_required_structural_evidence(self) -> None:
        fixture = _release_fixture()
        result = self._validate(fixture)
        self.assertEqual(
            result["dubbed_sha256"],
            fixture["dubbed_bundle"]["object_ref"]["sha256"],
        )
        self.assertEqual(result["translation_preflight_approval_count"], 1)

    def test_release_rejects_wrong_clean_approval_or_release_ref(self) -> None:
        wrong_clean = _release_fixture()
        wrong_clean["release_bundle"]["document"]["payload"][
            "clean_approval"
        ] = _placeholder_ref("wrong-clean-approval.json")
        _repack_release_authority(wrong_clean)
        with self.assertRaises(ContractValidationError):
            self._validate(wrong_clean)

        wrong_index = _release_fixture()
        index_document = wrong_index["index_bundle"]["document"]
        index_document["release_approval"] = _placeholder_ref(
            "wrong-release-approval.json"
        )
        wrong_index["index_bundle"] = _repack_json_bundle(
            wrong_index["index_bundle"], index_document
        )
        with self.assertRaises(ContractValidationError):
            self._validate(wrong_index)

    def test_release_rejects_swapped_or_failing_final_verdict(self) -> None:
        swapped = _release_fixture()
        summaries = swapped["release_bundle"]["document"]["payload"][
            "final_verdicts"
        ]
        summaries[0]["object"], summaries[1]["object"] = (
            summaries[1]["object"],
            summaries[0]["object"],
        )
        _repack_release_authority(swapped)
        with self.assertRaises(ContractValidationError):
            self._validate(swapped)

        failing = _release_fixture()
        failed_bundle = copy.deepcopy(failing["final_bundles"][0])
        failed_document = failed_bundle["document"]
        failed_document["decision"] = "fail"
        failed_bundle = _repack_json_bundle(failed_bundle, failed_document)
        failing["final_bundles"][0] = failed_bundle
        failing["evidence"][4] = failed_bundle
        failing["release_bundle"]["document"]["payload"]["final_verdicts"][
            0
        ]["object"] = copy.deepcopy(failed_bundle["object_ref"])
        _repack_release_authority(failing)
        with self.assertRaises(ContractValidationError):
            self._validate(failing)

    def test_release_rejects_duplicate_preflight_and_signature_flags(self) -> None:
        duplicate = _release_fixture()
        refs = duplicate["release_bundle"]["document"]["payload"][
            "translation_preflight_approvals"
        ]
        refs.append(copy.deepcopy(refs[0]))
        _repack_release_authority(duplicate)
        with self.assertRaises(ContractValidationError):
            self._validate(duplicate)

        fixture = _release_fixture()
        preflight_ref = fixture["preflight_bundle"]["object_ref"]
        with self.assertRaises(ContractValidationError):
            validate_release_evidence_structure(
                fixture["clean_fixture"]["approval_bundle"],
                fixture["release_bundle"],
                fixture["index_bundle"],
                fixture["evidence"],
                clean_signature_verified=True,
                release_signature_verified=False,
                translation_preflight_bundle_values=[
                    fixture["preflight_bundle"]
                ],
                translation_preflight_signature_verified={
                    preflight_ref["sha256"]: True
                },
            )

        with self.assertRaises(ContractValidationError):
            validate_release_evidence_structure(
                fixture["clean_fixture"]["approval_bundle"],
                fixture["release_bundle"],
                fixture["index_bundle"],
                fixture["evidence"],
                clean_signature_verified=True,
                release_signature_verified=True,
                translation_preflight_bundle_values=[],
                translation_preflight_signature_verified={},
            )

        with self.assertRaises(TypeError):
            validate_release_evidence_structure(
                fixture["clean_fixture"]["approval_bundle"],
                fixture["release_bundle"],
                fixture["index_bundle"],
                fixture["evidence"],
                clean_signature_verified=True,
                release_signature_verified=True,
            )

    def test_release_rejects_tampered_dubbed_bytes(self) -> None:
        fixture = _release_fixture()
        tampered = copy.deepcopy(fixture["dubbed_bundle"])
        tampered["object_bytes"] += b"tampered"
        fixture["evidence"][3] = tampered
        with self.assertRaises(ContractValidationError):
            self._validate(fixture)


if __name__ == "__main__":
    unittest.main()
