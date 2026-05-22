"""LED-2268 P0 Phase 0.2 — tenant-scoped filesystem layout.

The gateway today stores everything under `~/.delimit/` (memory.jsonl,
ledger.jsonl, evidence/, etc). That's correct for the single-tenant
founder install but doesn't generalize once paying customers run their
own tenants against a shared gateway host.

This module owns the path-resolver primitive for the per-tenant layout:

    ~/.delimit/                      ← legacy / shared root (unchanged)
    ~/.delimit/tenants/
        <safe-user-id>/              ← one dir per resolved API-key user
            memory.jsonl
            ledger.jsonl
            evidence/
            ...

Phase 0.2 ONLY ships the resolver + sanitiser + base-dir creation. No
existing storage is migrated; no endpoint is yet rerouted through here.
Phase 0.3 will add the first endpoint that uses tenant_data_root() and
copy the founder's existing single-tenant data into her own tenant
folder.

Security note: the user_id segment comes from Supabase
`user_api_keys.user_id` (which itself comes from NextAuth users.id, a
GitHub-OAuth-derived string). It's NEVER raw user input from the
request — but we still sanitise it defensively so a malformed value in
the DB can't escape into adjacent dirs via `..` or NUL bytes.
"""
from __future__ import annotations

import os
import re
import string
from pathlib import Path
from typing import Optional


# Base of the whole per-tenant tree. Lives under the existing delimit
# home so backup/restore tooling sees it without extra wiring.
def _delimit_home() -> Path:
    """Resolve ~/.delimit/ — same convention as the rest of the gateway."""
    home = os.environ.get("DELIMIT_HOME")
    if home:
        return Path(home).expanduser().resolve()
    return Path.home() / ".delimit"


_TENANTS_DIRNAME = "tenants"
# Allowed chars in a sanitised user-id segment. Conservative: ASCII
# alphanumerics + a small set of safe punctuation. Nothing that could
# be interpreted by the shell, the path parser, or a downstream tool.
_SAFE_CHARS = frozenset(string.ascii_letters + string.digits + "-_.")
# Max chars in a single user-id segment. Filesystems generally allow
# 255-byte basenames; we cap well below that and prefix-truncate +
# hash-suffix any longer input so distinct over-long IDs don't collide.
_MAX_SEGMENT_LEN = 64


def safe_user_segment(user_id: str) -> Optional[str]:
    """Sanitise a user_id into a filesystem-safe directory name.

    Returns None for empty / suspicious input so callers MUST handle
    the rejection rather than silently writing to a default dir. The
    intentional asymmetry from `_hash_key` (which always produces a
    valid hex string) is that an unauthenticated request can't land
    here — only an already-validated identity does — so a None here
    represents a corrupted DB row, not a normal failure mode.

    Strategy:
      - Strip whitespace, lowercase.
      - Replace any char outside the safe set with '_'.
      - If result is empty or only underscores, reject.
      - If result is longer than _MAX_SEGMENT_LEN, truncate + append
        a short hash suffix so distinct over-long IDs don't collide.
      - Reject anything that resolves to '.' or '..' (defence in depth
        against malformed DB rows like literally the string "..").
    """
    if not isinstance(user_id, str) or not user_id:
        return None
    s = user_id.strip().lower()
    if not s:
        return None
    # Substitute unsafe chars one-for-one — preserves length / readability
    # for the common case (NextAuth GitHub uses bare integer-ish strings).
    safe = "".join(c if c in _SAFE_CHARS else "_" for c in s)
    if not safe or safe.strip("_") == "":
        return None
    if safe in (".", ".."):
        return None
    if len(safe) > _MAX_SEGMENT_LEN:
        # Truncate to (max - 9) so the suffix `-<8hex>` fits in budget.
        import hashlib
        digest = hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]
        safe = safe[: _MAX_SEGMENT_LEN - 9] + "-" + digest
    return safe


def tenants_root() -> Path:
    """The shared parent of all per-tenant dirs. Always under DELIMIT_HOME."""
    return _delimit_home() / _TENANTS_DIRNAME


def tenant_data_root(user_id: str, *, create: bool = False) -> Optional[Path]:
    """Resolve the on-disk root for a specific tenant's data.

    Returns None if `user_id` doesn't sanitise to a usable segment.
    Caller treats that as "unauthorised" — same shape as the validator.

    If `create=True`, ensures the directory exists (mkdir -p, mode 0700).
    Default is read-only resolve so this can be called on hot paths
    without making syscalls when the dir is already present.
    """
    seg = safe_user_segment(user_id)
    if seg is None:
        return None
    root = tenants_root() / seg
    # Defence in depth: ensure the resolved path stays under tenants_root.
    # Belt-and-braces against an unforeseen sanitiser bypass.
    try:
        if tenants_root().resolve() not in root.resolve().parents and \
                root.resolve() != tenants_root().resolve():
            return None
    except (OSError, RuntimeError):
        return None
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Ensure tenants_root itself has the right mode too — first-
        # ever tenant write would otherwise inherit umask.
        try:
            tenants_root().chmod(0o700)
        except OSError:
            pass
    return root


def list_tenants() -> list[str]:
    """List the segment names of all tenants currently with on-disk data.

    Used by maintenance / audit / backup tooling. Returns an empty list
    when no tenants exist yet (the directory simply doesn't exist).
    """
    root = tenants_root()
    if not root.is_dir():
        return []
    out: list[str] = []
    for entry in root.iterdir():
        if entry.is_dir() and entry.name and not entry.name.startswith("."):
            out.append(entry.name)
    return sorted(out)
