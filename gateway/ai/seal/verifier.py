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
import re

try:  # package-relative; falls back for direct-script execution
    from ai.seal.merkle import verify_merkle, merkle_root
except Exception:  # pragma: no cover - import-path fallback
    from merkle import verify_merkle, merkle_root

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONSTITUTION = os.path.join(_HERE, "constitution.json")
_DEFAULT_PUBKEY = os.path.join(_HERE, "seal_pubkey.ed25519")

# ── A1 profile (schema_version >= 0.3) constants (spec §2.2) ─────────────────
A1_CRYPTO_SUITES = frozenset({"delimit-a1-v1"})
A1_LEAF_ALGS = frozenset({"sha256", "sha256-nonce"})
_A1_MAX_MEMBER_BYTES = 10 * 1024 * 1024  # 10 MB per tar member (tar-bomb guard)
_A1_LEAF_CONTENT_PREFIX = b"\x02"        # nonced-leaf content domain (spec §1.3)
_HEX40_OR_64 = re.compile(r"^[0-9a-fA-F]{40}$|^[0-9a-fA-F]{64}$")
_SHA256_FIELD = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
_RFC3339_UTC = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


class _A1Reject(Exception):
    """Internal: a hard rejection during A1 unpack/parse. Never escapes the
    public verify_a1_bundle (which returns a fail verdict, never raises)."""


def _is_a1(schema_version):
    """True iff the receipt declares schema_version >= 0.3 (the A1 profile)."""
    if not schema_version:
        return False
    try:
        parts = str(schema_version).split(".")
        major, minor = int(parts[0]), int(parts[1] if len(parts) > 1 else 0)
        return (major, minor) >= (0, 3)
    except Exception:
        return False


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


# ═══════════════════════════════════════════════════════════════════════════
#  A1 offline-verifiable bundle path (schema_version >= 0.3, spec §2)
# ═══════════════════════════════════════════════════════════════════════════
#
# ADDITIVE: v0.1/v0.2 receipts continue through verify_receipt() unchanged. The
# A1 path is stricter by policy — it hard-requires schema_version >= 0.3, the
# allowlisted crypto_suite, dual distinct-pubkey sigs, subject binding, and a
# key-manifest crosscheck; it never silently falls back to a weaker path.


def _a1_subject_well_formed(subject):
    if not isinstance(subject, dict):
        return False
    if not (isinstance(subject.get("repo"), str) and _SHA256_FIELD.match(subject["repo"])):
        return False
    for f in ("merge_commit", "base_commit"):
        v = subject.get(f)
        if not (isinstance(v, str) and _HEX40_OR_64.match(v)):
            return False
    if not (isinstance(subject.get("subject_salt"), str)
            and re.match(r"^[0-9a-fA-F]{32}$", subject["subject_salt"])):
        return False
    if "repo_hint" in subject and not isinstance(subject["repo_hint"], str):
        return False
    return True


def _a1_leaves_well_formed(leaves):
    if not isinstance(leaves, list):
        return False
    for leaf in leaves:
        if not isinstance(leaf, dict):
            return False
        if leaf.get("kind") not in ("input", "output"):
            return False
        if leaf.get("alg") not in A1_LEAF_ALGS:
            return False
        h = leaf.get("hash")
        if not (isinstance(h, str) and _SHA256_FIELD.match(h)):
            return False
    return True


def _pubkey_fingerprint(pub_hex):
    return "sha256:" + hashlib.sha256(bytes.fromhex(pub_hex)).hexdigest()


def verify_a1_receipt(receipt, constitution, pubkeys, *,
                      expect_merge_commit=None, expect_repo=None):
    """Core A1 verification over already-parsed dicts (spec §2.2 steps 3-11).

    `pubkeys` is the verifier's PINNED {key_id: pub_hex} set (from the bundled
    key files — the trust root, NEVER from the bundle). Returns a verdict dict;
    raises only ImportError (missing 'cryptography') which the caller maps to
    verification_unavailable.
    """
    checks = {}
    sv = receipt.get("schema_version")
    checks["schema_version_a1"] = _is_a1(sv)  # step 3: version floor >= 0.3
    checks["crypto_suite_allowed"] = receipt.get("crypto_suite") in A1_CRYPTO_SUITES  # step 4

    # step 5: constitution — seed recompute + pinned-key signature + receipt pin
    pub_seed = constitution.get("layer0_seed_id")
    recomputed = _seed_id_from_rules(constitution.get("frozen_rules", []))
    checks["constitution_self_consistent"] = recomputed == pub_seed
    checks["receipt_pinned_to_constitution"] = (
        receipt.get("layer0_seed_id") == pub_seed == recomputed)
    primary_pub = pubkeys.get("seal-primary")
    checks["constitution_signature_valid"] = bool(
        primary_pub and "signature" in constitution
        and _verify_sig(primary_pub, _canonical(constitution), constitution["signature"]))

    # step 6: well-formedness (v0.2 required + A1 required)
    checks["receipt_well_formed"] = (
        all(k in receipt for k in ("schema", "layer0_seed_id", "transcript_hash", "action"))
        and isinstance(receipt.get("does_not_attest"), dict))
    subject = receipt.get("subject")
    checks["subject_well_formed"] = _a1_subject_well_formed(subject)
    checks["issued_at_present"] = bool(
        isinstance(receipt.get("issued_at"), str) and _RFC3339_UTC.match(receipt["issued_at"]))
    km = receipt.get("key_manifest")
    checks["key_manifest_present"] = isinstance(km, dict) and bool(km)
    ms = receipt.get("model_sequence")
    checks["model_sequence_present"] = (
        isinstance(ms, list) and len(ms) > 0
        and all(isinstance(m, str) and m for m in ms))
    checks["merkle_leaves_well_formed"] = _a1_leaves_well_formed(receipt.get("merkle_leaves"))

    # step 7: merkle recompute
    checks["merkle_root_consistent"] = verify_merkle(receipt)

    # steps 8-9: canonical payload + dual distinct-pubkey signatures
    canonical = _canonical(receipt)
    dual_ok, dual_detail = _verify_dual(receipt, canonical, pubkeys)
    checks["dual_signatures_valid"] = dual_ok

    # step 10: key-manifest crosscheck — every VERIFIED key's fingerprint must
    # match the receipt's manifest entry for that key_id (rotation/forgery flag).
    km_ok = isinstance(km, dict) and bool(km)
    if km_ok:
        for kid in dual_detail.get("distinct_valid_key_ids", []) or []:
            pub = pubkeys.get(kid)
            if not pub or km.get(kid) != _pubkey_fingerprint(pub):
                km_ok = False
                break
    checks["key_manifest_crosscheck"] = bool(km_ok and dual_ok)

    # step 11: subject binding (the relying party's --expect-* checks)
    if expect_merge_commit is not None:
        got = (subject or {}).get("merge_commit", "") if isinstance(subject, dict) else ""
        checks["subject_merge_commit_matches"] = (
            isinstance(got, str) and got.lower() == str(expect_merge_commit).lower())
    if expect_repo is not None:
        ok_repo = False
        if isinstance(subject, dict):
            salt = subject.get("subject_salt", "")
            try:
                recomputed_repo = "sha256:" + hashlib.sha256(
                    bytes.fromhex(salt) + str(expect_repo).encode("utf-8")).hexdigest()
                ok_repo = recomputed_repo == subject.get("repo")
            except Exception:
                ok_repo = False
        checks["subject_repo_matches"] = ok_repo

    verdict = all(checks.values())
    out = {
        "valid": verdict,
        "seal_valid": bool(dual_ok),
        "mode": "a1",
        "a1": True,
        "hardened": True,
        "schema_version": sv,
        "crypto_suite": receipt.get("crypto_suite"),
        "receipt_id": receipt.get("transcript_hash"),
        "product": receipt.get("product"),
        "layer0_seed_id": receipt.get("layer0_seed_id"),
        "checks": checks,
        "model_sequence": receipt.get("model_sequence"),
        "merkle_root": receipt.get("merkle_root"),
        "dual_signatures": dual_detail,
        "subject": subject if isinstance(subject, dict) else None,
        "issued_at": receipt.get("issued_at"),
        "does_not_attest": receipt.get("does_not_attest", {}),
        "warning": ("Proves the governance process ran under the stated constitution "
                    "and that these bytes were dual-signed by two independent Delimit "
                    "seal keys and bind to exactly this merge event — NOT factual "
                    "correctness, NOT that the named models actually ran (SEC-2), NOT "
                    "that the verifier itself was not substituted (SEC-5)."),
    }
    return out


def _a1_read_tar(bundle_path):
    """Steps 1-2: safely read receipt.json + constitution.json from a tar.gz.

    Tar safety: rejects members escaping the archive ('..' / absolute / symlink
    / hardlink), members > 10 MB, and a missing receipt.json. Reads members into
    memory (no on-disk extraction) — no path is ever written. Raises _A1Reject.
    """
    import tarfile

    files = {}
    with tarfile.open(bundle_path, "r:*") as tar:
        for m in tar.getmembers():
            if m.isdir():
                continue
            if m.issym() or m.islnk():
                raise _A1Reject("tar member is a symlink/hardlink (rejected)")
            if not m.isfile():
                raise _A1Reject("tar member is not a regular file (rejected)")
            name = m.name
            if name.startswith("/") or os.path.isabs(name):
                raise _A1Reject("tar member has an absolute path (rejected)")
            norm = os.path.normpath(name)
            if norm.startswith("..") or norm.startswith("/") or "/../" in ("/" + norm.replace(os.sep, "/")):
                raise _A1Reject("tar member escapes the archive (rejected)")
            if m.size > _A1_MAX_MEMBER_BYTES:
                raise _A1Reject("tar member exceeds the 10 MB cap (rejected)")
            f = tar.extractfile(m)
            data = f.read(_A1_MAX_MEMBER_BYTES + 1) if f else b""
            if len(data) > _A1_MAX_MEMBER_BYTES:
                raise _A1Reject("tar member exceeds the 10 MB cap (rejected)")
            files[os.path.basename(norm)] = data
    if "receipt.json" not in files:
        raise _A1Reject("bundle is missing receipt.json")
    return files


def verify_a1_bundle(bundle_path, *, disclosure_path=None,
                     expect_merge_commit=None, expect_repo=None,
                     constitution_path=None, pubkey_path=None):
    """Verify an A1 bundle (`.a1.tar.gz`) or a bare receipt.json, offline.

    Air-gapped by construction: no sockets are imported. `bundle_path` may be a
    `.a1.tar.gz` (constitution taken FROM the bundle) or a receipt.json (the
    bundled constitution / pubkey files are used). Returns a verdict dict; never
    raises (a rejection becomes {"valid": False, ...}).
    """
    pubkey_path = pubkey_path or _DEFAULT_PUBKEY
    try:
        import tarfile
        constitution = None
        if os.path.isfile(bundle_path) and tarfile.is_tarfile(bundle_path):
            files = _a1_read_tar(bundle_path)  # step 1
            receipt = json.loads(files["receipt.json"].decode("utf-8"))  # step 2
            if "constitution.json" in files:
                constitution = json.loads(files["constitution.json"].decode("utf-8"))
        else:
            with open(bundle_path, encoding="utf-8") as fh:
                receipt = json.load(fh)
        if constitution is None:
            cpath = constitution_path or _DEFAULT_CONSTITUTION
            with open(cpath, encoding="utf-8") as fh:
                constitution = json.load(fh)
    except _A1Reject as e:
        return {"valid": False, "seal_valid": False, "mode": "a1", "a1": True,
                "error": f"A1 bundle rejected: {e}"}
    except Exception as e:
        return {"valid": False, "seal_valid": False, "mode": "a1", "a1": True,
                "error": f"cannot read A1 bundle: {e}"}

    pubkeys = _load_pubkeys(pubkey_path)
    try:
        out = verify_a1_receipt(
            receipt, constitution, pubkeys,
            expect_merge_commit=expect_merge_commit, expect_repo=expect_repo)
    except ImportError:
        return {
            "valid": False, "seal_valid": False, "mode": "a1", "a1": True,
            "verification_unavailable": True,
            "receipt_id": receipt.get("transcript_hash"),
            "error": ("A1 verification requires the optional 'cryptography' package — "
                      "run `delimit doctor` or `pip install cryptography`"),
        }
    return out
