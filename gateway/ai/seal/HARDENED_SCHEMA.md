# Delimit Seal — hardened receipt schema v0.2 (LED-3127)

> **See also `A1_BUNDLE.md`** for the additive **v0.3 A1 profile** — the
> offline-verifiable `.a1.tar.gz` bundle (subject binding, hiding leaves,
> crypto-suite label, key-manifest fingerprints) and its standalone air-gapped
> verifier `verify_a1.py`. v0.2 receipts described below verify unchanged.


Goal: let a third party (auditor / LP / D&O underwriter) verify an attestation
bundle **offline** — with no call to Delimit infra — while (a) never leaking the
customer's source and (b) making the model trace non-suppressible.

`schema_version: "0.2"` is **opt-in and additive**. Receipts with no
`schema_version` (or `"0.1"`) verify on the unchanged legacy single-signature
path, so every already-issued customer bundle keeps verifying byte-for-byte.

## New required fields (only when `schema_version >= 0.2`)

| field            | type     | meaning |
|------------------|----------|---------|
| `schema_version` | string   | `"0.2"` — selects the hardened verification path |
| `model_sequence` | string[] | **REQUIRED, non-empty.** Ordered list of models that participated. Bound into the signed payload. |
| `merkle_leaves`  | object[] | Ordered hash-only leaves: `{"kind": "input"\|"output", "hash": "sha256:<hex>"}`. **Hashes only — never raw code/diffs/prompts.** |
| `merkle_root`    | string   | `"sha256:<hex>"` — Merkle root over `merkle_leaves` (domain-separated binary tree, RFC-6962 odd handling — see `merkle.py`). |
| `signatures`     | object[] | **Dual Ed25519.** `[{"key_id": str, "sig": "ed25519:<hex>"}, ...]`. ≥2 valid signatures from ≥2 **distinct public keys** (deduped by pubkey bytes — see below) over the canonical payload. |

The legacy `signature` (single string) field is no longer used by a v0.2
receipt; `signatures[]` replaces it. Both `signature` and `signatures` are
stripped before computing the canonical signed payload, so neither is
self-referential.

## Canonical payload

`canonical = json.dumps({k:v for receipt if k not in ("signature","signatures")},
sort_keys=True, separators=(",", ":"))` — identical rule to v0.1, just with the
extra `signatures` key excluded. The TypeScript verifier in delimit-ui mirrors
this byte-for-byte.

## Verification (hardened path)

A v0.2 receipt is `valid` iff ALL of:
1. constitution self-consistent + receipt pinned to it (unchanged)
2. receipt well-formed (unchanged)
3. `model_sequence_present` — present, a non-empty list of non-empty strings
4. `merkle_root_consistent` — `merkle_root` recomputes from `merkle_leaves`
5. `dual_signatures_valid` — ≥2 valid Ed25519 sigs from ≥2 distinct public keys

### SEC-1 — dual-sig dedup is by PUBLIC-KEY BYTES, not key_id

The verifier accumulates the set of **verified pubkey hex** (the `pub` value each
signature resolved to and validated under), and requires `len(set) ≥ 2`. It does
**not** count distinct `key_id` labels. If `seal_pubkeys.json` ever maps two
key_ids to the same Ed25519 pubkey, a single private key could otherwise emit two
"distinct-key_id" signatures and pass — defeating dual-sig. Deduping by pubkey
bytes closes that. (`distinct_valid_pubkeys` in the verdict is the gate;
`distinct_valid_key_ids` is reported for audit only.)

### SEC-3 — Merkle odd-level handling is RFC 6962, not Bitcoin

A lone trailing node on an odd level is **promoted up unchanged**, NOT duplicated
and re-hashed. Bitcoin-style last-node duplication is the CVE-2012-2459
malleability class: it makes the leaf array `L` and `L + [L[-1]]` collide on the
same root. Leaf vs node are also domain-separated (`0x00` leaf prefix / `0x01`
node prefix) to block leaf/node second-preimage. `merkle.py` and the delimit-ui
`verify.ts` implement this identically (cross-language root parity is asserted in
both test suites).

### SEC-4 — canonicalization conformance

`canon_vectors.json` + `canon_vectors.sha256.json` (duplicated byte-for-byte in
delimit-ui `lib/seal/`) pin the canonical bytes for edge-case inputs (unicode,
floats, control chars, nested unsorted keys, empty containers, signature
stripping). Both the Python `_canonical` and the TS `pyDumps` recompute every
vector's sha256 and assert it equals the committed map → byte-identical
canonicalization is enforced, not assumed.

**Divergence found + fixed:** Python `json.dumps` escapes `0x7f` (DEL) as
``, but the TS escape threshold was `> 0x7f`, leaving a raw DEL byte. Fixed
to `>= 0x7f` so DEL is escaped identically.

**Documented constraint (not reachable in practice):** a *float* whose value is a
whole number serializes as `1.0` in Python but `1` in JS. This cannot occur in a
seal payload loaded from JSON (`1.0` and `1` are indistinguishable post-parse) and
no seal field is a float, so it is left as a documented constraint rather than a
code change. Non-finite numbers (`NaN`/`Infinity`) are rejected by both.

## Non-forgeability (why model_sequence can't be silently dropped)

`model_sequence`, `merkle_root`, and `merkle_leaves` are all inside the canonical
payload the signatures cover. To remove or shorten `model_sequence`:
- **drop it** → check (3) fails; and the canonical payload changed so (5) fails.
- **alter it** → (3) may still pass, but the signed bytes changed so (5) fails.
- **re-sign a forged payload** → requires both private signing keys, which the
  forger does not have.

This is the same forge-by-omission class as the `.internal_dev` audit-lock
bypass closed in gateway v3.9.0: a field that must be *present and bound*, not
merely *checked-if-present*.

## Published verification keys

The verifier reads the bundled primary key from `seal_pubkey.ed25519`
(key_id `seal-primary`) plus an optional flat `{key_id: hex}` map in
`seal_pubkeys.json` for co-signing keys. Adding a co-signer is additive — no
existing key is removed.

## KEY-MANAGEMENT ASSUMPTION (founder action required before issuing v0.2)

Today the repo ships exactly **one** published key (`seal_pubkey.ed25519`) and
**no** private signing material (signing is external/proprietary). Dual
signatures require a **second independent Ed25519 keypair**:
- provision a second signing key in the external signer,
- publish its public half into `seal_pubkeys.json` (and the delimit-ui mirror),
- have the producer emit `signatures: [{key_id, sig}, {key_id, sig}]`.

Until the second key is provisioned, the verifier is ready but **no production
v0.2 receipt can be issued** (a single-key receipt fails `dual_signatures_valid`
by design). The two keys SHOULD be independently held (e.g. different KMS / HSM
custody) so a single key compromise cannot forge a bundle — this is the whole
point of dual signing. This provisioning is a founder ship-gate item, not an
open-core code change.

## Accepted risks (documented, not fixed)

These were surfaced by the LED-3127 security deliberation and consciously
accepted in scope. They are properties of the trust model, not verifier bugs.

- **SEC-2 — `model_sequence` completeness is producer-trust.** The signature
  proves what the producer *claimed* the model sequence was, not that no model
  was secretly omitted *at issuance*. A dishonest producer can sign a short
  sequence; the verifier cannot detect a model that was never recorded. What the
  hardening guarantees is non-repudiation + non-suppression *after* signing: the
  sequence cannot be silently altered or dropped downstream without breaking the
  signature. Detecting issuance-time omission would require a separate
  attested-execution channel (out of scope here).

- **SEC-5 — pubkey distribution is the verification trust root.** Offline
  verification is only as trustworthy as the published-key set the verifier
  pins. If an attacker can substitute `seal_pubkey.ed25519` / `seal_pubkeys.json`
  at the verifier, they can make a forged bundle verify. Mitigation (key pinning,
  a transparency log / cert-transparency-style append-only key history, or
  distributing keys out-of-band) is deferred and out of scope for the open-core
  verifier. Document the published keys' provenance when the second key is
  provisioned.

- **SEC-6 — empty `merkle_leaves` is a valid, well-defined receipt by design.**
  A receipt with zero leaves yields a fixed well-known empty root and verifies.
  This is intentional: a governed run may legitimately attest with no
  input/output hash leaves (e.g. a pure policy decision). It is not treated as a
  tamper signal. Consumers that require ≥1 leaf must enforce that as a separate
  policy check on top of the verifier.
