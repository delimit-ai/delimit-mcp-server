"""Delimit Seal — v0.2 hardened receipt PRODUCER (LED-3460).

The cryptographic core of the moat: emits a `schema_version: "0.2"` hardened
attestation receipt that the open-core verifier (`ai.seal.verifier`) accepts
on its hardened path. This module is the *issuance* side; `verifier.py` is the
*verification* side and is the AUTHORITATIVE CONTRACT. This producer is built
to match the verifier byte-for-byte — it does NOT redefine the contract.

What a v0.2 receipt must satisfy (read from verifier.py, do not guess):
  1. layer0_seed_id pinned to the bundled constitution's seed.
  2. well-formed: schema, layer0_seed_id, transcript_hash, action present;
     does_not_attest is a dict.
  3. model_sequence: REQUIRED, non-empty list of non-empty strings; bound into
     the signed payload (cannot be silently dropped or shortened downstream).
  4. merkle_root: computed via ai.seal.merkle over hash-only leaves; bound in.
  5. signatures: >= 2 Ed25519 sigs from >= 2 DISTINCT pubkeys over the canonical
     payload. Canonical = the receipt minus ("signature","signatures"),
     json.dumps(sort_keys=True, separators=(",",":")).

Signing is PLUGGABLE. The producer builds the canonical bytes once and hands
them to an ordered list of `Signer` callables; each returns one
`{"key_id", "sig"}` entry. At real issuance the two signers are:
  - cosign-2: `broker_cosign2_signer()` — reads the private key from the
    secrets broker (name SEAL_COSIGN2_PRIVATE_ED25519, scope 'seal_signer') at
    runtime. NEVER hardcoded, never written to disk.
  - seal-primary: founder-held / external. The founder signs the SAME canonical
    bytes offline and hands the signature back via `injected_signature(...)`,
    OR an external-signer callback is supplied. The producer never holds the
    primary private key.

Pure stdlib + the same `cryptography` Ed25519 primitive the verifier uses.
Never invents fields the verifier ignores; never weakens the verifier.
"""

from __future__ import annotations

import hashlib
import json
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ai.seal.merkle import merkle_root

# Fields excluded from the canonical signed payload — MUST stay identical to
# the verifier's _UNSIGNED_FIELDS (ai/seal/verifier.py). A drift here silently
# produces unverifiable bundles, so we import the verifier's tuple rather than
# re-declaring the literal, and assert equality at import time.
from ai.seal.verifier import _UNSIGNED_FIELDS as _VERIFIER_UNSIGNED_FIELDS

SCHEMA_VERSION = "0.2"
RECEIPT_SCHEMA = "delimit.governed_agent.receipt.v0"

# Broker coordinates for the second (co-signing) private key. Read at runtime,
# under the restricted scope the broker enforces.
COSIGN2_SECRET_NAME = "SEAL_COSIGN2_PRIVATE_ED25519"
COSIGN2_KEY_ID = "seal-cosign-2"
COSIGN2_BROKER_SCOPE = "seal_signer"  # agent_type/tool the broker scope allows

PRIMARY_KEY_ID = "seal-primary"

# A Signer takes the canonical signed bytes and returns one signature entry:
#   {"key_id": "<id>", "sig": "ed25519:<hex>"}
Signer = Callable[[bytes], Dict[str, str]]


def _canonical_body(receipt: Dict) -> Dict:
    """The receipt minus the unsigned signature fields — what actually gets
    signed. Mirrors verifier._canonical's body selection exactly."""
    return {k: v for k, v in receipt.items() if k not in _VERIFIER_UNSIGNED_FIELDS}


def canonical_bytes(receipt: Dict) -> bytes:
    """The exact bytes the verifier signs/verifies over.

    Byte-for-byte identical to verifier._canonical: drop signature/signatures,
    json.dumps(sort_keys=True, separators=(",",":")), UTF-8. Exposed publicly so
    the founder can sign these bytes OFFLINE with the seal-primary key and hand
    the signature back (see injected_signature)."""
    body = _canonical_body(receipt)
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def hash_leaf(kind: str, content: bytes) -> Dict[str, str]:
    """Hash one input/output into a hash-only Merkle leaf.

    `kind` is "input" or "output"; `content` is the RAW bytes (code, diff,
    prompt, tool output). We hash it here and keep ONLY the hash — the raw
    content never enters the receipt (IP-leakage mitigation, per merkle.py).
    """
    if kind not in ("input", "output"):
        raise ValueError("leaf kind must be 'input' or 'output'")
    return {"kind": kind, "hash": _sha256_hex(content)}


def _validate_model_sequence(model_sequence: Sequence[str]) -> List[str]:
    """Enforce the verifier's model_sequence_present contract at BUILD time so a
    malformed receipt is rejected by the producer, not silently emitted."""
    if not isinstance(model_sequence, (list, tuple)) or len(model_sequence) == 0:
        raise ValueError("model_sequence is REQUIRED and must be a non-empty list")
    out = list(model_sequence)
    if not all(isinstance(m, str) and m for m in out):
        raise ValueError("model_sequence entries must be non-empty strings")
    return out


def _load_constitution_seed(constitution_path: Optional[str]) -> str:
    """Pin to the SAME layer0_seed_id the verifier checks against. The verifier
    compares receipt.layer0_seed_id to the bundled constitution's seed AND to a
    recompute of it; we read the bundled constitution's published seed so the
    pin matches by construction."""
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    path = constitution_path or os.path.join(here, "constitution.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["layer0_seed_id"]


def build_receipt(
    *,
    action: str,
    transcript_hash: str,
    model_sequence: Sequence[str],
    merkle_leaves: Sequence[Dict[str, str]],
    does_not_attest: Dict,
    constitution_path: Optional[str] = None,
    schema: str = RECEIPT_SCHEMA,
    product: str = "Delimit Seal",
    extra_fields: Optional[Dict] = None,
) -> Dict:
    """Build an UNSIGNED v0.2 hardened receipt.

    Required (verifier-enforced) fields are filled here; merkle_root is computed
    via ai.seal.merkle (NOT reimplemented). The result has no `signatures` yet —
    call sign_receipt() to attach the dual signatures.

    Args:
        action: The governance action string (e.g. "ANNOTATE", "BLOCK").
        transcript_hash: "sha256:<hex>" of the run transcript.
        model_sequence: ordered non-empty list of model ids that participated.
        merkle_leaves: ordered hash-only leaves (use hash_leaf to build them).
        does_not_attest: the honest does-not-attest dict (verifier requires a dict).
        constitution_path: override the bundled constitution (test only).
        extra_fields: optional non-required carry-through fields (e.g. session_id,
            timestamp, checks_run, findings). These are bound into the signature
            like every other field; they must not be named signature/signatures.

    Returns:
        An unsigned receipt dict.
    """
    if not isinstance(does_not_attest, dict):
        raise ValueError("does_not_attest must be a dict (verifier requires it)")
    if not (isinstance(transcript_hash, str) and transcript_hash):
        raise ValueError("transcript_hash is required")
    if not (isinstance(action, str) and action):
        raise ValueError("action is required")

    leaves = [dict(leaf) for leaf in merkle_leaves]
    for leaf in leaves:
        if leaf.get("kind") not in ("input", "output"):
            raise ValueError("each merkle leaf needs kind 'input' or 'output'")
        h = leaf.get("hash", "")
        if not (isinstance(h, str) and h.startswith("sha256:") and len(h) == len("sha256:") + 64):
            raise ValueError("each merkle leaf needs a 'sha256:<64hex>' hash")

    receipt: Dict = {
        "schema": schema,
        "schema_version": SCHEMA_VERSION,
        "layer0_seed_id": _load_constitution_seed(constitution_path),
        "action": action,
        "transcript_hash": transcript_hash,
        "product": product,
        "does_not_attest": does_not_attest,
        "model_sequence": _validate_model_sequence(model_sequence),
        "merkle_leaves": leaves,
        "merkle_root": merkle_root(leaves),
    }
    if extra_fields:
        for k, v in extra_fields.items():
            if k in _VERIFIER_UNSIGNED_FIELDS:
                raise ValueError(f"extra field '{k}' collides with an unsigned field")
            if k not in receipt:  # never let extras clobber required fields
                receipt[k] = v
    return receipt


def sign_receipt(receipt: Dict, signers: Sequence[Signer]) -> Dict:
    """Attach `signatures: [...]` by running each signer over the canonical bytes.

    Every signer signs the SAME canonical payload (the receipt minus the
    signature fields). The producer requires >= 2 signers; whether the resulting
    signatures satisfy the verifier's DISTINCT-pubkey requirement is the
    verifier's call — we do not second-guess it here, but we do refuse to emit a
    single-signer bundle since that can never pass the dual-sig gate.

    Returns a NEW receipt dict with `signatures` set (the input is not mutated).
    """
    if len(signers) < 2:
        raise ValueError(
            "v0.2 requires >= 2 signers (dual Ed25519). Got "
            f"{len(signers)}. Provide cosign-2 + seal-primary.")
    body = dict(receipt)
    body.pop("signature", None)
    body.pop("signatures", None)
    payload = canonical_bytes(body)
    signatures: List[Dict[str, str]] = []
    for signer in signers:
        entry = signer(payload)
        if not (isinstance(entry, dict) and isinstance(entry.get("key_id"), str)
                and isinstance(entry.get("sig"), str) and entry["sig"].startswith("ed25519:")):
            raise ValueError(
                "a signer returned a malformed entry; expected "
                "{'key_id': str, 'sig': 'ed25519:<hex>'}")
        signatures.append({"key_id": entry["key_id"], "sig": entry["sig"]})
    out = dict(body)
    out["signatures"] = signatures
    return out


def produce(
    *,
    action: str,
    transcript_hash: str,
    model_sequence: Sequence[str],
    merkle_leaves: Sequence[Dict[str, str]],
    does_not_attest: Dict,
    signers: Sequence[Signer],
    constitution_path: Optional[str] = None,
    extra_fields: Optional[Dict] = None,
) -> Dict:
    """One-shot: build + sign a v0.2 hardened receipt. See build_receipt/sign_receipt."""
    receipt = build_receipt(
        action=action,
        transcript_hash=transcript_hash,
        model_sequence=model_sequence,
        merkle_leaves=merkle_leaves,
        does_not_attest=does_not_attest,
        constitution_path=constitution_path,
        extra_fields=extra_fields,
    )
    return sign_receipt(receipt, signers)


# ── Signers ──────────────────────────────────────────────────────────────────
#
# A signer is `Callable[[bytes], {"key_id", "sig"}]`. Three are provided:
#   - private_key_signer:  generic — sign with an in-memory Ed25519PrivateKey.
#                          Used by tests with EPHEMERAL keys (never real keys).
#   - broker_cosign2_signer: real-issuance cosign-2 — reads the private key from
#                          the secrets broker at call time under scope seal_signer.
#   - injected_signature:  wrap a signature produced OUT OF BAND (the founder's
#                          offline seal-primary signing) into a signer entry.


def _ed25519_sign(private_key, payload: bytes) -> str:
    return "ed25519:" + private_key.sign(payload).hex()


def private_key_signer(key_id: str, private_key) -> Signer:
    """Signer backed by an in-memory cryptography Ed25519PrivateKey.

    For real issuance this is ONLY appropriate for a key that is legitimately
    in-process (it is not used to hold the primary key). Tests pass EPHEMERAL
    throwaway keys here. `private_key` is an Ed25519PrivateKey instance."""
    def _sign(payload: bytes) -> Dict[str, str]:
        return {"key_id": key_id, "sig": _ed25519_sign(private_key, payload)}
    return _sign


def _coerce_ed25519_private(value: str):
    """Parse the broker-stored cosign-2 key. It is stored as 64-hex (raw 32-byte
    Ed25519 seed); PEM is accepted as a fallback. Never logged."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    v = value.strip()
    try:
        raw = bytes.fromhex(v)
        if len(raw) == 32:
            return Ed25519PrivateKey.from_private_bytes(raw)
    except ValueError:
        pass
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    return load_pem_private_key(v.encode(), password=None)


def broker_cosign2_signer(
    *,
    agent_type: str = COSIGN2_BROKER_SCOPE,
    tool: str = "seal_producer",
    secret_name: str = COSIGN2_SECRET_NAME,
    key_id: str = COSIGN2_KEY_ID,
) -> Signer:
    """Real-issuance signer for the cosign-2 key.

    Reads the private key from the secrets broker AT CALL TIME (lazy), under the
    restricted scope the broker enforces ('seal_signer'). The key is never
    hardcoded, never returned to the caller, and never written to disk. The
    private key object lives only for the duration of the signature.

    Raises if the broker denies access (e.g. wrong scope) — fail-closed, so a
    bundle is never emitted with a silently-missing cosign signature.
    """
    def _sign(payload: bytes) -> Dict[str, str]:
        from ai.secrets_broker import get_secret
        res = get_secret(name=secret_name, agent_type=agent_type, tool=tool)
        if not res.get("granted"):
            raise PermissionError(
                f"cosign-2 broker access denied: {res.get('error', 'unknown')}. "
                f"Request under scope '{COSIGN2_BROKER_SCOPE}'.")
        sk = _coerce_ed25519_private(res["value"])
        return {"key_id": key_id, "sig": _ed25519_sign(sk, payload)}
    return _sign


def injected_signature(key_id: str, sig: str) -> Signer:
    """Wrap an ALREADY-COMPUTED signature (e.g. the founder's offline
    seal-primary signature over canonical_bytes(receipt)) as a signer.

    The founder workflow: producer emits canonical_bytes -> founder signs them
    offline with the external seal-primary private key -> hands back the hex ->
    this wraps it as `{key_id, sig}`. The producer holds no primary key material.
    `sig` must be "ed25519:<hex>"."""
    if not (isinstance(sig, str) and sig.startswith("ed25519:")):
        raise ValueError("injected signature must be 'ed25519:<hex>'")
    captured = {"key_id": key_id, "sig": sig}

    def _sign(payload: bytes) -> Dict[str, str]:  # payload ignored: signed offline
        return dict(captured)
    return _sign


# Import-time guard: if the verifier ever changes which fields it strips, this
# producer would emit unverifiable bundles. Fail loudly at import instead.
assert _VERIFIER_UNSIGNED_FIELDS == ("signature", "signatures"), (
    "producer/verifier canonical-field drift: verifier._UNSIGNED_FIELDS changed; "
    "update producer.canonical_bytes to match before issuing any v0.2 receipt")
