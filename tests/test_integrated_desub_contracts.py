from __future__ import annotations

import copy
import hashlib
import json
import unicodedata
import unittest
from pathlib import Path

from integrated_desub_contracts import (
    CLEAN_VERDICT_ROLES,
    ERROR_CODE_SET,
    ERROR_SEMANTICS,
    ERROR_STAGE_SET,
    MAX_CLEAN_REPAIR_ATTEMPTS,
    MAX_COMPACT_RETRIES,
    REQUIRED_MACHINE_METRIC_NAMES,
    SAFE_ERROR_CONTEXT_KEYS,
    STATE_SET,
    TERMINAL_STATES,
    TRANSITIONS,
    TRANSLATION_PREFLIGHT_ROLES,
    ContractValidationError,
    InvalidTransition,
    assert_closed_graph,
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
    validate_transition,
    validate_translation_attempts,
    validate_tts_timing_evidence,
)
from integrated_desub_contracts.validation import (
    validate_clean_evidence_bindings_structure,
    validate_clean_gate_index_structure,
    validate_cue_ledger_structure,
    validate_dub_work_item_structure,
    validate_release_bundle_structure,
    validate_release_index_structure,
)


NOW = "2026-07-23T00:00:00Z"
LATER = "2026-07-24T00:00:00Z"


def _sha(label: str | bytes) -> str:
    value = label if isinstance(label, bytes) else label.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _attempt() -> dict[str, object]:
    return {
        "job_id": "desub-0123456789abcdef0123456789abcdef",
        "attempt_id": "att-0123456789abcdef0123456789abcdef",
        "attempt_seq": 1,
        "fence_digest": _sha("fence"),
    }


def _obj(
    label: str,
    *,
    content_type: str = "application/json",
    digest: str | None = None,
    size_bytes: int = 128,
) -> dict[str, object]:
    path = label.replace(" ", "-")
    return {
        "uri": f"gs://desub-media/integrated/{path}",
        "generation": "123456789",
        "size_bytes": size_bytes,
        "sha256": digest or _sha(label),
        "content_type": content_type,
    }


def _object_for_bytes(
    label: str,
    value: bytes,
    *,
    content_type: str = "application/json",
) -> dict[str, object]:
    return _obj(
        label,
        content_type=content_type,
        digest=_sha(value),
        size_bytes=len(value),
    )


def _policy(name: str) -> dict[str, str]:
    return {"name": name, "version": "1.0.0", "sha256": _sha(name)}


def _model(label: str, *, digested: bool = False) -> dict[str, object]:
    result: dict[str, object] = {
        "provider": "openai",
        "service_identity": "serviceAccount:qa@desub.iam.gserviceaccount.com",
        "model_id": label,
        "model_version": "2026-07-23",
        "endpoint_region": "global",
        "adapter_sha256": _sha(f"{label}-adapter"),
        "response_id": f"response-{label}",
    }
    if digested:
        result["model_digest"] = f"sha256:{_sha(f'{label}-weights')}"
    return result


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


def _repack_record(
    manifest: dict[str, object],
    bundles: list[dict[str, object]],
    index: int,
) -> None:
    record = bundles[index]["record"]
    assert isinstance(record, dict)
    record_bytes = _json_bytes(record)
    record_ref = _object_for_bytes(f"cue-{index:06d}.json", record_bytes)
    bundles[index]["record_bytes"] = record_bytes
    bundles[index]["record_ref"] = record_ref
    cues = manifest["cues"]
    assert isinstance(cues, list)
    cues[index]["record"] = record_ref


def _source_collection(
    count: int = 2,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    attempt = _attempt()
    source = _obj("source.mp4", content_type="video/mp4")
    cues: list[dict[str, object]] = []
    bundles: list[dict[str, object]] = []
    for index in range(count):
        start_sample = index * 4000
        start_frame = index * 10
        raw_text = "你好" if index == 0 else "Cafe\u0301"
        crop = _obj(f"cue-{index:06d}.png", content_type="image/png")
        pcm = _obj(f"cue-{index:06d}.wav", content_type="audio/wav")
        record: dict[str, object] = {
            "schema_version": "1",
            "attempt": copy.deepcopy(attempt),
            "cue_id": f"cue-{index:06d}",
            "cue_index": index,
            "source_sha256": source["sha256"],
            "source_audio": {
                "sample_rate_hz": 44100,
                "sample_format": "f32le",
                "channels": 2,
                "channel_layout": "stereo",
                "start_sample": start_sample,
                "end_sample_exclusive": start_sample + 2000,
                "speech_start_sample": start_sample + 100,
                "speech_end_sample_inclusive": start_sample + 1799,
                "decode_filter_sha256": _sha("decode-filter"),
                "ffmpeg_image_digest": f"sha256:{_sha('ffmpeg')}",
                "pcm_slice": pcm,
            },
            "source_visual": {
                "start_frame": start_frame,
                "end_frame_inclusive": start_frame + 4,
                "start_pts": f"{start_frame}/30",
                "end_pts": f"{start_frame + 4}/30",
                "sample_frame": start_frame + 2,
                "sample_frame_sha256": _sha(f"frame-{index}"),
                "caption_bbox_normalized": {
                    "x": 0.1,
                    "y": 0.7,
                    "width": 0.8,
                    "height": 0.2,
                },
                "caption_baseline_y_normalized": 0.85,
                "crop_geometry_px": {
                    "x": 100,
                    "y": 1200,
                    "width": 880,
                    "height": 220,
                },
                "crop": crop,
            },
            "text_zh_raw": raw_text,
            "text_zh_nfc": unicodedata.normalize("NFC", raw_text),
            "extractor": _model("extractor", digested=True),
            "aligner": _model("aligner", digested=True),
            "extractor_confidence": 0.96,
            "aligner_confidence": 0.93,
            "source_classification": {
                "role": "dialogue_caption",
                "script": "cjk",
                "inside_supported_band": True,
                "classifier": _model("classifier", digested=True),
                "confidence": 0.99,
                "classification_sha256": _sha(f"classification-{index}"),
            },
            "extractor_prompt_sha256": _sha("source-cue-extractor-prompt"),
            "extractor_response_schema_sha256": _sha(
                "source-cue-extractor-schema"
            ),
            "aligner_prompt_sha256": _sha("source-cue-aligner-prompt"),
            "aligner_response_schema_sha256": _sha(
                "source-cue-aligner-schema"
            ),
            "created_at": NOW,
        }
        record_bytes = _json_bytes(record)
        record_ref = _object_for_bytes(f"cue-{index:06d}.json", record_bytes)
        cues.append(
            {
                "cue_id": f"cue-{index:06d}",
                "cue_index": index,
                "record": record_ref,
                "crop": crop,
                "pcm_slice": pcm,
            }
        )
        bundles.append(
            {
                "record_ref": record_ref,
                "record_bytes": record_bytes,
                "record": record,
            }
        )
    manifest: dict[str, object] = {
        "schema_version": "1",
        "attempt": attempt,
        "source": source,
        "cue_count": count,
        "cues": cues,
        "collection_root_algorithm": "RFC8785-JCS-SHA256-ORDERED",
        "collection_root_contract_sha256": _sha("placeholder-contract"),
        "collection_root": _sha("placeholder-root"),
        "extractor_policy_sha256": _sha("extractor-policy"),
        "aligner_policy_sha256": _sha("aligner-policy"),
        "created_at": NOW,
    }
    return manifest, bundles


def _translation_attempt(
    cue_index: int,
    candidate_index: int,
    *,
    source_record_sha: str,
) -> dict[str, object]:
    text_raw = (
        f"Ứng viên {candidate_index}"
        if candidate_index == 0
        else f"Phương án {candidate_index}"
    )
    return {
        "schema_version": "1",
        "attempt": _attempt(),
        "cue_id": f"cue-{cue_index:06d}",
        "cue_index": cue_index,
        "source_cue_record": _obj(
            f"source-cue-{cue_index}.json", digest=source_record_sha
        ),
        "source_cue_record_sha256": source_record_sha,
        "candidate_index": candidate_index,
        "reason": "initial" if candidate_index == 0 else "compact_unfit",
        "text_vi_raw": text_raw,
        "text_vi_nfc": unicodedata.normalize("NFC", text_raw),
        "generator": _model("translator"),
        "generator_prompt_sha256": _sha("translation-prompt"),
        "candidate_sha256": _sha(
            f"candidate-{cue_index}-{candidate_index}"
        ),
        "created_at": NOW,
    }


def _silence_detector(*, active: bool = True) -> dict[str, object]:
    return {
        "sample_rate_hz": 44100,
        "decoded_sample_count": 1000,
        "window_samples": 882,
        "hop_samples": 441,
        "tail_window_padding": "right_zero_pad",
        "rms_denominator_samples": 882,
        "active_threshold_dbfs": -45,
        "active_threshold_linear": 0.005623413251903491,
        "consecutive_windows": 2,
        "active_runs": (
            [
                {"onset_sample": 100, "offset_sample_inclusive": 499},
                {"onset_sample": 600, "offset_sample_inclusive": 999},
            ]
            if active
            else []
        ),
        "onset_sample": 100 if active else None,
        "offset_sample_inclusive": 999 if active else None,
        "implementation_sha256": _sha("silence-detector"),
    }


def _tts_succeeded(
    cue_index: int,
    candidate_sha: str,
    *,
    anchor: int,
    previous_offset: int | None,
) -> dict[str, object]:
    target = max(
        anchor,
        anchor if previous_offset is None else previous_offset + 2205,
    )
    placement = max(0, target - 100)
    actual_onset = placement + 100
    actual_offset = placement + 999
    gap = None if previous_offset is None else actual_onset - previous_offset
    overlap = (
        0
        if previous_offset is None
        else max(0, previous_offset - actual_onset + 1)
    )
    return {
        "schema_version": "1",
        "attempt": _attempt(),
        "cue_id": f"cue-{cue_index:06d}",
        "cue_index": cue_index,
        "candidate_index": 0,
        "translation_candidate_sha256": candidate_sha,
        "provider": "capcut-private",
        "voice": "BV075_streaming",
        "prosody_rate": "1.5000",
        "normalized_request_sha256": _sha(f"tts-request-{cue_index}"),
        "provider_response_id": f"tts-response-{cue_index}",
        "outcome": "succeeded",
        "returned_media": _obj(
            f"tts-{cue_index}.mp3", content_type="audio/mpeg"
        ),
        "decoded_pcm": _obj(
            f"tts-{cue_index}.wav", content_type="audio/wav"
        ),
        "silence_detector": _silence_detector(),
        "schedule": {
            "speech_anchor_sample": anchor,
            "previous_actual_offset_sample_inclusive": previous_offset,
            "target_actual_onset_sample": target,
            "video_sample_count": 20000,
            "placement_sample": placement,
            "actual_onset_sample": actual_onset,
            "actual_offset_sample_inclusive": actual_offset,
            "lag_samples": actual_onset - anchor,
            "previous_gap_samples": gap,
            "overlap_samples": overlap,
            "fits": True,
        },
        "created_at": NOW,
    }


def _tts_failure(outcome: str) -> dict[str, object]:
    error_codes = {
        "provider_failed": "TTS_PROVIDER_FAILED",
        "media_invalid": "TTS_MEDIA_INVALID",
        "no_active_speech": "TTS_MEDIA_INVALID",
    }
    value: dict[str, object] = {
        "schema_version": "1",
        "attempt": _attempt(),
        "cue_id": "cue-000000",
        "cue_index": 0,
        "candidate_index": 0,
        "translation_candidate_sha256": _sha("candidate-0-0"),
        "provider": "capcut-private",
        "voice": "BV075_streaming",
        "prosody_rate": "1.5000",
        "normalized_request_sha256": _sha("tts-failure-request"),
        "provider_response_id": None,
        "outcome": outcome,
        "error_code": error_codes[outcome],
        "created_at": NOW,
    }
    if outcome == "no_active_speech":
        value.update(
            {
                "returned_media": _obj(
                    "no-active.mp3", content_type="audio/mpeg"
                ),
                "decoded_pcm": _obj(
                    "no-active.wav", content_type="audio/wav"
                ),
                "silence_detector": _silence_detector(active=False),
            }
        )
    return value


def _tts_schedule_unfit() -> dict[str, object]:
    value = _tts_succeeded(
        0,
        _sha("candidate-0-0"),
        anchor=900,
        previous_offset=None,
    )
    value["outcome"] = "schedule_unfit"
    value["error_code"] = "DUB_CUE_UNFIT"
    schedule = value["schedule"]
    assert isinstance(schedule, dict)
    schedule.update(
        {
            "previous_actual_offset_sample_inclusive": 1000,
            "target_actual_onset_sample": 3205,
            "placement_sample": 0,
            "actual_onset_sample": 100,
            "actual_offset_sample_inclusive": 999,
            "lag_samples": -800,
            "previous_gap_samples": -900,
            "overlap_samples": 901,
            "fits": False,
        }
    )
    return value


def _bundle(
    document: dict[str, object],
    label: str,
    *,
    include_bytes: bool = False,
) -> dict[str, object]:
    if include_bytes:
        object_bytes = _json_bytes(document)
        return {
            "object_ref": _object_for_bytes(label, object_bytes),
            "object_bytes": object_bytes,
            "document": document,
        }
    return {"object_ref": _obj(label), "document": document}


def _cue_ledger_fixture() -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    manifest, _ = _source_collection()
    manifest_ref = _obj("source-cue-manifest.json")
    translation_bundles: list[dict[str, object]] = []
    tts_bundles: list[dict[str, object]] = []
    entries: list[dict[str, object]] = []
    previous_offset: int | None = None
    manifest_cues = manifest["cues"]
    assert isinstance(manifest_cues, list)
    for index, manifest_entry in enumerate(manifest_cues):
        source_record_sha = manifest_entry["record"]["sha256"]
        translation = _translation_attempt(
            index,
            0,
            source_record_sha=source_record_sha,
        )
        anchor = 1000 + index * 2000
        tts = _tts_succeeded(
            index,
            translation["candidate_sha256"],
            anchor=anchor,
            previous_offset=previous_offset,
        )
        translation_bundle = _bundle(
            translation,
            f"translation-{index}.json",
            include_bytes=True,
        )
        tts_bundle = _bundle(
            tts,
            f"tts-{index}.json",
            include_bytes=True,
        )
        translation_bundles.append(translation_bundle)
        tts_bundles.append(tts_bundle)
        schedule = tts["schedule"]
        detector = tts["silence_detector"]
        assert isinstance(schedule, dict)
        assert isinstance(detector, dict)
        ledger_schedule = {
            "speech_anchor_sample": schedule["speech_anchor_sample"],
            "previous_actual_offset_sample_inclusive": schedule[
                "previous_actual_offset_sample_inclusive"
            ],
            "target_actual_onset_sample": schedule[
                "target_actual_onset_sample"
            ],
            "video_sample_count": schedule["video_sample_count"],
            "placement_sample": schedule["placement_sample"],
            "detected_onset_sample": detector["onset_sample"],
            "detected_offset_sample_inclusive": detector[
                "offset_sample_inclusive"
            ],
            "actual_onset_sample": schedule["actual_onset_sample"],
            "actual_offset_sample_inclusive": schedule[
                "actual_offset_sample_inclusive"
            ],
            "lag_samples": schedule["lag_samples"],
            "previous_gap_samples": schedule["previous_gap_samples"],
            "overlap_samples": schedule["overlap_samples"],
        }
        entries.append(
            {
                "cue_id": f"cue-{index:06d}",
                "cue_index": index,
                "source_cue_record_sha256": source_record_sha,
                "candidate_index": 0,
                "translation_attempt": translation_bundle["object_ref"],
                "translation_candidate_sha256": translation[
                    "candidate_sha256"
                ],
                "translation_text_vi_nfc": translation["text_vi_nfc"],
                "tts_attempt": tts_bundle["object_ref"],
                "tts_pcm_sha256": tts["decoded_pcm"]["sha256"],
                "tts_schedule": ledger_schedule,
                "vietsub_event": {
                    "text_vi_nfc": translation["text_vi_nfc"],
                    "start_sample": schedule["actual_onset_sample"],
                    "end_sample_inclusive": schedule[
                        "actual_offset_sample_inclusive"
                    ],
                    "line_count": 1,
                    "render_event_sha256": _sha(f"render-event-{index}"),
                },
            }
        )
        previous_offset = schedule["actual_offset_sample_inclusive"]
    ledger: dict[str, object] = {
        "schema_version": "1",
        "attempt": _attempt(),
        "source_cue_manifest": manifest_ref,
        "cue_count": len(entries),
        "cues": entries,
        "translation_policy_sha256": _sha("translation-policy"),
        "tts_policy_sha256": _sha("tts-policy"),
        "vietsub_style_policy_sha256": _sha("style-policy"),
        "created_at": NOW,
    }
    return (
        ledger,
        manifest,
        manifest_ref,
        translation_bundles,
        tts_bundles,
    )


def _clean_binding() -> dict[str, str]:
    return {
        "source_sha256": _sha("source"),
        "clean_sha256": _sha("clean"),
        "source_cue_manifest_sha256": _sha("source-cue-manifest"),
        "source_cue_collection_root": _sha("source-cue-root"),
        "clean_policy_sha256": _sha("clean_qa_policy"),
    }


def _clean_machine_and_verdicts() -> tuple[
    dict[str, object], list[dict[str, object]]
]:
    binding = _clean_binding()
    report: dict[str, object] = {
        "schema_version": "1",
        "gate": "clean",
        "attempt": _attempt(),
        "policy": _policy("clean_qa_policy"),
        "bindings": copy.deepcopy(binding),
        "decision": "pass",
        "metrics": [
            {
                "name": name,
                "decision": "pass",
                "value": 0,
                "unit": "count",
                "denominator": 1,
                "evidence": [_obj(f"clean-{name}.json")],
            }
            for name in sorted(REQUIRED_MACHINE_METRIC_NAMES["clean"])
        ],
        "implementation": {
            "image_digest": f"sha256:{_sha('clean-image')}",
            "code_sha256": _sha("clean-code"),
            "metric_decode_sha256": _sha("clean-decode"),
        },
        "started_at": NOW,
        "finished_at": NOW,
    }
    verdicts: list[dict[str, object]] = []
    for role in ("clean_a", "clean_b", "clean_c", "clean_final_sol"):
        verifier_binding = {
            "source_sha256": binding["source_sha256"],
            "clean_sha256": binding["clean_sha256"],
            "source_cue_manifest_sha256": binding[
                "source_cue_manifest_sha256"
            ],
            "source_cue_collection_root": binding[
                "source_cue_collection_root"
            ],
            "policy_sha256": binding["clean_policy_sha256"],
        }
        verdicts.append(
            {
                "schema_version": "1",
                "role": role,
                "attempt": _attempt(),
                "policy": _policy("clean_qa_policy"),
                "model": _model(role),
                "bindings": verifier_binding,
                "decision": "pass",
                "findings": [],
                "created_at": NOW,
            }
        )
    return report, verdicts


def _embedded_verdict(role: str, bindings: dict[str, str]) -> dict[str, object]:
    return {
        "role": role,
        "object": _obj(f"{role}.json"),
        "model": _model(role),
        "bindings": copy.deepcopy(bindings),
    }


def _clean_approval_and_index() -> tuple[
    dict[str, object], dict[str, object]
]:
    binding = _clean_binding()
    source = _obj("source-binding.mp4", content_type="video/mp4")
    source["sha256"] = binding["source_sha256"]
    clean = _obj("clean.mp4", content_type="video/mp4")
    clean["sha256"] = binding["clean_sha256"]
    manifest = _obj("source-cue-manifest.json")
    manifest["sha256"] = binding["source_cue_manifest_sha256"]
    clean_verdicts = [
        _embedded_verdict(role, binding)
        for role in ("clean_a", "clean_b", "clean_c", "clean_final_sol")
    ]
    payload: dict[str, object] = {
        "schema_version": "1",
        "clean_approval_id": "cap-0123456789abcdef0123456789abcdef",
        "issued_at": NOW,
        "expires_at": LATER,
        "attempt": _attempt(),
        "source": source,
        "clean": clean,
        "clean_manifest": _obj("clean-manifest.json"),
        "clean_contract_sha256": _sha("clean-contract"),
        "source_cue_manifest": manifest,
        "source_cue_collection_root": binding[
            "source_cue_collection_root"
        ],
        "extractor_aligner_policy_sha256": _sha("extractor-aligner-policy"),
        "clean_policy": _policy("clean_qa_policy"),
        "clean_machine_report": _obj("clean-machine-report.json"),
        "clean_verdicts": clean_verdicts,
        "controller_signer_principal": (
            "serviceAccount:controller@desub.iam.gserviceaccount.com"
        ),
    }
    approval = {"payload": payload, "signature": _signature()}
    index: dict[str, object] = {
        "schema_version": "1",
        "attempt": _attempt(),
        "clean_approval_id": payload["clean_approval_id"],
        "clean_approval": _obj("clean-approval.json"),
        "source_sha256": binding["source_sha256"],
        "clean_sha256": binding["clean_sha256"],
        "source_cue_manifest_sha256": binding[
            "source_cue_manifest_sha256"
        ],
        "source_cue_collection_root": binding[
            "source_cue_collection_root"
        ],
        "clean_policy_sha256": binding["clean_policy_sha256"],
        "state_version": 7,
        "activated_at": NOW,
    }
    return approval, index


def _release_approval_and_index(
    clean_approval: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    clean_payload = clean_approval["payload"]
    assert isinstance(clean_payload, dict)
    clean = copy.deepcopy(clean_payload["clean"])
    source = copy.deepcopy(clean_payload["source"])
    manifest = copy.deepcopy(clean_payload["source_cue_manifest"])
    ledger = _obj("cue-ledger.json")
    dubbed = _obj("dubbed.mp4", content_type="video/mp4")
    release_bindings = {
        "source_sha256": source["sha256"],
        "clean_sha256": clean["sha256"],
        "source_cue_manifest_sha256": manifest["sha256"],
        "source_cue_collection_root": clean_payload[
            "source_cue_collection_root"
        ],
        "clean_policy_sha256": clean_payload["clean_policy"]["sha256"],
        "cue_ledger_sha256": ledger["sha256"],
        "dubbed_sha256": dubbed["sha256"],
        "dub_policy_sha256": _sha("dub_qa_policy"),
    }
    final_verdicts = [
        _embedded_verdict(role, release_bindings)
        for role in (
            "translation_semantics",
            "translation_style",
            "dub_audio_video",
            "dub_final_sol",
        )
    ]
    payload: dict[str, object] = {
        "schema_version": "1",
        "release_approval_id": "rap-0123456789abcdef0123456789abcdef",
        "issued_at": NOW,
        "expires_at": LATER,
        "attempt": _attempt(),
        "clean_approval_id": clean_payload["clean_approval_id"],
        "clean_approval": _obj("clean-approval.json"),
        "source": source,
        "clean": clean,
        "clean_manifest": copy.deepcopy(clean_payload["clean_manifest"]),
        "source_cue_manifest": manifest,
        "source_cue_collection_root": clean_payload[
            "source_cue_collection_root"
        ],
        "translation_attempts": [_obj("translation-0.json")],
        "translation_preflight_verdicts": [_obj("preflight-0.json")],
        "tts_attempts": [_obj("tts-0.json")],
        "cue_ledger": ledger,
        "dubbed": dubbed,
        "dub_manifest": _obj("dub-manifest.json"),
        "no_vietsub_control": _obj(
            "no-vietsub.mp4", content_type="video/mp4"
        ),
        "vietsub_alpha_mask": _obj(
            "vietsub-mask.png", content_type="image/png"
        ),
        "mix_report": _obj("mix-report.json"),
        "stem_pcm": {
            name: _obj(f"stem-{name}.wav", content_type="audio/wav")
            for name in (
                "clean_input",
                "bed",
                "ducked_bed",
                "voice",
                "premaster",
            )
        },
        "clean_contract_sha256": clean_payload["clean_contract_sha256"],
        "dub_contract_sha256": _sha("dub-contract"),
        "render_policy_sha256": _sha("render-policy"),
        "style_policy_sha256": _sha("style-policy"),
        "mix_policy_sha256": _sha("mix-policy"),
        "clean_policy": copy.deepcopy(clean_payload["clean_policy"]),
        "dub_policy": _policy("dub_qa_policy"),
        "clean_machine_report": copy.deepcopy(
            clean_payload["clean_machine_report"]
        ),
        "dub_machine_report": _obj("dub-machine-report.json"),
        "final_verdicts": final_verdicts,
        "controller_signer_principal": (
            "serviceAccount:controller@desub.iam.gserviceaccount.com"
        ),
    }
    approval = {"payload": payload, "signature": _signature()}
    index: dict[str, object] = {
        "schema_version": "1",
        "attempt": _attempt(),
        "release_approval_id": payload["release_approval_id"],
        "release_approval": _obj("release-approval.json"),
        "clean_approval_id": payload["clean_approval_id"],
        "clean_sha256": clean["sha256"],
        "source_cue_manifest_sha256": manifest["sha256"],
        "cue_ledger_sha256": ledger["sha256"],
        "dubbed_sha256": dubbed["sha256"],
        "clean_policy_sha256": clean_payload["clean_policy"]["sha256"],
        "dub_policy_sha256": payload["dub_policy"]["sha256"],
        "state_version": 19,
        "activated_at": NOW,
    }
    return approval, index


class FrozenEnumsAndTransitionsTests(unittest.TestCase):
    def test_frozen_sets_and_translation_roles_match_v1(self) -> None:
        self.assertEqual(MAX_CLEAN_REPAIR_ATTEMPTS, 1)
        self.assertEqual(MAX_COMPACT_RETRIES, 2)
        self.assertIn("clean_approved", STATE_SET)
        self.assertIn("repairing_clean", STATE_SET)
        self.assertIn("DUB_CUE_UNFIT", ERROR_CODE_SET)
        self.assertIn("REQUEST_CANCELLED", ERROR_CODE_SET)
        self.assertIn("source_cue_extraction", ERROR_STAGE_SET)
        self.assertEqual(
            CLEAN_VERDICT_ROLES,
            {"clean_a", "clean_b", "clean_c", "clean_final_sol"},
        )
        self.assertEqual(
            TRANSLATION_PREFLIGHT_ROLES,
            {
                "translation_semantic_preflight",
                "translation_style_preflight",
            },
        )

    def test_transition_graph_is_closed_and_gates_cannot_be_skipped(self) -> None:
        assert_closed_graph()
        self.assertEqual(set(TRANSITIONS), STATE_SET)
        for state in TERMINAL_STATES:
            self.assertEqual(TRANSITIONS[state], frozenset())
        with self.assertRaises(InvalidTransition):
            validate_transition("encoding_clean", "aligning_speech")
        with self.assertRaises(InvalidTransition):
            validate_transition("verifying_dub_agents", "completed")
        validate_transition("verifying_clean_agents", "clean_approved")
        validate_transition("signing_release", "completed")

    def test_exactly_one_clean_repair_loop_is_available(self) -> None:
        for verifier_state in (
            "verifying_clean_machine",
            "verifying_clean_agents",
        ):
            validate_transition(
                verifier_state,
                "repairing_clean",
                clean_repair_attempts_used=0,
            )
            for invalid in (None, True, -1, 1, 2):
                with self.subTest(verifier_state=verifier_state, invalid=invalid):
                    with self.assertRaises(InvalidTransition):
                        validate_transition(
                            verifier_state,
                            "repairing_clean",
                            clean_repair_attempts_used=invalid,
                        )
        validate_transition("repairing_clean", "detecting")
        with self.assertRaises(InvalidTransition):
            validate_transition("repairing_clean", "clean_approved")

    def test_compact_retry_loop_is_bounded(self) -> None:
        for used in (0, 1):
            validate_transition(
                "scheduling_tts",
                "compacting_translation",
                compact_retries_used=used,
            )
        for invalid in (None, True, -1, 2, 3):
            with self.subTest(invalid=invalid):
                with self.assertRaises(InvalidTransition):
                    validate_transition(
                        "scheduling_tts",
                        "compacting_translation",
                        compact_retries_used=invalid,
                    )


class SafeErrorAndStatusTests(unittest.TestCase):
    def _safe_error(self) -> dict[str, object]:
        return {
            "code": "DUB_CUE_UNFIT",
            "stage": "scheduling",
            "message": "Không thể xếp lịch một câu lồng tiếng.",
            "retryable": False,
            "context": {"cue_index": 12, "limit_name": "tts_schedule"},
        }

    def test_safe_error_and_status_invariants(self) -> None:
        validate_safe_error(self._safe_error())
        failed = {
            "state": "scheduling_failed",
            "state_version": 12,
            "error": self._safe_error(),
        }
        validate_status_invariants(failed)
        completed = {
            "schema_version": "1",
            "job_id": _attempt()["job_id"],
            "attempt_id": _attempt()["attempt_id"],
            "attempt_seq": 1,
            "state": "completed",
            "state_version": 21,
            "launch_state": "launched",
            "cached": False,
            "callback_expected": True,
            "updated_at": NOW,
            "clean_approval_id": (
                "cap-0123456789abcdef0123456789abcdef"
            ),
            "release_approval_id": (
                "rap-0123456789abcdef0123456789abcdef"
            ),
            "clean_sha256": _sha("clean"),
            "source_cue_manifest_sha256": _sha("source-cue-manifest"),
            "cue_ledger_sha256": _sha("cue-ledger"),
            "artifact": {
                "kind": "dubbed",
                "ready": True,
                "sha256": _sha("dubbed"),
                "download_url": "https://download.example/object",
                "expires_at": LATER,
                "size_bytes": 1024,
            },
            "error": None,
        }
        validate_status_invariants(completed)

    def test_safe_error_rejects_unknown_or_sensitive_values(self) -> None:
        for field, value in (
            ("code", "NEW_UNREVIEWED_CODE"),
            ("stage", "unknown_stage"),
            ("message", "See gs://private-bucket/object"),
        ):
            error = self._safe_error()
            error[field] = value
            with self.subTest(field=field):
                with self.assertRaises(ContractValidationError):
                    validate_safe_error(error)
        missing_context = self._safe_error()
        del missing_context["context"]
        with self.assertRaises(ContractValidationError):
            validate_safe_error(missing_context)
        legacy_context = self._safe_error()
        legacy_context["context"] = {"overflow_samples": 2205}
        with self.assertRaises(ContractValidationError):
            validate_safe_error(legacy_context)
        for invalid_context in (
            {"cue_index": -1},
            {"cue_index": True},
            {"limit_name": "x" * 65},
            {"retry_after_seconds": 0},
            {"retry_after_seconds": 86401},
        ):
            error = self._safe_error()
            error["context"] = invalid_context
            with self.subTest(context=invalid_context):
                with self.assertRaises(ContractValidationError):
                    validate_safe_error(error)
        queued = {
            "state": "queued",
            "state_version": 1,
            "artifact": {
                "kind": "dubbed",
                "ready": False,
                "sha256": _sha("dubbed"),
                "download_url": "https://download.example/object",
            },
        }
        with self.assertRaises(ContractValidationError):
            validate_status_invariants(queued)

    def test_error_semantics_reject_nonsensical_pairs_and_wrong_http(self) -> None:
        invalid_url = {
            "code": "INVALID_DOUYIN_URL",
            "stage": "intake",
            "message": "Douyin URL is invalid.",
            "retryable": False,
            "context": {},
        }
        validate_safe_error(invalid_url, http_status=400)
        wrong_stage = copy.deepcopy(invalid_url)
        wrong_stage["stage"] = "release_signing"
        with self.assertRaises(ContractValidationError):
            validate_safe_error(wrong_stage)
        wrong_retry = copy.deepcopy(invalid_url)
        wrong_retry["retryable"] = True
        with self.assertRaises(ContractValidationError):
            validate_safe_error(wrong_retry)
        with self.assertRaises(ContractValidationError):
            validate_safe_error(invalid_url, http_status=422)
        cancelled = {
            "code": "REQUEST_CANCELLED",
            "stage": "cancellation",
            "message": "Attempt was cancelled.",
            "retryable": False,
            "context": {},
        }
        validate_safe_error(
            cancelled,
            http_status=200,
            terminal_state="cancelled",
        )
        with self.assertRaises(ContractValidationError):
            validate_safe_error(cancelled, terminal_state="failed")


class TaxonomyCallbackAndAggregateTests(unittest.TestCase):
    def _error(self, state: str) -> dict[str, object]:
        by_state: dict[str, dict[str, object]] = {
            "clean_qa_failed": {
                "code": "CLEAN_QA_FAILED",
                "stage": "clean_agent_qa",
                "message": "Clean verification did not pass.",
                "retryable": False,
                "context": {},
            },
            "translation_failed": {
                "code": "TRANSLATION_PREFLIGHT_FAILED",
                "stage": "translation_preflight",
                "message": "Translation preflight did not pass.",
                "retryable": False,
                "context": {"cue_index": 0},
            },
            "tts_failed": {
                "code": "TTS_MEDIA_INVALID",
                "stage": "tts",
                "message": "TTS media is invalid.",
                "retryable": False,
                "context": {"cue_index": 0},
            },
            "scheduling_failed": {
                "code": "DUB_CUE_UNFIT",
                "stage": "scheduling",
                "message": "A dub cue cannot fit its schedule.",
                "retryable": False,
                "context": {"cue_index": 0, "limit_name": "tts_schedule"},
            },
            "subtitle_failed": {
                "code": "DUB_SUBTITLE_UNFIT",
                "stage": "vietsub",
                "message": "A subtitle cue cannot fit.",
                "retryable": False,
                "context": {"cue_index": 0, "limit_name": "subtitle_geometry"},
            },
            "dub_qa_failed": {
                "code": "DUB_QA_FAILED",
                "stage": "dub_agent_qa",
                "message": "Dub verification did not pass.",
                "retryable": False,
                "context": {},
            },
            "failed": {
                "code": "SOURCE_REDIRECT_BLOCKED",
                "stage": "download",
                "message": "Source redirect was blocked.",
                "retryable": False,
                "context": {},
            },
            "cancelled": {
                "code": "REQUEST_CANCELLED",
                "stage": "cancellation",
                "message": "Attempt was cancelled.",
                "retryable": False,
                "context": {},
            },
        }
        return copy.deepcopy(by_state[state])

    def _callback(self, state: str) -> dict[str, object]:
        callback: dict[str, object] = {
            "schema_version": "1",
            "event": f"desub.{state}",
            "event_id": "evt-0123456789abcdef0123456789abcdef",
            "job_id": _attempt()["job_id"],
            "attempt_id": _attempt()["attempt_id"],
            "attempt_seq": 1,
            "state": state,
            "state_version": 21,
            "verification_passed": state == "completed",
            "occurred_at": NOW,
        }
        if state == "completed":
            callback.update(
                {
                    "clean_approval_id": (
                        "cap-0123456789abcdef0123456789abcdef"
                    ),
                    "release_approval_id": (
                        "rap-0123456789abcdef0123456789abcdef"
                    ),
                    "source_sha256": _sha("source"),
                    "clean_sha256": _sha("clean"),
                    "source_cue_manifest_sha256": _sha(
                        "source-cue-manifest"
                    ),
                    "cue_ledger_sha256": _sha("cue-ledger"),
                    "dubbed_sha256": _sha("dubbed"),
                }
            )
        else:
            callback["error"] = self._error(state)
        return callback

    def _completed_status(self) -> dict[str, object]:
        return {
            "schema_version": "1",
            "job_id": _attempt()["job_id"],
            "attempt_id": _attempt()["attempt_id"],
            "attempt_seq": 1,
            "state": "completed",
            "state_version": 21,
            "launch_state": "launched",
            "cached": False,
            "callback_expected": True,
            "updated_at": NOW,
            "clean_approval_id": (
                "cap-0123456789abcdef0123456789abcdef"
            ),
            "release_approval_id": (
                "rap-0123456789abcdef0123456789abcdef"
            ),
            "clean_sha256": _sha("clean"),
            "source_cue_manifest_sha256": _sha("source-cue-manifest"),
            "cue_ledger_sha256": _sha("cue-ledger"),
            "artifact": {
                "kind": "dubbed",
                "ready": True,
                "download_url": "https://download.example/dubbed",
                "expires_at": LATER,
                "size_bytes": 1234,
                "sha256": _sha("dubbed"),
            },
        }

    def _machine_report(
        self,
        gate: str,
        *,
        top_decision: str = "pass",
        metric_decision: str = "pass",
    ) -> dict[str, object]:
        metrics = [
            {
                "name": name,
                "decision": metric_decision,
                "value": 0,
                "unit": "count",
                "denominator": 1,
                "evidence": [],
            }
            for name in sorted(REQUIRED_MACHINE_METRIC_NAMES[gate])
        ]
        return {
            "schema_version": "1",
            "gate": gate,
            "attempt": _attempt(),
            "policy": _policy(f"{gate}_qa_policy"),
            "bindings": {
                (
                    "clean_policy_sha256"
                    if gate == "clean"
                    else "dub_policy_sha256"
                ): _sha(f"{gate}_qa_policy")
            },
            "decision": top_decision,
            "metrics": metrics,
            "implementation": {},
            "started_at": NOW,
            "finished_at": NOW,
        }

    def test_materialized_taxonomy_exactly_matches_python_enums(self) -> None:
        taxonomy_path = (
            Path(__file__).parents[1]
            / "integrated_desub_contracts"
            / "schemas"
            / "v1"
            / "taxonomy.schema.json"
        )
        taxonomy = json.loads(taxonomy_path.read_text(encoding="utf-8"))
        validate_taxonomy_document(taxonomy)
        stale = copy.deepcopy(taxonomy)
        roles = stale["$defs"]["verifierRole"]["enum"]
        roles[4] = "translation_semantics_preflight"
        with self.assertRaises(ContractValidationError):
            validate_taxonomy_document(stale)

    def test_error_context_allowlist_matches_job_status_schema(self) -> None:
        schema_path = (
            Path(__file__).parents[1]
            / "integrated_desub_contracts"
            / "schemas"
            / "v1"
            / "job_status.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        context_schema = schema["properties"]["error"]["properties"]["context"]
        self.assertFalse(context_schema["additionalProperties"])
        self.assertEqual(
            set(context_schema["properties"]),
            set(SAFE_ERROR_CONTEXT_KEYS),
        )

    def test_every_terminal_callback_has_one_exact_event_mapping(self) -> None:
        for state in TERMINAL_STATES:
            callback = self._callback(state)
            validate_callback_event(callback)
            callback["event"] = (
                "desub.cancelled"
                if state != "cancelled"
                else "desub.failed"
            )
            with self.subTest(state=state):
                with self.assertRaises(ContractValidationError):
                    validate_callback_event(callback)
        wrong_family = self._callback("clean_qa_failed")
        wrong_family["error"] = self._error("dub_qa_failed")
        with self.assertRaises(ContractValidationError):
            validate_callback_event(wrong_family)

    def test_completed_status_callback_hashes_and_download_ttl_cross_match(
        self,
    ) -> None:
        status = self._completed_status()
        callback = self._callback("completed")
        validate_status_callback_consistency(status, callback)
        for field in (
            "clean_approval_id",
            "release_approval_id",
            "clean_sha256",
            "source_cue_manifest_sha256",
            "cue_ledger_sha256",
        ):
            stale = copy.deepcopy(callback)
            stale[field] = (
                "cap-ffffffffffffffffffffffffffffffff"
                if field == "clean_approval_id"
                else (
                    "rap-ffffffffffffffffffffffffffffffff"
                    if field == "release_approval_id"
                    else _sha(f"stale-{field}")
                )
            )
            with self.subTest(field=field):
                with self.assertRaises(ContractValidationError):
                    validate_status_callback_consistency(status, stale)
        stale = copy.deepcopy(callback)
        stale["dubbed_sha256"] = _sha("other-dubbed")
        with self.assertRaises(ContractValidationError):
            validate_status_callback_consistency(status, stale)
        stale_status = copy.deepcopy(status)
        stale_status["artifact"]["expires_at"] = "2026-07-24T00:00:01Z"
        with self.assertRaises(ContractValidationError):
            validate_status_callback_consistency(stale_status, callback)

    def test_machine_report_exact_metric_set_and_aggregate_are_fail_closed(
        self,
    ) -> None:
        for gate in ("clean", "dub"):
            validate_machine_report_aggregate(self._machine_report(gate))
            top_lies = self._machine_report(
                gate,
                top_decision="pass",
                metric_decision="review",
            )
            with self.subTest(gate=gate, case="review"):
                with self.assertRaises(ContractValidationError):
                    validate_machine_report_aggregate(top_lies)
            top_lies["metrics"][0]["decision"] = "fail"
            with self.subTest(gate=gate, case="fail"):
                with self.assertRaises(ContractValidationError):
                    validate_machine_report_aggregate(top_lies)
            missing = self._machine_report(gate)
            missing["metrics"].pop()
            with self.subTest(gate=gate, case="missing"):
                with self.assertRaises(ContractValidationError):
                    validate_machine_report_aggregate(missing)
            false_failure = self._machine_report(
                gate,
                top_decision="fail",
            )
            with self.subTest(gate=gate, case="all-pass"):
                with self.assertRaises(ContractValidationError):
                    validate_machine_report_aggregate(false_failure)


class IdempotencyRecordTests(unittest.TestCase):
    def _fixture(self) -> tuple[dict[str, object], bytes]:
        projection = {
            "schema_version": "1",
            "douyin_url": "https://www.douyin.com/video/123456789",
            "force_refresh": False,
            "client_context": {"request_id": "telegram-100000001-1"},
        }
        canonical_bytes = _json_bytes(projection)
        record: dict[str, object] = {
            "schema_version": "1",
            "record_version": 1,
            "idempotency_key_sha256": _sha("idempotency-key"),
            "caller_scope_sha256": _sha("telegram-private-chat"),
            "request_bytes_contract": "stored_exact_utf8_json_v1",
            "canonical_request_projection": projection,
            "canonical_request_sha256": _sha(canonical_bytes),
            "bound_attempt": _attempt(),
            "replay_binding": {
                "initial_response_body_sha256": _sha("initial-response"),
                "exact_attempt_status_path": (
                    "/v1/desub/jobs/"
                    "desub-0123456789abcdef0123456789abcdef/attempts/1"
                ),
                "mint_fresh_download_url_only_after_release_revalidation": True,
            },
            "retention_seconds": 604800,
            "conflict_observations": [
                {
                    "conflicting_request_sha256": _sha(
                        "different-canonical-request"
                    ),
                    "error_code": "IDEMPOTENCY_CONFLICT",
                    "http_status": 409,
                    "observed_at": NOW,
                }
            ],
            "created_at": NOW,
            "expires_at": "2026-07-30T00:00:00Z",
        }
        return record, canonical_bytes

    def test_request_sha_attempt_path_and_exact_seven_day_expiry(self) -> None:
        record, canonical_bytes = self._fixture()
        validate_idempotency_record(record, canonical_bytes)

    def test_idempotency_tamper_and_same_hash_conflict_fail_closed(self) -> None:
        mutators = [
            lambda record, canonical: record.update(
                {"request_bytes_contract": "jcs_rfc8785"}
            ),
            lambda record, canonical: record.update(
                {"canonical_request_sha256": _sha("wrong-request")}
            ),
            lambda record, canonical: record["replay_binding"].update(
                {"exact_attempt_status_path": "/v1/desub/jobs/latest"}
            ),
            lambda record, canonical: record.update(
                {"expires_at": "2026-07-30T00:00:01Z"}
            ),
            lambda record, canonical: record["conflict_observations"][0].update(
                {"conflicting_request_sha256": _sha(canonical)}
            ),
        ]
        for mutate in mutators:
            record, canonical_bytes = self._fixture()
            mutate(record, canonical_bytes)
            with self.subTest(mutate=mutate):
                with self.assertRaises(ContractValidationError):
                    validate_idempotency_record(record, canonical_bytes)
        record, canonical_bytes = self._fixture()
        with self.assertRaises(ContractValidationError):
            validate_idempotency_record(record, canonical_bytes + b"\n")


class SourceCueCollectionTests(unittest.TestCase):
    def test_collection_accepts_exact_bytes_refs_ranges_and_nfc(self) -> None:
        manifest, bundles = _source_collection()
        validate_source_cue_collection(manifest, bundles)

    def test_collection_rejects_count_order_id_attempt_source_and_refs(self) -> None:
        mutators = [
            lambda manifest, bundles: manifest.update({"cue_count": 1}),
            lambda manifest, bundles: manifest["cues"][1].update(
                {"cue_index": 0}
            ),
            lambda manifest, bundles: manifest["cues"][0].update(
                {"cue_id": "cue-0"}
            ),
            lambda manifest, bundles: manifest["attempt"].update(
                {"fence_digest": _sha("stale-fence")}
            ),
            lambda manifest, bundles: manifest["source"].update(
                {"sha256": _sha("different-source")}
            ),
            lambda manifest, bundles: manifest["cues"][0].update(
                {"crop": _obj("other-crop.png", content_type="image/png")}
            ),
            lambda manifest, bundles: manifest["cues"][0].update(
                {"pcm_slice": _obj("other.wav", content_type="audio/wav")}
            ),
        ]
        for mutate in mutators:
            manifest, bundles = _source_collection()
            mutate(manifest, bundles)
            with self.subTest(mutate=mutate):
                with self.assertRaises(ContractValidationError):
                    validate_source_cue_collection(manifest, bundles)

    def test_collection_rejects_byte_tamper_nfc_and_separate_confidences(self) -> None:
        manifest, bundles = _source_collection()
        bundles[0]["record_bytes"] += b" "
        with self.assertRaises(ContractValidationError):
            validate_source_cue_collection(manifest, bundles)

        for path in (
            "extractor_confidence",
            "aligner_confidence",
            "classification_confidence",
        ):
            manifest, bundles = _source_collection()
            record = bundles[0]["record"]
            if path == "classification_confidence":
                record["source_classification"]["confidence"] = 1.01
            else:
                record[path] = -0.01
            _repack_record(manifest, bundles, 0)
            with self.subTest(path=path):
                with self.assertRaises(ContractValidationError):
                    validate_source_cue_collection(manifest, bundles)

        manifest, bundles = _source_collection()
        record = bundles[1]["record"]
        record["text_zh_nfc"] = record["text_zh_raw"]
        _repack_record(manifest, bundles, 1)
        with self.assertRaises(ContractValidationError):
            validate_source_cue_collection(manifest, bundles)

    def test_collection_rejects_nested_off_by_one_and_overlap(self) -> None:
        mutations = [
            lambda record: record["source_audio"].update(
                {"sample_rate_hz": 48000}
            ),
            lambda record: record["source_audio"].update(
                {
                    "speech_end_sample_inclusive": record["source_audio"][
                        "end_sample_exclusive"
                    ]
                }
            ),
            lambda record: record["source_visual"].update(
                {"sample_frame": record["source_visual"]["end_frame_inclusive"] + 1}
            ),
            lambda record: record["source_visual"].update(
                {"end_pts": "-1/30"}
            ),
            lambda record: record["source_visual"][
                "caption_bbox_normalized"
            ].update({"width": 0.95}),
            lambda record: record["source_audio"].update(
                {"start_sample": 1999}
            ),
        ]
        for mutate in mutations:
            manifest, bundles = _source_collection()
            record = bundles[1]["record"]
            mutate(record)
            _repack_record(manifest, bundles, 1)
            with self.subTest(mutate=mutate):
                with self.assertRaises(ContractValidationError):
                    validate_source_cue_collection(manifest, bundles)


class SourceCueRootTests(unittest.TestCase):
    def _fixture(
        self,
    ) -> tuple[dict[str, object], bytes, bytes]:
        manifest, _ = _source_collection()
        contract_bytes = b'{"contract":"source-cue-collection-v1"}'
        manifest["collection_root_contract_sha256"] = _sha(contract_bytes)
        projection = build_source_cue_collection_root_preimage(manifest)
        external_bytes = _json_bytes(projection)
        manifest["collection_root"] = _sha(external_bytes)
        return manifest, external_bytes, contract_bytes

    def test_external_canonical_bytes_root_contract_and_golden_match(self) -> None:
        manifest, external_bytes, contract_bytes = self._fixture()
        projection = validate_source_cue_collection_root(
            manifest,
            external_bytes,
            root_contract_bytes=contract_bytes,
            golden_bytes=external_bytes,
        )
        self.assertEqual(
            projection["algorithm"],
            "RFC8785-JCS-SHA256-ORDERED-v1",
        )

    def test_root_rejects_projection_contract_root_and_golden_tamper(self) -> None:
        manifest, external_bytes, contract_bytes = self._fixture()
        cases = [
            {
                "external": external_bytes.replace(
                    b'"cue_count":2', b'"cue_count":1'
                ),
                "contract": contract_bytes,
                "golden": None,
            },
            {
                "external": external_bytes,
                "contract": contract_bytes + b"\n",
                "golden": None,
            },
            {
                "external": external_bytes,
                "contract": contract_bytes,
                "golden": external_bytes + b"\n",
            },
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ContractValidationError):
                    validate_source_cue_collection_root(
                        manifest,
                        case["external"],
                        root_contract_bytes=case["contract"],
                        golden_bytes=case["golden"],
                    )


class TranslationAndTtsTests(unittest.TestCase):
    def test_translation_attempts_use_schema_ids_fields_and_bounded_candidates(
        self,
    ) -> None:
        attempts = [
            _translation_attempt(
                0,
                index,
                source_record_sha=_sha("source-record"),
            )
            for index in range(3)
        ]
        validate_translation_attempts(attempts)
        missing = [attempts[0], attempts[2]]
        with self.assertRaises(ContractValidationError):
            validate_translation_attempts(missing)
        stale = copy.deepcopy(attempts)
        stale[1]["source_cue_record_sha256"] = _sha("stale-record")
        with self.assertRaises(ContractValidationError):
            validate_translation_attempts(stale)
        bad_id = copy.deepcopy(attempts)
        bad_id[0]["cue_id"] = "cue-0"
        with self.assertRaises(ContractValidationError):
            validate_translation_attempts(bad_id)

    def test_successful_tts_recomputes_all_inclusive_timing_equations(self) -> None:
        first = _tts_succeeded(
            0,
            _sha("candidate-0-0"),
            anchor=1000,
            previous_offset=None,
        )
        first_result = validate_tts_timing_evidence(first, selected=True)
        second = _tts_succeeded(
            1,
            _sha("candidate-1-0"),
            anchor=3000,
            previous_offset=first_result["actual_offset_sample_inclusive"],
        )
        result = validate_tts_timing_evidence(
            second,
            previous_actual_offset_inclusive=first_result[
                "actual_offset_sample_inclusive"
            ],
            selected=True,
        )
        self.assertEqual(result["previous_gap_samples"], 2205)
        self.assertEqual(result["overlap_samples"], 0)

    def test_tts_rejects_active_run_and_schedule_off_by_one_tamper(self) -> None:
        base = _tts_succeeded(
            0,
            _sha("candidate-0-0"),
            anchor=1000,
            previous_offset=None,
        )
        mutations = [
            lambda value: value["silence_detector"].update(
                {"offset_sample_inclusive": 998}
            ),
            lambda value: value["schedule"].update(
                {"actual_offset_sample_inclusive": 1898}
            ),
            lambda value: value["schedule"].update({"lag_samples": 1}),
            lambda value: value["schedule"].update({"overlap_samples": 1}),
            lambda value: value["schedule"].update(
                {"previous_actual_offset_sample_inclusive": 0}
            ),
            lambda value: value["schedule"].update(
                {"target_actual_onset_sample": 1001}
            ),
            lambda value: value["schedule"].update(
                {"video_sample_count": 1899, "fits": True}
            ),
        ]
        for mutate in mutations:
            value = copy.deepcopy(base)
            mutate(value)
            with self.subTest(mutate=mutate):
                with self.assertRaises(ContractValidationError):
                    validate_tts_timing_evidence(value, selected=True)

    def test_tts_preserves_failure_evidence_and_raw_negative_lag_gap(self) -> None:
        for outcome in ("provider_failed", "media_invalid", "no_active_speech"):
            value = _tts_failure(outcome)
            result = validate_tts_timing_evidence(value)
            self.assertFalse(result["selectable"])
            with self.assertRaises(ContractValidationError):
                validate_tts_timing_evidence(value, selected=True)

        unfit = _tts_schedule_unfit()
        result = validate_tts_timing_evidence(
            unfit,
            previous_actual_offset_inclusive=1000,
        )
        self.assertEqual(result["lag_samples"], -800)
        self.assertEqual(result["previous_gap_samples"], -900)
        self.assertEqual(result["overlap_samples"], 901)
        self.assertFalse(result["selectable"])
        with self.assertRaises(ContractValidationError):
            validate_tts_timing_evidence(
                unfit,
                previous_actual_offset_inclusive=1000,
                selected=True,
            )


class CueLedgerTests(unittest.TestCase):
    def test_ledger_cross_matches_selected_objects_pcm_text_and_timing(self) -> None:
        validate_cue_ledger_structure(*_cue_ledger_fixture())

    def test_ledger_rejects_n_to_n_object_candidate_pcm_text_and_time_tamper(
        self,
    ) -> None:
        mutations = [
            lambda ledger: ledger.update({"cue_count": 1}),
            lambda ledger: ledger["cues"][0].update(
                {"translation_attempt": _obj("missing-translation.json")}
            ),
            lambda ledger: ledger["cues"][0].update(
                {"translation_candidate_sha256": _sha("other-candidate")}
            ),
            lambda ledger: ledger["cues"][0].update(
                {"candidate_index": 1}
            ),
            lambda ledger: ledger["cues"][0].update(
                {"tts_pcm_sha256": _sha("other-pcm")}
            ),
            lambda ledger: ledger["cues"][0].update(
                {"translation_text_vi_nfc": "Sai"}
            ),
            lambda ledger: ledger["cues"][0]["vietsub_event"].update(
                {"end_sample_inclusive": 1898}
            ),
            lambda ledger: ledger["cues"][0]["tts_schedule"].update(
                {"actual_offset_sample_inclusive": 1898}
            ),
        ]
        for mutate in mutations:
            fixture = list(_cue_ledger_fixture())
            ledger = fixture[0]
            mutate(ledger)
            with self.subTest(mutate=mutate):
                with self.assertRaises(ContractValidationError):
                    validate_cue_ledger_structure(*fixture)

        fixture = list(_cue_ledger_fixture())
        fixture[3][0]["object_bytes"] += b" "
        with self.assertRaises(ContractValidationError):
            validate_cue_ledger_structure(*fixture)

    def test_lightweight_n_to_n_validator_uses_schema_field_names(self) -> None:
        sources = [
            {
                "cue_id": "cue-000000",
                "cue_index": 0,
                "source_cue_record_sha256": _sha("record"),
            }
        ]
        translations = [
            {
                "cue_id": "cue-000000",
                "cue_index": 0,
                "source_cue_record_sha256": _sha("record"),
                "candidate_sha256": _sha("candidate"),
                "text_vi_nfc": "Xin chào",
            }
        ]
        tts = [
            {
                "cue_id": "cue-000000",
                "cue_index": 0,
                "translation_candidate_sha256": _sha("candidate"),
                "decoded_pcm_sha256": _sha("pcm"),
            }
        ]
        vietsub = [
            {
                "cue_id": "cue-000000",
                "cue_index": 0,
                "tts_pcm_sha256": _sha("pcm"),
                "text_vi_nfc": "Xin chào",
            }
        ]
        validate_n_to_n(sources, translations, tts, vietsub)
        vietsub[0]["tts_pcm_sha256"] = _sha("wrong")
        with self.assertRaises(ContractValidationError):
            validate_n_to_n(sources, translations, tts, vietsub)


class ApprovalAndIndexBindingTests(unittest.TestCase):
    def test_clean_machine_four_verdicts_and_collection_root_cross_match(
        self,
    ) -> None:
        report, verdicts = _clean_machine_and_verdicts()
        self.assertEqual(
            validate_clean_evidence_bindings_structure(report, verdicts),
            _clean_binding(),
        )
        for field in (
            "source_cue_manifest_sha256",
            "source_cue_collection_root",
            "policy_sha256",
        ):
            stale = copy.deepcopy(verdicts)
            stale[0]["bindings"][field] = _sha(f"stale-{field}")
            with self.subTest(field=field):
                with self.assertRaises(ContractValidationError):
                    validate_clean_evidence_bindings_structure(report, stale)

    def test_clean_gate_index_uses_nested_attempt_and_exact_root_name(self) -> None:
        approval, index = _clean_approval_and_index()
        validate_clean_gate_index_structure(approval, index)
        stale = copy.deepcopy(index)
        stale["attempt"]["fence_digest"] = _sha("stale-fence")
        with self.assertRaises(ContractValidationError):
            validate_clean_gate_index_structure(approval, stale)
        stale = copy.deepcopy(index)
        stale["source_cue_collection_root"] = _sha("stale-root")
        with self.assertRaises(ContractValidationError):
            validate_clean_gate_index_structure(approval, stale)

    def test_approval_windows_are_ordered_and_at_most_seven_days(self) -> None:
        approval, index = _clean_approval_and_index()
        too_long = copy.deepcopy(approval)
        too_long["payload"]["expires_at"] = "2026-07-31T00:00:01Z"
        with self.assertRaises(ContractValidationError):
            validate_clean_gate_index_structure(too_long, index)
        reversed_window = copy.deepcopy(approval)
        reversed_window["payload"]["expires_at"] = "2026-07-22T23:59:59Z"
        with self.assertRaises(ContractValidationError):
            validate_clean_gate_index_structure(reversed_window, index)

        release, release_index = _release_approval_and_index(approval)
        release["payload"]["expires_at"] = "2026-07-31T00:00:01Z"
        with self.assertRaises(ContractValidationError):
            validate_release_index_structure(release, release_index)

    def test_release_index_and_clean_approval_are_cross_bound(self) -> None:
        clean_approval, _ = _clean_approval_and_index()
        release_approval, release_index = _release_approval_and_index(
            clean_approval
        )
        validate_release_index_structure(release_approval, release_index)
        validate_release_bundle_structure(
            clean_approval,
            release_approval,
            release_index,
        )
        stale = copy.deepcopy(release_index)
        stale["cue_ledger_sha256"] = _sha("other-ledger")
        with self.assertRaises(ContractValidationError):
            validate_release_index_structure(release_approval, stale)
        swapped = copy.deepcopy(release_approval)
        swapped["payload"]["source_cue_collection_root"] = _sha(
            "other-root"
        )
        with self.assertRaises(ContractValidationError):
            validate_release_bundle_structure(
                clean_approval,
                swapped,
                release_index,
            )


class DubAuthorizationAndGoldenFixtureTests(unittest.TestCase):
    def _work_item_bundle(
        self,
    ) -> tuple[dict[str, object], dict[str, object]]:
        approval, clean_index = _clean_approval_and_index()
        approval_payload = approval["payload"]
        assert isinstance(approval_payload, dict)
        source_manifest, _ = _source_collection()
        source_manifest["source"] = copy.deepcopy(approval_payload["source"])
        source_manifest["collection_root"] = approval_payload[
            "source_cue_collection_root"
        ]
        source_manifest_bytes = _json_bytes(source_manifest)
        source_manifest_ref = _object_for_bytes(
            "signed-source-cue-manifest.json",
            source_manifest_bytes,
        )
        source_manifest_bundle = {
            "object_ref": source_manifest_ref,
            "object_bytes": source_manifest_bytes,
            "document": source_manifest,
        }
        approval_payload["source_cue_manifest"] = source_manifest_ref
        for verdict in approval_payload["clean_verdicts"]:
            verdict["bindings"]["source_cue_manifest_sha256"] = (
                source_manifest_ref["sha256"]
            )
        clean_index["source_cue_manifest_sha256"] = source_manifest_ref[
            "sha256"
        ]
        approval_bytes = _json_bytes(approval)
        approval_ref = _object_for_bytes(
            "signed-clean-approval.json",
            approval_bytes,
        )
        approval_bundle = {
            "object_ref": approval_ref,
            "object_bytes": approval_bytes,
            "document": approval,
        }
        clean_index["clean_approval"] = approval_ref
        index_bytes = _json_bytes(clean_index)
        index_ref = _object_for_bytes("active-clean-index.json", index_bytes)
        index_bundle = {
            "object_ref": index_ref,
            "object_bytes": index_bytes,
            "document": clean_index,
        }
        expected_evidence: list[dict[str, object]] = []
        for cue in source_manifest["cues"]:
            for field in ("record", "crop", "pcm_slice"):
                expected_evidence.append(copy.deepcopy(cue[field]))
        work_item = {
            "schema_version": "1",
            "attempt": _attempt(),
            "clean_approval_id": (
                "cap-0123456789abcdef0123456789abcdef"
            ),
            "clean_approval": approval_ref,
            "clean_gate_index": index_ref,
            "clean": copy.deepcopy(approval_payload["clean"]),
            "clean_manifest": copy.deepcopy(
                approval_payload["clean_manifest"]
            ),
            "source_cue_manifest": source_manifest_ref,
            "source_cue_evidence": expected_evidence,
            "clean_policy_sha256": approval_payload["clean_policy"]["sha256"],
            "expires_at": LATER,
        }
        bundles = {
            "clean_approval_bundle_value": approval_bundle,
            "clean_gate_index_bundle_value": index_bundle,
            "source_cue_manifest_bundle_value": source_manifest_bundle,
        }
        return work_item, bundles

    def test_work_item_is_cross_bound_and_denies_raw_list_latest(self) -> None:
        work_item, bundles = self._work_item_bundle()
        validate_dub_work_item_structure(work_item, **bundles)
        raw = copy.deepcopy(work_item)
        raw["source_uri"] = "gs://desub-media/integrated/source.mp4"
        with self.assertRaises(ContractValidationError):
            validate_dub_work_item_structure(raw, **bundles)
        latest = copy.deepcopy(work_item)
        latest["clean"]["uri"] = "gs://desub-media/integrated/latest"
        with self.assertRaises(ContractValidationError):
            validate_dub_work_item_structure(latest, **bundles)
        duplicate = copy.deepcopy(work_item)
        duplicate["source_cue_evidence"].append(
            copy.deepcopy(duplicate["source_cue_evidence"][0])
        )
        with self.assertRaises(ContractValidationError):
            validate_dub_work_item_structure(duplicate, **bundles)

    def test_renamed_raw_source_cannot_substitute_for_approved_clean(self) -> None:
        work_item, bundles = self._work_item_bundle()
        renamed_source = copy.deepcopy(work_item)
        renamed_source["clean"] = _obj(
            "input.mp4",
            content_type="video/mp4",
            digest=_sha("source"),
        )
        with self.assertRaises(ContractValidationError):
            validate_dub_work_item_structure(renamed_source, **bundles)
        renamed_evidence = copy.deepcopy(work_item)
        renamed_evidence["source_cue_evidence"][0] = _obj(
            "input.json",
            digest=_sha("source"),
        )
        with self.assertRaises(ContractValidationError):
            validate_dub_work_item_structure(renamed_evidence, **bundles)

    def test_golden_bytes_are_compared_without_claiming_jcs(self) -> None:
        golden = b'{"approval_id":"cap-1","attempt_seq":1}'
        validate_golden_canonical_bytes(
            golden,
            golden,
            expected_sha256=_sha(golden),
        )
        with self.assertRaises(ContractValidationError):
            validate_golden_canonical_bytes(golden + b"\n", golden)
        with self.assertRaises(ContractValidationError):
            validate_golden_canonical_bytes(
                golden,
                golden,
                expected_sha256=_sha("wrong"),
            )


if __name__ == "__main__":
    unittest.main()
