# DESUB KMS signing and verification runbook

Evidence date: 2026-07-23
Decision status: **BLOCKED / fail-closed — no KMS key version is allowlisted.**

## Normative envelope procedure

For both `clean_approval` and `release_approval`:

```text
canonical_bytes = UTF8(RFC8785_JCS(envelope.payload))
signed_digest  = SHA256(canonical_bytes)
signature      = CloudKMS.AsymmetricSign(key_version, digest.sha256=signed_digest)
```

Use a Cloud KMS asymmetric-signing key version with algorithm
`EC_SIGN_P256_SHA256`, purpose `ASYMMETRIC_SIGN`, and state `ENABLED`. The KMS
API accepts a SHA-256 digest and returns integrity fields that must be checked:
`verifiedDigestCrc32c`, `signatureCrc32c`, and the signing key-version name.
[Official asymmetricSign API](https://docs.cloud.google.com/kms/docs/reference/rest/v1/projects.locations.keyRings.cryptoKeys.cryptoKeyVersions/asymmetricSign)

An asymmetric key version must be enabled to sign, and KMS exposes the key
version's state and algorithm as metadata. [Official key-state
documentation](https://docs.cloud.google.com/kms/docs/key-states) The signer
needs `cloudkms.cryptoKeyVersions.useToSign`; a verifier retrieves the public
key through the documented public-key interface. [Official signing and
verification guide](https://docs.cloud.google.com/kms/docs/create-validate-signatures)

## Empty allowlist — do not bypass

```text
approved_key_versions: []
approved_public_key_sha256: []
signer_principals: []
verifier_principals: []
```

These lists intentionally remain empty. A placeholder, a key alias, an
unversioned CryptoKey name, a disabled version, or an unverifiable public-key
hash is rejected. No approval may be created, verified as valid, cache-hit,
or used to mint a download link while the allowlist is empty.

## Provisioning and validation evidence required

After explicit authorization, record the following without storing a secret or
private key:

1. Full CryptoKeyVersion resource name; key purpose; algorithm; protection
   level; state; creation time; and current allowlist status.
2. Retrieved public key in a canonical public format and SHA-256 of its exact
   public-key bytes; store the hash and retrieval evidence, not private key
   material.
3. IAM proof that only the controller signer has
   `cloudkms.cryptoKeyVersions.useToSign`; verifier/read-only identities have
   only the minimum public-key/metadata access required.
4. RFC 8785 canonicalization fixtures across every implementation language,
   including Unicode, number, ordering, and byte-for-byte SHA-256 vectors.
5. Sign/verify test using a non-production fixture payload. Verify request and
   response CRC32C fields, expected key-version name, ECDSA signature, public
   key hash, payload digest, schema version, and exact bindings.
6. Fail-closed tests for malformed JSON, changed payload byte, unknown key,
   disabled/destroyed/revoked version, KMS metadata lookup error, stale fence,
   wrong generation, and approval replay to a different artifact, policy, or
   cue ledger.

The production verifier must return one typed, non-reusable verification result
bound to all of the following: exact approval ObjectRef URI/generation/size/SHA,
RFC 8785 payload digest, signature-byte SHA, algorithm, full CryptoKeyVersion,
public-key SHA, observed `ENABLED` state, allowlist-policy SHA, verifier
principal, and timezone-aware `evaluated_at`. The consuming gate must enforce
`issued_at <= evaluated_at < expires_at` and any work-item/index validity window.
A detached `signature_verified=true` boolean, cached pass, or result for different
bytes/key/time is not authorization. Phase-A structure tests use booleans only as
tamper fixtures; those helpers are deliberately excluded from the public API.

## Rotation and revocation

- New approvals use only the current allowlisted `ENABLED` key version.
- An old version is accepted only while it remains explicitly allowlisted and
  `ENABLED`; otherwise clean authorization, release verification, cache use,
  and link minting fail closed.
- Before disabling or revoking a version, inventory every unexpired approval
  that names it and re-run the affected authorization/release gate with an
  authorized current version.
- A compromised, unavailable, disabled, destroyed, or metadata-unreadable key
  causes immediate denial. Do not reuse a cached pass or silently switch keys.
- Audit records contain key-version resource, public-key hash, payload digest,
  approval ID, attempt/fence, actor principal, time, and outcome; never include
  a credential, raw private key, or signature secret.

## Phase ownership

Phase B implements and proves the shared RFC 8785/KMS sign-and-verify path for
`clean_approval`, including key-state, generation, fence, and clean-index
validation. Phase F reuses that proven implementation and adds only
release-specific schema, role, SHA, release-index, cache, and link checks. A
release implementation must not be used to defer clean-authorization
verification.
