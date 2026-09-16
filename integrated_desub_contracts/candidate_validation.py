"""Fail-closed semantic checks for the immutable candidate/preflight/TTS chain.

The v1 JSON Schemas freeze document shape, but they cannot fetch immutable
objects or compare fields across those objects.  This module performs those
cross-document checks using only the Python standard library.

Two deliberately conservative choices resolve otherwise unsafe ambiguity:

* this module does not implement KMS cryptography. Its signature boolean exists
  only for structure/tamper tests and is not production authentication; all
  dependent helpers are deliberately excluded from the package public API;
* ``validate_candidate_tts_family_structure`` validates a completed family.
  Therefore a last ``schedule_unfit`` at candidate 0 or 1 is rejected instead
  of being interpreted as an in-progress retry.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final

from .validation import ContractValidationError


_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_GCS_URI_RE: Final = re.compile(
    r"^gs://[a-z0-9][a-z0-9._-]{1,221}/[^\r\n]+$"
)
_GENERATION_RE: Final = re.compile(r"^[1-9][0-9]*$")
_JOB_ID_RE: Final = re.compile(r"^desub-[0-9a-f]{32}$")
_ATTEMPT_ID_RE: Final = re.compile(r"^att-[0-9a-f]{32}$")
_CUE_ID_RE: Final = re.compile(r"^cue-[0-9]{6}$")
_APPROVAL_ID_RE: Final = re.compile(r"^tpa-[0-9a-f]{32}$")
_SIGNER_RE: Final = re.compile(
    r"^serviceAccount:[^@\s]+@[^@\s]+\.iam\.gserviceaccount\.com$"
)
_KMS_KEY_VERSION_RE: Final = re.compile(
    r"^projects/[^/]+/locations/[^/]+/keyRings/[^/]+/"
    r"cryptoKeys/[^/]+/cryptoKeyVersions/[1-9][0-9]*$"
)
_BASE64_RE: Final = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")

_OBJECT_REF_FIELDS: Final = frozenset(
    {"uri", "generation", "size_bytes", "sha256", "content_type"}
)
_ATTEMPT_FIELDS: Final = frozenset(
    {"job_id", "attempt_id", "attempt_seq", "fence_digest"}
)
_MODEL_REQUIRED_FIELDS: Final = frozenset(
    {
        "provider",
        "service_identity",
        "model_id",
        "model_version",
        "endpoint_region",
        "adapter_sha256",
        "response_id",
    }
)
_MODEL_FIELDS: Final = _MODEL_REQUIRED_FIELDS | {"model_digest"}
_CANDIDATE_FIELDS: Final = frozenset(
    {
        "schema_version",
        "attempt",
        "cue_id",
        "cue_index",
        "source_cue_record",
        "source_cue_record_sha256",
        "candidate_index",
        "reason",
        "text_vi_raw",
        "text_vi_nfc",
        "generator",
        "generator_prompt_sha256",
        "candidate_sha256",
        "created_at",
    }
)
_APPROVAL_PAYLOAD_FIELDS: Final = frozenset(
    {
        "schema_version",
        "translation_preflight_approval_id",
        "issued_at",
        "expires_at",
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
        "controller_signer_principal",
    }
)
_VERDICT_FIELDS: Final = frozenset(
    {
        "schema_version",
        "role",
        "attempt",
        "policy",
        "model",
        "bindings",
        "decision",
        "findings",
        "created_at",
    }
)
_PREFLIGHT_BINDING_FIELDS: Final = frozenset(
    {
        "source_sha256",
        "clean_sha256",
        "source_cue_manifest_sha256",
        "source_cue_collection_root",
        "policy_sha256",
        "cue_id",
        "source_cue_record_sha256",
        "translation_attempt_sha256",
        "candidate_sha256",
    }
)
_TTS_REQUIRED_FIELDS: Final = frozenset(
    {
        "schema_version",
        "attempt",
        "cue_id",
        "cue_index",
        "candidate_index",
        "translation_candidate",
        "translation_candidate_object_sha256",
        "translation_candidate_sha256",
        "translation_preflight_approval",
        "translation_preflight_approval_sha256",
        "provider",
        "voice",
        "prosody_rate",
        "normalized_request_sha256",
        "provider_response_id",
        "outcome",
        "created_at",
    }
)
_TTS_OPTIONAL_FIELDS: Final = frozenset(
    {
        "error_code",
        "returned_media",
        "decoded_pcm",
        "silence_detector",
        "schedule",
    }
)
_DETECTOR_FIELDS: Final = frozenset(
    {
        "sample_rate_hz",
        "decoded_sample_count",
        "window_samples",
        "hop_samples",
        "tail_window_padding",
        "rms_denominator_samples",
        "active_threshold_dbfs",
        "active_threshold_linear",
        "consecutive_windows",
        "active_runs",
        "onset_sample",
        "offset_sample_inclusive",
        "implementation_sha256",
    }
)
_SCHEDULE_FIELDS: Final = frozenset(
    {
        "speech_anchor_sample",
        "previous_actual_offset_sample_inclusive",
        "target_actual_onset_sample",
        "video_sample_count",
        "placement_sample",
        "actual_onset_sample",
        "actual_offset_sample_inclusive",
        "lag_samples",
        "previous_gap_samples",
        "overlap_samples",
        "fits",
    }
)

_APPROVAL_MAX_LIFETIME: Final = timedelta(days=7)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError(f"{label} must be an object")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ContractValidationError(f"{label} must be an array")
    return value


def _exact_fields(
    value: Mapping[str, Any],
    expected: frozenset[str] | set[str],
    label: str,
) -> None:
    if set(value) != set(expected):
        raise ContractValidationError(f"{label} fields do not match the closed contract")


def _strict_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractValidationError(f"{label} must be an integer >= {minimum}")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ContractValidationError(f"{label} must be lowercase SHA-256 hex")
    return value


def _same(expected: Any, actual: Any, label: str) -> None:
    if actual != expected:
        raise ContractValidationError(f"{label} mismatch")


def _rfc3339(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T[^\s]+(?:Z|[+-]\d{2}:\d{2})",
        value,
    ):
        raise ContractValidationError(f"{label} must be an RFC 3339 timestamp")
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ContractValidationError(
            f"{label} must be an RFC 3339 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractValidationError(f"{label} must contain a timezone")
    return parsed


def _attempt(value: Any, label: str) -> dict[str, Any]:
    attempt = _mapping(value, label)
    _exact_fields(attempt, _ATTEMPT_FIELDS, label)
    if (
        not isinstance(attempt.get("job_id"), str)
        or _JOB_ID_RE.fullmatch(attempt["job_id"]) is None
    ):
        raise ContractValidationError(f"{label}.job_id is invalid")
    if (
        not isinstance(attempt.get("attempt_id"), str)
        or _ATTEMPT_ID_RE.fullmatch(attempt["attempt_id"]) is None
    ):
        raise ContractValidationError(f"{label}.attempt_id is invalid")
    _strict_int(attempt.get("attempt_seq"), f"{label}.attempt_seq", minimum=1)
    _sha256(attempt.get("fence_digest"), f"{label}.fence_digest")
    return dict(attempt)


def _cue(value: Mapping[str, Any], label: str) -> tuple[str, int]:
    index = _strict_int(value.get("cue_index"), f"{label}.cue_index")
    cue_id = value.get("cue_id")
    expected = f"cue-{index:06d}"
    if (
        not isinstance(cue_id, str)
        or _CUE_ID_RE.fullmatch(cue_id) is None
        or cue_id != expected
    ):
        raise ContractValidationError(f"{label}.cue_id must equal {expected!r}")
    return cue_id, index


def _object_ref(
    value: Any,
    label: str,
    *,
    content_type: str | None = None,
) -> dict[str, Any]:
    ref = _mapping(value, label)
    _exact_fields(ref, _OBJECT_REF_FIELDS, label)
    uri = ref.get("uri")
    generation = ref.get("generation")
    if not isinstance(uri, str) or _GCS_URI_RE.fullmatch(uri) is None:
        raise ContractValidationError(f"{label}.uri is invalid")
    if (
        not isinstance(generation, str)
        or _GENERATION_RE.fullmatch(generation) is None
    ):
        raise ContractValidationError(f"{label}.generation is invalid")
    _strict_int(ref.get("size_bytes"), f"{label}.size_bytes", minimum=1)
    _sha256(ref.get("sha256"), f"{label}.sha256")
    allowed_types = {
        "application/json",
        "application/octet-stream",
        "video/mp4",
        "image/png",
        "image/jpeg",
        "audio/wav",
        "audio/mpeg",
        "audio/mp4",
    }
    if ref.get("content_type") not in allowed_types:
        raise ContractValidationError(f"{label}.content_type is invalid")
    if content_type is not None and ref.get("content_type") != content_type:
        raise ContractValidationError(
            f"{label}.content_type must equal {content_type!r}"
        )
    return dict(ref)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractValidationError(
                f"stored JSON contains duplicate object key {key!r}"
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ContractValidationError(f"stored JSON contains non-finite number {value}")


def _reject_nonfinite(value: Any, label: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractValidationError(f"{label} contains a non-finite number")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_nonfinite(item, f"{label}.{key}")
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for index, item in enumerate(value):
            _reject_nonfinite(item, f"{label}[{index}]")


def _same_json(expected: Any, actual: Any, label: str) -> None:
    """Compare decoded JSON without Python's bool/int or int/float coercion."""

    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping) or set(expected) != set(actual):
            raise ContractValidationError(f"{label} mismatch")
        for key in expected:
            _same_json(expected[key], actual[key], f"{label}.{key}")
        return
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            raise ContractValidationError(f"{label} mismatch")
        for index, item in enumerate(expected):
            _same_json(item, actual[index], f"{label}[{index}]")
        return
    if type(expected) is not type(actual) or expected != actual:
        raise ContractValidationError(f"{label} mismatch")


def verify_stored_json_bundle(
    bundle_value: Mapping[str, Any],
    *,
    label: str = "stored_json_bundle",
) -> dict[str, Any]:
    """Verify exact stored JSON bytes against their immutable ObjectRef.

    A bundle is exactly ``object_ref`` + the decoded ``document`` + the literal
    ``object_bytes`` fetched from that ObjectRef generation.  Size, SHA-256,
    UTF-8 JSON decoding, duplicate keys, non-finite numbers and the decoded
    document are all checked.  No canonical re-serialization is substituted for
    the bytes that were actually stored.
    """

    bundle = _mapping(bundle_value, label)
    _exact_fields(bundle, {"object_ref", "document", "object_bytes"}, label)
    ref = _object_ref(
        bundle.get("object_ref"),
        f"{label}.object_ref",
        content_type="application/json",
    )
    object_bytes = bundle.get("object_bytes")
    if type(object_bytes) is not bytes:
        raise ContractValidationError(f"{label}.object_bytes must be exact bytes")
    _same(len(object_bytes), ref["size_bytes"], f"{label} ObjectRef size")
    digest = hashlib.sha256(object_bytes).hexdigest()
    _same(digest, ref["sha256"], f"{label} ObjectRef SHA")
    try:
        parsed = json.loads(
            object_bytes.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except ContractValidationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            f"{label}.object_bytes must be UTF-8 JSON"
        ) from exc
    parsed_document = _mapping(parsed, f"{label}.parsed_document")
    document = _mapping(bundle.get("document"), f"{label}.document")
    _reject_nonfinite(document, f"{label}.document")
    _same_json(parsed_document, document, f"{label} decoded document")
    return {
        "object_ref": ref,
        "document": document,
        "object_bytes": object_bytes,
    }


def _model_identity(value: Any, label: str) -> dict[str, Any]:
    model = _mapping(value, label)
    if not _MODEL_REQUIRED_FIELDS.issubset(model) or not set(model).issubset(
        _MODEL_FIELDS
    ):
        raise ContractValidationError(f"{label} fields do not match modelIdentity")
    for field in (
        "provider",
        "service_identity",
        "model_id",
        "model_version",
        "endpoint_region",
        "response_id",
    ):
        item = model.get(field)
        if not isinstance(item, str) or not item:
            raise ContractValidationError(f"{label}.{field} is invalid")
    _sha256(model.get("adapter_sha256"), f"{label}.adapter_sha256")
    digest = model.get("model_digest")
    if digest is not None and (
        not isinstance(digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
    ):
        raise ContractValidationError(f"{label}.model_digest is invalid")
    return dict(model)


def validate_translation_candidate(
    candidate_bundle_value: Mapping[str, Any],
    *,
    source_cue_bundle_value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one create-only candidate against its exact source cue bundle."""

    source_bundle = verify_stored_json_bundle(
        source_cue_bundle_value,
        label="source_cue_bundle",
    )
    candidate_bundle = verify_stored_json_bundle(
        candidate_bundle_value,
        label="translation_candidate_bundle",
    )
    source = source_bundle["document"]
    candidate = candidate_bundle["document"]
    _exact_fields(candidate, _CANDIDATE_FIELDS, "translation_candidate")
    if candidate.get("schema_version") != "1" or source.get("schema_version") != "1":
        raise ContractValidationError("candidate/source schema_version must equal '1'")

    source_attempt = _attempt(source.get("attempt"), "source_cue.attempt")
    candidate_attempt = _attempt(
        candidate.get("attempt"),
        "translation_candidate.attempt",
    )
    _same(source_attempt, candidate_attempt, "candidate/source attempt")
    source_cue_id, source_cue_index = _cue(source, "source_cue")
    cue_id, cue_index = _cue(candidate, "translation_candidate")
    _same(source_cue_id, cue_id, "candidate/source cue_id")
    _same(source_cue_index, cue_index, "candidate/source cue_index")

    source_ref = source_bundle["object_ref"]
    candidate_source_ref = _object_ref(
        candidate.get("source_cue_record"),
        "translation_candidate.source_cue_record",
        content_type="application/json",
    )
    _same(source_ref, candidate_source_ref, "candidate source cue ObjectRef")
    _same(
        source_ref["sha256"],
        _sha256(
            candidate.get("source_cue_record_sha256"),
            "translation_candidate.source_cue_record_sha256",
        ),
        "candidate source cue SHA",
    )

    candidate_index = _strict_int(
        candidate.get("candidate_index"),
        "translation_candidate.candidate_index",
    )
    if candidate_index > 2:
        raise ContractValidationError("candidate index exceeds the 0..2 budget")
    expected_reason = "initial" if candidate_index == 0 else "compact_unfit"
    _same(expected_reason, candidate.get("reason"), "candidate reason")
    raw_text = candidate.get("text_vi_raw")
    nfc_text = candidate.get("text_vi_nfc")
    if not isinstance(raw_text, str) or not raw_text:
        raise ContractValidationError("translation_candidate.text_vi_raw is invalid")
    if not isinstance(nfc_text, str) or not nfc_text:
        raise ContractValidationError("translation_candidate.text_vi_nfc is invalid")
    expected_nfc = unicodedata.normalize("NFC", raw_text)
    if nfc_text != expected_nfc or unicodedata.normalize("NFC", nfc_text) != nfc_text:
        raise ContractValidationError("translation candidate NFC binding is invalid")
    generator = _model_identity(
        candidate.get("generator"),
        "translation_candidate.generator",
    )
    _sha256(
        candidate.get("generator_prompt_sha256"),
        "translation_candidate.generator_prompt_sha256",
    )
    candidate_sha = _sha256(
        candidate.get("candidate_sha256"),
        "translation_candidate.candidate_sha256",
    )
    source_created_at = _rfc3339(source.get("created_at"), "source_cue.created_at")
    candidate_created_at = _rfc3339(
        candidate.get("created_at"),
        "translation_candidate.created_at",
    )
    if candidate_created_at < source_created_at:
        raise ContractValidationError("translation candidate predates its source cue")
    source_sha = _sha256(source.get("source_sha256"), "source_cue.source_sha256")
    source_audio = _mapping(
        source.get("source_audio"), "source_cue.source_audio"
    )
    _same(
        44100,
        _strict_int(
            source_audio.get("sample_rate_hz"),
            "source_cue.source_audio.sample_rate_hz",
        ),
        "source cue evidence sample rate",
    )
    source_speech_anchor = _strict_int(
        source_audio.get("speech_start_sample"),
        "source_cue.source_audio.speech_start_sample",
    )
    return {
        "source_bundle": source_bundle,
        "candidate_bundle": candidate_bundle,
        "attempt": candidate_attempt,
        "cue_id": cue_id,
        "cue_index": cue_index,
        "candidate_index": candidate_index,
        "candidate_sha256": candidate_sha,
        "candidate_object_sha256": candidate_bundle["object_ref"]["sha256"],
        "source_cue_record_sha256": source_ref["sha256"],
        "source_sha256": source_sha,
        "source_speech_anchor_sample": source_speech_anchor,
        "text_vi_nfc": nfc_text,
        "generator": generator,
        "source_created_at": source_created_at,
        "candidate_created_at": candidate_created_at,
    }


def _policy(value: Any, label: str, expected_sha: str) -> dict[str, Any]:
    policy = _mapping(value, label)
    _exact_fields(policy, {"name", "version", "sha256"}, label)
    if not isinstance(policy.get("name"), str) or not policy["name"]:
        raise ContractValidationError(f"{label}.name is invalid")
    if policy.get("version") != "1.0.0":
        raise ContractValidationError(f"{label}.version must equal '1.0.0'")
    _same(
        expected_sha,
        _sha256(policy.get("sha256"), f"{label}.sha256"),
        f"{label} SHA",
    )
    return dict(policy)


def _findings(value: Any, label: str) -> None:
    findings = _sequence(value, label)
    for index, raw_finding in enumerate(findings):
        item_label = f"{label}[{index}]"
        finding = _mapping(raw_finding, item_label)
        _exact_fields(
            finding,
            {"code", "severity", "message", "evidence_sha256"},
            item_label,
        )
        code = finding.get("code")
        if (
            not isinstance(code, str)
            or re.fullmatch(r"^[A-Z][A-Z0-9_]{2,63}$", code) is None
        ):
            raise ContractValidationError(f"{item_label}.code is invalid")
        if finding.get("severity") not in {"info", "warning", "error"}:
            raise ContractValidationError(f"{item_label}.severity is invalid")
        message = finding.get("message")
        if not isinstance(message, str) or not 1 <= len(message) <= 500:
            raise ContractValidationError(f"{item_label}.message is invalid")
        evidence = _sequence(
            finding.get("evidence_sha256"),
            f"{item_label}.evidence_sha256",
        )
        for evidence_index, digest in enumerate(evidence):
            _sha256(digest, f"{item_label}.evidence_sha256[{evidence_index}]")


def _validate_preflight_verdict(
    verdict_bundle_value: Mapping[str, Any],
    *,
    role: str,
    policy_sha256: str,
    candidate_context: Mapping[str, Any],
) -> dict[str, Any]:
    bundle = verify_stored_json_bundle(
        verdict_bundle_value,
        label=f"{role}_bundle",
    )
    verdict = bundle["document"]
    _exact_fields(verdict, _VERDICT_FIELDS, role)
    if verdict.get("schema_version") != "1":
        raise ContractValidationError(f"{role}.schema_version must equal '1'")
    _same(role, verdict.get("role"), f"{role} role")
    _same("pass", verdict.get("decision"), f"{role} decision")
    _same(
        candidate_context["attempt"],
        _attempt(verdict.get("attempt"), f"{role}.attempt"),
        f"{role} attempt",
    )
    _policy(verdict.get("policy"), f"{role}.policy", policy_sha256)
    model = _model_identity(verdict.get("model"), f"{role}.model")
    bindings = _mapping(verdict.get("bindings"), f"{role}.bindings")
    _exact_fields(bindings, _PREFLIGHT_BINDING_FIELDS, f"{role}.bindings")
    for field in _PREFLIGHT_BINDING_FIELDS - {"cue_id"}:
        _sha256(bindings.get(field), f"{role}.bindings.{field}")
    _same(
        candidate_context["source_sha256"],
        bindings.get("source_sha256"),
        f"{role} source SHA",
    )
    _same(candidate_context["cue_id"], bindings.get("cue_id"), f"{role} cue")
    _same(
        candidate_context["source_cue_record_sha256"],
        bindings.get("source_cue_record_sha256"),
        f"{role} source cue record SHA",
    )
    _same(
        candidate_context["candidate_object_sha256"],
        bindings.get("translation_attempt_sha256"),
        f"{role} translation candidate object SHA",
    )
    _same(
        candidate_context["candidate_sha256"],
        bindings.get("candidate_sha256"),
        f"{role} candidate SHA",
    )
    _same(policy_sha256, bindings.get("policy_sha256"), f"{role} policy binding")
    _findings(verdict.get("findings"), f"{role}.findings")
    created_at = _rfc3339(verdict.get("created_at"), f"{role}.created_at")
    if created_at < candidate_context["candidate_created_at"]:
        raise ContractValidationError(f"{role} verdict predates its candidate")
    return {
        "bundle": bundle,
        "document": verdict,
        "model": model,
        "bindings": dict(bindings),
        "created_at": created_at,
    }


def _signature(value: Any) -> None:
    signature = _mapping(value, "translation_preflight_approval.signature")
    _exact_fields(
        signature,
        {
            "algorithm",
            "kms_key_version",
            "public_key_sha256",
            "signed_digest",
            "signature_b64",
        },
        "translation_preflight_approval.signature",
    )
    if signature.get("algorithm") != "EC_SIGN_P256_SHA256":
        raise ContractValidationError("approval signature algorithm is invalid")
    key_version = signature.get("kms_key_version")
    if (
        not isinstance(key_version, str)
        or _KMS_KEY_VERSION_RE.fullmatch(key_version) is None
    ):
        raise ContractValidationError("approval KMS key version is invalid")
    _sha256(signature.get("public_key_sha256"), "signature.public_key_sha256")
    _sha256(signature.get("signed_digest"), "signature.signed_digest")
    signature_b64 = signature.get("signature_b64")
    if (
        not isinstance(signature_b64, str)
        or _BASE64_RE.fullmatch(signature_b64) is None
    ):
        raise ContractValidationError("approval signature_b64 is invalid")
    try:
        decoded = base64.b64decode(signature_b64, validate=True)
    except ValueError as exc:
        raise ContractValidationError("approval signature_b64 is invalid") from exc
    if not decoded:
        raise ContractValidationError("approval signature_b64 is empty")


def _approval_verdict_binding(
    value: Any,
    *,
    label: str,
    role: str,
    verdict_ref: Mapping[str, Any],
) -> None:
    binding = _mapping(value, label)
    _exact_fields(binding, {"role", "object", "decision"}, label)
    _same(role, binding.get("role"), f"{label} role")
    _same("pass", binding.get("decision"), f"{label} decision")
    _same(
        verdict_ref,
        _object_ref(
            binding.get("object"),
            f"{label}.object",
            content_type="application/json",
        ),
        f"{label} ObjectRef",
    )


def _assert_independent_models(
    generator: Mapping[str, Any],
    semantic: Mapping[str, Any],
    style: Mapping[str, Any],
) -> None:
    models = (generator, semantic, style)
    model_keys = [
        (
            model["provider"],
            model["model_id"],
            model["model_version"],
            model.get("model_digest"),
        )
        for model in models
    ]
    if len(set(model_keys)) != 3:
        raise ContractValidationError(
            "generator and preflight verifier model/provider identities must differ"
        )
    response_ids = [model["response_id"] for model in models]
    if len(set(response_ids)) != 3:
        raise ContractValidationError(
            "generator and preflight verifier response identities must differ"
        )
    execution_keys = [
        (
            model["provider"],
            model["service_identity"],
            model["model_id"],
            model["model_version"],
            model["endpoint_region"],
            model["adapter_sha256"],
            model["response_id"],
        )
        for model in models
    ]
    if len(set(execution_keys)) != 3:
        raise ContractValidationError(
            "generator and preflight verifier execution identities must differ"
        )


def validate_translation_preflight_approval_structure(
    approval_bundle_value: Mapping[str, Any],
    *,
    candidate_bundle_value: Mapping[str, Any],
    source_cue_bundle_value: Mapping[str, Any],
    semantic_verdict_bundle_value: Mapping[str, Any],
    style_verdict_bundle_value: Mapping[str, Any],
    expected_translation_policy_sha256: str,
    expected_semantic_policy_sha256: str,
    expected_style_policy_sha256: str,
    signature_verified: bool,
) -> dict[str, Any]:
    """Validate signed-approval structure and exact transitive evidence.

    This helper does not perform KMS or allowlist verification and is not an
    authorization boundary. ``signature_verified`` is only an external test
    flag and must not be reused as production proof.
    """

    if signature_verified is not True:
        raise ContractValidationError(
            "translation preflight approval requires signature_verified=True"
        )
    translation_policy_sha = _sha256(
        expected_translation_policy_sha256,
        "expected_translation_policy_sha256",
    )
    semantic_policy_sha = _sha256(
        expected_semantic_policy_sha256,
        "expected_semantic_policy_sha256",
    )
    style_policy_sha = _sha256(
        expected_style_policy_sha256,
        "expected_style_policy_sha256",
    )
    candidate_context = validate_translation_candidate(
        candidate_bundle_value,
        source_cue_bundle_value=source_cue_bundle_value,
    )
    semantic = _validate_preflight_verdict(
        semantic_verdict_bundle_value,
        role="translation_semantic_preflight",
        policy_sha256=semantic_policy_sha,
        candidate_context=candidate_context,
    )
    style = _validate_preflight_verdict(
        style_verdict_bundle_value,
        role="translation_style_preflight",
        policy_sha256=style_policy_sha,
        candidate_context=candidate_context,
    )
    parity_fields = {
        "source_sha256",
        "clean_sha256",
        "source_cue_manifest_sha256",
        "source_cue_collection_root",
        "cue_id",
        "source_cue_record_sha256",
        "translation_attempt_sha256",
        "candidate_sha256",
    }
    for field in parity_fields:
        _same(
            semantic["bindings"][field],
            style["bindings"][field],
            f"semantic/style verdict {field}",
        )
    _assert_independent_models(
        candidate_context["generator"],
        semantic["model"],
        style["model"],
    )

    approval_bundle = verify_stored_json_bundle(
        approval_bundle_value,
        label="translation_preflight_approval_bundle",
    )
    approval = approval_bundle["document"]
    _exact_fields(
        approval,
        {"payload", "signature"},
        "translation_preflight_approval",
    )
    _signature(approval.get("signature"))
    payload = _mapping(
        approval.get("payload"),
        "translation_preflight_approval.payload",
    )
    _exact_fields(
        payload,
        _APPROVAL_PAYLOAD_FIELDS,
        "translation_preflight_approval.payload",
    )
    if payload.get("schema_version") != "1":
        raise ContractValidationError("approval payload schema_version must equal '1'")
    approval_id = payload.get("translation_preflight_approval_id")
    if (
        not isinstance(approval_id, str)
        or _APPROVAL_ID_RE.fullmatch(approval_id) is None
    ):
        raise ContractValidationError("translation preflight approval ID is invalid")
    _same("pass", payload.get("decision"), "translation preflight decision")
    signer = payload.get("controller_signer_principal")
    if not isinstance(signer, str) or _SIGNER_RE.fullmatch(signer) is None:
        raise ContractValidationError("controller signer principal is invalid")

    issued_at = _rfc3339(payload.get("issued_at"), "approval.issued_at")
    expires_at = _rfc3339(payload.get("expires_at"), "approval.expires_at")
    lifetime = expires_at - issued_at
    if lifetime <= timedelta(0) or lifetime > _APPROVAL_MAX_LIFETIME:
        raise ContractValidationError(
            "approval window must be positive and no longer than seven days"
        )
    if issued_at < max(
        candidate_context["candidate_created_at"],
        semantic["created_at"],
        style["created_at"],
    ):
        raise ContractValidationError(
            "approval issuance predates candidate or verifier evidence"
        )

    _same(
        candidate_context["attempt"],
        _attempt(payload.get("attempt"), "approval.attempt"),
        "approval attempt",
    )
    approval_cue_id, approval_cue_index = _cue(payload, "approval")
    _same(candidate_context["cue_id"], approval_cue_id, "approval cue_id")
    _same(candidate_context["cue_index"], approval_cue_index, "approval cue_index")
    _same(
        candidate_context["source_bundle"]["object_ref"],
        _object_ref(
            payload.get("source_cue_record"),
            "approval.source_cue_record",
            content_type="application/json",
        ),
        "approval source cue ObjectRef",
    )
    _same(
        candidate_context["source_cue_record_sha256"],
        _sha256(
            payload.get("source_cue_record_sha256"),
            "approval.source_cue_record_sha256",
        ),
        "approval source cue SHA",
    )
    _same(
        candidate_context["candidate_bundle"]["object_ref"],
        _object_ref(
            payload.get("translation_candidate"),
            "approval.translation_candidate",
            content_type="application/json",
        ),
        "approval candidate ObjectRef",
    )
    _same(
        candidate_context["candidate_object_sha256"],
        _sha256(
            payload.get("translation_candidate_object_sha256"),
            "approval.translation_candidate_object_sha256",
        ),
        "approval candidate object SHA",
    )
    _same(
        candidate_context["candidate_index"],
        _strict_int(payload.get("candidate_index"), "approval.candidate_index"),
        "approval candidate index",
    )
    _same(
        candidate_context["candidate_sha256"],
        _sha256(payload.get("candidate_sha256"), "approval.candidate_sha256"),
        "approval candidate SHA",
    )
    _same(
        translation_policy_sha,
        _sha256(
            payload.get("translation_policy_sha256"),
            "approval.translation_policy_sha256",
        ),
        "approval translation policy",
    )
    _same(
        semantic_policy_sha,
        _sha256(
            payload.get("semantic_policy_sha256"),
            "approval.semantic_policy_sha256",
        ),
        "approval semantic policy",
    )
    _same(
        style_policy_sha,
        _sha256(
            payload.get("style_policy_sha256"),
            "approval.style_policy_sha256",
        ),
        "approval style policy",
    )
    _approval_verdict_binding(
        payload.get("semantic_verdict"),
        label="approval.semantic_verdict",
        role="translation_semantic_preflight",
        verdict_ref=semantic["bundle"]["object_ref"],
    )
    _approval_verdict_binding(
        payload.get("style_verdict"),
        label="approval.style_verdict",
        role="translation_style_preflight",
        verdict_ref=style["bundle"]["object_ref"],
    )
    return {
        "approval_bundle": approval_bundle,
        "payload": payload,
        "candidate_context": candidate_context,
        "semantic_verdict": semantic,
        "style_verdict": style,
        "approval_id": approval_id,
        "issued_at": issued_at,
        "expires_at": expires_at,
    }


def _validate_detector(
    value: Any,
    label: str,
    *,
    require_active: bool,
) -> dict[str, Any]:
    detector = _mapping(value, label)
    _exact_fields(detector, _DETECTOR_FIELDS, label)
    constants = {
        "sample_rate_hz": 44100,
        "window_samples": 882,
        "hop_samples": 441,
        "tail_window_padding": "right_zero_pad",
        "rms_denominator_samples": 882,
        "active_threshold_dbfs": -45,
        "active_threshold_linear": 0.005623413251903491,
        "consecutive_windows": 2,
    }
    for field, expected in constants.items():
        _same(expected, detector.get(field), f"{label}.{field}")
    decoded_count = _strict_int(
        detector.get("decoded_sample_count"),
        f"{label}.decoded_sample_count",
        minimum=1,
    )
    _sha256(detector.get("implementation_sha256"), f"{label}.implementation_sha256")
    runs = _sequence(detector.get("active_runs"), f"{label}.active_runs")
    if not runs:
        if require_active:
            raise ContractValidationError(f"{label}.active_runs cannot be empty")
        if (
            detector.get("onset_sample") is not None
            or detector.get("offset_sample_inclusive") is not None
        ):
            raise ContractValidationError(
                f"{label} inactive detector endpoints must be null"
            )
        return {
            "decoded_sample_count": decoded_count,
            "onset_sample": None,
            "offset_sample_inclusive": None,
        }
    if not require_active:
        raise ContractValidationError(f"{label}.active_runs must be empty")
    normalized: list[tuple[int, int]] = []
    previous_offset: int | None = None
    for index, raw_run in enumerate(runs):
        run_label = f"{label}.active_runs[{index}]"
        run = _mapping(raw_run, run_label)
        _exact_fields(run, {"onset_sample", "offset_sample_inclusive"}, run_label)
        onset = _strict_int(run.get("onset_sample"), f"{run_label}.onset_sample")
        offset = _strict_int(
            run.get("offset_sample_inclusive"),
            f"{run_label}.offset_sample_inclusive",
        )
        if onset > offset or offset >= decoded_count:
            raise ContractValidationError(f"{run_label} is outside decoded PCM")
        if previous_offset is not None and onset <= previous_offset:
            raise ContractValidationError(f"{label}.active_runs overlap or reorder")
        normalized.append((onset, offset))
        previous_offset = offset
    _same(normalized[0][0], detector.get("onset_sample"), f"{label}.onset_sample")
    _same(
        normalized[-1][1],
        detector.get("offset_sample_inclusive"),
        f"{label}.offset_sample_inclusive",
    )
    return {
        "decoded_sample_count": decoded_count,
        "onset_sample": normalized[0][0],
        "offset_sample_inclusive": normalized[-1][1],
    }


def _validate_timing(
    schedule_value: Any,
    detector: Mapping[str, Any],
    *,
    previous_actual_offset_inclusive: int | None,
    outcome: str,
) -> dict[str, Any]:
    schedule = _mapping(schedule_value, "tts_attempt.schedule")
    _exact_fields(schedule, _SCHEDULE_FIELDS, "tts_attempt.schedule")
    anchor = _strict_int(
        schedule.get("speech_anchor_sample"),
        "schedule.speech_anchor_sample",
    )
    video_count = _strict_int(
        schedule.get("video_sample_count"),
        "schedule.video_sample_count",
        minimum=1,
    )
    placement = _strict_int(
        schedule.get("placement_sample"),
        "schedule.placement_sample",
    )
    actual_onset = _strict_int(
        schedule.get("actual_onset_sample"),
        "schedule.actual_onset_sample",
    )
    actual_offset = _strict_int(
        schedule.get("actual_offset_sample_inclusive"),
        "schedule.actual_offset_sample_inclusive",
    )
    expected_onset = placement + detector["onset_sample"]
    expected_offset = placement + detector["offset_sample_inclusive"]
    if actual_onset > actual_offset:
        raise ContractValidationError("schedule active interval is reversed")
    _same(expected_onset, actual_onset, "schedule actual onset equation")
    _same(expected_offset, actual_offset, "schedule actual offset equation")
    lag = actual_onset - anchor
    _same(lag, schedule.get("lag_samples"), "schedule lag equation")

    if previous_actual_offset_inclusive is None:
        previous = None
        expected_gap = None
        expected_overlap = 0
    else:
        previous = _strict_int(
            previous_actual_offset_inclusive,
            "previous_actual_offset_inclusive",
        )
        expected_gap = actual_onset - previous
        expected_overlap = max(0, previous - actual_onset + 1)
    _same(
        previous,
        schedule.get("previous_actual_offset_sample_inclusive"),
        "schedule previous offset binding",
    )
    _same(expected_gap, schedule.get("previous_gap_samples"), "schedule gap equation")
    _same(
        expected_overlap,
        schedule.get("overlap_samples"),
        "schedule overlap equation",
    )
    computed_fits = (
        0 <= lag <= 26460
        and (expected_gap is None or expected_gap >= 2205)
        and expected_overlap == 0
        and actual_offset <= video_count - 1
    )
    _same(computed_fits, schedule.get("fits"), "schedule fits decision")
    if outcome == "succeeded":
        target_onset = max(
            anchor,
            anchor if previous is None else previous + 2205,
        )
        _same(
            target_onset,
            schedule.get("target_actual_onset_sample"),
            "successful schedule target equation",
        )
        _same(
            max(0, target_onset - detector["onset_sample"]),
            placement,
            "successful schedule placement equation",
        )
        if not computed_fits:
            raise ContractValidationError("succeeded TTS schedule does not fit")
    elif computed_fits:
        raise ContractValidationError("schedule_unfit preserved a fitting schedule")
    return {
        "actual_onset_sample": actual_onset,
        "actual_offset_sample_inclusive": actual_offset,
        "fits": computed_fits,
        "lag_samples": lag,
        "previous_gap_samples": expected_gap,
        "overlap_samples": expected_overlap,
    }


def _validate_tts_document(
    tts: Mapping[str, Any],
    *,
    approval_context: Mapping[str, Any],
    previous_actual_offset_inclusive: int | None,
) -> dict[str, Any]:
    if not _TTS_REQUIRED_FIELDS.issubset(tts) or not set(tts).issubset(
        _TTS_REQUIRED_FIELDS | _TTS_OPTIONAL_FIELDS
    ):
        raise ContractValidationError("tts_attempt fields do not match the closed contract")
    if tts.get("schema_version") != "1":
        raise ContractValidationError("tts_attempt.schema_version must equal '1'")
    candidate_context = approval_context["candidate_context"]
    _same(
        candidate_context["attempt"],
        _attempt(tts.get("attempt"), "tts_attempt.attempt"),
        "TTS attempt fence",
    )
    cue_id, cue_index = _cue(tts, "tts_attempt")
    _same(candidate_context["cue_id"], cue_id, "TTS cue_id")
    _same(candidate_context["cue_index"], cue_index, "TTS cue_index")
    candidate_index = _strict_int(
        tts.get("candidate_index"),
        "tts_attempt.candidate_index",
    )
    _same(candidate_context["candidate_index"], candidate_index, "TTS candidate index")
    _same(
        candidate_context["candidate_bundle"]["object_ref"],
        _object_ref(
            tts.get("translation_candidate"),
            "tts_attempt.translation_candidate",
            content_type="application/json",
        ),
        "TTS candidate ObjectRef",
    )
    _same(
        candidate_context["candidate_object_sha256"],
        _sha256(
            tts.get("translation_candidate_object_sha256"),
            "tts_attempt.translation_candidate_object_sha256",
        ),
        "TTS candidate object SHA",
    )
    _same(
        candidate_context["candidate_sha256"],
        _sha256(
            tts.get("translation_candidate_sha256"),
            "tts_attempt.translation_candidate_sha256",
        ),
        "TTS candidate semantic SHA",
    )
    _same(
        approval_context["approval_bundle"]["object_ref"],
        _object_ref(
            tts.get("translation_preflight_approval"),
            "tts_attempt.translation_preflight_approval",
            content_type="application/json",
        ),
        "TTS approval ObjectRef",
    )
    _same(
        approval_context["approval_bundle"]["object_ref"]["sha256"],
        _sha256(
            tts.get("translation_preflight_approval_sha256"),
            "tts_attempt.translation_preflight_approval_sha256",
        ),
        "TTS approval SHA",
    )
    if (
        tts.get("provider") != "capcut-private"
        or tts.get("voice") != "BV075_streaming"
        or tts.get("prosody_rate") != "1.5000"
    ):
        raise ContractValidationError("TTS provider/voice/rate binding is invalid")
    _sha256(
        tts.get("normalized_request_sha256"),
        "tts_attempt.normalized_request_sha256",
    )
    created_at = _rfc3339(tts.get("created_at"), "tts_attempt.created_at")
    if not (
        approval_context["issued_at"]
        <= created_at
        < approval_context["expires_at"]
    ):
        raise ContractValidationError(
            "TTS creation is outside the live approval window"
        )

    outcome = tts.get("outcome")
    response_id = tts.get("provider_response_id")
    if response_id is not None and (
        not isinstance(response_id, str) or not response_id
    ):
        raise ContractValidationError("TTS provider_response_id is invalid")
    media_fields = {"returned_media", "decoded_pcm", "silence_detector", "schedule"}
    timing: dict[str, Any] | None = None
    if outcome == "provider_failed":
        _same("TTS_PROVIDER_FAILED", tts.get("error_code"), "provider failure code")
        if set(tts) & media_fields:
            raise ContractValidationError("provider failure cannot contain media evidence")
    elif outcome == "media_invalid":
        if not isinstance(response_id, str) or not response_id:
            raise ContractValidationError("media_invalid requires provider response ID")
        _object_ref(
            tts.get("returned_media"),
            "tts_attempt.returned_media",
            content_type="audio/mpeg",
        )
        error_code = tts.get("error_code")
        if error_code == "TTS_MEDIA_INVALID":
            if set(tts) & {"decoded_pcm", "silence_detector", "schedule"}:
                raise ContractValidationError(
                    "undecodable media cannot contain decoded/schedule evidence"
                )
        elif error_code == "TTS_VOICE_RATE_MISMATCH":
            _object_ref(tts.get("decoded_pcm"), "tts_attempt.decoded_pcm")
            _validate_detector(
                tts.get("silence_detector"),
                "tts_attempt.silence_detector",
                require_active=True,
            )
            if "schedule" in tts:
                raise ContractValidationError(
                    "voice/rate mismatch cannot contain schedule evidence"
                )
        else:
            raise ContractValidationError("media_invalid error code is invalid")
    elif outcome == "no_active_speech":
        _same("TTS_MEDIA_INVALID", tts.get("error_code"), "no-speech error code")
        if not isinstance(response_id, str) or not response_id:
            raise ContractValidationError(
                "no_active_speech requires provider response ID"
            )
        _object_ref(
            tts.get("returned_media"),
            "tts_attempt.returned_media",
            content_type="audio/mpeg",
        )
        _object_ref(tts.get("decoded_pcm"), "tts_attempt.decoded_pcm")
        _validate_detector(
            tts.get("silence_detector"),
            "tts_attempt.silence_detector",
            require_active=False,
        )
        if "schedule" in tts:
            raise ContractValidationError(
                "no_active_speech cannot contain schedule evidence"
            )
    elif outcome in {"succeeded", "schedule_unfit"}:
        if not isinstance(response_id, str) or not response_id:
            raise ContractValidationError(f"{outcome} requires provider response ID")
        _object_ref(
            tts.get("returned_media"),
            "tts_attempt.returned_media",
            content_type="audio/mpeg",
        )
        _object_ref(tts.get("decoded_pcm"), "tts_attempt.decoded_pcm")
        detector = _validate_detector(
            tts.get("silence_detector"),
            "tts_attempt.silence_detector",
            require_active=True,
        )
        schedule = _mapping(tts.get("schedule"), "tts_attempt.schedule")
        _same(
            candidate_context["source_speech_anchor_sample"],
            _strict_int(
                schedule.get("speech_anchor_sample"),
                "tts_attempt.schedule.speech_anchor_sample",
            ),
            "TTS speech anchor/source cue binding",
        )
        timing = _validate_timing(
            schedule,
            detector,
            previous_actual_offset_inclusive=previous_actual_offset_inclusive,
            outcome=outcome,
        )
        if outcome == "succeeded":
            if "error_code" in tts:
                raise ContractValidationError("succeeded TTS cannot contain error_code")
        else:
            expected_error = (
                "DUB_CUE_UNFIT"
                if candidate_index == 2
                else "DUB_SCHEDULE_INVALID"
            )
            _same(expected_error, tts.get("error_code"), "schedule failure code")
    else:
        raise ContractValidationError("TTS outcome is invalid")

    return {
        "candidate_index": candidate_index,
        "outcome": outcome,
        "error_code": tts.get("error_code"),
        "created_at": created_at,
        "timing": timing,
    }


def validate_tts_attempt_chain_structure(
    tts_bundle_value: Mapping[str, Any],
    *,
    candidate_bundle_value: Mapping[str, Any],
    source_cue_bundle_value: Mapping[str, Any],
    approval_bundle_value: Mapping[str, Any],
    semantic_verdict_bundle_value: Mapping[str, Any],
    style_verdict_bundle_value: Mapping[str, Any],
    expected_translation_policy_sha256: str,
    expected_semantic_policy_sha256: str,
    expected_style_policy_sha256: str,
    signature_verified: bool,
    previous_actual_offset_inclusive: int | None = None,
) -> dict[str, Any]:
    """Validate TTS-chain structure without granting synthesis authority."""

    approval_context = validate_translation_preflight_approval_structure(
        approval_bundle_value,
        candidate_bundle_value=candidate_bundle_value,
        source_cue_bundle_value=source_cue_bundle_value,
        semantic_verdict_bundle_value=semantic_verdict_bundle_value,
        style_verdict_bundle_value=style_verdict_bundle_value,
        expected_translation_policy_sha256=expected_translation_policy_sha256,
        expected_semantic_policy_sha256=expected_semantic_policy_sha256,
        expected_style_policy_sha256=expected_style_policy_sha256,
        signature_verified=signature_verified,
    )
    tts_bundle = verify_stored_json_bundle(
        tts_bundle_value,
        label="tts_attempt_bundle",
    )
    result = _validate_tts_document(
        tts_bundle["document"],
        approval_context=approval_context,
        previous_actual_offset_inclusive=previous_actual_offset_inclusive,
    )
    result["tts_bundle"] = tts_bundle
    result["approval_context"] = approval_context
    return result


def validate_candidate_tts_family_structure(
    *,
    source_cue_bundle_value: Mapping[str, Any],
    candidate_bundle_values: Sequence[Mapping[str, Any]],
    preflight_evidence_values: Sequence[Mapping[str, Any]],
    tts_bundle_values: Sequence[Mapping[str, Any]],
    expected_translation_policy_sha256: str,
    expected_semantic_policy_sha256: str,
    expected_style_policy_sha256: str,
    previous_actual_offset_inclusive: int | None = None,
) -> dict[str, Any]:
    """Validate one cue-family structure from candidate 0 through terminal TTS.

    Each preflight evidence item is a closed object with:
    ``approval_bundle``, ``semantic_verdict_bundle``,
    ``style_verdict_bundle`` and the externally established
    ``signature_verified`` external test flag. This helper does not perform KMS
    or allowlist verification and cannot authorize production synthesis.
    """

    candidates = _sequence(candidate_bundle_values, "candidate_bundle_values")
    evidence_items = _sequence(
        preflight_evidence_values,
        "preflight_evidence_values",
    )
    tts_bundles = _sequence(tts_bundle_values, "tts_bundle_values")
    if not candidates or len(candidates) > 3:
        raise ContractValidationError("candidate family must contain 1..3 candidates")
    if len(evidence_items) != len(candidates) or len(tts_bundles) != len(candidates):
        raise ContractValidationError(
            "family requires exactly one approval evidence set and one TTS per candidate"
        )

    candidate_contexts = [
        validate_translation_candidate(
            candidate_bundle,
            source_cue_bundle_value=source_cue_bundle_value,
        )
        for candidate_bundle in candidates
    ]
    candidate_contexts.sort(key=lambda item: item["candidate_index"])
    indexes = [item["candidate_index"] for item in candidate_contexts]
    if indexes != list(range(len(candidate_contexts))) or indexes[-1] > 2:
        raise ContractValidationError("candidate indices must be contiguous 0..k<=2")
    baseline = candidate_contexts[0]
    for context in candidate_contexts:
        _same(baseline["attempt"], context["attempt"], "family attempt")
        _same(baseline["cue_id"], context["cue_id"], "family cue_id")
        _same(baseline["cue_index"], context["cue_index"], "family cue_index")
        _same(
            baseline["source_cue_record_sha256"],
            context["source_cue_record_sha256"],
            "family source cue SHA",
        )
    if len({item["candidate_sha256"] for item in candidate_contexts}) != len(
        candidate_contexts
    ):
        raise ContractValidationError("family reuses a candidate semantic SHA")
    if len({item["candidate_object_sha256"] for item in candidate_contexts}) != len(
        candidate_contexts
    ):
        raise ContractValidationError("family reuses a candidate object SHA")

    evidence_by_index: dict[int, Mapping[str, Any]] = {}
    for offset, raw_evidence in enumerate(evidence_items):
        evidence = _mapping(
            raw_evidence,
            f"preflight_evidence_values[{offset}]",
        )
        _exact_fields(
            evidence,
            {
                "approval_bundle",
                "semantic_verdict_bundle",
                "style_verdict_bundle",
                "signature_verified",
            },
            f"preflight_evidence_values[{offset}]",
        )
        candidate_index = validate_translation_candidate(
            candidates[offset],
            source_cue_bundle_value=source_cue_bundle_value,
        )["candidate_index"]
        if candidate_index in evidence_by_index:
            raise ContractValidationError("duplicate preflight evidence candidate index")
        evidence_by_index[candidate_index] = evidence

    candidate_bundle_by_index = {
        validate_translation_candidate(
            candidate_bundle,
            source_cue_bundle_value=source_cue_bundle_value,
        )["candidate_index"]: candidate_bundle
        for candidate_bundle in candidates
    }
    tts_bundle_by_index: dict[int, Mapping[str, Any]] = {}
    for offset, tts_bundle in enumerate(tts_bundles):
        verified = verify_stored_json_bundle(
            tts_bundle,
            label=f"tts_bundle_values[{offset}]",
        )
        index = _strict_int(
            verified["document"].get("candidate_index"),
            f"tts_bundle_values[{offset}].candidate_index",
        )
        if index in tts_bundle_by_index:
            raise ContractValidationError("family contains multiple TTS attempts for a candidate")
        tts_bundle_by_index[index] = tts_bundle
    if set(tts_bundle_by_index) != set(indexes) or set(evidence_by_index) != set(
        indexes
    ):
        raise ContractValidationError(
            "candidate/approval/TTS indices do not form one-to-one family"
        )

    results: list[dict[str, Any]] = []
    seen_approval_shas: set[str] = set()
    seen_tts_shas: set[str] = set()
    for index in indexes:
        evidence = evidence_by_index[index]
        result = validate_tts_attempt_chain_structure(
            tts_bundle_by_index[index],
            candidate_bundle_value=candidate_bundle_by_index[index],
            source_cue_bundle_value=source_cue_bundle_value,
            approval_bundle_value=evidence["approval_bundle"],
            semantic_verdict_bundle_value=evidence["semantic_verdict_bundle"],
            style_verdict_bundle_value=evidence["style_verdict_bundle"],
            expected_translation_policy_sha256=expected_translation_policy_sha256,
            expected_semantic_policy_sha256=expected_semantic_policy_sha256,
            expected_style_policy_sha256=expected_style_policy_sha256,
            signature_verified=evidence["signature_verified"],
            previous_actual_offset_inclusive=previous_actual_offset_inclusive,
        )
        approval_sha = result["approval_context"]["approval_bundle"]["object_ref"][
            "sha256"
        ]
        tts_sha = result["tts_bundle"]["object_ref"]["sha256"]
        if approval_sha in seen_approval_shas or tts_sha in seen_tts_shas:
            raise ContractValidationError("family reuses approval or TTS object SHA")
        seen_approval_shas.add(approval_sha)
        seen_tts_shas.add(tts_sha)
        results.append(result)

    terminal_outcomes = {
        "succeeded",
        "provider_failed",
        "media_invalid",
        "no_active_speech",
    }
    for index, result in enumerate(results):
        if index > 0 and results[index - 1]["outcome"] != "schedule_unfit":
            raise ContractValidationError(
                "compact candidate exists without prior schedule_unfit"
            )
        if (
            index > 0
            and candidate_contexts[index]["candidate_created_at"]
            < results[index - 1]["created_at"]
        ):
            raise ContractValidationError(
                "compact candidate predates the prior schedule_unfit evidence"
            )
        if result["outcome"] in terminal_outcomes and index != len(results) - 1:
            raise ContractValidationError(
                "candidate family continues after a terminal TTS outcome"
            )
        if result["error_code"] == "DUB_CUE_UNFIT" and not (
            index == 2 and index == len(results) - 1
        ):
            raise ContractValidationError(
                "DUB_CUE_UNFIT is allowed only on final candidate 2"
            )
    final = results[-1]
    if final["outcome"] == "schedule_unfit":
        if indexes[-1] != 2 or final["error_code"] != "DUB_CUE_UNFIT":
            raise ContractValidationError(
                "completed schedule-unfit family must exhaust candidate 2"
            )
    elif final["outcome"] not in terminal_outcomes:
        raise ContractValidationError("candidate family has no terminal outcome")
    return {
        "cue_id": baseline["cue_id"],
        "candidate_count": len(results),
        "terminal_outcome": final["outcome"],
        "terminal_error_code": final["error_code"],
        "results": results,
    }


__all__ = [
    "validate_translation_candidate",
    "verify_stored_json_bundle",
]
