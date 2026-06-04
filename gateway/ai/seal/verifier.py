"""Delimit Seal — receipt verifier (Free tier, open-core public layer).

Verifies a receipt against the bundled, content-hashed Layer-0 constitution and
the published Ed25519 public key — with NO access to the engine or signing key:
  1. content-pin  — receipt.layer0_seed_id == the bundled constitution's id
  2. signature    — Ed25519 signature valid under the published public key
  3. structure    — receipt is well-formed
Honest by design: it reports what it does NOT attest.

`cryptography` is imported LAZILY inside the signature check and the whole call
is fail-closed: if the optional dependency is missing, verification returns
`verification_unavailable` instead of raising — so a missing wheel never breaks
the rest of the server. Never raises.
"""

import hashlib
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONSTITUTION = os.path.join(_HERE, "constitution.json")
_DEFAULT_PUBKEY = os.path.join(_HERE, "public.key")


def _seed_id_from_rules(frozen_rules):
    payload = json.dumps(
        [{k: r[k] for k in ("id", "title", "severity", "clause")} for r in frozen_rules],
        sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical(obj):
    body = {k: v for k, v in obj.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def _verify_sig(pub_hex, data, sig):
    # Lazy import: a missing optional dep must never crash the server.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if not isinstance(sig, str) or not sig.startswith("ed25519:"):
        return False
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex)).verify(
            bytes.fromhex(sig.split(":", 1)[1]), data)
        return True
    except Exception:
        return False


def verify_receipt(receipt_path, constitution_path=None, pubkey_path=None, verbose=False):
    """Verify a Delimit Seal receipt. Returns a verdict dict; never raises."""
    constitution_path = constitution_path or _DEFAULT_CONSTITUTION
    pubkey_path = pubkey_path or _DEFAULT_PUBKEY
    try:
        with open(receipt_path, encoding="utf-8") as fh:
            receipt = json.load(fh)
    except Exception as e:
        return {"valid": False, "seal_valid": False, "error": f"cannot read receipt: {e}"}
    try:
        with open(constitution_path, encoding="utf-8") as fh:
            constitution = json.load(fh)
        with open(pubkey_path, encoding="utf-8") as fh:
            pub_hex = fh.read().strip()
    except Exception as e:
        return {"valid": False, "seal_valid": False,
                "error": f"cannot read bundled constitution/key: {e}"}

    pub_seed = constitution.get("layer0_seed_id")
    recomputed = _seed_id_from_rules(constitution.get("frozen_rules", []))
    checks = {
        "constitution_self_consistent": recomputed == pub_seed,
        "receipt_pinned_to_constitution": receipt.get("layer0_seed_id") == pub_seed == recomputed,
        "receipt_well_formed": (
            all(k in receipt for k in ("schema", "layer0_seed_id", "transcript_hash", "action"))
            and isinstance(receipt.get("does_not_attest"), dict)),
    }
    try:
        checks["receipt_signature_valid"] = _verify_sig(
            pub_hex, _canonical(receipt), receipt.get("signature", ""))
        if "signature" in constitution:
            checks["constitution_signature_valid"] = _verify_sig(
                pub_hex, _canonical(constitution), constitution["signature"])
    except ImportError:
        return {
            "valid": False, "seal_valid": False, "verification_unavailable": True,
            "receipt_id": receipt.get("transcript_hash"),
            "error": ("seal verification requires the optional 'cryptography' package — "
                      "run `delimit doctor` or `pip install cryptography`"),
        }

    verdict = all(checks.values())
    return {
        "valid": verdict,
        "seal_valid": bool(checks.get("receipt_signature_valid", False)),
        "receipt_id": receipt.get("transcript_hash"),
        "product": receipt.get("product"),
        "layer0_seed_id": receipt.get("layer0_seed_id"),
        "checks": checks,
        "does_not_attest": receipt.get("does_not_attest", {}),
        "warning": ("Proves a Layer-0 governance process ran and which invariants were "
                    "checked under the stated constitution — NOT factual correctness, "
                    "NOT goodness, NOT the absence of subtle manipulation."),
    }
