from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from fractions import Fraction
from typing import Any, Final

from .enums import (
    CLEAN_VERDICT_ROLES,
    ERROR_CODE_VALUES,
    ERROR_CODE_SET,
    ERROR_SEMANTICS,
    ERROR_STAGE_VALUES,
    ERROR_STAGE_SET,
    FAILURE_STATES,
    FINAL_DUB_VERDICT_ROLES,
    LAUNCH_STATE_VALUES,
    MAX_COMPACT_RETRIES,
    SAFE_ERROR_CONTEXT_KEYS,
    STATE_VALUES,
    STATE_SET,
    TERMINAL_STATES,
    TRANSLATION_PREFLIGHT_ROLES,
)


class ContractValidationError(ValueError):
    """Raised when a value violates a semantic v1 contract invariant."""


SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
CUE_ID_RE: Final = re.compile(r"^cue-[0-9]{6}$")
GCS_URI_RE: Final = re.compile(r"^gs://[^/\s]+/[^\r\n]+$")
DECIMAL_GENERATION_RE: Final = re.compile(r"^[1-9][0-9]*$")
PTS_RE: Final = re.compile(r"^-?[0-9]+/[1-9][0-9]*$")

OBJECT_REF_FIELDS: Final = frozenset(
    {"uri", "generation", "size_bytes", "sha256", "content_type"}
)
ATTEMPT_FIELDS: Final = frozenset(
    {"job_id", "attempt_id", "attempt_seq", "fence_digest"}
)
CLEAN_BINDING_FIELDS: Final = (
    "source_sha256",
    "clean_sha256",
    "source_cue_manifest_sha256",
    "source_cue_collection_root",
    "clean_policy_sha256",
)
ATTEMPT_BINDING_FIELDS: Final = (
    "job_id",
    "attempt_id",
    "attempt_seq",
    "fence_digest",
)
CLEAN_INDEX_BINDING_FIELDS: Final = ATTEMPT_BINDING_FIELDS + (
    "clean_approval_id",
) + CLEAN_BINDING_FIELDS
RELEASE_BINDING_FIELDS: Final = ATTEMPT_BINDING_FIELDS + (
    "release_approval_id",
    "clean_approval_id",
    "clean_sha256",
    "source_cue_manifest_sha256",
    "cue_ledger_sha256",
    "dubbed_sha256",
    "clean_policy_sha256",
    "dub_policy_sha256",
)

_ALLOWED_CONTENT_TYPES: Final = frozenset(
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
_SAFE_ERROR_FIELDS: Final = frozenset(
    {"code", "stage", "message", "retryable", "context"}
)
_FORBIDDEN_SAFE_TEXT_RE: Final = re.compile(
    r"(?:https?://|gs://|bearer\s+|api[_ -]?key|token\s*[=:]|"
    r"secret\s*[=:]|sig\s*=|traceback|-----BEGIN)",
    re.IGNORECASE,
)
_DUB_WORK_ITEM_FIELDS: Final = frozenset(
    {
        "schema_version",
        "attempt",
        "clean_approval_id",
        "clean_approval",
        "clean_gate_index",
        "clean",
        "clean_manifest",
        "source_cue_manifest",
        "source_cue_evidence",
        "clean_policy_sha256",
        "expires_at",
    }
)
_CALLBACK_FIELDS: Final = frozenset(
    {
        "schema_version",
        "event",
        "event_id",
        "job_id",
        "attempt_id",
        "attempt_seq",
        "state",
        "state_version",
        "verification_passed",
        "clean_approval_id",
        "release_approval_id",
        "source_sha256",
        "clean_sha256",
        "source_cue_manifest_sha256",
        "cue_ledger_sha256",
        "dubbed_sha256",
        "error",
        "occurred_at",
    }
)
_IDEMPOTENCY_RECORD_FIELDS: Final = frozenset(
    {
        "schema_version",
        "record_version",
        "idempotency_key_sha256",
        "caller_scope_sha256",
        "request_bytes_contract",
        "canonical_request_projection",
        "canonical_request_sha256",
        "bound_attempt",
        "replay_binding",
        "retention_seconds",
        "conflict_observations",
        "created_at",
        "expires_at",
    }
)
_CREATE_REQUEST_FIELDS: Final = frozenset(
    {"schema_version", "douyin_url", "force_refresh", "client_context"}
)
_TRANSLATION_ATTEMPT_FIELDS: Final = frozenset(
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
_COMPLETED_CALLBACK_HASH_FIELDS: Final = (
    "source_sha256",
    "clean_sha256",
    "source_cue_manifest_sha256",
    "cue_ledger_sha256",
    "dubbed_sha256",
)
_APPROVAL_MAX_LIFETIME: Final = timedelta(days=7)
_DOWNLOAD_LINK_MAX_LIFETIME: Final = timedelta(hours=24)
REQUIRED_MACHINE_METRIC_NAMES: Final = {
    "clean": frozenset(
        {
            "video_timeline",
            "video_metadata",
            "clean_audio",
            "source_cue_provenance",
            "residual_ocr",
            "mask_precision",
            "outside_mask_damage",
            "temporal_flicker",
        }
    ),
    "dub": frozenset(
        {
            "cue_cardinality",
            "translation",
            "tts",
            "silence_detector",
            "schedule",
            "vietsub_style_sync",
            "dubbed_video_integrity",
            "mix_graph_stem_integrity",
            "audio_metrics",
            "asr_backcheck",
        }
    ),
}
_MACHINE_POLICY_NAME_BY_GATE: Final = {
    "clean": "clean_qa_policy",
    "dub": "dub_qa_policy",
}
_FORBIDDEN_WORK_KEY_RE: Final = re.compile(
    r"(?:raw[_-]?source|(?:^|_)source_uri$|(?:^|_)source_video_uri$|"
    r"(?:^|_)source_prefix$|(?:^|_)list(?:_|$)|latest)",
    re.IGNORECASE,
)
_FORBIDDEN_WORK_VALUE_RE: Final = re.compile(
    r"(?:/source\.mp4(?:$|[?#])|/raw[-_]source(?:/|$)|/latest(?:/|$))",
    re.IGNORECASE,
)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError(f"{label} must be an object")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ContractValidationError(f"{label} must be an array")
    return value


def _strict_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractValidationError(f"{label} must be an integer >= {minimum}")
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractValidationError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ContractValidationError(f"{label} must be a finite number")
    return result


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ContractValidationError(f"{label} must be a lowercase SHA-256 hex")
    return value


def _cue_id(value: Any, cue_index: int, label: str) -> str:
    expected = f"cue-{cue_index:06d}"
    if not isinstance(value, str) or not CUE_ID_RE.fullmatch(value) or value != expected:
        raise ContractValidationError(f"{label} must equal {expected!r}")
    return value


def _nfc_pair(raw: Any, normalized: Any, label: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise ContractValidationError(f"{label}.raw must be a non-empty string")
    if not isinstance(normalized, str) or not normalized:
        raise ContractValidationError(f"{label}.nfc must be a non-empty string")
    expected = unicodedata.normalize("NFC", raw)
    if normalized != expected or unicodedata.normalize("NFC", normalized) != normalized:
        raise ContractValidationError(f"{label} NFC value does not match raw text")
    return normalized


def _attempt(value: Any, label: str) -> dict[str, Any]:
    attempt = _mapping(value, label)
    if set(attempt) != ATTEMPT_FIELDS:
        raise ContractValidationError(f"{label} must contain the exact attempt fields")
    job_id = attempt.get("job_id")
    attempt_id = attempt.get("attempt_id")
    if not isinstance(job_id, str) or not re.fullmatch(r"desub-[0-9a-f]{32}", job_id):
        raise ContractValidationError(f"{label}.job_id is invalid")
    if not isinstance(attempt_id, str) or not re.fullmatch(
        r"att-[0-9a-f]{32}", attempt_id
    ):
        raise ContractValidationError(f"{label}.attempt_id is invalid")
    attempt_seq = _strict_int(attempt.get("attempt_seq"), f"{label}.attempt_seq", minimum=1)
    fence_digest = _sha256(attempt.get("fence_digest"), f"{label}.fence_digest")
    return {
        "job_id": job_id,
        "attempt_id": attempt_id,
        "attempt_seq": attempt_seq,
        "fence_digest": fence_digest,
    }


def _object_ref(value: Any, label: str) -> dict[str, Any]:
    ref = _mapping(value, label)
    if set(ref) != OBJECT_REF_FIELDS:
        raise ContractValidationError(f"{label} must be an exact ObjectRef")
    uri = ref.get("uri")
    generation = ref.get("generation")
    size_bytes = ref.get("size_bytes")
    content_type = ref.get("content_type")
    if not isinstance(uri, str) or not GCS_URI_RE.fullmatch(uri):
        raise ContractValidationError(f"{label}.uri is invalid")
    if not isinstance(generation, str) or not DECIMAL_GENERATION_RE.fullmatch(
        generation
    ):
        raise ContractValidationError(f"{label}.generation is invalid")
    _strict_int(size_bytes, f"{label}.size_bytes", minimum=1)
    sha256 = _sha256(ref.get("sha256"), f"{label}.sha256")
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise ContractValidationError(f"{label}.content_type is invalid")
    return {
        "uri": uri,
        "generation": generation,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "content_type": content_type,
    }


def _fraction(value: Any, label: str) -> Fraction:
    if not isinstance(value, str) or not PTS_RE.fullmatch(value):
        raise ContractValidationError(f"{label} must be an integer rational")
    numerator, denominator = value.split("/", 1)
    return Fraction(int(numerator), int(denominator))


def _rfc3339(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T[^\s]+(?:Z|[+-]\d{2}:\d{2})",
        value,
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


def _approval_window(payload: Mapping[str, Any], label: str) -> None:
    issued_at = _rfc3339(payload.get("issued_at"), f"{label}.issued_at")
    expires_at = _rfc3339(payload.get("expires_at"), f"{label}.expires_at")
    lifetime = expires_at - issued_at
    if lifetime <= timedelta(0) or lifetime > _APPROVAL_MAX_LIFETIME:
        raise ContractValidationError(
            f"{label} expiry must be after issuance and no more than seven days"
        )


def _same(expected: Any, actual: Any, label: str) -> None:
    if actual != expected:
        raise ContractValidationError(f"{label} mismatch")


def _payload(value: Any, label: str) -> Mapping[str, Any]:
    envelope = _mapping(value, label)
    return _mapping(envelope.get("payload"), f"{label}.payload")


def validate_safe_error(
    value: Mapping[str, Any],
    *,
    http_status: int | None = None,
    terminal_state: str | None = None,
) -> None:
    error = _mapping(value, "error")
    unknown = set(error) - _SAFE_ERROR_FIELDS
    missing = {"code", "stage", "message", "retryable", "context"} - set(error)
    if unknown or missing:
        raise ContractValidationError("error fields do not match the closed contract")
    if error["code"] not in ERROR_CODE_SET:
        raise ContractValidationError(f"unknown stable error code: {error['code']!r}")
    if error["stage"] not in ERROR_STAGE_SET:
        raise ContractValidationError(f"unknown error stage: {error['stage']!r}")
    semantics = ERROR_SEMANTICS[error["code"]]
    stage = error["stage"]
    if stage not in semantics["allowed_stages"]:
        raise ContractValidationError(
            f"error stage is not allowed for {error['code']}"
        )
    message = error["message"]
    if not isinstance(message, str) or not 1 <= len(message) <= 240:
        raise ContractValidationError("error.message must contain 1..240 characters")
    if "\n" in message or "\r" in message or _FORBIDDEN_SAFE_TEXT_RE.search(message):
        raise ContractValidationError("error.message contains sensitive or unsafe text")
    if not isinstance(error["retryable"], bool):
        raise ContractValidationError("error.retryable must be a boolean")
    _same(
        semantics["retryable"],
        error["retryable"],
        f"{error['code']} retryable",
    )
    context = _mapping(error.get("context"), "error.context")
    if set(context) != set(semantics["context_keys"]):
        raise ContractValidationError(
            f"error.context keys do not match {error['code']} semantics"
        )
    for key, item in context.items():
        if key == "cue_index":
            _strict_int(item, "error.context.cue_index")
        elif key == "limit_name":
            if (
                not isinstance(item, str)
                or len(item) > 64
                or _FORBIDDEN_SAFE_TEXT_RE.search(item)
            ):
                raise ContractValidationError(
                    "error.context.limit_name is invalid"
                )
        elif key == "retry_after_seconds":
            retry_after = _strict_int(
                item,
                "error.context.retry_after_seconds",
                minimum=1,
            )
            if retry_after > 86400:
                raise ContractValidationError(
                    "error.context.retry_after_seconds exceeds 86400"
                )
    if http_status is not None:
        if isinstance(http_status, bool) or not isinstance(http_status, int):
            raise ContractValidationError("public HTTP status must be an integer")
        _same(
            semantics["public_http_status"],
            http_status,
            f"{error['code']} public HTTP status",
        )
    if terminal_state is not None:
        if terminal_state not in TERMINAL_STATES:
            raise ContractValidationError("error terminal state is invalid")
        expected_terminal = semantics["terminal_state_by_stage"][stage]
        if expected_terminal is None:
            raise ContractValidationError(
                f"{error['code']} is not a terminal-attempt error at stage {stage}"
            )
        _same(
            expected_terminal,
            terminal_state,
            f"{error['code']} terminal state",
        )


def validate_machine_report_aggregate(
    report_value: Mapping[str, Any],
) -> None:
    report = _mapping(report_value, "machine_report")
    gate = report.get("gate")
    if gate not in REQUIRED_MACHINE_METRIC_NAMES:
        raise ContractValidationError("machine report gate is invalid")
    policy = _mapping(report.get("policy"), "machine_report.policy")
    _same(
        _MACHINE_POLICY_NAME_BY_GATE[gate],
        policy.get("name"),
        "machine report policy/gate",
    )
    _same("1.0.0", policy.get("version"), "machine report policy version")
    policy_sha = _sha256(
        policy.get("sha256"),
        "machine_report.policy.sha256",
    )
    bindings = _mapping(report.get("bindings"), "machine_report.bindings")
    policy_binding_field = (
        "clean_policy_sha256" if gate == "clean" else "dub_policy_sha256"
    )
    _same(
        policy_sha,
        bindings.get(policy_binding_field),
        "machine report bound policy SHA",
    )
    metrics = _sequence(report.get("metrics"), "machine_report.metrics")
    names: list[str] = []
    all_pass = True
    for index, metric_value in enumerate(metrics):
        metric = _mapping(metric_value, f"machine_report.metrics[{index}]")
        name = metric.get("name")
        if not isinstance(name, str):
            raise ContractValidationError(
                f"machine_report.metrics[{index}].name is invalid"
            )
        names.append(name)
        decision = metric.get("decision")
        if decision not in {"pass", "fail", "review"}:
            raise ContractValidationError(
                f"machine_report.metrics[{index}].decision is invalid"
            )
        all_pass = all_pass and decision == "pass"
    expected_names = REQUIRED_MACHINE_METRIC_NAMES[gate]
    if len(names) != len(expected_names) or frozenset(names) != expected_names:
        raise ContractValidationError(
            f"{gate} machine report metric names do not match its policy"
        )
    expected_decision = "pass" if all_pass else "fail"
    _same(
        expected_decision,
        report.get("decision"),
        "machine report aggregate decision",
    )


def validate_status_invariants(status_value: Mapping[str, Any]) -> None:
    status = _mapping(status_value, "status")
    state = status.get("state")
    if state not in STATE_SET:
        raise ContractValidationError(f"unknown status state: {state!r}")
    _strict_int(status.get("state_version"), "status.state_version", minimum=1)
    artifact = status.get("artifact")
    has_download = bool(
        status.get("download_url")
        or (isinstance(artifact, Mapping) and artifact.get("download_url"))
    )
    if state == "completed":
        artifact = _mapping(artifact, "status.artifact")
        if set(artifact) != {
            "kind",
            "ready",
            "download_url",
            "expires_at",
            "size_bytes",
            "sha256",
        }:
            raise ContractValidationError(
                "completed status artifact fields do not match the schema"
            )
        if artifact.get("kind") != "dubbed" or artifact.get("ready") is not True:
            raise ContractValidationError("completed status requires a ready dubbed artifact")
        _sha256(artifact.get("sha256"), "status.artifact.sha256")
        _strict_int(artifact.get("size_bytes"), "status.artifact.size_bytes", minimum=1)
        if not isinstance(artifact.get("download_url"), str) or not artifact["download_url"]:
            raise ContractValidationError("completed artifact download URL is invalid")
        for field, pattern in (
            ("clean_approval_id", r"cap-[0-9a-f]{32}"),
            ("release_approval_id", r"rap-[0-9a-f]{32}"),
        ):
            value = status.get(field)
            if not isinstance(value, str) or not re.fullmatch(pattern, value):
                raise ContractValidationError(f"completed status {field} is invalid")
        for field in (
            "clean_sha256",
            "source_cue_manifest_sha256",
            "cue_ledger_sha256",
        ):
            _sha256(status.get(field), f"status.{field}")
        if status.get("updated_at") is not None:
            minted_evidence = _rfc3339(status["updated_at"], "status.updated_at")
            expires_at = _rfc3339(
                artifact.get("expires_at"), "status.artifact.expires_at"
            )
            link_lifetime = expires_at - minted_evidence
            if (
                link_lifetime <= timedelta(0)
                or link_lifetime > _DOWNLOAD_LINK_MAX_LIFETIME
            ):
                raise ContractValidationError(
                    "signed download link expiry must be within 24 hours"
                )
        else:
            _rfc3339(artifact.get("expires_at"), "status.artifact.expires_at")
        if not has_download or status.get("error") is not None:
            raise ContractValidationError("completed status artifact/error invariant failed")
    elif artifact is not None or has_download:
        raise ContractValidationError("non-completed status cannot expose an artifact")
    elif state in FAILURE_STATES:
        validate_safe_error(
            _mapping(status.get("error"), "status.error"),
            terminal_state=state,
        )
    elif status.get("error") is not None:
        raise ContractValidationError("nonterminal status cannot contain an error")


def validate_callback_event(callback_value: Mapping[str, Any]) -> None:
    callback = _mapping(callback_value, "callback")
    if set(callback) - _CALLBACK_FIELDS:
        raise ContractValidationError("callback contains fields outside its schema")
    required = {
        "schema_version",
        "event",
        "event_id",
        "job_id",
        "attempt_id",
        "attempt_seq",
        "state",
        "state_version",
        "verification_passed",
        "occurred_at",
    }
    if required - set(callback) or callback.get("schema_version") != "1":
        raise ContractValidationError("callback required fields are invalid")
    state = callback.get("state")
    if state not in TERMINAL_STATES:
        raise ContractValidationError("callback state must be terminal")
    _same(f"desub.{state}", callback.get("event"), "callback event-to-state mapping")
    if not isinstance(callback.get("event_id"), str) or not re.fullmatch(
        r"evt-[0-9a-f]{32}", callback["event_id"]
    ):
        raise ContractValidationError("callback event_id is invalid")
    if not isinstance(callback.get("job_id"), str) or not re.fullmatch(
        r"desub-[0-9a-f]{32}", callback["job_id"]
    ):
        raise ContractValidationError("callback job_id is invalid")
    if not isinstance(callback.get("attempt_id"), str) or not re.fullmatch(
        r"att-[0-9a-f]{32}", callback["attempt_id"]
    ):
        raise ContractValidationError("callback attempt_id is invalid")
    _strict_int(callback.get("attempt_seq"), "callback.attempt_seq", minimum=1)
    _strict_int(callback.get("state_version"), "callback.state_version", minimum=1)
    _rfc3339(callback.get("occurred_at"), "callback.occurred_at")
    if state == "completed":
        if callback.get("verification_passed") is not True:
            raise ContractValidationError("completed callback must be verified")
        if callback.get("error") is not None:
            raise ContractValidationError("completed callback cannot contain an error")
        for field, pattern in (
            ("clean_approval_id", r"cap-[0-9a-f]{32}"),
            ("release_approval_id", r"rap-[0-9a-f]{32}"),
        ):
            value = callback.get(field)
            if not isinstance(value, str) or not re.fullmatch(pattern, value):
                raise ContractValidationError(
                    f"completed callback {field} is invalid"
                )
        for field in _COMPLETED_CALLBACK_HASH_FIELDS:
            _sha256(callback.get(field), f"callback.{field}")
    else:
        if callback.get("verification_passed") is not False:
            raise ContractValidationError("failure callback cannot be verified")
        validate_safe_error(
            _mapping(callback.get("error"), "callback.error"),
            terminal_state=state,
        )
        if (
            callback.get("release_approval_id") is not None
            or callback.get("dubbed_sha256") is not None
        ):
            raise ContractValidationError(
                "failure callback cannot expose release/dubbed authority"
            )


def validate_status_callback_consistency(
    status_value: Mapping[str, Any],
    callback_value: Mapping[str, Any],
) -> None:
    status = _mapping(status_value, "status")
    callback = _mapping(callback_value, "callback")
    validate_status_invariants(status)
    validate_callback_event(callback)
    for field in (
        "job_id",
        "attempt_id",
        "attempt_seq",
        "state",
        "state_version",
    ):
        _same(status.get(field), callback.get(field), f"status/callback {field}")
    if status.get("state") == "completed":
        for field in (
            "clean_approval_id",
            "release_approval_id",
            "clean_sha256",
            "source_cue_manifest_sha256",
            "cue_ledger_sha256",
        ):
            _same(status.get(field), callback.get(field), f"status/callback {field}")
        artifact = _mapping(status.get("artifact"), "status.artifact")
        _same(
            artifact.get("sha256"),
            callback.get("dubbed_sha256"),
            "status artifact/callback dubbed SHA",
        )
    else:
        _same(status.get("error"), callback.get("error"), "status/callback error")


def validate_taxonomy_document(taxonomy_value: Mapping[str, Any]) -> None:
    taxonomy = _mapping(taxonomy_value, "taxonomy")
    definitions = _mapping(taxonomy.get("$defs"), "taxonomy.$defs")
    expected = {
        "state": STATE_VALUES,
        "terminalState": TERMINAL_STATES,
        "failureState": FAILURE_STATES,
        "launchState": LAUNCH_STATE_VALUES,
        "errorStage": ERROR_STAGE_VALUES,
        "errorCode": ERROR_CODE_VALUES,
        "verifierRole": (
            "clean_a",
            "clean_b",
            "clean_c",
            "clean_final_sol",
            "translation_semantic_preflight",
            "translation_style_preflight",
            "translation_semantics",
            "translation_style",
            "dub_audio_video",
            "dub_final_sol",
        ),
    }
    for name, values in expected.items():
        definition = _mapping(definitions.get(name), f"taxonomy.$defs.{name}")
        actual = _sequence(definition.get("enum"), f"taxonomy.$defs.{name}.enum")
        _same(list(values), list(actual), f"taxonomy {name}")
    verifier_roles = frozenset(expected["verifierRole"])
    expected_roles = (
        CLEAN_VERDICT_ROLES
        | TRANSLATION_PREFLIGHT_ROLES
        | FINAL_DUB_VERDICT_ROLES
    )
    _same(expected_roles, verifier_roles, "taxonomy verifier role sets")


def validate_idempotency_record(
    record_value: Mapping[str, Any],
    stored_request_utf8_bytes: bytes,
) -> None:
    """Validate the exact stored request bytes without claiming JCS support."""

    record = _mapping(record_value, "idempotency_record")
    if set(record) != _IDEMPOTENCY_RECORD_FIELDS:
        raise ContractValidationError(
            "idempotency record fields do not match its exact schema"
        )
    if record.get("schema_version") != "1":
        raise ContractValidationError("idempotency schema_version must be '1'")
    _strict_int(record.get("record_version"), "record_version", minimum=1)
    _sha256(record.get("idempotency_key_sha256"), "idempotency_key_sha256")
    _sha256(record.get("caller_scope_sha256"), "caller_scope_sha256")
    _same(
        "stored_exact_utf8_json_v1",
        record.get("request_bytes_contract"),
        "idempotency request bytes contract",
    )
    projection = _mapping(
        record.get("canonical_request_projection"),
        "canonical_request_projection",
    )
    if set(projection) != _CREATE_REQUEST_FIELDS:
        raise ContractValidationError(
            "canonical request projection fields do not match create request"
        )
    if projection.get("schema_version") != "1":
        raise ContractValidationError("canonical request schema_version is invalid")
    douyin_url = projection.get("douyin_url")
    if (
        not isinstance(douyin_url, str)
        or not 12 <= len(douyin_url) <= 2048
        or not re.fullmatch(
            r"https://(?:[a-z0-9-]+\.)*douyin\.com/[^\s]+",
            douyin_url,
        )
    ):
        raise ContractValidationError("canonical request Douyin URL is invalid")
    if not isinstance(projection.get("force_refresh"), bool):
        raise ContractValidationError("canonical request force_refresh is invalid")
    client_context = _mapping(
        projection.get("client_context"),
        "canonical_request_projection.client_context",
    )
    if set(client_context) != {"request_id"}:
        raise ContractValidationError("canonical request client context is invalid")
    request_id = client_context.get("request_id")
    if (
        not isinstance(request_id, str)
        or not 1 <= len(request_id) <= 128
        or not re.fullmatch(r"[A-Za-z0-9._:-]+", request_id)
    ):
        raise ContractValidationError("canonical request request_id is invalid")
    if not isinstance(stored_request_utf8_bytes, bytes):
        raise ContractValidationError(
            "stored request value must be exact bytes"
        )
    try:
        decoded = json.loads(
            stored_request_utf8_bytes.decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            "stored request bytes are not UTF-8 JSON"
        ) from exc
    _same(projection, decoded, "stored request projection bytes")
    request_sha = hashlib.sha256(
        stored_request_utf8_bytes
    ).hexdigest()
    _same(
        _sha256(
            record.get("canonical_request_sha256"),
            "canonical_request_sha256",
        ),
        request_sha,
        "canonical request SHA",
    )
    bound_attempt = _attempt(record.get("bound_attempt"), "bound_attempt")
    replay = _mapping(record.get("replay_binding"), "replay_binding")
    if set(replay) != {
        "initial_response_body_sha256",
        "exact_attempt_status_path",
        "mint_fresh_download_url_only_after_release_revalidation",
    }:
        raise ContractValidationError("idempotency replay binding is invalid")
    _sha256(
        replay.get("initial_response_body_sha256"),
        "replay_binding.initial_response_body_sha256",
    )
    expected_status_path = (
        f"/v1/desub/jobs/{bound_attempt['job_id']}/attempts/"
        f"{bound_attempt['attempt_seq']}"
    )
    _same(
        expected_status_path,
        replay.get("exact_attempt_status_path"),
        "idempotency exact attempt status path",
    )
    if (
        replay.get("mint_fresh_download_url_only_after_release_revalidation")
        is not True
    ):
        raise ContractValidationError(
            "idempotency replay cannot bypass release revalidation"
        )
    _same(604800, record.get("retention_seconds"), "idempotency retention")
    created_at = _rfc3339(record.get("created_at"), "idempotency.created_at")
    expires_at = _rfc3339(record.get("expires_at"), "idempotency.expires_at")
    _same(
        created_at + timedelta(seconds=604800),
        expires_at,
        "idempotency exact seven-day expiry",
    )
    observations = _sequence(
        record.get("conflict_observations"),
        "conflict_observations",
    )
    for index, observation_value in enumerate(observations):
        observation = _mapping(
            observation_value,
            f"conflict_observations[{index}]",
        )
        if set(observation) != {
            "conflicting_request_sha256",
            "error_code",
            "http_status",
            "observed_at",
        }:
            raise ContractValidationError(
                f"conflict_observations[{index}] fields are invalid"
            )
        conflicting_sha = _sha256(
            observation.get("conflicting_request_sha256"),
            f"conflict_observations[{index}].conflicting_request_sha256",
        )
        if conflicting_sha == request_sha:
            raise ContractValidationError(
                "idempotency conflict hash must differ from canonical request"
            )
        _same(
            "IDEMPOTENCY_CONFLICT",
            observation.get("error_code"),
            f"conflict_observations[{index}].error_code",
        )
        _same(
            409,
            observation.get("http_status"),
            f"conflict_observations[{index}].http_status",
        )
        _rfc3339(
            observation.get("observed_at"),
            f"conflict_observations[{index}].observed_at",
        )


def _validate_audio_range(audio_value: Any, label: str) -> dict[str, int]:
    audio = _mapping(audio_value, label)
    sample_rate = _strict_int(audio.get("sample_rate_hz"), f"{label}.sample_rate_hz", minimum=1)
    _same(44100, sample_rate, f"{label}.sample_rate_hz")
    start = _strict_int(audio.get("start_sample"), f"{label}.start_sample")
    end_exclusive = _strict_int(
        audio.get("end_sample_exclusive"), f"{label}.end_sample_exclusive", minimum=1
    )
    speech_start = _strict_int(
        audio.get("speech_start_sample"), f"{label}.speech_start_sample"
    )
    speech_end = _strict_int(
        audio.get("speech_end_sample_inclusive"),
        f"{label}.speech_end_sample_inclusive",
    )
    if not (start < end_exclusive and start <= speech_start <= speech_end < end_exclusive):
        raise ContractValidationError(f"{label} has invalid nested sample ranges")
    _sha256(audio.get("decode_filter_sha256"), f"{label}.decode_filter_sha256")
    _object_ref(audio.get("pcm_slice"), f"{label}.pcm_slice")
    return {
        "sample_rate_hz": sample_rate,
        "start_sample": start,
        "end_sample_exclusive": end_exclusive,
        "speech_start_sample": speech_start,
        "speech_end_sample_inclusive": speech_end,
    }


def _validate_visual_range(visual_value: Any, label: str) -> dict[str, Any]:
    visual = _mapping(visual_value, label)
    start_frame = _strict_int(visual.get("start_frame"), f"{label}.start_frame")
    end_frame = _strict_int(
        visual.get("end_frame_inclusive"), f"{label}.end_frame_inclusive"
    )
    sample_frame = _strict_int(visual.get("sample_frame"), f"{label}.sample_frame")
    if not start_frame <= sample_frame <= end_frame:
        raise ContractValidationError(f"{label} has invalid nested frame ranges")
    start_pts = _fraction(visual.get("start_pts"), f"{label}.start_pts")
    end_pts = _fraction(visual.get("end_pts"), f"{label}.end_pts")
    if end_pts < start_pts:
        raise ContractValidationError(f"{label} PTS range is reversed")
    _sha256(visual.get("sample_frame_sha256"), f"{label}.sample_frame_sha256")
    bbox = _mapping(visual.get("caption_bbox_normalized"), f"{label}.caption_bbox_normalized")
    x = _finite_number(bbox.get("x"), f"{label}.bbox.x")
    y = _finite_number(bbox.get("y"), f"{label}.bbox.y")
    width = _finite_number(bbox.get("width"), f"{label}.bbox.width")
    height = _finite_number(bbox.get("height"), f"{label}.bbox.height")
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
        raise ContractValidationError(f"{label} normalized bbox exceeds the frame")
    baseline = _finite_number(
        visual.get("caption_baseline_y_normalized"), f"{label}.caption_baseline"
    )
    if not 0 <= baseline <= 1:
        raise ContractValidationError(f"{label} caption baseline is invalid")
    crop_geometry = _mapping(visual.get("crop_geometry_px"), f"{label}.crop_geometry_px")
    _strict_int(crop_geometry.get("x"), f"{label}.crop.x")
    _strict_int(crop_geometry.get("y"), f"{label}.crop.y")
    _strict_int(crop_geometry.get("width"), f"{label}.crop.width", minimum=1)
    _strict_int(crop_geometry.get("height"), f"{label}.crop.height", minimum=1)
    _object_ref(visual.get("crop"), f"{label}.crop")
    return {
        "start_frame": start_frame,
        "end_frame_inclusive": end_frame,
        "start_pts": start_pts,
        "end_pts": end_pts,
    }


def validate_source_cue_collection(
    manifest_value: Mapping[str, Any],
    record_bundles_value: Sequence[Mapping[str, Any]],
) -> None:
    """Validate the ordered manifest against exact stored record bytes and docs.

    Each record bundle has ``record_ref``, ``record_bytes`` and parsed ``record``.
    Crop/PCM bytes are not required here, but their exact ObjectRefs must match
    both the manifest entry and nested record fields.
    """

    manifest = _mapping(manifest_value, "source_cue_manifest")
    manifest_attempt = _attempt(manifest.get("attempt"), "source_cue_manifest.attempt")
    source_ref = _object_ref(manifest.get("source"), "source_cue_manifest.source")
    cues = _sequence(manifest.get("cues"), "source_cue_manifest.cues")
    bundles = _sequence(record_bundles_value, "record_bundles")
    cue_count = _strict_int(manifest.get("cue_count"), "source_cue_manifest.cue_count", minimum=1)
    if cue_count != len(cues) or cue_count != len(bundles):
        raise ContractValidationError("source cue count/record cardinality mismatch")

    seen_ids: set[str] = set()
    previous_audio: dict[str, int] | None = None
    previous_visual: dict[str, Any] | None = None
    sample_rate: int | None = None
    for index, (entry_value, bundle_value) in enumerate(zip(cues, bundles, strict=True)):
        entry = _mapping(entry_value, f"source_cue_manifest.cues[{index}]")
        bundle = _mapping(bundle_value, f"record_bundles[{index}]")
        cue_index = _strict_int(entry.get("cue_index"), f"manifest.cues[{index}].cue_index")
        if cue_index != index:
            raise ContractValidationError("source cue indices must be contiguous and ordered")
        cue_id = _cue_id(entry.get("cue_id"), cue_index, f"manifest.cues[{index}].cue_id")
        if cue_id in seen_ids:
            raise ContractValidationError("source cue IDs must be unique")
        seen_ids.add(cue_id)

        entry_record_ref = _object_ref(entry.get("record"), f"manifest.cues[{index}].record")
        entry_crop_ref = _object_ref(entry.get("crop"), f"manifest.cues[{index}].crop")
        entry_pcm_ref = _object_ref(entry.get("pcm_slice"), f"manifest.cues[{index}].pcm_slice")
        record_ref = _object_ref(bundle.get("record_ref"), f"record_bundles[{index}].record_ref")
        _same(entry_record_ref, record_ref, f"record_bundles[{index}].record_ref")
        record_bytes = bundle.get("record_bytes")
        if not isinstance(record_bytes, bytes):
            raise ContractValidationError(f"record_bundles[{index}].record_bytes must be bytes")
        if hashlib.sha256(record_bytes).hexdigest() != record_ref["sha256"]:
            raise ContractValidationError(f"record_bundles[{index}] byte SHA mismatch")
        record = _mapping(bundle.get("record"), f"record_bundles[{index}].record")
        try:
            decoded_record = json.loads(record_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractValidationError(f"record_bundles[{index}] bytes are not JSON") from exc
        _same(record, decoded_record, f"record_bundles[{index}] parsed record")

        _same(manifest_attempt, _attempt(record.get("attempt"), f"record[{index}].attempt"), f"record[{index}].attempt")
        _same(cue_id, record.get("cue_id"), f"record[{index}].cue_id")
        _same(cue_index, record.get("cue_index"), f"record[{index}].cue_index")
        _same(source_ref["sha256"], _sha256(record.get("source_sha256"), f"record[{index}].source_sha256"), f"record[{index}].source_sha256")
        _nfc_pair(record.get("text_zh_raw"), record.get("text_zh_nfc"), f"record[{index}].text_zh")
        extractor_confidence = _finite_number(
            record.get("extractor_confidence"),
            f"record[{index}].extractor_confidence",
        )
        aligner_confidence = _finite_number(
            record.get("aligner_confidence"),
            f"record[{index}].aligner_confidence",
        )
        classification = _mapping(
            record.get("source_classification"),
            f"record[{index}].source_classification",
        )
        classification_confidence = _finite_number(
            classification.get("confidence"),
            f"record[{index}].source_classification.confidence",
        )
        if not all(
            0 <= value <= 1
            for value in (
                extractor_confidence,
                aligner_confidence,
                classification_confidence,
            )
        ):
            raise ContractValidationError(
                f"record[{index}] has a confidence outside 0..1"
            )

        audio = _validate_audio_range(record.get("source_audio"), f"record[{index}].source_audio")
        visual = _validate_visual_range(record.get("source_visual"), f"record[{index}].source_visual")
        _same(entry_pcm_ref, record["source_audio"]["pcm_slice"], f"record[{index}] PCM ObjectRef")
        _same(entry_crop_ref, record["source_visual"]["crop"], f"record[{index}] crop ObjectRef")
        if sample_rate is None:
            sample_rate = audio["sample_rate_hz"]
        elif audio["sample_rate_hz"] != sample_rate:
            raise ContractValidationError("source cue sample rates must be identical")

        if previous_audio is not None:
            if audio["start_sample"] < previous_audio["end_sample_exclusive"]:
                raise ContractValidationError("source cue audio ranges overlap or reorder")
            if audio["speech_start_sample"] <= previous_audio["speech_end_sample_inclusive"]:
                raise ContractValidationError("source speech ranges overlap or reorder")
        if previous_visual is not None:
            if visual["start_frame"] <= previous_visual["end_frame_inclusive"]:
                raise ContractValidationError("source cue frame ranges overlap or reorder")
            if visual["start_pts"] <= previous_visual["end_pts"]:
                raise ContractValidationError("source cue PTS ranges overlap or reorder")
        previous_audio = audio
        previous_visual = visual


def build_source_cue_collection_root_preimage(
    manifest_value: Mapping[str, Any],
) -> dict[str, Any]:
    """Build only the logical projection defined by the v1 root contract.

    The returned mapping is not canonical bytes. Callers must use a separately
    vetted RFC 8785 implementation and pass its bytes to the validator below.
    """

    manifest = _mapping(manifest_value, "source_cue_manifest")
    source = _object_ref(manifest.get("source"), "source_cue_manifest.source")
    cues = _sequence(manifest.get("cues"), "source_cue_manifest.cues")
    count = _strict_int(manifest.get("cue_count"), "source_cue_manifest.cue_count", minimum=1)
    if count != len(cues):
        raise ContractValidationError("source cue count does not match cues")
    projected_cues: list[dict[str, Any]] = []
    for index, entry_value in enumerate(cues):
        entry = _mapping(entry_value, f"source_cue_manifest.cues[{index}]")
        cue_index = _strict_int(entry.get("cue_index"), f"cues[{index}].cue_index")
        if cue_index != index:
            raise ContractValidationError("source cue indices must be contiguous")
        projected_cues.append(
            {
                "cue_id": _cue_id(entry.get("cue_id"), cue_index, f"cues[{index}].cue_id"),
                "cue_index": cue_index,
                "record": _object_ref(entry.get("record"), f"cues[{index}].record"),
                "crop": _object_ref(entry.get("crop"), f"cues[{index}].crop"),
                "pcm_slice": _object_ref(entry.get("pcm_slice"), f"cues[{index}].pcm_slice"),
            }
        )
    return {
        "algorithm": "RFC8785-JCS-SHA256-ORDERED-v1",
        "schema_version": "1",
        "source": source,
        "cue_count": count,
        "extractor_policy_sha256": _sha256(
            manifest.get("extractor_policy_sha256"), "extractor_policy_sha256"
        ),
        "aligner_policy_sha256": _sha256(
            manifest.get("aligner_policy_sha256"), "aligner_policy_sha256"
        ),
        "cues": projected_cues,
    }


def validate_source_cue_collection_root(
    manifest_value: Mapping[str, Any],
    externally_canonicalized_bytes: bytes,
    *,
    root_contract_bytes: bytes,
    golden_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Check external canonical bytes, their SHA/root and optional golden bytes.

    This function does not canonicalize JSON and is not an RFC 8785
    implementation. It verifies the decoded logical projection and trusts the
    caller's separately vetted canonicalizer for byte production.
    """

    manifest = _mapping(manifest_value, "source_cue_manifest")
    if not isinstance(externally_canonicalized_bytes, bytes):
        raise ContractValidationError("external canonicalized value must be bytes")
    if not isinstance(root_contract_bytes, bytes):
        raise ContractValidationError("root contract value must be bytes")
    projection = build_source_cue_collection_root_preimage(manifest)
    try:
        decoded = json.loads(externally_canonicalized_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError("external canonical bytes are not UTF-8 JSON") from exc
    _same(projection, decoded, "source cue root preimage projection")
    contract_sha = hashlib.sha256(root_contract_bytes).hexdigest()
    _same(
        _sha256(manifest.get("collection_root_contract_sha256"), "collection_root_contract_sha256"),
        contract_sha,
        "source cue root contract SHA",
    )
    root = hashlib.sha256(externally_canonicalized_bytes).hexdigest()
    _same(_sha256(manifest.get("collection_root"), "collection_root"), root, "source cue collection root")
    if manifest.get("collection_root_algorithm") != "RFC8785-JCS-SHA256-ORDERED":
        raise ContractValidationError("source cue collection root algorithm is invalid")
    if golden_bytes is not None:
        validate_golden_canonical_bytes(externally_canonicalized_bytes, golden_bytes)
    return projection


def validate_translation_attempts(attempt_values: Sequence[Mapping[str, Any]]) -> None:
    attempts = _sequence(attempt_values, "translation_attempts")
    if not attempts:
        raise ContractValidationError("translation attempts cannot be empty")
    by_cue: dict[str, list[Mapping[str, Any]]] = {}
    global_attempt: dict[str, Any] | None = None
    for offset, raw_attempt in enumerate(attempts):
        attempt = _mapping(raw_attempt, f"translation_attempts[{offset}]")
        if set(attempt) != _TRANSLATION_ATTEMPT_FIELDS:
            raise ContractValidationError(
                f"translation_attempts[{offset}] fields do not match the pure create-only schema"
            )
        if attempt.get("schema_version") != "1":
            raise ContractValidationError(
                f"translation_attempts[{offset}].schema_version must be '1'"
            )
        bound_attempt = _attempt(attempt.get("attempt"), f"translation_attempts[{offset}].attempt")
        if global_attempt is None:
            global_attempt = bound_attempt
        else:
            _same(global_attempt, bound_attempt, f"translation_attempts[{offset}].attempt")
        cue_index = _strict_int(attempt.get("cue_index"), f"translation_attempts[{offset}].cue_index")
        cue_id = _cue_id(attempt.get("cue_id"), cue_index, f"translation_attempts[{offset}].cue_id")
        source_record_sha = _sha256(
            attempt.get("source_cue_record_sha256"),
            f"translation_attempts[{offset}].source_cue_record_sha256",
        )
        source_record_ref = _object_ref(
            attempt.get("source_cue_record"),
            f"translation_attempts[{offset}].source_cue_record",
        )
        _same(
            source_record_sha,
            source_record_ref["sha256"],
            f"translation_attempts[{offset}] source cue ObjectRef",
        )
        _nfc_pair(attempt.get("text_vi_raw"), attempt.get("text_vi_nfc"), f"translation_attempts[{offset}].text_vi")
        _sha256(
            attempt.get("generator_prompt_sha256"),
            f"translation_attempts[{offset}].generator_prompt_sha256",
        )
        _sha256(attempt.get("candidate_sha256"), f"translation_attempts[{offset}].candidate_sha256")
        _mapping(attempt.get("generator"), f"translation_attempts[{offset}].generator")
        _rfc3339(attempt.get("created_at"), f"translation_attempts[{offset}].created_at")
        by_cue.setdefault(cue_id, []).append(attempt)
    for cue_id, cue_attempts in by_cue.items():
        ordered = sorted(cue_attempts, key=lambda item: item["candidate_index"])
        indexes = [
            _strict_int(item.get("candidate_index"), f"{cue_id}.candidate_index")
            for item in ordered
        ]
        if indexes != list(range(len(ordered))) or indexes[-1] > MAX_COMPACT_RETRIES:
            raise ContractValidationError(f"{cue_id} candidate indexes must be contiguous 0..2")
        source_record_sha = ordered[0]["source_cue_record_sha256"]
        seen_hashes: set[str] = set()
        for index, attempt in enumerate(ordered):
            expected_reason = "initial" if index == 0 else "compact_unfit"
            if attempt.get("reason") != expected_reason:
                raise ContractValidationError(f"{cue_id} candidate {index} reason is invalid")
            _same(source_record_sha, attempt["source_cue_record_sha256"], f"{cue_id} source record")
            candidate_sha = attempt["candidate_sha256"]
            if candidate_sha in seen_hashes:
                raise ContractValidationError(f"{cue_id} reuses a candidate SHA")
            seen_hashes.add(candidate_sha)


def _validate_silence_detector(
    detector_value: Any,
    label: str,
    *,
    require_active: bool,
) -> dict[str, Any]:
    detector = _mapping(detector_value, label)
    expected_constants = {
        "sample_rate_hz": 44100,
        "window_samples": 882,
        "hop_samples": 441,
        "tail_window_padding": "right_zero_pad",
        "rms_denominator_samples": 882,
        "active_threshold_dbfs": -45,
        "active_threshold_linear": 0.005623413251903491,
        "consecutive_windows": 2,
    }
    for field, expected in expected_constants.items():
        _same(expected, detector.get(field), f"{label}.{field}")
    decoded_count = _strict_int(
        detector.get("decoded_sample_count"), f"{label}.decoded_sample_count", minimum=1
    )
    _sha256(detector.get("implementation_sha256"), f"{label}.implementation_sha256")
    runs = _sequence(detector.get("active_runs"), f"{label}.active_runs")
    if not runs:
        if require_active:
            raise ContractValidationError(f"{label}.active_runs cannot be empty")
        if detector.get("onset_sample") is not None:
            raise ContractValidationError(f"{label}.onset_sample must be null")
        if detector.get("offset_sample_inclusive") is not None:
            raise ContractValidationError(
                f"{label}.offset_sample_inclusive must be null"
            )
        return {
            "decoded_sample_count": decoded_count,
            "onset_sample": None,
            "offset_sample_inclusive": None,
        }
    if not require_active:
        raise ContractValidationError(f"{label}.active_runs must be empty")
    normalized_runs: list[tuple[int, int]] = []
    previous_offset: int | None = None
    for index, run_value in enumerate(runs):
        run = _mapping(run_value, f"{label}.active_runs[{index}]")
        onset = _strict_int(run.get("onset_sample"), f"{label}.active_runs[{index}].onset")
        offset = _strict_int(
            run.get("offset_sample_inclusive"), f"{label}.active_runs[{index}].offset"
        )
        if onset > offset or offset >= decoded_count:
            raise ContractValidationError(f"{label}.active_runs[{index}] is outside decoded PCM")
        if previous_offset is not None and onset <= previous_offset:
            raise ContractValidationError(f"{label}.active_runs overlap or reorder")
        normalized_runs.append((onset, offset))
        previous_offset = offset
    _same(normalized_runs[0][0], detector.get("onset_sample"), f"{label}.onset_sample")
    _same(normalized_runs[-1][1], detector.get("offset_sample_inclusive"), f"{label}.offset_sample_inclusive")
    return {
        "decoded_sample_count": decoded_count,
        "onset_sample": normalized_runs[0][0],
        "offset_sample_inclusive": normalized_runs[-1][1],
    }


def validate_tts_timing_evidence(
    attempt_value: Mapping[str, Any],
    *,
    previous_actual_offset_inclusive: int | None = None,
    selected: bool = False,
) -> dict[str, Any]:
    """Validate standalone TTS media/timing evidence only.

    This function does not authenticate the referenced translation candidate or
    preflight approval. Production authorization must call
    Production authorization must use the future KMS-bound Phase-C validator;
    the Phase-A candidate helpers validate structure only.
    """
    attempt = _mapping(attempt_value, "tts_attempt")
    _attempt(attempt.get("attempt"), "tts_attempt.attempt")
    cue_index = _strict_int(attempt.get("cue_index"), "tts_attempt.cue_index")
    _cue_id(attempt.get("cue_id"), cue_index, "tts_attempt.cue_id")
    candidate_index = _strict_int(attempt.get("candidate_index"), "tts_attempt.candidate_index")
    if candidate_index > MAX_COMPACT_RETRIES:
        raise ContractValidationError("tts candidate index exceeds retry budget")
    _sha256(attempt.get("translation_candidate_sha256"), "tts_attempt.translation_candidate_sha256")
    if attempt.get("provider") != "capcut-private" or attempt.get("voice") != "BV075_streaming":
        raise ContractValidationError("tts provider/voice is not the frozen v1 identity")
    if attempt.get("prosody_rate") != "1.5000":
        raise ContractValidationError("tts prosody rate is not the frozen global rate")
    _sha256(attempt.get("normalized_request_sha256"), "tts_attempt.normalized_request_sha256")
    outcome = attempt.get("outcome")
    allowed_outcomes = {
        "succeeded",
        "provider_failed",
        "media_invalid",
        "no_active_speech",
        "schedule_unfit",
    }
    if outcome not in allowed_outcomes:
        raise ContractValidationError("tts outcome is invalid")
    if selected and outcome != "succeeded":
        raise ContractValidationError("only a succeeded TTS attempt may be selected")

    allowed_error_codes = {
        "TTS_PROVIDER_FAILED",
        "TTS_MEDIA_INVALID",
        "TTS_VOICE_RATE_MISMATCH",
        "DUB_SCHEDULE_INVALID",
        "DUB_CUE_UNFIT",
    }
    error_code = attempt.get("error_code")
    if outcome != "succeeded" and error_code not in allowed_error_codes:
        raise ContractValidationError("failed TTS attempt requires a stable error_code")

    if outcome == "no_active_speech":
        _object_ref(attempt.get("returned_media"), "tts_attempt.returned_media")
        _object_ref(attempt.get("decoded_pcm"), "tts_attempt.decoded_pcm")
        _validate_silence_detector(
            attempt.get("silence_detector"),
            "tts_attempt.silence_detector",
            require_active=False,
        )
        if selected:
            raise ContractValidationError("failed TTS attempt cannot be selected")
        return {"outcome": outcome, "selectable": False}

    if outcome not in {"succeeded", "schedule_unfit"}:
        if error_code not in allowed_error_codes:
            raise ContractValidationError("failed TTS attempt requires error_code")
        if selected:
            raise ContractValidationError("failed TTS attempt cannot be selected")
        return {"outcome": outcome, "selectable": False}

    media_ref = _object_ref(attempt.get("returned_media"), "tts_attempt.returned_media")
    pcm_ref = _object_ref(attempt.get("decoded_pcm"), "tts_attempt.decoded_pcm")
    detector = _validate_silence_detector(
        attempt.get("silence_detector"),
        "tts_attempt.silence_detector",
        require_active=True,
    )
    schedule = _mapping(attempt.get("schedule"), "tts_attempt.schedule")
    anchor = _strict_int(schedule.get("speech_anchor_sample"), "schedule.speech_anchor_sample")
    video_count = _strict_int(schedule.get("video_sample_count"), "schedule.video_sample_count", minimum=1)
    placement = _strict_int(schedule.get("placement_sample"), "schedule.placement_sample")
    actual_onset = _strict_int(schedule.get("actual_onset_sample"), "schedule.actual_onset_sample")
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
        "schedule previous actual offset binding",
    )
    _same(expected_gap, schedule.get("previous_gap_samples"), "schedule gap equation")
    _same(expected_overlap, schedule.get("overlap_samples"), "schedule overlap equation")

    within_lag = 0 <= lag <= 26460
    within_gap = expected_gap is None or expected_gap >= 2205
    within_overlap = expected_overlap == 0
    within_tail = actual_offset <= video_count - 1
    computed_fits = within_lag and within_gap and within_overlap and within_tail
    _same(computed_fits, schedule.get("fits"), "schedule fits decision")
    if outcome == "succeeded":
        if attempt.get("error_code") is not None or not computed_fits:
            raise ContractValidationError("succeeded TTS attempt must fit without error")
        response_id = attempt.get("provider_response_id")
        if not isinstance(response_id, str) or not response_id:
            raise ContractValidationError("succeeded TTS attempt requires provider response ID")
        target_onset = max(
            anchor,
            anchor
            if previous_actual_offset_inclusive is None
            else previous_actual_offset_inclusive + 2205,
        )
        _same(
            target_onset,
            schedule.get("target_actual_onset_sample"),
            "successful schedule target equation",
        )
        expected_placement = max(0, target_onset - detector["onset_sample"])
        _same(expected_placement, placement, "successful schedule placement equation")
    else:
        if not isinstance(attempt.get("error_code"), str) or computed_fits:
            raise ContractValidationError("schedule_unfit must preserve a failing schedule")
    if selected and not computed_fits:
        raise ContractValidationError("selected TTS attempt must fit")
    return {
        "outcome": outcome,
        "selectable": outcome == "succeeded" and computed_fits,
        "candidate_index": candidate_index,
        "translation_candidate_sha256": attempt["translation_candidate_sha256"],
        "decoded_pcm_sha256": pcm_ref["sha256"],
        "returned_media_sha256": media_ref["sha256"],
        "actual_onset_sample": actual_onset,
        "actual_offset_sample_inclusive": actual_offset,
        "lag_samples": lag,
        "previous_gap_samples": expected_gap,
        "overlap_samples": expected_overlap,
    }


def _artifact_bundle_index(
    bundles_value: Sequence[Mapping[str, Any]], label: str
) -> dict[str, tuple[dict[str, Any], Mapping[str, Any]]]:
    bundles = _sequence(bundles_value, label)
    result: dict[str, tuple[dict[str, Any], Mapping[str, Any]]] = {}
    for index, bundle_value in enumerate(bundles):
        bundle = _mapping(bundle_value, f"{label}[{index}]")
        ref = _object_ref(bundle.get("object_ref"), f"{label}[{index}].object_ref")
        document = _mapping(bundle.get("document"), f"{label}[{index}].document")
        object_bytes = bundle.get("object_bytes")
        if not isinstance(object_bytes, bytes):
            raise ContractValidationError(
                f"{label}[{index}].object_bytes must be exact stored bytes"
            )
        if hashlib.sha256(object_bytes).hexdigest() != ref["sha256"]:
            raise ContractValidationError(f"{label}[{index}] object byte SHA mismatch")
        try:
            parsed = json.loads(object_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractValidationError(f"{label}[{index}] bytes are not JSON") from exc
        _same(document, parsed, f"{label}[{index}] parsed object")
        if ref["sha256"] in result:
            raise ContractValidationError(f"{label} contains duplicate object SHA")
        result[ref["sha256"]] = (ref, document)
    return result


def validate_cue_ledger_structure(
    ledger_value: Mapping[str, Any],
    source_manifest_value: Mapping[str, Any],
    source_manifest_ref_value: Mapping[str, Any],
    translation_bundle_values: Sequence[Mapping[str, Any]],
    tts_bundle_values: Sequence[Mapping[str, Any]],
) -> None:
    """Validate ledger media/timing structure without granting release.

    This helper does not authenticate the selected translation-preflight
    approvals. Production release validation must resolve that transitive
    signed chain in Phase F.
    """
    ledger = _mapping(ledger_value, "cue_ledger")
    manifest = _mapping(source_manifest_value, "source_cue_manifest")
    ledger_attempt = _attempt(ledger.get("attempt"), "cue_ledger.attempt")
    _same(
        _attempt(manifest.get("attempt"), "source_cue_manifest.attempt"),
        ledger_attempt,
        "cue ledger attempt",
    )
    manifest_ref = _object_ref(source_manifest_ref_value, "source_manifest_ref")
    _same(
        manifest_ref,
        _object_ref(ledger.get("source_cue_manifest"), "cue_ledger.source_cue_manifest"),
        "cue ledger source manifest ObjectRef",
    )
    manifest_cues = _sequence(manifest.get("cues"), "source_cue_manifest.cues")
    ledger_cues = _sequence(ledger.get("cues"), "cue_ledger.cues")
    count = _strict_int(ledger.get("cue_count"), "cue_ledger.cue_count", minimum=1)
    if count != manifest.get("cue_count") or count != len(manifest_cues) or count != len(ledger_cues):
        raise ContractValidationError("cue ledger N-to-N cardinality mismatch")
    translations = _artifact_bundle_index(translation_bundle_values, "translation_bundles")
    tts_attempts = _artifact_bundle_index(tts_bundle_values, "tts_bundles")
    previous_offset: int | None = None
    for index, (manifest_entry_value, ledger_entry_value) in enumerate(
        zip(manifest_cues, ledger_cues, strict=True)
    ):
        manifest_entry = _mapping(manifest_entry_value, f"manifest.cues[{index}]")
        entry = _mapping(ledger_entry_value, f"ledger.cues[{index}]")
        cue_id = _cue_id(entry.get("cue_id"), index, f"ledger.cues[{index}].cue_id")
        _same(index, entry.get("cue_index"), f"ledger.cues[{index}].cue_index")
        _same(cue_id, manifest_entry.get("cue_id"), f"manifest.cues[{index}].cue_id")
        source_record_sha = _object_ref(
            manifest_entry.get("record"), f"manifest.cues[{index}].record"
        )["sha256"]
        _same(
            source_record_sha,
            _sha256(entry.get("source_cue_record_sha256"), f"ledger.cues[{index}].source_cue_record_sha256"),
            f"ledger.cues[{index}] source record",
        )

        translation_ref = _object_ref(
            entry.get("translation_attempt"), f"ledger.cues[{index}].translation_attempt"
        )
        if translation_ref["sha256"] not in translations:
            raise ContractValidationError(f"ledger.cues[{index}] translation object is missing")
        stored_translation_ref, translation = translations[translation_ref["sha256"]]
        _same(translation_ref, stored_translation_ref, f"ledger.cues[{index}] translation ObjectRef")
        _same(ledger_attempt, _attempt(translation.get("attempt"), f"translation[{index}].attempt"), f"translation[{index}].attempt")
        _same(cue_id, translation.get("cue_id"), f"translation[{index}].cue_id")
        _same(index, translation.get("cue_index"), f"translation[{index}].cue_index")
        _same(source_record_sha, translation.get("source_cue_record_sha256"), f"translation[{index}] source record")
        candidate_sha = _sha256(
            translation.get("candidate_sha256"), f"translation[{index}].candidate_sha256"
        )
        _same(candidate_sha, entry.get("translation_candidate_sha256"), f"ledger.cues[{index}] candidate")
        _same(
            translation.get("candidate_index"),
            entry.get("candidate_index"),
            f"ledger.cues[{index}] candidate index",
        )
        text_vi_nfc = _nfc_pair(
            translation.get("text_vi_raw"),
            translation.get("text_vi_nfc"),
            f"translation[{index}].text_vi",
        )

        tts_ref = _object_ref(entry.get("tts_attempt"), f"ledger.cues[{index}].tts_attempt")
        if tts_ref["sha256"] not in tts_attempts:
            raise ContractValidationError(f"ledger.cues[{index}] TTS object is missing")
        stored_tts_ref, tts_document = tts_attempts[tts_ref["sha256"]]
        _same(tts_ref, stored_tts_ref, f"ledger.cues[{index}] TTS ObjectRef")
        _same(ledger_attempt, _attempt(tts_document.get("attempt"), f"tts[{index}].attempt"), f"tts[{index}].attempt")
        _same(cue_id, tts_document.get("cue_id"), f"tts[{index}].cue_id")
        _same(index, tts_document.get("cue_index"), f"tts[{index}].cue_index")
        _same(translation.get("candidate_index"), tts_document.get("candidate_index"), f"tts[{index}] candidate index")
        _same(candidate_sha, tts_document.get("translation_candidate_sha256"), f"tts[{index}] candidate SHA")
        tts_result = validate_tts_timing_evidence(
            tts_document,
            previous_actual_offset_inclusive=previous_offset,
            selected=True,
        )
        _same(
            tts_result["decoded_pcm_sha256"],
            _sha256(entry.get("tts_pcm_sha256"), f"ledger.cues[{index}].tts_pcm_sha256"),
            f"ledger.cues[{index}] decoded PCM",
        )
        _same(
            text_vi_nfc,
            entry.get("translation_text_vi_nfc"),
            f"ledger.cues[{index}] selected translation text",
        )
        ledger_schedule = _mapping(
            entry.get("tts_schedule"),
            f"ledger.cues[{index}].tts_schedule",
        )
        stored_schedule = _mapping(
            tts_document.get("schedule"),
            f"tts[{index}].schedule",
        )
        expected_ledger_schedule = {
            "speech_anchor_sample": stored_schedule.get("speech_anchor_sample"),
            "previous_actual_offset_sample_inclusive": stored_schedule.get(
                "previous_actual_offset_sample_inclusive"
            ),
            "target_actual_onset_sample": stored_schedule.get(
                "target_actual_onset_sample"
            ),
            "video_sample_count": stored_schedule.get("video_sample_count"),
            "placement_sample": stored_schedule.get("placement_sample"),
            "detected_onset_sample": tts_document["silence_detector"].get(
                "onset_sample"
            ),
            "detected_offset_sample_inclusive": tts_document[
                "silence_detector"
            ].get("offset_sample_inclusive"),
            "actual_onset_sample": tts_result["actual_onset_sample"],
            "actual_offset_sample_inclusive": tts_result[
                "actual_offset_sample_inclusive"
            ],
            "lag_samples": tts_result["lag_samples"],
            "previous_gap_samples": tts_result["previous_gap_samples"],
            "overlap_samples": tts_result["overlap_samples"],
        }
        _same(
            expected_ledger_schedule,
            dict(ledger_schedule),
            f"ledger.cues[{index}] selected TTS schedule",
        )
        event = _mapping(entry.get("vietsub_event"), f"ledger.cues[{index}].vietsub_event")
        _same(text_vi_nfc, event.get("text_vi_nfc"), f"ledger.cues[{index}] Vietsub NFC text")
        _same(tts_result["actual_onset_sample"], event.get("start_sample"), f"ledger.cues[{index}] Vietsub start")
        _same(tts_result["actual_offset_sample_inclusive"], event.get("end_sample_inclusive"), f"ledger.cues[{index}] Vietsub end")
        line_count = _strict_int(event.get("line_count"), f"ledger.cues[{index}].line_count", minimum=1)
        if line_count > 2:
            raise ContractValidationError("Vietsub line_count exceeds two")
        _sha256(event.get("render_event_sha256"), f"ledger.cues[{index}].render_event_sha256")
        previous_offset = tts_result["actual_offset_sample_inclusive"]


def validate_n_to_n(
    source_cues: Sequence[Mapping[str, Any]],
    selected_translations: Sequence[Mapping[str, Any]],
    selected_tts: Sequence[Mapping[str, Any]],
    vietsub_events: Sequence[Mapping[str, Any]],
) -> None:
    sources = _sequence(source_cues, "source_cues")
    translations = _sequence(selected_translations, "selected_translations")
    tts_items = _sequence(selected_tts, "selected_tts")
    subtitles = _sequence(vietsub_events, "vietsub_events")
    counts = (len(sources), len(translations), len(tts_items), len(subtitles))
    if counts[0] < 1 or len(set(counts)) != 1:
        raise ContractValidationError(f"N-to-N cardinality mismatch: {counts!r}")
    for index in range(counts[0]):
        source = _mapping(sources[index], f"source_cues[{index}]")
        translation = _mapping(translations[index], f"translations[{index}]")
        tts = _mapping(tts_items[index], f"tts[{index}]")
        subtitle = _mapping(subtitles[index], f"vietsub[{index}]")
        cue_id = _cue_id(source.get("cue_id"), index, f"source_cues[{index}].cue_id")
        source_sha = _sha256(
            source.get("source_cue_record_sha256"),
            f"source_cues[{index}].source_cue_record_sha256",
        )
        for label, item in (("translation", translation), ("tts", tts), ("vietsub", subtitle)):
            _same(cue_id, item.get("cue_id"), f"{label}[{index}].cue_id")
            _same(index, item.get("cue_index"), f"{label}[{index}].cue_index")
        _same(source_sha, translation.get("source_cue_record_sha256"), f"translation[{index}] source record")
        candidate_sha = _sha256(translation.get("candidate_sha256"), f"translation[{index}].candidate_sha256")
        _same(candidate_sha, tts.get("translation_candidate_sha256"), f"tts[{index}] candidate")
        pcm_sha = _sha256(tts.get("decoded_pcm_sha256"), f"tts[{index}].decoded_pcm_sha256")
        _same(pcm_sha, subtitle.get("tts_pcm_sha256"), f"vietsub[{index}] PCM")
        _same(translation.get("text_vi_nfc"), subtitle.get("text_vi_nfc"), f"vietsub[{index}] text")


def _normalize_clean_bindings(document_value: Mapping[str, Any], label: str) -> dict[str, str]:
    document = _mapping(document_value, label)
    bindings = _mapping(document.get("bindings", document), f"{label}.bindings")
    policy_value = bindings.get("clean_policy_sha256")
    if policy_value is None:
        policy_value = bindings.get("policy_sha256")
    return {
        "source_sha256": _sha256(bindings.get("source_sha256"), f"{label}.source_sha256"),
        "clean_sha256": _sha256(bindings.get("clean_sha256"), f"{label}.clean_sha256"),
        "source_cue_manifest_sha256": _sha256(
            bindings.get("source_cue_manifest_sha256"), f"{label}.source_cue_manifest_sha256"
        ),
        "source_cue_collection_root": _sha256(
            bindings.get("source_cue_collection_root"), f"{label}.source_cue_collection_root"
        ),
        "clean_policy_sha256": _sha256(policy_value, f"{label}.clean_policy_sha256"),
    }


def validate_clean_evidence_bindings_structure(
    machine_report_value: Mapping[str, Any],
    verdict_values: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """Validate cross-document shape only; this does not verify signatures."""
    report = _mapping(machine_report_value, "clean_machine_report")
    validate_machine_report_aggregate(report)
    if report.get("gate") != "clean" or report.get("decision") != "pass":
        raise ContractValidationError("clean machine report must be a passing clean gate")
    report_attempt = _attempt(report.get("attempt"), "clean_machine_report.attempt")
    expected = _normalize_clean_bindings(report, "clean_machine_report")
    policy = _mapping(report.get("policy"), "clean_machine_report.policy")
    _same(expected["clean_policy_sha256"], policy.get("sha256"), "clean machine policy")
    verdicts = _sequence(verdict_values, "clean_verdicts")
    if len(verdicts) != 4:
        raise ContractValidationError("clean verdict set must contain exactly four roles")
    roles: set[str] = set()
    for index, verdict_value in enumerate(verdicts):
        verdict = _mapping(verdict_value, f"clean_verdicts[{index}]")
        role = verdict.get("role")
        if role not in CLEAN_VERDICT_ROLES or role in roles:
            raise ContractValidationError(f"invalid or duplicate clean role: {role!r}")
        roles.add(role)
        if verdict.get("decision") != "pass":
            raise ContractValidationError(f"clean verdict {role!r} did not pass")
        _same(report_attempt, _attempt(verdict.get("attempt"), f"clean_verdicts[{index}].attempt"), f"clean_verdicts[{index}].attempt")
        actual = _normalize_clean_bindings(verdict, f"clean_verdicts[{index}]")
        _same(expected, actual, f"clean_verdicts[{index}] bindings")
        verdict_policy = _mapping(verdict.get("policy"), f"clean_verdicts[{index}].policy")
        _same(expected["clean_policy_sha256"], verdict_policy.get("sha256"), f"clean_verdicts[{index}] policy")
    if roles != CLEAN_VERDICT_ROLES:
        raise ContractValidationError("clean verdict role set is incomplete")
    return expected


def _clean_approval_binding(clean_approval_value: Mapping[str, Any]) -> dict[str, Any]:
    payload = _payload(clean_approval_value, "clean_approval")
    _approval_window(payload, "clean_approval.payload")
    attempt = _attempt(payload.get("attempt"), "clean_approval.payload.attempt")
    source = _object_ref(payload.get("source"), "clean_approval.payload.source")
    clean = _object_ref(payload.get("clean"), "clean_approval.payload.clean")
    manifest = _object_ref(
        payload.get("source_cue_manifest"), "clean_approval.payload.source_cue_manifest"
    )
    clean_policy = _mapping(payload.get("clean_policy"), "clean_approval.payload.clean_policy")
    result = {
        **attempt,
        "clean_approval_id": payload.get("clean_approval_id"),
        "source_sha256": source["sha256"],
        "clean_sha256": clean["sha256"],
        "source_cue_manifest_sha256": manifest["sha256"],
        "source_cue_collection_root": _sha256(
            payload.get("source_cue_collection_root"),
            "clean_approval.payload.source_cue_collection_root",
        ),
        "clean_policy_sha256": _sha256(
            clean_policy.get("sha256"), "clean_approval.payload.clean_policy.sha256"
        ),
    }
    if not isinstance(result["clean_approval_id"], str) or not re.fullmatch(
        r"cap-[0-9a-f]{32}", result["clean_approval_id"]
    ):
        raise ContractValidationError("clean approval ID is invalid")
    verdicts = _sequence(payload.get("clean_verdicts"), "clean_approval.payload.clean_verdicts")
    roles: list[str] = []
    expected_bindings = {field: result[field] for field in CLEAN_BINDING_FIELDS}
    for index, verdict_value in enumerate(verdicts):
        verdict = _mapping(verdict_value, f"clean_approval.clean_verdicts[{index}]")
        roles.append(str(verdict.get("role")))
        _same(
            expected_bindings,
            _normalize_clean_bindings(verdict, f"clean_approval.clean_verdicts[{index}]"),
            f"clean_approval.clean_verdicts[{index}] bindings",
        )
    if roles != ["clean_a", "clean_b", "clean_c", "clean_final_sol"]:
        raise ContractValidationError("clean approval verdict roles/order are invalid")
    return result


def validate_clean_gate_index_structure(
    clean_approval_value: Mapping[str, Any], index_value: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate approval/index structure only; this is not authorization."""
    expected = _clean_approval_binding(clean_approval_value)
    index = _mapping(index_value, "clean_gate_index")
    actual = {
        **_attempt(index.get("attempt"), "clean_gate_index.attempt"),
        "clean_approval_id": index.get("clean_approval_id"),
        "source_sha256": _sha256(index.get("source_sha256"), "clean_gate_index.source_sha256"),
        "clean_sha256": _sha256(index.get("clean_sha256"), "clean_gate_index.clean_sha256"),
        "source_cue_manifest_sha256": _sha256(
            index.get("source_cue_manifest_sha256"), "clean_gate_index.source_cue_manifest_sha256"
        ),
        "source_cue_collection_root": _sha256(
            index.get("source_cue_collection_root"), "clean_gate_index.source_cue_collection_root"
        ),
        "clean_policy_sha256": _sha256(
            index.get("clean_policy_sha256"), "clean_gate_index.clean_policy_sha256"
        ),
    }
    _same(expected, actual, "clean gate index binding")
    _object_ref(index.get("clean_approval"), "clean_gate_index.clean_approval")
    _strict_int(index.get("state_version"), "clean_gate_index.state_version", minimum=1)
    return expected


def _release_binding(release_approval_value: Mapping[str, Any]) -> dict[str, Any]:
    payload = _payload(release_approval_value, "release_approval")
    _approval_window(payload, "release_approval.payload")
    attempt = _attempt(payload.get("attempt"), "release_approval.payload.attempt")
    clean = _object_ref(payload.get("clean"), "release_approval.payload.clean")
    source_manifest = _object_ref(
        payload.get("source_cue_manifest"), "release_approval.payload.source_cue_manifest"
    )
    ledger = _object_ref(payload.get("cue_ledger"), "release_approval.payload.cue_ledger")
    dubbed = _object_ref(payload.get("dubbed"), "release_approval.payload.dubbed")
    clean_policy = _mapping(payload.get("clean_policy"), "release_approval.payload.clean_policy")
    dub_policy = _mapping(payload.get("dub_policy"), "release_approval.payload.dub_policy")
    result = {
        **attempt,
        "release_approval_id": payload.get("release_approval_id"),
        "clean_approval_id": payload.get("clean_approval_id"),
        "clean_sha256": clean["sha256"],
        "source_cue_manifest_sha256": source_manifest["sha256"],
        "cue_ledger_sha256": ledger["sha256"],
        "dubbed_sha256": dubbed["sha256"],
        "clean_policy_sha256": _sha256(clean_policy.get("sha256"), "release clean policy"),
        "dub_policy_sha256": _sha256(dub_policy.get("sha256"), "release dub policy"),
    }
    if not isinstance(result["release_approval_id"], str) or not re.fullmatch(
        r"rap-[0-9a-f]{32}", result["release_approval_id"]
    ):
        raise ContractValidationError("release approval ID is invalid")
    if not isinstance(result["clean_approval_id"], str) or not re.fullmatch(
        r"cap-[0-9a-f]{32}", result["clean_approval_id"]
    ):
        raise ContractValidationError("release clean approval ID is invalid")
    final_verdicts = _sequence(payload.get("final_verdicts"), "release_approval.final_verdicts")
    roles = [str(_mapping(item, "final verdict").get("role")) for item in final_verdicts]
    if roles != ["translation_semantics", "translation_style", "dub_audio_video", "dub_final_sol"]:
        raise ContractValidationError("release final verdict roles/order are invalid")
    expected_common = {
        "source_sha256": _object_ref(payload.get("source"), "release source")["sha256"],
        "clean_sha256": result["clean_sha256"],
        "source_cue_manifest_sha256": result["source_cue_manifest_sha256"],
        "source_cue_collection_root": _sha256(
            payload.get("source_cue_collection_root"), "release source cue root"
        ),
        "clean_policy_sha256": result["clean_policy_sha256"],
    }
    for index, verdict_value in enumerate(final_verdicts):
        verdict = _mapping(verdict_value, f"release final_verdicts[{index}]")
        bindings = _normalize_clean_bindings(verdict, f"release final_verdicts[{index}]")
        _same(expected_common, bindings, f"release final_verdicts[{index}] clean bindings")
        raw_bindings = _mapping(verdict.get("bindings"), f"release final_verdicts[{index}].bindings")
        _same(result["cue_ledger_sha256"], raw_bindings.get("cue_ledger_sha256"), "final verdict cue ledger")
        _same(result["dubbed_sha256"], raw_bindings.get("dubbed_sha256"), "final verdict dubbed SHA")
        _same(result["dub_policy_sha256"], raw_bindings.get("dub_policy_sha256"), "final verdict dub policy")
    return result


def validate_release_index_structure(
    release_approval_value: Mapping[str, Any], index_value: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate release/index structure only; this is not authorization."""
    expected = _release_binding(release_approval_value)
    index = _mapping(index_value, "release_index")
    actual = {
        **_attempt(index.get("attempt"), "release_index.attempt"),
        "release_approval_id": index.get("release_approval_id"),
        "clean_approval_id": index.get("clean_approval_id"),
        "clean_sha256": _sha256(index.get("clean_sha256"), "release_index.clean_sha256"),
        "source_cue_manifest_sha256": _sha256(
            index.get("source_cue_manifest_sha256"), "release_index.source_cue_manifest_sha256"
        ),
        "cue_ledger_sha256": _sha256(index.get("cue_ledger_sha256"), "release_index.cue_ledger_sha256"),
        "dubbed_sha256": _sha256(index.get("dubbed_sha256"), "release_index.dubbed_sha256"),
        "clean_policy_sha256": _sha256(index.get("clean_policy_sha256"), "release_index.clean_policy_sha256"),
        "dub_policy_sha256": _sha256(index.get("dub_policy_sha256"), "release_index.dub_policy_sha256"),
    }
    _same(expected, actual, "release index binding")
    _object_ref(index.get("release_approval"), "release_index.release_approval")
    _strict_int(index.get("state_version"), "release_index.state_version", minimum=1)
    return expected


def validate_release_bundle_structure(
    clean_approval_value: Mapping[str, Any],
    release_approval_value: Mapping[str, Any],
    release_index_value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate clean/release/index structure only; this is not authorization."""
    clean_binding = _clean_approval_binding(clean_approval_value)
    release_payload = _payload(release_approval_value, "release_approval")
    release_binding = _release_binding(release_approval_value)
    comparisons = {
        "job_id": release_binding["job_id"],
        "attempt_id": release_binding["attempt_id"],
        "attempt_seq": release_binding["attempt_seq"],
        "fence_digest": release_binding["fence_digest"],
        "clean_approval_id": release_binding["clean_approval_id"],
        "clean_sha256": release_binding["clean_sha256"],
        "source_cue_manifest_sha256": release_binding["source_cue_manifest_sha256"],
        "source_cue_collection_root": release_payload.get("source_cue_collection_root"),
        "clean_policy_sha256": release_binding["clean_policy_sha256"],
    }
    expected = {field: clean_binding[field] for field in comparisons}
    _same(expected, comparisons, "release-to-clean approval binding")
    return validate_release_index_structure(
        release_approval_value, release_index_value
    )


def _scan_forbidden_work_item(value: Any, path: str = "dub_work_item") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _FORBIDDEN_WORK_KEY_RE.search(str(key)):
                raise ContractValidationError(f"{path}.{key} exposes raw/list/latest access")
            _scan_forbidden_work_item(item, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _scan_forbidden_work_item(item, f"{path}[{index}]")
    elif isinstance(value, str) and _FORBIDDEN_WORK_VALUE_RE.search(value):
        raise ContractValidationError(f"{path} contains a raw/latest object path")


def _required_stored_bundle(
    bundle_value: Mapping[str, Any],
    label: str,
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    bundle = _mapping(bundle_value, label)
    if not isinstance(bundle.get("object_bytes"), bytes):
        raise ContractValidationError(
            f"{label} requires exact stored bytes for SHA verification"
        )
    indexed = _artifact_bundle_index([bundle], label)
    return next(iter(indexed.values()))


def validate_dub_work_item_structure(
    work_item_value: Mapping[str, Any],
    *,
    clean_approval_bundle_value: Mapping[str, Any],
    clean_gate_index_bundle_value: Mapping[str, Any],
    source_cue_manifest_bundle_value: Mapping[str, Any],
) -> None:
    """Validate work-item bindings only; this does not verify approval signatures."""
    work_item = _mapping(work_item_value, "dub_work_item")
    if set(work_item) != _DUB_WORK_ITEM_FIELDS:
        raise ContractValidationError("dub work item fields do not match its exact schema")
    if work_item.get("schema_version") != "1":
        raise ContractValidationError("dub work item schema_version must be '1'")
    work_attempt = _attempt(work_item.get("attempt"), "dub_work_item.attempt")
    approval_id = work_item.get("clean_approval_id")
    if not isinstance(approval_id, str) or not re.fullmatch(r"cap-[0-9a-f]{32}", approval_id):
        raise ContractValidationError("dub work item clean approval ID is invalid")
    work_refs = {
        field: _object_ref(work_item.get(field), f"dub_work_item.{field}")
        for field in (
            "clean_approval",
            "clean_gate_index",
            "clean",
            "clean_manifest",
            "source_cue_manifest",
        )
    }
    approval_ref, approval = _required_stored_bundle(
        clean_approval_bundle_value,
        "clean_approval_bundle",
    )
    index_ref, clean_index = _required_stored_bundle(
        clean_gate_index_bundle_value,
        "clean_gate_index_bundle",
    )
    manifest_ref, source_manifest = _required_stored_bundle(
        source_cue_manifest_bundle_value,
        "source_cue_manifest_bundle",
    )
    approval_binding = validate_clean_gate_index_structure(
        approval, clean_index
    )
    approval_payload = _payload(approval, "clean_approval")
    _same(work_attempt, _attempt(approval_payload.get("attempt"), "clean approval attempt"), "work item clean approval attempt")
    _same(work_attempt, _attempt(clean_index.get("attempt"), "clean index attempt"), "work item clean index attempt")
    _same(
        work_attempt,
        _attempt(source_manifest.get("attempt"), "source manifest attempt"),
        "work item source manifest attempt",
    )
    _same(approval_ref, work_refs["clean_approval"], "work item clean approval ref")
    _same(
        approval_ref,
        _object_ref(clean_index.get("clean_approval"), "clean index approval ref"),
        "clean index approval ref",
    )
    _same(index_ref, work_refs["clean_gate_index"], "work item clean index ref")
    _same(
        _object_ref(approval_payload.get("clean"), "clean approval clean ref"),
        work_refs["clean"],
        "work item approved clean ref",
    )
    _same(
        _object_ref(
            approval_payload.get("clean_manifest"),
            "clean approval clean manifest ref",
        ),
        work_refs["clean_manifest"],
        "work item approved clean manifest ref",
    )
    _same(
        _object_ref(
            approval_payload.get("source_cue_manifest"),
            "clean approval source cue manifest ref",
        ),
        manifest_ref,
        "stored source cue manifest ref",
    )
    _same(
        manifest_ref,
        work_refs["source_cue_manifest"],
        "work item source cue manifest ref",
    )
    _same(
        approval_binding["source_sha256"],
        _object_ref(source_manifest.get("source"), "source manifest source")[
            "sha256"
        ],
        "source manifest source SHA",
    )
    _same(
        approval_binding["source_cue_collection_root"],
        source_manifest.get("collection_root"),
        "source manifest collection root",
    )
    _same(
        approval_binding["clean_policy_sha256"],
        work_item.get("clean_policy_sha256"),
        "work item clean policy",
    )
    _same(
        approval_binding["clean_approval_id"],
        approval_id,
        "work item clean approval ID",
    )
    evidence = _sequence(work_item.get("source_cue_evidence"), "dub_work_item.source_cue_evidence")
    if not evidence:
        raise ContractValidationError("dub work item requires source cue evidence")
    seen_refs: set[tuple[str, str]] = set()
    for index, ref_value in enumerate(evidence):
        ref = _object_ref(ref_value, f"dub_work_item.source_cue_evidence[{index}]")
        identity = (ref["uri"], ref["generation"])
        if identity in seen_refs:
            raise ContractValidationError("dub work item repeats an evidence ObjectRef")
        seen_refs.add(identity)
    expected_evidence: list[dict[str, Any]] = []
    manifest_cues = _sequence(source_manifest.get("cues"), "source manifest cues")
    for index, cue_value in enumerate(manifest_cues):
        cue = _mapping(cue_value, f"source manifest cues[{index}]")
        for field in ("record", "crop", "pcm_slice"):
            expected_evidence.append(
                _object_ref(cue.get(field), f"source manifest cues[{index}].{field}")
            )
    _same(
        expected_evidence,
        [_object_ref(item, "work item evidence") for item in evidence],
        "work item exact source cue evidence",
    )
    expires_at = _rfc3339(work_item.get("expires_at"), "dub_work_item.expires_at")
    approval_issued_at = _rfc3339(
        approval_payload.get("issued_at"),
        "clean approval issued_at",
    )
    approval_expires_at = _rfc3339(
        approval_payload.get("expires_at"),
        "clean approval expires_at",
    )
    if not approval_issued_at < expires_at <= approval_expires_at:
        raise ContractValidationError(
            "dub work item expiry must stay within clean approval authority"
        )
    _scan_forbidden_work_item(work_item)


def validate_golden_canonical_bytes(
    actual: bytes,
    expected: bytes,
    *,
    expected_sha256: str | None = None,
) -> None:
    """Compare bytes with a golden fixture; this is not an RFC 8785 implementation."""

    if not isinstance(actual, bytes) or not isinstance(expected, bytes):
        raise ContractValidationError("golden canonical fixtures must be bytes")
    if actual != expected:
        raise ContractValidationError("canonical bytes do not match golden fixture")
    if expected_sha256 is not None:
        _sha256(expected_sha256, "expected_sha256")
        if hashlib.sha256(actual).hexdigest() != expected_sha256:
            raise ContractValidationError("canonical byte digest does not match fixture")
