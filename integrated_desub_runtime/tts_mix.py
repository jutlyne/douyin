"""Pure structure helpers for direct CapCut TTS, scheduling, and audio tails.

This module deliberately does not make a network request at import time and
does not run FFmpeg.  ``synthesize_capcut_direct`` lazily uses the existing
CapCut adapter only when called, and accepts an injected adapter so unit tests
remain offline.

The mix manifest returned here is structure-only.  It records deterministic
sample arithmetic and the required five stem digests, but it is never release
authorization.  The unresolved FFmpeg, resampler, limiter, encoder, and JCS/KMS
bindings remain fail-closed in the Phase-A contracts.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


SAMPLE_RATE_HZ = 44_100
MINIMUM_GAP_SAMPLES = 2_205
MAXIMUM_LAG_SAMPLES = 26_460

CAPCUT_PROVIDER = "capcut-private"
CAPCUT_BASE_URL = "https://editor-api-sg.capcutapi.com"
CAPCUT_CREATE_PATH = "/lv/v1/common_task/new"
CAPCUT_REQUEST_KEY = "sami_text_to_speech"
CAPCUT_TASK_VERSION = "v3"
CAPCUT_AUDIO_FORMAT = "mp3"
CAPCUT_VOICE = "BV075_streaming"
CAPCUT_RESOURCE_ID = "7102355803792740865"
CAPCUT_XML_LANG = "vi-VN"
CAPCUT_PROSODY_RATE_TEXT = "1.5000"
CAPCUT_PROSODY_RATE = 1.5

REQUIRED_STEMS = (
    "clean_input",
    "bed",
    "ducked_bed",
    "voice",
    "premaster",
)

ORDERED_MIX_GRAPH = (
    "decode approved clean AAC",
    "resample and deterministically map to 44100 Hz stereo",
    "apad then atrim to exact video sample duration",
    "apply bed pre-duck gain -20.000 dB",
    "assemble selected voice clips at 0.000 dB",
    "sidechaincompress bed from voice bus",
    "amix ducked bed and voice with normalize=0",
    "two-pass BS.1770 loudnorm I=-16:LRA=11:TP=-1",
    "true-peak limiter at -1 dBTP",
    "apad and atrim to exact video sample duration",
    "encode exactly one AAC 44100 Hz stereo stream",
)

UNRESOLVED_MEDIA_BINDINGS = (
    "exact_channel_layout_matrix_table",
    "resampler_options",
    "internal_sample_format",
    "all_implicit_sidechaincompress_options",
    "loudnorm_pass2_measured_field_rounding",
    "loudnorm_linear_and_dual_mono_options",
    "true_peak_limiter_oversampling_and_options",
    "aac_encoder_profile_and_bitrate",
    "video_sample_count_rounding",
    "stem_canonical_byte_format",
    "ffmpeg_image_digest",
    "filter_graph_sha256",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_INVALID_XML_CONTROL_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
)


class RuntimeContractError(ValueError):
    """Raised when a runtime input cannot satisfy the frozen v1 structure."""


class CueScheduleUnfit(RuntimeContractError):
    """Raised by ``schedule_cues`` when a candidate cannot be selected."""

    def __init__(self, cue_index: int, schedule: "CueSchedule") -> None:
        super().__init__(f"cue {cue_index} does not fit the frozen schedule")
        self.cue_index = cue_index
        self.schedule = schedule


def _strict_int(
    value: object,
    label: str,
    *,
    minimum: int = 0,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeContractError(f"{label} must be an integer")
    if value < minimum:
        raise RuntimeContractError(f"{label} must be >= {minimum}")
    return value


def _nfc_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeContractError("text_vi_nfc must be a non-empty string")
    if unicodedata.normalize("NFC", value) != value:
        raise RuntimeContractError("text_vi_nfc must already be NFC")
    if _INVALID_XML_CONTROL_RE.search(value):
        raise RuntimeContractError("text_vi_nfc contains an XML-invalid control")
    return value


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def build_capcut_direct_request(text_vi_nfc: str) -> dict[str, Any]:
    """Build the immutable semantic projection and exact direct-rate SSML.

    No request digest is emitted because runtime RFC8785/JCS recomputation is a
    declared Gate-A blocker.  Ephemeral device identity and signatures are also
    intentionally absent from the semantic projection.
    """

    text = _nfc_text(text_vi_nfc)
    projection = {
        "provider": CAPCUT_PROVIDER,
        "base_url": CAPCUT_BASE_URL,
        "create_path": CAPCUT_CREATE_PATH,
        "request_key": CAPCUT_REQUEST_KEY,
        "task_version": CAPCUT_TASK_VERSION,
        "audio_format": CAPCUT_AUDIO_FORMAT,
        "voice": CAPCUT_VOICE,
        "resource_id": CAPCUT_RESOURCE_ID,
        "xml_lang": CAPCUT_XML_LANG,
        "prosody_rate": CAPCUT_PROSODY_RATE_TEXT,
        "text_vi_nfc": text,
    }
    ssml = (
        '<speak version="1.0" '
        'xmlns="http://www.w3.org/2001/10/synthesis" '
        f'xml:lang="{CAPCUT_XML_LANG}">\n'
        f'    <voice name="{CAPCUT_VOICE}" mock_tone_info="" platform="sami" '
        f'resource_id="{CAPCUT_RESOURCE_ID}" emotion="" emotion_scale="0" '
        'style="" role="" moyin_emotion="" is_clone_tone="false" '
        'need_subtitle_timestamp="false">\n'
        f'        <prosody rate="{CAPCUT_PROSODY_RATE_TEXT}">'
        f"{_xml_escape(text)}</prosody>\n"
        "    </voice>\n"
        "</speak>"
    )
    return {
        "semantic_projection": projection,
        "ssml": ssml,
        "execution_constraints": {
            "direct_provider_rate_only": True,
            "post_atempo_allowed": False,
            "per_cue_speed_allowed": False,
            "provider_fallback_allowed": False,
            "voice_fallback_allowed": False,
        },
        "normalized_request_sha256": None,
        "hash_status": "blocked_pending_vetted_rfc8785",
    }


def synthesize_capcut_direct(
    text_vi_nfc: str,
    dst_mp3: str,
    *,
    adapter: Callable[..., Any] | None = None,
    device: Mapping[str, Any] | None = None,
    device_json_path: str | None = None,
    poll_timeout: int = 300,
) -> Any:
    """Invoke exactly one CapCut synthesis at provider rate ``1.5000``.

    There is no provider fallback, voice fallback, adaptive retry rate, or
    post-``atempo`` path in this wrapper.  Network behavior belongs to the
    supplied/existing adapter and is not exercised by this module's tests.
    """

    request = build_capcut_direct_request(text_vi_nfc)
    if not isinstance(dst_mp3, str) or not dst_mp3.strip():
        raise RuntimeContractError("dst_mp3 must be a non-empty path")
    timeout = _strict_int(poll_timeout, "poll_timeout", minimum=1)
    if adapter is None:
        from container_short.steps.capcut_tts import (  # local lazy import
            synthesize_once as adapter,
        )
    projection = request["semantic_projection"]
    return adapter(
        projection["text_vi_nfc"],
        dst_mp3,
        voice=CAPCUT_VOICE,
        resource_id=CAPCUT_RESOURCE_ID,
        device=None if device is None else dict(device),
        device_json_path=device_json_path,
        rate=CAPCUT_PROSODY_RATE,
        poll_timeout=timeout,
    )


@dataclass(frozen=True)
class CueTimingInput:
    """Decoded TTS timing evidence required to place one cue."""

    cue_id: str
    cue_index: int
    speech_anchor_sample: int
    decoded_sample_count: int
    detected_onset_sample: int
    detected_offset_sample_inclusive: int


@dataclass(frozen=True)
class CueSchedule:
    """Frozen 44.1-kHz placement result plus private mix-planning metadata."""

    cue_id: str
    cue_index: int
    decoded_sample_count: int
    speech_anchor_sample: int
    previous_actual_offset_sample_inclusive: int | None
    target_actual_onset_sample: int
    video_sample_count: int
    placement_sample: int
    detected_onset_sample: int
    detected_offset_sample_inclusive: int
    actual_onset_sample: int
    actual_offset_sample_inclusive: int
    lag_samples: int
    previous_gap_samples: int | None
    overlap_samples: int
    fits: bool

    @property
    def placed_clip_end_sample_exclusive(self) -> int:
        """End of decoded clip including any trailing silence."""

        return self.placement_sample + self.decoded_sample_count

    def contract_dict(self) -> dict[str, int | bool | None]:
        """Return exactly the fields accepted by ``tts_attempt.schedule``."""

        return {
            "speech_anchor_sample": self.speech_anchor_sample,
            "previous_actual_offset_sample_inclusive": (
                self.previous_actual_offset_sample_inclusive
            ),
            "target_actual_onset_sample": self.target_actual_onset_sample,
            "video_sample_count": self.video_sample_count,
            "placement_sample": self.placement_sample,
            "actual_onset_sample": self.actual_onset_sample,
            "actual_offset_sample_inclusive": (
                self.actual_offset_sample_inclusive
            ),
            "lag_samples": self.lag_samples,
            "previous_gap_samples": self.previous_gap_samples,
            "overlap_samples": self.overlap_samples,
            "fits": self.fits,
        }


def schedule_cue(
    cue: CueTimingInput,
    *,
    video_sample_count: int,
    previous_actual_offset_sample_inclusive: int | None = None,
) -> CueSchedule:
    """Place one decoded clip using the frozen actual-speech equations."""

    if not isinstance(cue, CueTimingInput):
        raise RuntimeContractError("cue must be CueTimingInput")
    if not re.fullmatch(r"cue-[0-9]{6}", cue.cue_id):
        raise RuntimeContractError("cue_id must match cue-%06d")
    cue_index = _strict_int(cue.cue_index, "cue_index")
    if cue.cue_id != f"cue-{cue_index:06d}":
        raise RuntimeContractError("cue_id and cue_index do not match")
    anchor = _strict_int(cue.speech_anchor_sample, "speech_anchor_sample")
    decoded_count = _strict_int(
        cue.decoded_sample_count,
        "decoded_sample_count",
        minimum=1,
    )
    detected_onset = _strict_int(
        cue.detected_onset_sample,
        "detected_onset_sample",
    )
    detected_offset = _strict_int(
        cue.detected_offset_sample_inclusive,
        "detected_offset_sample_inclusive",
    )
    if detected_onset > detected_offset or detected_offset >= decoded_count:
        raise RuntimeContractError(
            "detected active interval must stay inside decoded PCM"
        )
    video_count = _strict_int(
        video_sample_count,
        "video_sample_count",
        minimum=1,
    )
    previous = (
        None
        if previous_actual_offset_sample_inclusive is None
        else _strict_int(
            previous_actual_offset_sample_inclusive,
            "previous_actual_offset_sample_inclusive",
        )
    )
    target = max(
        anchor,
        anchor if previous is None else previous + MINIMUM_GAP_SAMPLES,
    )
    placement = max(0, target - detected_onset)
    actual_onset = placement + detected_onset
    actual_offset = placement + detected_offset
    lag = actual_onset - anchor
    previous_gap = None if previous is None else actual_onset - previous
    overlap = 0 if previous is None else max(0, previous - actual_onset + 1)
    fits = (
        0 <= lag <= MAXIMUM_LAG_SAMPLES
        and (previous_gap is None or previous_gap >= MINIMUM_GAP_SAMPLES)
        and overlap == 0
        and actual_offset <= video_count - 1
    )
    return CueSchedule(
        cue_id=cue.cue_id,
        cue_index=cue_index,
        decoded_sample_count=decoded_count,
        speech_anchor_sample=anchor,
        previous_actual_offset_sample_inclusive=previous,
        target_actual_onset_sample=target,
        video_sample_count=video_count,
        placement_sample=placement,
        detected_onset_sample=detected_onset,
        detected_offset_sample_inclusive=detected_offset,
        actual_onset_sample=actual_onset,
        actual_offset_sample_inclusive=actual_offset,
        lag_samples=lag,
        previous_gap_samples=previous_gap,
        overlap_samples=overlap,
        fits=fits,
    )


def schedule_cues(
    cues: Sequence[CueTimingInput],
    *,
    video_sample_count: int,
) -> tuple[CueSchedule, ...]:
    """Schedule a contiguous cue sequence, stopping at the first unfit cue."""

    if not isinstance(cues, Sequence) or isinstance(cues, (str, bytes)):
        raise RuntimeContractError("cues must be a sequence")
    if not cues:
        raise RuntimeContractError("cues cannot be empty")
    schedules: list[CueSchedule] = []
    previous: int | None = None
    for expected_index, cue in enumerate(cues):
        if not isinstance(cue, CueTimingInput) or cue.cue_index != expected_index:
            raise RuntimeContractError("cue indexes must be contiguous from zero")
        schedule = schedule_cue(
            cue,
            video_sample_count=video_sample_count,
            previous_actual_offset_sample_inclusive=previous,
        )
        if not schedule.fits:
            raise CueScheduleUnfit(expected_index, schedule)
        schedules.append(schedule)
        previous = schedule.actual_offset_sample_inclusive
    return tuple(schedules)


@dataclass(frozen=True)
class TailAdjustment:
    """Exact apad/atrim arithmetic for one PCM stream."""

    source_sample_count: int
    target_sample_count: int
    pad_samples: int
    trim_samples: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_sample_count": self.source_sample_count,
            "target_sample_count": self.target_sample_count,
            "pad_samples": self.pad_samples,
            "trim_samples": self.trim_samples,
            "ordered_operations": ["apad", "atrim"],
        }


def plan_tail_adjustment(
    source_sample_count: int,
    target_sample_count: int,
) -> TailAdjustment:
    """Return mutually exclusive padding/trimming to an exact sample count."""

    source = _strict_int(source_sample_count, "source_sample_count")
    target = _strict_int(
        target_sample_count,
        "target_sample_count",
        minimum=1,
    )
    return TailAdjustment(
        source_sample_count=source,
        target_sample_count=target,
        pad_samples=max(0, target - source),
        trim_samples=max(0, source - target),
    )


def _stem_evidence(
    value: object,
    *,
    name: str,
    expected_sample_count: int,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "sha256",
        "sample_count",
    }:
        raise RuntimeContractError(
            f"stem_pcm.{name} must contain only sha256 and sample_count"
        )
    sha256 = value.get("sha256")
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise RuntimeContractError(f"stem_pcm.{name}.sha256 is invalid")
    count = _strict_int(
        value.get("sample_count"),
        f"stem_pcm.{name}.sample_count",
        minimum=1,
    )
    if count != expected_sample_count:
        raise RuntimeContractError(
            f"stem_pcm.{name}.sample_count must equal {expected_sample_count}"
        )
    return {"sha256": sha256, "sample_count": count}


def build_mix_manifest_structure(
    *,
    video_sample_count: int,
    clean_input_sample_count: int,
    schedules: Sequence[CueSchedule],
    stem_pcm: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a fail-closed mix/tail evidence structure.

    ``bed``, ``ducked_bed``, ``voice``, and ``premaster`` must already have
    exact video duration.  ``clean_input`` records the pre-pad decoded clean
    stream, and the returned tail plan states how it reaches the video clock.
    """

    video_count = _strict_int(
        video_sample_count,
        "video_sample_count",
        minimum=1,
    )
    clean_count = _strict_int(
        clean_input_sample_count,
        "clean_input_sample_count",
        minimum=1,
    )
    if not isinstance(schedules, Sequence) or isinstance(
        schedules, (str, bytes)
    ):
        raise RuntimeContractError("schedules must be a sequence")
    if not schedules:
        raise RuntimeContractError("schedules cannot be empty")
    previous: int | None = None
    placed_clip_end = 0
    normalized_schedules: list[dict[str, Any]] = []
    for expected_index, schedule in enumerate(schedules):
        if not isinstance(schedule, CueSchedule):
            raise RuntimeContractError("every schedule must be CueSchedule")
        if schedule.cue_index != expected_index:
            raise RuntimeContractError(
                "schedule indexes must be contiguous from zero"
            )
        if schedule.video_sample_count != video_count:
            raise RuntimeContractError("schedule/video sample count mismatch")
        if not schedule.fits:
            raise RuntimeContractError("unfit schedules cannot enter the mix")
        if schedule.previous_actual_offset_sample_inclusive != previous:
            raise RuntimeContractError("schedule chain previous offset mismatch")
        normalized_schedules.append(
            {
                "cue_id": schedule.cue_id,
                "cue_index": schedule.cue_index,
                "decoded_sample_count": schedule.decoded_sample_count,
                "detected_onset_sample": schedule.detected_onset_sample,
                "detected_offset_sample_inclusive": (
                    schedule.detected_offset_sample_inclusive
                ),
                "schedule": schedule.contract_dict(),
            }
        )
        previous = schedule.actual_offset_sample_inclusive
        placed_clip_end = max(
            placed_clip_end,
            schedule.placed_clip_end_sample_exclusive,
        )
    if not isinstance(stem_pcm, Mapping) or set(stem_pcm) != set(
        REQUIRED_STEMS
    ):
        raise RuntimeContractError(
            "stem_pcm must contain exactly the five named stems"
        )
    normalized_stems = {
        "clean_input": _stem_evidence(
            stem_pcm["clean_input"],
            name="clean_input",
            expected_sample_count=clean_count,
        )
    }
    for name in REQUIRED_STEMS[1:]:
        normalized_stems[name] = _stem_evidence(
            stem_pcm[name],
            name=name,
            expected_sample_count=video_count,
        )
    return {
        "schema_version": "1",
        "kind": "integrated_desub_mix_manifest_structure",
        "status": "blocked_pending_media_runtime_bindings",
        "authorization_ready": False,
        "sample_clock": {
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "sample_format": "f32le_internal",
            "channels": 2,
            "channel_layout": "stereo",
            "video_sample_count": video_count,
        },
        "tts_identity": {
            "provider": CAPCUT_PROVIDER,
            "voice": CAPCUT_VOICE,
            "resource_id": CAPCUT_RESOURCE_ID,
            "prosody_rate": CAPCUT_PROSODY_RATE_TEXT,
            "post_atempo_allowed": False,
            "provider_or_voice_fallback_allowed": False,
        },
        "cue_schedules": normalized_schedules,
        "tail_adjustments": {
            "clean_input_to_bed": plan_tail_adjustment(
                clean_count,
                video_count,
            ).as_dict(),
            "placed_voice_to_bus": plan_tail_adjustment(
                placed_clip_end,
                video_count,
            ).as_dict(),
            "premaster_to_output": {
                "target_sample_count": video_count,
                "ordered_operations": ["apad", "atrim"],
            },
        },
        "ordered_graph": list(ORDERED_MIX_GRAPH),
        "gains_db": {"bed_pre_duck": "-20.000", "voice": "0.000"},
        "sidechaincompress": {
            "threshold": "0.020000",
            "ratio": "10.000",
            "attack_ms": 8,
            "release_ms": 250,
        },
        "amix": {"normalize": 0},
        "loudnorm_two_pass": {
            "integrated_lufs": -16,
            "lra": 11,
            "true_peak_dbtp": -1,
        },
        "limiter": {"true_peak_dbtp": -1},
        "stem_pcm": normalized_stems,
        "output_audio": {
            "codec_name": "aac",
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "channels": 2,
            "channel_layout": "stereo",
            "decoded_sample_count": video_count,
        },
        "unresolved_runtime_bindings": list(UNRESOLVED_MEDIA_BINDINGS),
    }
