# ADR: Integrated DESUB v1 contract freeze

Date: 2026-07-23
Status: **Phase A in progress; Gate A blocked, fail-closed**

## Decision

Integrated DESUB is a new product contract. It does not mutate the existing Cover
Visub runner or reuse its job identity, cache prefix, output meaning, or download
behavior.

The v1 public intake accepts exactly one HTTPS Douyin video URL. The internal clean
artifact is always `clean.mp4` and is never public. The only user artifact is
`dubbed.mp4`, and a link may be minted only after an active signed integrated release
approval passes exact-attempt revalidation.

The normative machine-readable contract lives in:

- `integrated_desub_contracts/schemas/v1/`
- `integrated_desub_contracts/policies/v1/`
- `integrated_desub_contracts/fixtures/v1/`
- `integrated_desub_contracts/enums.py`
- `integrated_desub_contracts/transitions.py`
- `integrated_desub_contracts/validation.py`
- `integrated_desub_contracts/candidate_validation.py`
- `integrated_desub_contracts/evidence_validation.py`

JSON Schema fixes document shape. Python semantic validators fix cross-document
bindings, state transitions, cue cardinality, ranges, sample arithmetic and approval
index invariants. Neither layer may be weakened to accept a legacy artifact.
Phase A intentionally does not expose a final-release authorization function:
release/ledger structure helpers cannot mark `completed` and remain outside the
package public API until Phase F implements the full selected candidate, signed
preflight, authorized TTS, cue-ledger and live KMS/allowlist chain.
For the same reason, Phase-A clean/preflight/TTS/Dub-work helpers that accept a
test-only signature boolean are structure-only and non-public. Phase B/C must
consume a KMS verification result bound to the exact payload digest, signature
bytes, key version, public-key hash, live key state, allowlist-policy digest and
evaluation time; a detached or reused boolean is never authorization.

Sample timing uses inclusive active offsets to preserve the approved contract:
`gap_delta = next_onset - previous_offset`, the next target is
`max(anchor, previous_offset + 2205)`, overlap is
`max(0, previous_offset - next_onset + 1)`, and the final offset must be no later
than `video_sample_count - 1`. Implementations must not reinterpret `2205` as 2205
fully silent samples by adding another sample; that would be a new contract version.

## V1 input and reject boundary

The frozen caps are one MP4, 1–600 seconds, at most 1 GiB, SDR 8-bit CFR video,
15–60 fps, no rotation, at most 1920×1080 landscape or 1080×1920 portrait, with
strictly monotonic PTS and exactly one MP4-compatible AAC stream. VFR, HDR,
unsupported rotation, oversize media, no audio, multiple audio streams, or a source
requiring audio transcoding is rejected with a stable error code. No silent
normalization is allowed.

Only dialogue captions within normalized frame band `y=0.55–0.90` are removable.
Full-frame preflight must reject likely dialogue-caption tracks outside that band as
`UNSUPPORTED_SUBTITLE_LAYOUT`. Watermarks, logos, titles, product text and legitimate
non-caption CJK remain out of scope and must be preserved.

The complete values are frozen in `policies/v1/input_contract.json`; stable states,
stages and errors are frozen in `enums.py`.

## Identity and isolation

Reserved v1 identities are:

| Kind | Integrated DESUB v1 value |
|---|---|
| Runner service | `integrated-desub-runner` |
| Worker job | `integrated-desub-worker` |
| QA controller | `integrated-desub-qa-controller` |
| Storage root | `desub/integrated/v1/` |
| Environment prefix | `DESUB_` |
| User artifact kind | `dubbed` |

These are contract names, not deployed resources. Phase A does not create them. The
legacy `cover-visub-lab`, Cover Visub URL-hash prefix and `visub.mp4` semantics remain
unchanged.

## State and failure semantics

The transition graph is closed. Terminal states have no outgoing transition.
There are exactly two bounded loops: translation permits two compact retries after
candidate `0`, and clean visual QA permits one repair transition through
`repairing_clean` before restarting detection and the entire clean gate with a new
clean SHA. The second clean repair is invalid. Any state, edge or retry not listed in
`transitions.py` is invalid.

Idempotency is a separate seven-day caller-key binding, not the work cache and not an
attempt identity. Its generation-versioned record stores the exact canonical create
request projection/hash, the bound attempt, exact-attempt replay path and durable 409
conflict observations. A replay may mint a new download URL only after release
revalidation; it cannot change the immutable attempt result.

Only `completed` may contain a ready `artifact.kind=dubbed` and download URL. Every
failure terminal contains a redacted structured error; no URL, OCR text, credential,
provider payload, token or stack trace is allowed. A callback is notification only:
n8n must fetch the exact attempt and cross-match both approvals plus the clean,
source-cue-manifest, cue-ledger and dubbed hashes.

## Immutable evidence and approvals

Source cue evidence is collected before inpainting. Ordered cue identity is
`cue-%06d`; raw/NFC Chinese, source PCM slice, crop, timing, classifier, extractor and
aligner provenance are create-only. Collection-root preimage is defined by
`policies/v1/source_cue_collection_root_contract.json` and is separate from the raw
manifest byte hash.

`clean_approval` authorizes one exact clean artifact and signed cue collection for
the dub stage; it never authorizes a user link. `release_approval` is the only
integrated release authority. Both bind the winning attempt fence, immutable object
generations and byte hashes, policy hashes, machine reports, exact verifier roles and
provider response identity. Active clean/release indices are generation-conditional
and must match their envelopes exactly.

Signatures use UTF-8 RFC 8785 canonical payload bytes, SHA-256, and Cloud KMS
`EC_SIGN_P256_SHA256`. The signature field is excluded from the canonical payload.
No key is currently allowlisted; see `DESUB_KMS_RUNBOOK.md`.

## Retention, capacity and SLO

Artifacts/evidence/manifests/reports/verdicts/approvals remain seven days; raw QA
packets remain two days; exact release-validated user links last 24 hours and are
refreshable. Capacity is two active executions plus ten queued jobs, then stable
HTTP 429 `CAPACITY_BUSY`. The reference execution SLO is a seven-minute 1080p video
within 60 minutes; hard timeout is 90 minutes, with queue time reported separately.
Cost is measured but cannot automatically reduce model, reasoning or quality gates.

## Gate A blockers

The schemas and numeric candidate policies are materialized but are intentionally not
approved while any of these remain unresolved:

1. No production verifier configuration simultaneously proves the requested GPT-5.6
   role and processing/transient storage in Singapore; see
   `DESUB_QA_RUNTIME_DECISION.md`.
2. The documented Sol/Luna/Terra API modalities do not accept direct audio/video, so
   the perceptual Final Dub verifier C has no approved callable runtime. An
   audio/video-capable verifier or an explicitly revised derived-evidence contract is
   required.
3. Cloud Run `validateOnly` cannot prove a launch token in a persisted
   `Execution.template`; one separately authorized no-op execution is required; see
   `DESUB_CLOUD_RUN_DRY_RUN.md`.
4. The KMS key-version/public-key allowlist is empty.
5. Font binary/license/glyph hashes, pinned FFmpeg/OCR/ASR implementations and several
   exact media algorithm options are absent.
6. Only one full fixture and one partial high-motion candidate are available; the
   manhua, early-source-audio-tail and valid 1.5000-rate TTS calibration evidence are
   missing.

No Phase B engine work, production video processing, Cloud Run execution, deployment,
link minting, or n8n mutation is authorized by this ADR.
