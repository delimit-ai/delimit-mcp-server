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

try:  # package-relative; falls back for direct-script execution
    from ai.seal.merkle import verify_merkle
except Exception:  # pragma: no cover - import-path fallback
    from merkle import verify_merkle

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONSTITUTION = os.path.join(_HERE, "constitution.json")
_DEFAULT_PUBKEY = os.path.join(_HERE, "seal_pubkey.ed25519")


def _is_hardened(schema_version):
    """True iff the receipt declares the LED-3127 hardened schema (>= 0.2).

    Receipts with no schema_version (or "0.1") are legacy single-sig bundles
    and follow the original verification path unchanged.
    """
    if not schema_version:
        return False
    try:
        parts = str(schema_version).split(".")
        major, minor = int(parts[0]), int(parts[1] if len(parts) > 1 else 0)
        return (major, minor) >= (0, 2)
    except Exception:
        return False


def _seed_id_from_rules(frozen_rules):
    payload = json.dumps(
        [{k: r[k] for k in ("id", "title", "severity", "clause")} for r in frozen_rules],
        sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


# Fields that are NOT part of the signed canonical payload. `signature` is the
# legacy (v0.1) single-sig field; `signatures` is the v0.2 dual-sig array. Both
# are stripped so the signatures are never self-referential. Stripping a key
# the receipt does not contain is a no-op, so this is fully backward compatible.
_UNSIGNED_FIELDS = ("signature", "signatures")


def _canonical(obj):
    body = {k: v for k, v in obj.items() if k not in _UNSIGNED_FIELDS}
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


def _load_pubkeys(pubkey_path):
    """Return {key_id: pub_hex} of every published verification key.

    The primary published key (the bundled seal_pubkey.ed25519) is always
    keyed as "seal-primary". An optional sibling file `seal_pubkeys.json`
    (a flat {key_id: hex} map) carries any co-signing keys (e.g. the second
    Ed25519 key for v0.2 dual signatures) without breaking the single-file
    contract older callers rely on.
    """
    keys = {}
    try:
        with open(pubkey_path, encoding="utf-8") as fh:
            keys["seal-primary"] = fh.read().strip()
    except Exception:
        pass
    extra = os.path.join(os.path.dirname(pubkey_path), "seal_pubkeys.json")
    try:
        with open(extra, encoding="utf-8") as fh:
            for kid, hx in (json.load(fh) or {}).items():
                if isinstance(hx, str) and hx.strip():
                    keys[str(kid)] = hx.strip()
    except Exception:
        pass
    return keys


def _verify_dual(receipt, canonical, pubkeys):
    """Verify the v0.2 `signatures: [{key_id, sig}, ...]` array.

    Requires AT LEAST TWO valid signatures from TWO DISTINCT PUBLIC KEYS over
    the canonical payload. Each signature names its key_id; the verifier looks
    the key up in the published set. (SEC-1) Dedup is by the verified PUBKEY
    BYTES, NOT by key_id — two key_ids that map to the same Ed25519 pubkey are
    one key, so one private key cannot satisfy dual-sig by relabelling.
    Returns (ok: bool, detail: dict).
    """
    sigs = receipt.get("signatures")
    if not isinstance(sigs, list) or len(sigs) < 2:
        return False, {"reason": "fewer than two signatures", "count": len(sigs) if isinstance(sigs, list) else 0}
    verified_pubkeys = set()   # SEC-1: dedup by pubkey hex, not key_id
    verified_key_ids = set()
    for entry in sigs:
        if not isinstance(entry, dict):
            continue
        kid = entry.get("key_id")
        sig = entry.get("sig", "")
        pub = pubkeys.get(kid)
        if pub and _verify_sig(pub, canonical, sig):
            verified_pubkeys.add(pub.lower())
            verified_key_ids.add(kid)
    ok = len(verified_pubkeys) >= 2
    return ok, {
        "distinct_valid_pubkeys": len(verified_pubkeys),
        "distinct_valid_key_ids": sorted(verified_key_ids),
        "required": 2,
    }


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

    pubkeys = _load_pubkeys(pubkey_path)
    # v0.2 (LED-3127) hardening is opt-in by schema_version. Receipts with no
    # schema_version (or "0.1") follow the exact legacy single-sig path so every
    # already-issued customer bundle still verifies byte-for-byte.
    hardened = _is_hardened(receipt.get("schema_version"))

    pub_seed = constitution.get("layer0_seed_id")
    recomputed = _seed_id_from_rules(constitution.get("frozen_rules", []))
    checks = {
        "constitution_self_consistent": recomputed == pub_seed,
        "receipt_pinned_to_constitution": receipt.get("layer0_seed_id") == pub_seed == recomputed,
        "receipt_well_formed": (
            all(k in receipt for k in ("schema", "layer0_seed_id", "transcript_hash", "action"))
            and isinstance(receipt.get("does_not_attest"), dict)),
    }
    canonical = _canonical(receipt)
    try:
        if hardened:
            # ── v0.2 hardened path (LED-3127) ───────────────────────────────
            # model_sequence is REQUIRED and bound into the signed payload, so a
            # bundle that drops or alters it cannot pass: either the field is
            # absent (this check fails) or the canonical payload changed (the
            # signatures below fail). This closes the forge-by-omission class.
            ms = receipt.get("model_sequence")
            checks["model_sequence_present"] = (
                isinstance(ms, list) and len(ms) > 0
                and all(isinstance(m, str) and m for m in ms))
            checks["merkle_root_consistent"] = verify_merkle(receipt)
            dual_ok, dual_detail = _verify_dual(receipt, canonical, pubkeys)
            checks["dual_signatures_valid"] = dual_ok
            # seal_valid for hardened receipts means: both sigs valid.
            checks["receipt_signature_valid"] = dual_ok
        else:
            # ── v0.1 legacy path (unchanged) ────────────────────────────────
            checks["receipt_signature_valid"] = _verify_sig(
                pub_hex, canonical, receipt.get("signature", ""))
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
    out = {
        "valid": verdict,
        "seal_valid": bool(checks.get("receipt_signature_valid", False)),
        "schema_version": receipt.get("schema_version", "0.1"),
        "hardened": hardened,
        "receipt_id": receipt.get("transcript_hash"),
        "product": receipt.get("product"),
        "layer0_seed_id": receipt.get("layer0_seed_id"),
        "checks": checks,
        "does_not_attest": receipt.get("does_not_attest", {}),
        "warning": ("Proves a Layer-0 governance process ran and which invariants were "
                    "checked under the stated constitution — NOT factual correctness, "
                    "NOT goodness, NOT the absence of subtle manipulation."),
    }
    if hardened:
        out["model_sequence"] = receipt.get("model_sequence")
        out["merkle_root"] = receipt.get("merkle_root")
        out["dual_signatures"] = dual_detail
    return out
