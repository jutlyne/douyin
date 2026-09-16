"""Pure-stdlib v1 contracts shared by the integrated clean+dub pipeline.

This package deliberately does not import the existing ``desub_job_runner``;
that service remains the isolated Cover Visub runner.

This Phase-A package does not implement KMS verification or production
authorization. Every KMS-dependent approval, TTS, Dub-work, release, and ledger
helper is explicitly structure-only and intentionally omitted from this public
API; see ``phase_a_gate_status.json``.
"""

from .enums import (
    CLEAN_VERDICT_ROLES,
    ERROR_CODE_SET,
    ERROR_CODE_VALUES,
    ERROR_SEMANTICS,
    ERROR_STAGE_SET,
    ERROR_STAGE_VALUES,
    FAILURE_STATES,
    FINAL_DUB_VERDICT_ROLES,
    LAUNCH_STATE_SET,
    LAUNCH_STATE_VALUES,
    MAX_CLEAN_REPAIR_ATTEMPTS,
    MAX_COMPACT_RETRIES,
    NONTERMINAL_STATES,
    SAFE_ERROR_CONTEXT_KEYS,
    STATE_SET,
    STATE_VALUES,
    SUCCESS_STATES,
    TERMINAL_STATES,
    TRANSLATION_PREFLIGHT_ROLES,
)
from .transitions import (
    TRANSITIONS,
    InvalidTransition,
    allowed_transitions,
    assert_closed_graph,
    is_terminal,
    validate_transition,
)
from .validation import (
    ATTEMPT_BINDING_FIELDS,
    CLEAN_BINDING_FIELDS,
    CLEAN_INDEX_BINDING_FIELDS,
    RELEASE_BINDING_FIELDS,
    REQUIRED_MACHINE_METRIC_NAMES,
    ContractValidationError,
    build_source_cue_collection_root_preimage,
    validate_callback_event,
    validate_golden_canonical_bytes,
    validate_idempotency_record,
    validate_machine_report_aggregate,
    validate_n_to_n,
    validate_safe_error,
    validate_source_cue_collection,
    validate_source_cue_collection_root,
    validate_status_invariants,
    validate_status_callback_consistency,
    validate_taxonomy_document,
    validate_tts_timing_evidence,
    validate_translation_attempts,
)
from .candidate_validation import (
    validate_translation_candidate,
    verify_stored_json_bundle,
)
from .evidence_validation import (
    validate_qa_packet_bundles,
    validate_stored_bundle,
)

__all__ = [
    "CLEAN_VERDICT_ROLES",
    "CLEAN_BINDING_FIELDS",
    "CLEAN_INDEX_BINDING_FIELDS",
    "ATTEMPT_BINDING_FIELDS",
    "RELEASE_BINDING_FIELDS",
    "REQUIRED_MACHINE_METRIC_NAMES",
    "ContractValidationError",
    "ERROR_CODE_SET",
    "ERROR_CODE_VALUES",
    "ERROR_SEMANTICS",
    "ERROR_STAGE_SET",
    "ERROR_STAGE_VALUES",
    "FAILURE_STATES",
    "FINAL_DUB_VERDICT_ROLES",
    "InvalidTransition",
    "LAUNCH_STATE_SET",
    "LAUNCH_STATE_VALUES",
    "MAX_CLEAN_REPAIR_ATTEMPTS",
    "MAX_COMPACT_RETRIES",
    "NONTERMINAL_STATES",
    "SAFE_ERROR_CONTEXT_KEYS",
    "STATE_SET",
    "STATE_VALUES",
    "SUCCESS_STATES",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "TRANSLATION_PREFLIGHT_ROLES",
    "allowed_transitions",
    "assert_closed_graph",
    "build_source_cue_collection_root_preimage",
    "is_terminal",
    "validate_callback_event",
    "validate_transition",
    "validate_golden_canonical_bytes",
    "validate_idempotency_record",
    "validate_machine_report_aggregate",
    "validate_n_to_n",
    "validate_qa_packet_bundles",
    "validate_safe_error",
    "validate_source_cue_collection",
    "validate_source_cue_collection_root",
    "validate_status_invariants",
    "validate_status_callback_consistency",
    "validate_stored_bundle",
    "validate_taxonomy_document",
    "validate_tts_timing_evidence",
    "validate_translation_candidate",
    "validate_translation_attempts",
    "verify_stored_json_bundle",
]
