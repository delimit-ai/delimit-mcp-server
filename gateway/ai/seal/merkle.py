"""Delimit Seal — hash-only Merkle payload (LED-3127, open-core public layer).

A third party (auditor / LP / underwriter) must be able to verify an
attestation bundle OFFLINE — without calling Delimit infra — AND the bundle
must not leak the customer's source (IP-leakage mitigation). So the Merkle
trace is built over the *hashes* of each input/output, never the raw content.

Leaf shape (hashes only — NEVER raw code/diffs/prompts):
    {"kind": "input" | "output", "hash": "sha256:<64-hex>"}

The Merkle root is a binary hash tree over the leaf hashes in order. A third
party recomputes the root from the (already-hashed) leaves and checks it equals
the receipt's `merkle_root`; because the root is bound into the signed payload,
tampering with any leaf, leaf order, or the root invalidates the signature.

Pure stdlib (hashlib only). Never raises on well-typed input; callers treat a
mismatch as a failed check, not an exception.
"""

import hashlib

_LEAF_PREFIX = b"\x00"   # domain-separation: leaf vs internal node (2nd-preimage)
_NODE_PREFIX = b"\x01"


def _h(*parts):
    d = hashlib.sha256()
    for p in parts:
        d.update(p)
    return d.digest()


def leaf_hash(leaf):
    """Hash one canonical leaf: sha256(0x00 || kind || 0x1f || hash-hex-bytes).

    `leaf` is {"kind": "input"|"output", "hash": "sha256:<hex>"}. The leaf's
    own `hash` is already a content hash supplied by the producer — we never
    see raw content here.
    """
    kind = str(leaf.get("kind", "")).encode("utf-8")
    h = str(leaf.get("hash", "")).encode("utf-8")
    return _h(_LEAF_PREFIX, kind, b"\x1f", h)


def merkle_root(leaves):
    """Compute the hex Merkle root over an ordered list of hash-only leaves.

    Returns "sha256:<64-hex>". Empty leaf set → root over the empty marker
    (a v0.2 receipt with zero leaves is still well-defined and verifiable).

    (SEC-3) Odd levels promote the lone trailing node UP UNCHANGED (RFC 6962
    style) — it is NOT duplicated and re-hashed. Bitcoin-style last-node
    duplication is the CVE-2012-2459 malleability class: it lets the leaf array
    `L` and `L + [L[-1]]` collide on the same root. Promoting unchanged keeps
    distinct leaf arrays mapped to distinct roots.
    """
    level = [leaf_hash(x) for x in (leaves or [])]
    if not level:
        return "sha256:" + hashlib.sha256(_LEAF_PREFIX + b"empty").hexdigest()
    while len(level) > 1:
        nxt = [_h(_NODE_PREFIX, level[i], level[i + 1])
               for i in range(0, len(level) - 1, 2)]
        if len(level) % 2:
            nxt.append(level[-1])   # promote lone node unchanged (RFC 6962)
        level = nxt
    return "sha256:" + level[0].hex()


def verify_merkle(receipt):
    """True iff receipt['merkle_root'] matches the root recomputed from
    receipt['merkle_leaves']. Missing leaves but a present root → False
    (cannot independently confirm). Used by the v0.2 verifier path.
    """
    leaves = receipt.get("merkle_leaves")
    root = receipt.get("merkle_root")
    if not isinstance(root, str) or leaves is None:
        return False
    if not isinstance(leaves, list):
        return False
    return merkle_root(leaves) == root
