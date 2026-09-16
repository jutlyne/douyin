"""Exact-byte evidence validation for integrated DESUB v1.

The schema documents describe the shape of signed approvals and QA packets.
This module adds the storage boundary that JSON Schema cannot express: every
referenced object must resolve to the exact immutable bytes named by its
ObjectRef, and all approval summaries must agree with the resolved documents.

Cryptographic signature verification deliberately remains outside this module.
The signature booleans used by structure/tamper tests are not bound KMS proofs;
all dependent helpers are structure-only and excluded from the package public
API. Production authorization must wait for the KMS/key-state/allowlist boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .enums import CLEAN_VERDICT_ROLES, FINAL_DUB_VERDICT_ROLES
from .validation import (
    ContractValidationError,
    validate_clean_evidence_bindings_structure,
    validate_clean_gate_index_structure,
    validate_dub_work_item_structure,
    validate_machine_report_aggregate,
    validate_release_bundle_structure,
)


_OBJECT_REF_FIELDS = frozenset(
    {"uri", "generation", "size_bytes", "sha256", "content_type"}
)
_ATTEMPT_FIELDS = frozenset(
    {"job_id", "attempt_id", "attempt_seq", "fence_digest"}
)
_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "application/octet-stream",
        "video/mp4",
        "image/png",
        "image/jpeg",
        "audio/wav",
        "audio/mpeg",
        "audio/mp4",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GCS_URI_RE = re.compile(r"^gs://[^/\s]+/[^\r\n]+$")
_GENERATION_RE = re.compile(r"^[1-9][0-9]*$")
_JOB_ID_RE = re.compile(r"^desub-[0-9a-f]{32}$")
_ATTEMPT_ID_RE = re.compile(r"^att-[0-9a-f]{32}$")
_CUE_ID_RE = re.compile(r"^cue-[0-9]{6}$")
_QA_PACKET_MAX_TTL = timedelta(days=2)
_CLEAN_ROLE_ORDER = ("clean_a", "clean_b", "clean_c", "clean_final_sol")
_FINAL_ROLE_ORDER = (
    "translation_semantics",
    "translation_style",
    "dub_audio_video",
    "dub_final_sol",
)
_PREFLIGHT_ROLES = frozenset(
    {"translation_semantic_preflight", "translation_style_preflight"}
)


@dataclass(frozen=True)
class _StoredArtifact:
    ref: dict[str, Any]
    document: Mapping[str, Any] | None
    object_bytes: bytes


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError(f"{label} must be an object")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, Sequence
    ):
        raise ContractValidationError(f"{label} must be an array")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ContractValidationError(f"{label} must be lowercase SHA-256 hex")
    return value


def _strict_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractValidationError(f"{label} must be an integer >= {minimum}")
    return value


def _object_ref(value: Any, label: str) -> dict[str, Any]:
    ref = _mapping(value, label)
    if set(ref) != _OBJECT_REF_FIELDS:
        raise ContractValidationError(f"{label} must be an exact ObjectRef")
    uri = ref.get("uri")
    generation = ref.get("generation")
    content_type = ref.get("content_type")
    if not isinstance(uri, str) or not _GCS_URI_RE.fullmatch(uri):
        raise ContractValidationError(f"{label}.uri is invalid")
    if not isinstance(generation, str) or not _GENERATION_RE.fullmatch(
        generation
    ):
        raise ContractValidationError(f"{label}.generation is invalid")
    size_bytes = _strict_int(
        ref.get("size_bytes"), f"{label}.size_bytes", minimum=1
    )
    digest = _sha256(ref.get("sha256"), f"{label}.sha256")
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise ContractValidationError(f"{label}.content_type is invalid")
    return {
        "uri": uri,
        "generation": generation,
        "size_bytes": size_bytes,
        "sha256": digest,
        "content_type": content_type,
    }


def _attempt(value: Any, label: str) -> dict[str, Any]:
    attempt = _mapping(value, label)
    if set(attempt) != _ATTEMPT_FIELDS:
        raise ContractValidationError(
            f"{label} must contain the exact attempt fields"
        )
    job_id = attempt.get("job_id")
    attempt_id = attempt.get("attempt_id")
    if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
        raise ContractValidationError(f"{label}.job_id is invalid")
    if not isinstance(attempt_id, str) or not _ATTEMPT_ID_RE.fullmatch(
        attempt_id
    ):
        raise ContractValidationError(f"{label}.attempt_id is invalid")
    return {
        "job_id": job_id,
        "attempt_id": attempt_id,
        "attempt_seq": _strict_int(
            attempt.get("attempt_seq"), f"{label}.attempt_seq", minimum=1
        ),
        "fence_digest": _sha256(
            attempt.get("fence_digest"), f"{label}.fence_digest"
        ),
    }


def _cue_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _CUE_ID_RE.fullmatch(value):
        raise ContractValidationError(f"{label} is invalid")
    return value


def _rfc3339(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T[^\s]+(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise ContractValidationError(f"{label} must be an RFC 3339 timestamp")
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ContractValidationError(
            f"{label} must be an RFC 3339 timestamp"
        ) from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ContractValidationError(f"{label} must include a timezone")
    return result


def _same(expected: Any, actual: Any, label: str) -> None:
    if expected != actual:
        raise ContractValidationError(f"{label} mismatch")


def _strict_json_loads(object_bytes: bytes, label: str) -> Mapping[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant {value}")

    def reject_duplicate_keys(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        parsed = json.loads(
            object_bytes.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractValidationError(
            f"{label} bytes are not strict UTF-8 JSON"
        ) from exc
    return _mapping(parsed, f"{label} decoded JSON")


def _json_value_equal(expected: Any, actual: Any) -> bool:
    """Compare decoded JSON without Python's bool/int or int/float coercion."""

    if type(expected) is not type(actual):
        return False
    if isinstance(expected, Mapping):
        return set(expected) == set(actual) and all(
            _json_value_equal(expected[key], actual[key]) for key in expected
        )
    if isinstance(expected, list):
        return len(expected) == len(actual) and all(
            _json_value_equal(left, right)
            for left, right in zip(expected, actual, strict=True)
        )
    return expected == actual


def validate_stored_bundle(
    bundle_value: Mapping[str, Any],
    *,
    label: str = "stored_bundle",
) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
    """Validate one immutable stored object against its exact bytes.

    JSON objects require an exact ``document`` projection and are decoded with
    duplicate-key and non-finite-number rejection. Binary objects omit
    ``document`` (or set it to ``None``). The returned document is decoded from
    the verified bytes, never trusted from the caller-provided projection.
    """

    bundle = _mapping(bundle_value, label)
    allowed_fields = {"object_ref", "object_bytes", "document"}
    if not {"object_ref", "object_bytes"}.issubset(bundle) or (
        set(bundle) - allowed_fields
    ):
        raise ContractValidationError(f"{label} fields are invalid")
    ref = _object_ref(bundle.get("object_ref"), f"{label}.object_ref")
    object_bytes = bundle.get("object_bytes")
    if not isinstance(object_bytes, bytes):
        raise ContractValidationError(
            f"{label}.object_bytes must be exact immutable bytes"
        )
    if len(object_bytes) != ref["size_bytes"]:
        raise ContractValidationError(f"{label} object byte size mismatch")
    if hashlib.sha256(object_bytes).hexdigest() != ref["sha256"]:
        raise ContractValidationError(f"{label} object byte SHA mismatch")

    if ref["content_type"] == "application/json":
        if "document" not in bundle:
            raise ContractValidationError(
                f"{label}.document is required for JSON content"
            )
        supplied_document = _mapping(
            bundle.get("document"), f"{label}.document"
        )
        decoded_document = _strict_json_loads(object_bytes, label)
        if not _json_value_equal(supplied_document, decoded_document):
            raise ContractValidationError(
                f"{label}.document does not match exact stored JSON"
            )
        return ref, decoded_document

    if bundle.get("document") is not None:
        raise ContractValidationError(
            f"{label}.document is forbidden for non-JSON content"
        )
    return ref, None


def _ref_key(ref: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        ref["uri"],
        ref["generation"],
        ref["size_bytes"],
        ref["sha256"],
        ref["content_type"],
    )


class _BundleIndex:
    def __init__(
        self,
        bundle_values: Sequence[Mapping[str, Any]],
        label: str,
    ) -> None:
        self._by_ref: dict[tuple[Any, ...], _StoredArtifact] = {}
        self._by_sha: dict[str, _StoredArtifact] = {}
        bundles = _sequence(bundle_values, label)
        for index, bundle_value in enumerate(bundles):
            item_label = f"{label}[{index}]"
            ref, document = validate_stored_bundle(
                bundle_value, label=item_label
            )
            artifact = _StoredArtifact(
                ref=ref,
                document=document,
                object_bytes=bundle_value["object_bytes"],
            )
            key = _ref_key(ref)
            if key in self._by_ref:
                raise ContractValidationError(
                    f"{label} contains a duplicate ObjectRef"
                )
            if ref["sha256"] in self._by_sha:
                raise ContractValidationError(
                    f"{label} contains a duplicate object SHA"
                )
            self._by_ref[key] = artifact
            self._by_sha[ref["sha256"]] = artifact

    def resolve(
        self,
        ref_value: Mapping[str, Any],
        label: str,
        *,
        require_json: bool = False,
    ) -> _StoredArtifact:
        ref = _object_ref(ref_value, label)
        artifact = self._by_ref.get(_ref_key(ref))
        if artifact is None:
            same_sha = self._by_sha.get(ref["sha256"])
            if same_sha is not None:
                raise ContractValidationError(
                    f"{label} does not exactly match stored ObjectRef metadata"
                )
            raise ContractValidationError(
                f"{label} has no supplied exact stored bytes"
            )
        if require_json and artifact.document is None:
            raise ContractValidationError(f"{label} must resolve to JSON")
        return artifact

    @property
    def refs(self) -> tuple[dict[str, Any], ...]:
        return tuple(item.ref for item in self._by_ref.values())


def _policy_sha(value: Any, label: str) -> str:
    policy = _mapping(value, label)
    return _sha256(policy.get("sha256"), f"{label}.sha256")


def _clean_bindings_from_payload(
    payload_value: Mapping[str, Any], label: str
) -> dict[str, str]:
    payload = _mapping(payload_value, label)
    return {
        "source_sha256": _sha256(
            _object_ref(payload.get("source"), f"{label}.source")["sha256"],
            f"{label}.source.sha256",
        ),
        "clean_sha256": _object_ref(
            payload.get("clean"), f"{label}.clean"
        )["sha256"],
        "source_cue_manifest_sha256": _object_ref(
            payload.get("source_cue_manifest"),
            f"{label}.source_cue_manifest",
        )["sha256"],
        "source_cue_collection_root": _sha256(
            payload.get("source_cue_collection_root"),
            f"{label}.source_cue_collection_root",
        ),
        "clean_policy_sha256": _policy_sha(
            payload.get("clean_policy"), f"{label}.clean_policy"
        ),
    }


def _release_bindings_from_payload(
    payload_value: Mapping[str, Any], label: str
) -> dict[str, str]:
    payload = _mapping(payload_value, label)
    clean = _clean_bindings_from_payload(payload, label)
    return {
        **clean,
        "cue_ledger_sha256": _object_ref(
            payload.get("cue_ledger"), f"{label}.cue_ledger"
        )["sha256"],
        "dubbed_sha256": _object_ref(
            payload.get("dubbed"), f"{label}.dubbed"
        )["sha256"],
        "dub_policy_sha256": _policy_sha(
            payload.get("dub_policy"), f"{label}.dub_policy"
        ),
    }


def _normalize_clean_bindings(
    bindings_value: Mapping[str, Any], label: str
) -> dict[str, str]:
    bindings = _mapping(bindings_value, label)
    policy_sha = bindings.get("clean_policy_sha256")
    if policy_sha is None:
        policy_sha = bindings.get("policy_sha256")
    return {
        "source_sha256": _sha256(
            bindings.get("source_sha256"), f"{label}.source_sha256"
        ),
        "clean_sha256": _sha256(
            bindings.get("clean_sha256"), f"{label}.clean_sha256"
        ),
        "source_cue_manifest_sha256": _sha256(
            bindings.get("source_cue_manifest_sha256"),
            f"{label}.source_cue_manifest_sha256",
        ),
        "source_cue_collection_root": _sha256(
            bindings.get("source_cue_collection_root"),
            f"{label}.source_cue_collection_root",
        ),
        "clean_policy_sha256": _sha256(
            policy_sha, f"{label}.clean_policy_sha256"
        ),
    }


def _normalize_release_summary(
    bindings_value: Mapping[str, Any], label: str
) -> dict[str, str]:
    bindings = _mapping(bindings_value, label)
    result = _normalize_clean_bindings(bindings, label)
    result.update(
        {
            "cue_ledger_sha256": _sha256(
                bindings.get("cue_ledger_sha256"),
                f"{label}.cue_ledger_sha256",
            ),
            "dubbed_sha256": _sha256(
                bindings.get("dubbed_sha256"), f"{label}.dubbed_sha256"
            ),
            "dub_policy_sha256": _sha256(
                bindings.get("dub_policy_sha256"),
                f"{label}.dub_policy_sha256",
            ),
        }
    )
    return result


def _validate_report_pass(
    report_value: Mapping[str, Any],
    *,
    gate: str,
    attempt: Mapping[str, Any],
    expected_bindings: Mapping[str, str],
    expected_policy: Mapping[str, Any],
    label: str,
) -> None:
    report = _mapping(report_value, label)
    validate_machine_report_aggregate(report)
    _same(gate, report.get("gate"), f"{label}.gate")
    _same("pass", report.get("decision"), f"{label}.decision")
    _same(attempt, _attempt(report.get("attempt"), f"{label}.attempt"), f"{label}.attempt")
    _same(expected_policy, report.get("policy"), f"{label}.policy")
    report_bindings = _mapping(report.get("bindings"), f"{label}.bindings")
    for field, expected in expected_bindings.items():
        _same(expected, report_bindings.get(field), f"{label}.bindings.{field}")
    started_at = _rfc3339(report.get("started_at"), f"{label}.started_at")
    finished_at = _rfc3339(report.get("finished_at"), f"{label}.finished_at")
    if finished_at < started_at:
        raise ContractValidationError(
            f"{label}.finished_at precedes started_at"
        )


def validate_qa_packet_bundles(
    packet_value: Mapping[str, Any],
    bundle_values: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate a redacted QA packet and every referenced evidence byte."""

    packet = _mapping(packet_value, "qa_packet")
    if packet.get("full_source_video_included") is not False:
        raise ContractValidationError(
            "QA packet must not include the full source video"
        )
    created_at = _rfc3339(packet.get("created_at"), "qa_packet.created_at")
    expires_at = _rfc3339(packet.get("expires_at"), "qa_packet.expires_at")
    ttl = expires_at - created_at
    if ttl <= timedelta(0) or ttl > _QA_PACKET_MAX_TTL:
        raise ContractValidationError(
            "QA packet TTL must be positive and no more than two days"
        )
    attempt = _attempt(packet.get("attempt"), "qa_packet.attempt")
    policy_sha = _policy_sha(packet.get("policy"), "qa_packet.policy")
    bindings = _mapping(packet.get("bindings"), "qa_packet.bindings")
    _same(
        policy_sha,
        _sha256(
            bindings.get("policy_sha256"),
            "qa_packet.bindings.policy_sha256",
        ),
        "QA packet policy binding",
    )
    source_sha = _sha256(
        bindings.get("source_sha256"), "qa_packet.bindings.source_sha256"
    )

    index = _BundleIndex(bundle_values, "qa_packet_bundles")
    evidence_values = _sequence(packet.get("evidence"), "qa_packet.evidence")
    if not evidence_values:
        raise ContractValidationError("QA packet evidence cannot be empty")
    resolved: list[tuple[Mapping[str, Any], _StoredArtifact]] = []
    seen_refs: set[tuple[Any, ...]] = set()
    declared_total = 0
    for offset, evidence_value in enumerate(evidence_values):
        evidence = _mapping(
            evidence_value, f"qa_packet.evidence[{offset}]"
        )
        kind = evidence.get("kind")
        if kind in {"full_source", "full_source_video", "source_video"}:
            raise ContractValidationError("QA packet exposes full source evidence")
        ref = _object_ref(
            evidence.get("object"), f"qa_packet.evidence[{offset}].object"
        )
        key = _ref_key(ref)
        if key in seen_refs:
            raise ContractValidationError(
                "QA packet repeats an evidence ObjectRef"
            )
        seen_refs.add(key)
        if ref["sha256"] == source_sha:
            raise ContractValidationError(
                "QA packet evidence resolves to the full source object"
            )
        artifact = index.resolve(
            ref, f"qa_packet.evidence[{offset}].object"
        )
        declared_total += len(artifact.object_bytes)
        resolved.append((evidence, artifact))
    _same(
        declared_total,
        _strict_int(
            packet.get("declared_total_bytes"),
            "qa_packet.declared_total_bytes",
            minimum=1,
        ),
        "QA packet declared byte total",
    )

    role = packet.get("role")
    if role in _PREFLIGHT_ROLES:
        source_items = [
            item for item in resolved if item[0].get("kind") == "source_cue_record"
        ]
        candidate_items = [
            item
            for item in resolved
            if item[0].get("kind") == "translation_candidate"
        ]
        if len(source_items) != 1 or len(candidate_items) != 1:
            raise ContractValidationError(
                "translation preflight requires exactly one source cue record "
                "and one translation candidate"
            )
        source_evidence, source_artifact = source_items[0]
        candidate_evidence, candidate_artifact = candidate_items[0]
        if source_artifact.document is None or candidate_artifact.document is None:
            raise ContractValidationError(
                "translation preflight source and candidate must be JSON"
            )
        source_document = source_artifact.document
        candidate_document = candidate_artifact.document
        cue_id = _cue_id(
            bindings.get("cue_id"), "qa_packet.bindings.cue_id"
        )
        _same(cue_id, source_evidence.get("cue_id"), "source evidence cue")
        _same(
            cue_id, candidate_evidence.get("cue_id"), "candidate evidence cue"
        )
        _same(
            attempt,
            _attempt(source_document.get("attempt"), "source cue attempt"),
            "source cue attempt",
        )
        _same(
            attempt,
            _attempt(
                candidate_document.get("attempt"),
                "translation candidate attempt",
            ),
            "translation candidate attempt",
        )
        _same(cue_id, source_document.get("cue_id"), "source cue ID")
        _same(cue_id, candidate_document.get("cue_id"), "candidate cue ID")
        _same(
            source_sha,
            _sha256(
                source_document.get("source_sha256"),
                "source cue source_sha256",
            ),
            "source cue source SHA",
        )
        source_record_sha = _sha256(
            bindings.get("source_cue_record_sha256"),
            "qa_packet.bindings.source_cue_record_sha256",
        )
        _same(
            source_artifact.ref["sha256"],
            source_record_sha,
            "source cue record object SHA",
        )
        _same(
            source_artifact.ref,
            _object_ref(
                candidate_document.get("source_cue_record"),
                "translation candidate source_cue_record",
            ),
            "translation candidate source ObjectRef",
        )
        _same(
            source_record_sha,
            _sha256(
                candidate_document.get("source_cue_record_sha256"),
                "translation candidate source_cue_record_sha256",
            ),
            "translation candidate source record SHA",
        )
        _same(
            candidate_artifact.ref["sha256"],
            _sha256(
                bindings.get("translation_attempt_sha256"),
                "qa_packet.bindings.translation_attempt_sha256",
            ),
            "translation candidate document SHA",
        )
        _same(
            _sha256(
                candidate_document.get("candidate_sha256"),
                "translation candidate candidate_sha256",
            ),
            _sha256(
                bindings.get("candidate_sha256"),
                "qa_packet.bindings.candidate_sha256",
            ),
            "translation semantic candidate SHA",
        )

    return {
        "evidence_count": len(resolved),
        "declared_total_bytes": declared_total,
        "expires_at": expires_at,
    }


def validate_clean_authorization_structure(
    clean_approval_bundle_value: Mapping[str, Any],
    clean_gate_index_bundle_value: Mapping[str, Any],
    evidence_bundle_values: Sequence[Mapping[str, Any]],
    *,
    signature_verified: bool,
) -> dict[str, Any]:
    """Validate clean-approval structure without granting Dub authority.

    This Phase-A helper does not perform KMS/key-state/allowlist verification;
    ``signature_verified`` is an external test flag only.
    """

    if signature_verified is not True:
        raise ContractValidationError(
            "clean approval KMS signature_verified must be True"
        )
    approval_ref, approval_document = validate_stored_bundle(
        clean_approval_bundle_value, label="clean_approval_bundle"
    )
    index_ref, index_document = validate_stored_bundle(
        clean_gate_index_bundle_value, label="clean_gate_index_bundle"
    )
    if approval_document is None or index_document is None:
        raise ContractValidationError("clean approval and index must be JSON")
    payload = _mapping(
        approval_document.get("payload"), "clean_approval.payload"
    )
    attempt = _attempt(payload.get("attempt"), "clean_approval.payload.attempt")
    expected = _clean_bindings_from_payload(payload, "clean_approval.payload")

    validate_clean_gate_index_structure(approval_document, index_document)
    _same(
        approval_ref,
        _object_ref(
            index_document.get("clean_approval"),
            "clean_gate_index.clean_approval",
        ),
        "clean gate index approval ObjectRef",
    )

    evidence_index = _BundleIndex(
        evidence_bundle_values, "clean_authorization_evidence"
    )
    report_artifact = evidence_index.resolve(
        payload.get("clean_machine_report"),
        "clean_approval.payload.clean_machine_report",
        require_json=True,
    )
    assert report_artifact.document is not None
    clean_policy = _mapping(
        payload.get("clean_policy"), "clean_approval.payload.clean_policy"
    )
    _validate_report_pass(
        report_artifact.document,
        gate="clean",
        attempt=attempt,
        expected_bindings=expected,
        expected_policy=clean_policy,
        label="clean_machine_report",
    )

    embedded_values = _sequence(
        payload.get("clean_verdicts"), "clean_approval.payload.clean_verdicts"
    )
    if len(embedded_values) != 4:
        raise ContractValidationError(
            "clean approval must contain exactly four verdict summaries"
        )
    verdict_documents: list[Mapping[str, Any]] = []
    for offset, (expected_role, embedded_value) in enumerate(
        zip(_CLEAN_ROLE_ORDER, embedded_values, strict=True)
    ):
        embedded = _mapping(
            embedded_value, f"clean_approval.clean_verdicts[{offset}]"
        )
        _same(
            expected_role,
            embedded.get("role"),
            f"clean verdict summary {offset} role",
        )
        verdict_artifact = evidence_index.resolve(
            embedded.get("object"),
            f"clean verdict summary {offset} object",
            require_json=True,
        )
        assert verdict_artifact.document is not None
        verdict = verdict_artifact.document
        _same(expected_role, verdict.get("role"), f"clean verdict {offset} role")
        _same("pass", verdict.get("decision"), f"clean verdict {offset} decision")
        _same(
            attempt,
            _attempt(verdict.get("attempt"), f"clean verdict {offset} attempt"),
            f"clean verdict {offset} attempt",
        )
        _same(
            embedded.get("model"),
            verdict.get("model"),
            f"clean verdict {offset} model summary",
        )
        _same(
            expected,
            _normalize_clean_bindings(
                embedded.get("bindings"),
                f"clean verdict summary {offset} bindings",
            ),
            f"clean verdict summary {offset} bindings",
        )
        _same(
            expected,
            _normalize_clean_bindings(
                verdict.get("bindings"),
                f"clean verdict {offset} bindings",
            ),
            f"clean verdict {offset} bindings",
        )
        _same(
            clean_policy,
            verdict.get("policy"),
            f"clean verdict {offset} policy",
        )
        verdict_documents.append(verdict)
    if frozenset(item["role"] for item in verdict_documents) != CLEAN_VERDICT_ROLES:
        raise ContractValidationError("clean verdict role set is incomplete")
    _same(
        expected,
        validate_clean_evidence_bindings_structure(
            report_artifact.document, verdict_documents
        ),
        "clean evidence aggregate binding",
    )
    return {
        **expected,
        "clean_approval_ref": approval_ref,
        "clean_gate_index_ref": index_ref,
    }


def validate_dub_work_item_authority_structure(
    work_item_value: Mapping[str, Any],
    *,
    clean_approval_bundle_value: Mapping[str, Any],
    clean_gate_index_bundle_value: Mapping[str, Any],
    source_cue_manifest_bundle_value: Mapping[str, Any],
    clean_authorization_evidence_bundle_values: Sequence[Mapping[str, Any]],
    clean_signature_verified: bool,
) -> dict[str, Any]:
    """Validate the Dub work-item/clean-authority structure.

    This helper cannot authorize a production read: Phase A has no KMS
    verification boundary or populated key allowlist. The signature boolean is
    an external test flag only.
    """

    authorization = validate_clean_authorization_structure(
        clean_approval_bundle_value,
        clean_gate_index_bundle_value,
        clean_authorization_evidence_bundle_values,
        signature_verified=clean_signature_verified,
    )
    validate_dub_work_item_structure(
        work_item_value,
        clean_approval_bundle_value=clean_approval_bundle_value,
        clean_gate_index_bundle_value=clean_gate_index_bundle_value,
        source_cue_manifest_bundle_value=source_cue_manifest_bundle_value,
    )
    return authorization


def _validate_unique_preflight_refs(
    payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if "translation_preflight_verdicts" in payload:
        raise ContractValidationError(
            "release approval uses obsolete translation_preflight_verdicts"
        )
    values = _sequence(
        payload.get("translation_preflight_approvals"),
        "release_approval.payload.translation_preflight_approvals",
    )
    if not values:
        raise ContractValidationError(
            "release approval requires translation preflight approvals"
        )
    result: list[dict[str, Any]] = []
    seen_refs: set[tuple[Any, ...]] = set()
    seen_shas: set[str] = set()
    for offset, value in enumerate(values):
        ref = _object_ref(
            value,
            "release_approval.payload."
            f"translation_preflight_approvals[{offset}]",
        )
        if ref["content_type"] != "application/json":
            raise ContractValidationError(
                "translation preflight approval refs must be JSON"
            )
        key = _ref_key(ref)
        if key in seen_refs or ref["sha256"] in seen_shas:
            raise ContractValidationError(
                "translation preflight approval refs must be unique"
            )
        seen_refs.add(key)
        seen_shas.add(ref["sha256"])
        result.append(ref)
    return result


def _validate_required_preflight_bundles(
    refs: Sequence[Mapping[str, Any]],
    bundle_values: Sequence[Mapping[str, Any]],
    signature_verified: Mapping[str, bool],
) -> None:
    expected_shas = {ref["sha256"] for ref in refs}
    index = _BundleIndex(
        bundle_values, "translation_preflight_approval_bundles"
    )
    if {ref["sha256"] for ref in index.refs} != expected_shas:
        raise ContractValidationError(
            "translation preflight bundle set does not exactly match refs"
        )
    for offset, ref in enumerate(refs):
        artifact = index.resolve(
            ref,
            f"translation_preflight_approvals[{offset}]",
            require_json=True,
        )
        assert artifact.document is not None
        payload = _mapping(
            artifact.document.get("payload"),
            f"translation_preflight_approvals[{offset}].payload",
        )
        _same(
            "pass",
            payload.get("decision"),
            f"translation preflight approval {offset} decision",
        )
    if set(signature_verified) != expected_shas or any(
        value is not True for value in signature_verified.values()
    ):
        raise ContractValidationError(
            "translation preflight signature_verified flags are incomplete"
        )


def _validate_final_verdict_bindings(
    verdict: Mapping[str, Any],
    *,
    expected_release: Mapping[str, str],
    dub_report_sha256: str,
    dub_policy: Mapping[str, Any],
    label: str,
) -> None:
    bindings = _mapping(verdict.get("bindings"), f"{label}.bindings")
    for field in (
        "source_sha256",
        "clean_sha256",
        "source_cue_manifest_sha256",
        "source_cue_collection_root",
        "cue_ledger_sha256",
        "dubbed_sha256",
    ):
        _same(
            expected_release[field],
            bindings.get(field),
            f"{label}.bindings.{field}",
        )
    _same(
        _policy_sha(dub_policy, "release dub policy"),
        _sha256(bindings.get("policy_sha256"), f"{label}.bindings.policy_sha256"),
        f"{label} policy binding",
    )
    _same(
        dub_report_sha256,
        _sha256(
            bindings.get("machine_report_sha256"),
            f"{label}.bindings.machine_report_sha256",
        ),
        f"{label} machine report binding",
    )


def validate_release_evidence_structure(
    clean_approval_bundle_value: Mapping[str, Any],
    release_approval_bundle_value: Mapping[str, Any],
    release_index_bundle_value: Mapping[str, Any],
    evidence_bundle_values: Sequence[Mapping[str, Any]],
    *,
    clean_signature_verified: bool,
    release_signature_verified: bool,
    translation_preflight_bundle_values: Sequence[Mapping[str, Any]],
    translation_preflight_signature_verified: Mapping[str, bool],
) -> dict[str, Any]:
    """Validate stored release evidence structure without granting release.

    This Phase-A helper deliberately is not an authorization boundary: it does
    not yet resolve the complete selected cue-ledger → candidate → TTS family
    chain required by the release schema comment. Phase F must implement that
    transitive validator plus live KMS/allowlist verification before a release
    may be signed, completed, cached, or linked.
    """

    if clean_signature_verified is not True:
        raise ContractValidationError(
            "embedded clean approval signature_verified must be True"
        )
    if release_signature_verified is not True:
        raise ContractValidationError(
            "release approval signature_verified must be True"
        )
    clean_ref, clean_document = validate_stored_bundle(
        clean_approval_bundle_value, label="embedded_clean_approval_bundle"
    )
    release_ref, release_document = validate_stored_bundle(
        release_approval_bundle_value, label="release_approval_bundle"
    )
    index_ref, index_document = validate_stored_bundle(
        release_index_bundle_value, label="release_index_bundle"
    )
    if (
        clean_document is None
        or release_document is None
        or index_document is None
    ):
        raise ContractValidationError(
            "clean/release approvals and release index must be JSON"
        )
    clean_payload = _mapping(
        clean_document.get("payload"), "clean_approval.payload"
    )
    release_payload = _mapping(
        release_document.get("payload"), "release_approval.payload"
    )
    attempt = _attempt(
        release_payload.get("attempt"), "release_approval.payload.attempt"
    )
    _same(
        clean_ref,
        _object_ref(
            release_payload.get("clean_approval"),
            "release_approval.payload.clean_approval",
        ),
        "release embedded clean approval ObjectRef",
    )
    _same(
        release_ref,
        _object_ref(
            index_document.get("release_approval"),
            "release_index.release_approval",
        ),
        "release index approval ObjectRef",
    )
    validate_release_bundle_structure(
        clean_document, release_document, index_document
    )

    expected_clean = _clean_bindings_from_payload(
        clean_payload, "clean_approval.payload"
    )
    expected_release = _release_bindings_from_payload(
        release_payload, "release_approval.payload"
    )
    for field, expected in expected_clean.items():
        _same(
            expected,
            expected_release[field],
            f"release-to-clean {field}",
        )

    preflight_refs = _validate_unique_preflight_refs(release_payload)
    _validate_required_preflight_bundles(
        preflight_refs,
        translation_preflight_bundle_values,
        translation_preflight_signature_verified,
    )

    evidence_index = _BundleIndex(
        evidence_bundle_values, "release_authorization_evidence"
    )
    clean_report_ref = _object_ref(
        release_payload.get("clean_machine_report"),
        "release_approval.payload.clean_machine_report",
    )
    _same(
        _object_ref(
            clean_payload.get("clean_machine_report"),
            "clean_approval.payload.clean_machine_report",
        ),
        clean_report_ref,
        "release clean machine report ObjectRef",
    )
    clean_report_artifact = evidence_index.resolve(
        clean_report_ref,
        "release clean machine report",
        require_json=True,
    )
    dub_report_artifact = evidence_index.resolve(
        release_payload.get("dub_machine_report"),
        "release dub machine report",
        require_json=True,
    )
    assert clean_report_artifact.document is not None
    assert dub_report_artifact.document is not None
    clean_policy = _mapping(
        release_payload.get("clean_policy"),
        "release_approval.payload.clean_policy",
    )
    dub_policy = _mapping(
        release_payload.get("dub_policy"),
        "release_approval.payload.dub_policy",
    )
    _validate_report_pass(
        clean_report_artifact.document,
        gate="clean",
        attempt=attempt,
        expected_bindings=expected_clean,
        expected_policy=clean_policy,
        label="release_clean_machine_report",
    )
    _validate_report_pass(
        dub_report_artifact.document,
        gate="dub",
        attempt=attempt,
        expected_bindings=expected_release,
        expected_policy=dub_policy,
        label="release_dub_machine_report",
    )

    ledger_artifact = evidence_index.resolve(
        release_payload.get("cue_ledger"),
        "release cue ledger",
        require_json=True,
    )
    dubbed_artifact = evidence_index.resolve(
        release_payload.get("dubbed"), "release dubbed artifact"
    )
    assert ledger_artifact.document is not None
    _same(
        attempt,
        _attempt(ledger_artifact.document.get("attempt"), "cue_ledger.attempt"),
        "release cue ledger attempt",
    )
    _same(
        expected_release["dubbed_sha256"],
        dubbed_artifact.ref["sha256"],
        "release dubbed SHA",
    )

    embedded_values = _sequence(
        release_payload.get("final_verdicts"),
        "release_approval.payload.final_verdicts",
    )
    if len(embedded_values) != 4:
        raise ContractValidationError(
            "release approval must contain exactly four final verdict summaries"
        )
    seen_roles: set[str] = set()
    for offset, (expected_role, embedded_value) in enumerate(
        zip(_FINAL_ROLE_ORDER, embedded_values, strict=True)
    ):
        embedded = _mapping(
            embedded_value, f"release_approval.final_verdicts[{offset}]"
        )
        _same(
            expected_role,
            embedded.get("role"),
            f"final verdict summary {offset} role",
        )
        verdict_artifact = evidence_index.resolve(
            embedded.get("object"),
            f"final verdict summary {offset} object",
            require_json=True,
        )
        assert verdict_artifact.document is not None
        verdict = verdict_artifact.document
        _same(expected_role, verdict.get("role"), f"final verdict {offset} role")
        _same("pass", verdict.get("decision"), f"final verdict {offset} decision")
        _same(
            attempt,
            _attempt(verdict.get("attempt"), f"final verdict {offset} attempt"),
            f"final verdict {offset} attempt",
        )
        _same(
            embedded.get("model"),
            verdict.get("model"),
            f"final verdict {offset} model summary",
        )
        _same(
            expected_release,
            _normalize_release_summary(
                embedded.get("bindings"),
                f"final verdict summary {offset} bindings",
            ),
            f"final verdict summary {offset} bindings",
        )
        _same(
            dub_policy,
            verdict.get("policy"),
            f"final verdict {offset} policy",
        )
        _validate_final_verdict_bindings(
            verdict,
            expected_release=expected_release,
            dub_report_sha256=dub_report_artifact.ref["sha256"],
            dub_policy=dub_policy,
            label=f"final verdict {offset}",
        )
        seen_roles.add(expected_role)
    if frozenset(seen_roles) != FINAL_DUB_VERDICT_ROLES:
        raise ContractValidationError("final verdict role set is incomplete")

    return {
        **expected_release,
        "clean_approval_ref": clean_ref,
        "release_approval_ref": release_ref,
        "release_index_ref": index_ref,
        "translation_preflight_approval_count": len(preflight_refs),
    }


__all__ = [
    "validate_qa_packet_bundles",
    "validate_stored_bundle",
]
