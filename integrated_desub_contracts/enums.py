from __future__ import annotations

from types import MappingProxyType
from typing import Final


MAX_COMPACT_RETRIES: Final = 2
MAX_CLEAN_REPAIR_ATTEMPTS: Final = 1

NONTERMINAL_STATES: Final = (
    "queued",
    "downloading",
    "detecting",
    "inpainting",
    "encoding_clean",
    "verifying_clean_machine",
    "verifying_clean_agents",
    "repairing_clean",
    "clean_approved",
    "aligning_speech",
    "translating",
    "verifying_translation",
    "compacting_translation",
    "synthesizing_tts",
    "scheduling_tts",
    "rendering_vietsub",
    "mixing_audio",
    "verifying_dub_machine",
    "verifying_dub_agents",
    "signing_release",
)

FAILURE_STATES: Final = (
    "clean_qa_failed",
    "translation_failed",
    "tts_failed",
    "scheduling_failed",
    "subtitle_failed",
    "dub_qa_failed",
    "failed",
    "cancelled",
)

SUCCESS_STATES: Final = ("completed",)
TERMINAL_STATES: Final = SUCCESS_STATES + FAILURE_STATES
STATE_VALUES: Final = NONTERMINAL_STATES + TERMINAL_STATES
STATE_SET: Final = frozenset(STATE_VALUES)

LAUNCH_STATE_VALUES: Final = (
    "reserving",
    "launching",
    "unknown",
    "launched",
)
LAUNCH_STATE_SET: Final = frozenset(LAUNCH_STATE_VALUES)

ERROR_STAGE_VALUES: Final = (
    "intake",
    "admission",
    "launch",
    "download",
    "input_preflight",
    "source_cue_extraction",
    "clean_detection",
    "clean_inpaint",
    "clean_encode",
    "clean_machine_qa",
    "clean_agent_qa",
    "clean_signing",
    "speech_alignment",
    "translation",
    "translation_preflight",
    "tts",
    "scheduling",
    "vietsub",
    "audio_mix",
    "dub_machine_qa",
    "dub_agent_qa",
    "release_signing",
    "callback",
    "download_delivery",
    "cancellation",
)
ERROR_STAGE_SET: Final = frozenset(ERROR_STAGE_VALUES)

ERROR_CODE_VALUES: Final = (
    # Intake, admission and lookup.
    "INVALID_JSON",
    "SCHEMA_VALIDATION_FAILED",
    "INVALID_DOUYIN_URL",
    "UNAUTHORIZED",
    "IDEMPOTENCY_CONFLICT",
    "ATTEMPT_IN_PROGRESS",
    "CAPACITY_BUSY",
    "JOB_NOT_FOUND",
    "ATTEMPT_NOT_FOUND",
    # Source retrieval and v1 input policy.
    "SOURCE_DOWNLOAD_TIMEOUT",
    "SOURCE_DOWNLOAD_FAILED",
    "SOURCE_REDIRECT_BLOCKED",
    "SOURCE_TOO_LARGE",
    "SOURCE_MIME_UNSUPPORTED",
    "SOURCE_CONTAINER_UNSUPPORTED",
    "SOURCE_DURATION_UNSUPPORTED",
    "SOURCE_RESOLUTION_UNSUPPORTED",
    "SOURCE_FPS_UNSUPPORTED",
    "SOURCE_VFR_UNSUPPORTED",
    "SOURCE_HDR_UNSUPPORTED",
    "SOURCE_ROTATION_UNSUPPORTED",
    "SOURCE_AUDIO_CODEC_UNSUPPORTED",
    "SOURCE_MULTI_AUDIO_UNSUPPORTED",
    "DUB_SOURCE_AUDIO_REQUIRED",
    "UNSUPPORTED_SUBTITLE_LAYOUT",
    # Launch, fencing and execution lifecycle.
    "LAUNCH_PRECONDITION_FAILED",
    "LAUNCH_STATE_UNRESOLVED",
    "EXECUTION_TIMEOUT",
    "STALE_FENCE",
    "INVALID_STATE_TRANSITION",
    # Clean and immutable source-cue evidence.
    "SOURCE_CUE_EXTRACTION_FAILED",
    "SOURCE_CUE_MANIFEST_INVALID",
    "SOURCE_CUE_MISMATCH",
    "CLEAN_DETECTION_FAILED",
    "CLEAN_INPAINT_FAILED",
    "CLEAN_ENCODE_FAILED",
    "CLEAN_MEDIA_PARITY_FAILED",
    "CLEAN_AUDIO_PARITY_FAILED",
    "CLEAN_RESIDUAL_TEXT",
    "CLEAN_MASK_PRECISION_FAILED",
    "CLEAN_OUTSIDE_MASK_DAMAGE",
    "CLEAN_TEMPORAL_FLICKER",
    "CLEAN_QA_FAILED",
    "CLEAN_APPROVAL_INVALID",
    # Translation, TTS, scheduling, subtitles and dub QA.
    "SPEECH_ALIGNMENT_FAILED",
    "TRANSLATION_PROVIDER_FAILED",
    "TRANSLATION_PREFLIGHT_FAILED",
    "DUB_CUE_UNFIT",
    "TTS_PROVIDER_FAILED",
    "TTS_MEDIA_INVALID",
    "TTS_VOICE_RATE_MISMATCH",
    "DUB_SCHEDULE_INVALID",
    "DUB_SUBTITLE_UNFIT",
    "DUB_VIDEO_INTEGRITY_FAILED",
    "DUB_AUDIO_POLICY_FAILED",
    "DUB_AUDIO_TAIL_FAILED",
    "DUB_QA_FAILED",
    # Agent/runtime, signed approvals and retained results.
    "QA_RUNTIME_UNAVAILABLE",
    "QA_TIMEOUT",
    "QA_VERDICT_INVALID",
    "QA_VERDICT_BINDING_MISMATCH",
    "APPROVAL_INVALID",
    "APPROVAL_EXPIRED",
    "APPROVAL_REVERIFICATION_REQUIRED",
    "ATTEMPT_RESULT_EXPIRED",
    "KMS_KEY_INVALID",
    "REQUEST_CANCELLED",
    # Callback and download delivery.
    "CALLBACK_SIGNATURE_INVALID",
    "CALLBACK_REPLAY",
    "CALLBACK_DELIVERY_EXHAUSTED",
    "DOWNLOAD_SIGNATURE_INVALID",
    "DOWNLOAD_EXPIRED",
    "DOWNLOAD_RANGE_INVALID",
)
ERROR_CODE_SET: Final = frozenset(ERROR_CODE_VALUES)

SAFE_ERROR_CONTEXT_KEYS: Final = frozenset(
    {
        "cue_index",
        "limit_name",
        "retry_after_seconds",
    }
)


def _error_semantics(
    family: str,
    stage_terminals: tuple[tuple[str, str | None], ...],
    retryable: bool,
    public_http_status: int,
    context_keys: tuple[str, ...] = (),
):
    """Build one immutable entry in the frozen public-error semantics table."""

    return MappingProxyType(
        {
            "family": family,
            "allowed_stages": tuple(stage for stage, _ in stage_terminals),
            "retryable": retryable,
            "public_http_status": public_http_status,
            "terminal_state_by_stage": MappingProxyType(dict(stage_terminals)),
            "context_keys": frozenset(context_keys),
        }
    )


# This table is the executable twin of policies/v1/error_semantics_policy.json.
# ``public_http_status`` applies when the error is emitted as the direct HTTP
# response. Reading an already-persisted terminal attempt remains HTTP 200.
ERROR_SEMANTICS: Final = MappingProxyType(
    {
        "INVALID_JSON": _error_semantics(
            "intake", (("intake", None),), False, 400
        ),
        "SCHEMA_VALIDATION_FAILED": _error_semantics(
            "intake", (("intake", None),), False, 400
        ),
        "INVALID_DOUYIN_URL": _error_semantics(
            "intake", (("intake", None),), False, 400
        ),
        "UNAUTHORIZED": _error_semantics(
            "authorization",
            (("intake", None), ("callback", None), ("download_delivery", None)),
            False,
            401,
        ),
        "IDEMPOTENCY_CONFLICT": _error_semantics(
            "admission", (("admission", None),), False, 409
        ),
        "ATTEMPT_IN_PROGRESS": _error_semantics(
            "admission", (("admission", None),), False, 409
        ),
        "CAPACITY_BUSY": _error_semantics(
            "admission",
            (("admission", None),),
            True,
            429,
            ("limit_name", "retry_after_seconds"),
        ),
        "JOB_NOT_FOUND": _error_semantics(
            "lookup", (("admission", None),), False, 404
        ),
        "ATTEMPT_NOT_FOUND": _error_semantics(
            "lookup", (("admission", None),), False, 404
        ),
        "SOURCE_DOWNLOAD_TIMEOUT": _error_semantics(
            "download",
            (("download", "failed"),),
            True,
            504,
            ("retry_after_seconds",),
        ),
        "SOURCE_DOWNLOAD_FAILED": _error_semantics(
            "download",
            (("download", "failed"),),
            True,
            502,
            ("retry_after_seconds",),
        ),
        "SOURCE_REDIRECT_BLOCKED": _error_semantics(
            "download", (("download", "failed"),), False, 422
        ),
        "SOURCE_TOO_LARGE": _error_semantics(
            "input_policy",
            (("input_preflight", "failed"),),
            False,
            413,
            ("limit_name",),
        ),
        "SOURCE_MIME_UNSUPPORTED": _error_semantics(
            "input_policy", (("input_preflight", "failed"),), False, 415
        ),
        "SOURCE_CONTAINER_UNSUPPORTED": _error_semantics(
            "input_policy", (("input_preflight", "failed"),), False, 422
        ),
        "SOURCE_DURATION_UNSUPPORTED": _error_semantics(
            "input_policy",
            (("input_preflight", "failed"),),
            False,
            422,
            ("limit_name",),
        ),
        "SOURCE_RESOLUTION_UNSUPPORTED": _error_semantics(
            "input_policy",
            (("input_preflight", "failed"),),
            False,
            422,
            ("limit_name",),
        ),
        "SOURCE_FPS_UNSUPPORTED": _error_semantics(
            "input_policy",
            (("input_preflight", "failed"),),
            False,
            422,
            ("limit_name",),
        ),
        "SOURCE_VFR_UNSUPPORTED": _error_semantics(
            "input_policy", (("input_preflight", "failed"),), False, 422
        ),
        "SOURCE_HDR_UNSUPPORTED": _error_semantics(
            "input_policy", (("input_preflight", "failed"),), False, 422
        ),
        "SOURCE_ROTATION_UNSUPPORTED": _error_semantics(
            "input_policy", (("input_preflight", "failed"),), False, 422
        ),
        "SOURCE_AUDIO_CODEC_UNSUPPORTED": _error_semantics(
            "input_policy", (("input_preflight", "failed"),), False, 422
        ),
        "SOURCE_MULTI_AUDIO_UNSUPPORTED": _error_semantics(
            "input_policy", (("input_preflight", "failed"),), False, 422
        ),
        "DUB_SOURCE_AUDIO_REQUIRED": _error_semantics(
            "input_policy", (("input_preflight", "failed"),), False, 422
        ),
        "UNSUPPORTED_SUBTITLE_LAYOUT": _error_semantics(
            "input_policy", (("input_preflight", "failed"),), False, 422
        ),
        "LAUNCH_PRECONDITION_FAILED": _error_semantics(
            "lifecycle", (("launch", "failed"),), False, 409
        ),
        "LAUNCH_STATE_UNRESOLVED": _error_semantics(
            "lifecycle",
            (("launch", "failed"),),
            True,
            503,
            ("retry_after_seconds",),
        ),
        "EXECUTION_TIMEOUT": _error_semantics(
            "lifecycle",
            (("launch", "failed"),),
            True,
            504,
            ("limit_name",),
        ),
        "STALE_FENCE": _error_semantics(
            "lifecycle",
            (("launch", "failed"),),
            False,
            409,
        ),
        "INVALID_STATE_TRANSITION": _error_semantics(
            "lifecycle", (("launch", "failed"),), False, 409
        ),
        "SOURCE_CUE_EXTRACTION_FAILED": _error_semantics(
            "source_cue", (("source_cue_extraction", "failed"),), False, 422
        ),
        "SOURCE_CUE_MANIFEST_INVALID": _error_semantics(
            "source_cue", (("source_cue_extraction", "failed"),), False, 422
        ),
        "SOURCE_CUE_MISMATCH": _error_semantics(
            "source_cue",
            (("source_cue_extraction", "failed"),),
            False,
            422,
            ("cue_index",),
        ),
        "CLEAN_DETECTION_FAILED": _error_semantics(
            "clean", (("clean_detection", "clean_qa_failed"),), False, 422
        ),
        "CLEAN_INPAINT_FAILED": _error_semantics(
            "clean", (("clean_inpaint", "clean_qa_failed"),), False, 422
        ),
        "CLEAN_ENCODE_FAILED": _error_semantics(
            "clean", (("clean_encode", "clean_qa_failed"),), False, 422
        ),
        "CLEAN_MEDIA_PARITY_FAILED": _error_semantics(
            "clean", (("clean_machine_qa", "clean_qa_failed"),), False, 422
        ),
        "CLEAN_AUDIO_PARITY_FAILED": _error_semantics(
            "clean", (("clean_machine_qa", "clean_qa_failed"),), False, 422
        ),
        "CLEAN_RESIDUAL_TEXT": _error_semantics(
            "clean", (("clean_machine_qa", "clean_qa_failed"),), False, 422
        ),
        "CLEAN_MASK_PRECISION_FAILED": _error_semantics(
            "clean", (("clean_machine_qa", "clean_qa_failed"),), False, 422
        ),
        "CLEAN_OUTSIDE_MASK_DAMAGE": _error_semantics(
            "clean", (("clean_machine_qa", "clean_qa_failed"),), False, 422
        ),
        "CLEAN_TEMPORAL_FLICKER": _error_semantics(
            "clean", (("clean_machine_qa", "clean_qa_failed"),), False, 422
        ),
        "CLEAN_QA_FAILED": _error_semantics(
            "clean",
            (
                ("clean_machine_qa", "clean_qa_failed"),
                ("clean_agent_qa", "clean_qa_failed"),
            ),
            False,
            422,
        ),
        "CLEAN_APPROVAL_INVALID": _error_semantics(
            "approval", (("clean_signing", "clean_qa_failed"),), False, 409
        ),
        "SPEECH_ALIGNMENT_FAILED": _error_semantics(
            "translation",
            (("speech_alignment", "translation_failed"),),
            False,
            422,
        ),
        "TRANSLATION_PROVIDER_FAILED": _error_semantics(
            "translation",
            (("translation", "translation_failed"),),
            True,
            502,
            ("retry_after_seconds",),
        ),
        "TRANSLATION_PREFLIGHT_FAILED": _error_semantics(
            "translation",
            (("translation_preflight", "translation_failed"),),
            False,
            422,
            ("cue_index",),
        ),
        "DUB_CUE_UNFIT": _error_semantics(
            "scheduling",
            (("scheduling", "scheduling_failed"),),
            False,
            422,
            ("cue_index", "limit_name"),
        ),
        "TTS_PROVIDER_FAILED": _error_semantics(
            "tts",
            (("tts", "tts_failed"),),
            True,
            502,
            ("cue_index", "retry_after_seconds"),
        ),
        "TTS_MEDIA_INVALID": _error_semantics(
            "tts",
            (("tts", "tts_failed"),),
            False,
            422,
            ("cue_index",),
        ),
        "TTS_VOICE_RATE_MISMATCH": _error_semantics(
            "tts",
            (("tts", "tts_failed"),),
            False,
            422,
            ("cue_index",),
        ),
        "DUB_SCHEDULE_INVALID": _error_semantics(
            "scheduling",
            (("scheduling", "scheduling_failed"),),
            False,
            422,
            ("cue_index", "limit_name"),
        ),
        "DUB_SUBTITLE_UNFIT": _error_semantics(
            "subtitle",
            (("vietsub", "subtitle_failed"),),
            False,
            422,
            ("cue_index", "limit_name"),
        ),
        "DUB_VIDEO_INTEGRITY_FAILED": _error_semantics(
            "dub", (("dub_machine_qa", "dub_qa_failed"),), False, 422
        ),
        "DUB_AUDIO_POLICY_FAILED": _error_semantics(
            "dub",
            (
                ("audio_mix", "dub_qa_failed"),
                ("dub_machine_qa", "dub_qa_failed"),
            ),
            False,
            422,
        ),
        "DUB_AUDIO_TAIL_FAILED": _error_semantics(
            "dub", (("dub_machine_qa", "dub_qa_failed"),), False, 422
        ),
        "DUB_QA_FAILED": _error_semantics(
            "dub",
            (
                ("dub_machine_qa", "dub_qa_failed"),
                ("dub_agent_qa", "dub_qa_failed"),
            ),
            False,
            422,
        ),
        "QA_RUNTIME_UNAVAILABLE": _error_semantics(
            "qa_runtime",
            (
                ("clean_agent_qa", "clean_qa_failed"),
                ("translation_preflight", "translation_failed"),
                ("dub_agent_qa", "dub_qa_failed"),
            ),
            True,
            503,
            ("retry_after_seconds",),
        ),
        "QA_TIMEOUT": _error_semantics(
            "qa_runtime",
            (
                ("clean_agent_qa", "clean_qa_failed"),
                ("translation_preflight", "translation_failed"),
                ("dub_agent_qa", "dub_qa_failed"),
            ),
            True,
            504,
            ("retry_after_seconds",),
        ),
        "QA_VERDICT_INVALID": _error_semantics(
            "qa_runtime",
            (
                ("clean_agent_qa", "clean_qa_failed"),
                ("translation_preflight", "translation_failed"),
                ("dub_agent_qa", "dub_qa_failed"),
            ),
            False,
            422,
        ),
        "QA_VERDICT_BINDING_MISMATCH": _error_semantics(
            "qa_runtime",
            (
                ("clean_agent_qa", "clean_qa_failed"),
                ("translation_preflight", "translation_failed"),
                ("dub_agent_qa", "dub_qa_failed"),
            ),
            False,
            409,
        ),
        "APPROVAL_INVALID": _error_semantics(
            "approval",
            (
                ("clean_signing", "clean_qa_failed"),
                ("release_signing", "dub_qa_failed"),
                ("download_delivery", None),
            ),
            False,
            409,
        ),
        "APPROVAL_EXPIRED": _error_semantics(
            "approval",
            (
                ("clean_signing", "clean_qa_failed"),
                ("release_signing", "dub_qa_failed"),
                ("download_delivery", None),
            ),
            False,
            409,
        ),
        "APPROVAL_REVERIFICATION_REQUIRED": _error_semantics(
            "approval", (("download_delivery", None),), False, 409
        ),
        "ATTEMPT_RESULT_EXPIRED": _error_semantics(
            "retention", (("download_delivery", None),), False, 410
        ),
        "KMS_KEY_INVALID": _error_semantics(
            "approval",
            (
                ("clean_signing", "clean_qa_failed"),
                ("release_signing", "dub_qa_failed"),
            ),
            False,
            500,
        ),
        "REQUEST_CANCELLED": _error_semantics(
            "cancellation", (("cancellation", "cancelled"),), False, 200
        ),
        "CALLBACK_SIGNATURE_INVALID": _error_semantics(
            "callback", (("callback", None),), False, 401
        ),
        "CALLBACK_REPLAY": _error_semantics(
            "callback", (("callback", None),), False, 409
        ),
        "CALLBACK_DELIVERY_EXHAUSTED": _error_semantics(
            "callback",
            (("callback", "failed"),),
            True,
            503,
            ("retry_after_seconds",),
        ),
        "DOWNLOAD_SIGNATURE_INVALID": _error_semantics(
            "download", (("download_delivery", None),), False, 401
        ),
        "DOWNLOAD_EXPIRED": _error_semantics(
            "download", (("download_delivery", None),), False, 410
        ),
        "DOWNLOAD_RANGE_INVALID": _error_semantics(
            "download",
            (("download_delivery", None),),
            False,
            416,
            ("limit_name",),
        ),
    }
)

if set(ERROR_SEMANTICS) != ERROR_CODE_SET:
    raise RuntimeError("ERROR_SEMANTICS must cover the frozen error code set exactly")
for _error_code, _error_contract in ERROR_SEMANTICS.items():
    if set(_error_contract["allowed_stages"]) != set(
        _error_contract["terminal_state_by_stage"]
    ):
        raise RuntimeError(f"{_error_code} stage/terminal semantics are inconsistent")
    if not set(_error_contract["allowed_stages"]) <= ERROR_STAGE_SET:
        raise RuntimeError(f"{_error_code} contains an unknown stage")
    if not set(_error_contract["context_keys"]) <= SAFE_ERROR_CONTEXT_KEYS:
        raise RuntimeError(f"{_error_code} contains an unsafe context key")
    if not {
        state
        for state in _error_contract["terminal_state_by_stage"].values()
        if state is not None
    } <= set(TERMINAL_STATES):
        raise RuntimeError(f"{_error_code} contains an unknown terminal state")

CLEAN_VERDICT_ROLES: Final = frozenset(
    {"clean_a", "clean_b", "clean_c", "clean_final_sol"}
)
TRANSLATION_PREFLIGHT_ROLES: Final = frozenset(
    {"translation_semantic_preflight", "translation_style_preflight"}
)
FINAL_DUB_VERDICT_ROLES: Final = frozenset(
    {
        "translation_semantics",
        "translation_style",
        "dub_audio_video",
        "dub_final_sol",
    }
)
