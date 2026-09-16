# DESUB QA runtime decision

Evidence date: 2026-07-23
Decision status: **BLOCKED / fail-closed — no production QA runtime is approved.**

## Decision

The integrated DESUB pipeline must not dispatch a clean verifier, translation
preflight verifier, final dub verifier, or Sol final-audit role until this
document is replaced by an approved, test-backed decision record. A missing,
ambiguous, expired, or mismatched runtime record is a gate failure; it is not a
reason to downgrade, use a chat-session agent, or substitute a provider.

The production role set that must be pinned is:

- clean A/B/C and clean Sol;
- translation generator and independent preflight A/B;
- final dub A/B/C and final dub Sol.

Every role must have an exact provider, model ID/version, regional endpoint,
authentication-secret reference (name only), request/response schema, timeout,
quota/rate limit, response ID, and provenance record. Generator and verifier
roles must meet the independence rules in `N8N_CLEAN_DESUB_PLAN.md`.

## Current evidence and non-decisions

| Candidate | What official evidence establishes | Why it is not approved |
|---|---|---|
| OpenAI `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` | OpenAI documents all three as API models and recommends the Responses API for reasoning/tool workflows. [Official model guidance](https://developers.openai.com/api/docs/guides/latest-model) | The model pages document text/image input and text output, not direct audio or video input. That is insufficient for the required final Dub verifier C listening/perceptual role. In addition, the Singapore data-residency table currently documents regional storage but **not regional processing**. Non-US residency requires approval for Modified Abuse Monitoring **or** Zero Data Retention. The target organization/project, MAM/ZDR status, quota and model call have not been proven. [Sol model modalities](https://developers.openai.com/api/docs/models/gpt-5.6-sol) · [Official data-controls table](https://developers.openai.com/api/docs/guides/your-data#default-usage-policies-by-endpoint) |
| Vertex AI `gemini-2.5-flash` | Google documents multimodal/structured output and ML processing availability in `asia-southeast1` (Singapore). [Official model page](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models/gemini/2-5-flash) | It is a candidate only. It is not GPT-5.6 Sol, and the target project has not demonstrated endpoint callability, enabled API, IAM, quota, exact version availability, or the required runtime/data-control configuration. |

Google's current documentation states that managed-model customer data is not
used for training or fine-tuning without permission or instruction, but it also
documents conditional prompt logging and caching/retention behavior. That does
not by itself prove this workload's short-retention configuration. [Official
data-retention documentation](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/vertex-ai-zero-data-retention)

The current requirements therefore have no proven single provider configuration:
OpenAI preserves the requested GPT-5.6 roles but does not establish Singapore
processing, while the Vertex candidate establishes a Singapore processing location
but changes the requested model family. Gate A must not silently trade one hard
requirement for the other.

There is also no approved runtime for the audio/perceptual portion of final Dub
verifier C. Sol/Luna/Terra may review frozen text, images and deterministic reports,
but they cannot be claimed to have listened to the dubbed audio or watched the video
through the documented API modalities. Gate A therefore also requires one explicit
architecture decision: either approve an independent audio/video-capable verifier,
or replace the listening requirement with precisely versioned derived evidence and
record the resulting perceptual limitation. The latter is a contract change, not an
implicit fallback.

## Required evidence before approval

The approver must attach immutable evidence for every selected role:

1. Exact model ID/version and a provider-specific endpoint explicitly mapped to
   processing in Singapore; no global endpoint. For example, Google uses the
   region identifier `asia-southeast1`, while OpenAI documents a Singapore
   regional URL separately. A storage endpoint must not be treated as proof of
   regional processing.
2. Auth principal and secret-reference name, with least-privilege IAM proof;
   no credential value in this record, image, workflow, or prompt.
3. A real dry-run/request-response proof from the target project showing model
   callability, quota/rate behavior, timeout, response ID, and structured JSON
   schema validation.
4. Official terms/product evidence for no training, data residency, retention,
   prompt logging, caching, and all feature flags. The decision must record
   whether caching is disabled and must reject unsupported features such as
   grounding if they violate the retention policy.
5. The exact packet limits: media types, maximum bytes, signed-URL TTL,
   redaction rules, and the deletion schedule. Full source video is not sent by
   default; only the minimum QA packet defined by policy is allowed.
6. A test showing each final verdict binds the policy digest, artifact/candidate
   hashes, model/version, response ID, timestamp, and findings.
7. For every role, proof that the selected endpoint supports every required input
   modality. Final Dub verifier C must have a tested audio/video-capable path, or an
   approved contract revision defining the exact ASR, PCM, frame and metric evidence
   it consumes and the perceptual checks it no longer claims to perform.

## Approval record template

Populate all fields below only with observed evidence. Bracketed values are
placeholders, not approved values.

```text
decision_id: [generated-id]
approved_at: [RFC3339 time]
provider: [provider]
processing_jurisdiction: [must be Singapore]
regional_endpoint: [provider-specific endpoint backed by processing evidence]
roles:
  - role: [clean_a | ...]
    model_id_and_version: [exact immutable ID]
    auth_secret_reference: [reference name only]
    quota_and_timeout: [observed settings]
    request_schema_digest: [observed SHA-256]
    response_schema_digest: [observed SHA-256]
    callable_proof_uri_and_sha256: [evidence]
data_controls:
  no_training_evidence_uri: [official source]
  data_residency_evidence_uri: [official source]
  retention_and_logging_configuration: [observed configuration]
  cache_setting: [observed configuration]
approval: [Sol approver identity and signed evidence]
```

If any field is absent, a provider/model changes, a quota test fails, a required
input modality is unsupported, or Singapore processing cannot be evidenced, the
relevant clean/translation/dub gate must terminally fail according to the plan.
