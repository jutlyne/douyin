"""Isolated runtime scaffolding for the integrated DESUB pipeline.

The helpers exported here are deterministic structure builders.  They do not
authorize a clean artifact, a dub artifact, a release, or a download link.
"""

from .tts_mix import (
    CAPCUT_PROSODY_RATE,
    CAPCUT_PROSODY_RATE_TEXT,
    CAPCUT_RESOURCE_ID,
    CAPCUT_VOICE,
    REQUIRED_STEMS,
    SAMPLE_RATE_HZ,
    CueSchedule,
    CueScheduleUnfit,
    CueTimingInput,
    RuntimeContractError,
    TailAdjustment,
    build_capcut_direct_request,
    build_mix_manifest_structure,
    plan_tail_adjustment,
    schedule_cue,
    schedule_cues,
    synthesize_capcut_direct,
)

__all__ = [
    "CAPCUT_PROSODY_RATE",
    "CAPCUT_PROSODY_RATE_TEXT",
    "CAPCUT_RESOURCE_ID",
    "CAPCUT_VOICE",
    "REQUIRED_STEMS",
    "SAMPLE_RATE_HZ",
    "CueSchedule",
    "CueScheduleUnfit",
    "CueTimingInput",
    "RuntimeContractError",
    "TailAdjustment",
    "build_capcut_direct_request",
    "build_mix_manifest_structure",
    "plan_tail_adjustment",
    "schedule_cue",
    "schedule_cues",
    "synthesize_capcut_direct",
]
