# Integrated DESUB v1 schemas

These Draft 2020-12 schemas freeze document shape. Objects reject unknown fields.
GCS generations are decimal strings to preserve exactness through JavaScript/n8n;
hashes are lowercase SHA-256; rates are strings when decimal spelling is normative.

Schema validation alone is insufficient. Call the semantic validators in the parent
Python package for transition, range, NFC, N-to-N, timing-equation, cross-object,
attempt-fence, approval and active-index checks.

Every production JSON Schema engine MUST run Draft 2020-12 with `format`
assertions enabled for `date-time` and `uri`. Treating `format` as annotation-only
is invalid for this contract. The package RFC3339 and ObjectRef semantic
validators remain mandatory even when the schema engine asserts these formats.

Phase A does not expose a final-release authorization API. Structural
release/ledger helpers are deliberately named `*_structure` and omitted from the
package public API because the complete selected-ledger -> candidate -> signed
preflight -> authorized-TTS chain belongs to the Phase-F implementation. Until
that implementation and live KMS allowlist verification exist, no helper in this
package may be used to mark a job completed or mint a link.

The same boundary applies earlier in the pipeline: approval/TTS/Dub-work helpers
that consume a test-only signature boolean are named `*_structure` and are not
public authorization APIs. Phase B/C must replace that flag with a verification
result bound to the exact payload digest, signature bytes, key version, public-key
hash, live key state, allowlist-policy digest, and evaluation time.

## Translation → preflight → TTS family ordering

`translation_attempt` is the generator-owned, create-only candidate and therefore
contains no reference to a verdict or approval that is created later. For every
candidate family, the only valid create order is:

1. source cue record;
2. translation candidate;
3. independent semantic and style preflight verdicts;
4. controller-signed `translation_preflight_approval` with `decision=pass`;
5. TTS attempt authorized by that exact, unexpired approval;
6. optional selected cue-ledger entry;
7. final release approval.

The semantic validator must load the referenced documents, not trust copied fields.
It must require one attempt fence and exact cue ID/index, candidate index,
candidate hash, source-cue hash and policy hashes across the family. Every adjacent
`*_object_sha256` or `*_approval_sha256` value must equal the SHA inside its
ObjectRef. The two verdict objects must have the exact
`translation_semantic_preflight` and `translation_style_preflight` roles, distinct
approved verifier identities, `decision=pass`, and matching bindings. The
controller signature and approval window must be valid before TTS synthesis.

The final cue ledger must bind the same preflight approval as its selected TTS
attempt. A release approval must list every distinct preflight approval used by
its TTS history and selected ledger, with no missing, duplicate or orphan
approval. Family order, timestamp ordering, ObjectRef equality, transitive
bindings and collection cardinality are semantic-validator requirements because
JSON Schema cannot fetch or compare immutable external objects.

`tts_attempt` has an exact outcome/evidence matrix: provider failure has no media;
undecodable media has only returned-media evidence; a voice/rate mismatch also has
decoded active-speech evidence; no-active-speech has an empty detector and no
schedule; schedule failure preserves full evidence with `fits=false`; success has
full evidence, `fits=true` and no error. Candidate 0/1 schedule failures use
`DUB_SCHEDULE_INVALID`; only candidate 2 exhaustion uses `DUB_CUE_UNFIT`.

The schemas are a Phase A contract artifact, not evidence that Gate A passed. See
`docs/DESUB_V1_CONTRACT_ADR.md` for the current fail-closed blockers.
