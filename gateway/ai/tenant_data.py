"""LED-2268 P0 Phase 0.3 — first consumer of the tenant_data_root primitive.

Provides describe_tenant_data() — the read-only view of what's on disk
inside a given tenant's data root. Used by the /tenant/data endpoint
and intended to power the dashboard's "your data lives here" home tile
for browser-only operators.

The describe call is deliberately minimal:
  - data_root: absolute path string the gateway resolved for this tenant
  - exists:    has the dir been created yet?
  - files:     relative paths inside the dir (deepest-first, sorted)
  - dirs:      relative paths of subdirectories
  - total_size_bytes: sum of all file sizes (sentinel for usage display)
  - cap_bytes: soft cap if configured (Phase 0.3 hard-codes None — no cap)

Phase 0.3 ONLY reads. No write/delete API yet — that's Phase 0.4+, when
the dashboard ships its first "create note / save memory" surface.

Founder-data migration is handled by the SEPARATE manual script
scripts/delimit_seed_tenant_data.py (also in this PR), not by an
auto-trigger inside describe(). Keeps the read path side-effect-free.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional, TypedDict

from . import tenant_paths

logger = logging.getLogger("delimit.tenant_data")


# ─────────────────────────────────────────────────────────────────────
# Phase 0.4 — write/read/delete limits + allowlist
# ─────────────────────────────────────────────────────────────────────

# Max bytes a single tenant file may contain. Generous enough for
# memory.jsonl / ledger.jsonl scale (typically <100KB per tenant) but
# tight enough that a runaway client can't fill the disk. Future quota
# enforcement will sum across files; this is per-file.
MAX_FILE_BYTES = 1024 * 1024  # 1 MiB

# Allowlist of file extensions tenants may write/read. Restrictive on
# purpose: text-shaped data files only. Blocks .py / .sh / .so / .dll
# / anything executable so the tenant data root can never become a
# code-drop or LD-load source.
_ALLOWED_EXTENSIONS = frozenset({
    ".json",
    ".jsonl",
    ".md",
    ".txt",
    ".csv",
    ".yaml",
    ".yml",
})

# Max path-segment count (depth) to discourage deeply-nested layouts
# that complicate audit + backup. Practical cap; nothing in the
# legitimate use case needs >5 levels of subdirectory.
_MAX_PATH_DEPTH = 5


class TenantPathError(Exception):
    """Raised for any tenant-data path that fails validation.

    Caller pattern is `except TenantPathError as e: return 400 ...`.
    The message is the diagnostic suitable for surfacing to the user
    ("path_too_deep", "extension_forbidden", "path_escapes_root", etc).
    """


def _resolve_tenant_file(user_id: str, rel_path: str, *, create_root: bool = False) -> Path:
    """Validate + resolve `rel_path` inside the tenant's data root.

    Raises TenantPathError on any of:
      - empty / non-string rel_path
      - rel_path containing nul bytes
      - rel_path with absolute prefix ('/...')
      - rel_path with traversal segments ('..') that would escape root
      - rel_path with > _MAX_PATH_DEPTH segments
      - extension not in _ALLOWED_EXTENSIONS
      - user_id unsanitisable (no resolvable tenant root)

    Returns the absolute resolved Path, NEVER outside the tenant root.
    """
    if not isinstance(rel_path, str) or not rel_path:
        raise TenantPathError("path_required")
    if "\x00" in rel_path:
        raise TenantPathError("path_invalid")
    # Normalise separators (a tenant could send "\" on Windows-style
    # input even if the server is Linux; treat both as separators).
    norm = rel_path.replace("\\", "/").strip()
    if not norm:
        raise TenantPathError("path_required")
    if norm.startswith("/"):
        raise TenantPathError("path_must_be_relative")

    # Split + reject any traversal segments before resolving. The
    # post-resolve check below is a second line of defence; do this
    # pre-check too so we don't even touch the filesystem for obvious
    # attacks.
    parts = [p for p in norm.split("/") if p]
    if any(p in ("", ".", "..") for p in parts):
        raise TenantPathError("path_traversal_forbidden")
    if len(parts) > _MAX_PATH_DEPTH:
        raise TenantPathError("path_too_deep")

    # Extension allowlist applies to the final segment only.
    final = parts[-1]
    suffix = Path(final).suffix.lower()
    if suffix not in _ALLOWED_EXTENSIONS:
        raise TenantPathError("extension_forbidden")

    root = tenant_paths.tenant_data_root(user_id, create=create_root)
    if root is None:
        raise TenantPathError("tenant_resolve_failed")

    # Build the candidate path + verify it stays under the tenant root
    # after path-resolution. Defence in depth against any sanitiser
    # gap (symlinks, alternate path-separator tricks, OS-specific
    # weirdness).
    candidate = (root / Path(*parts)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as e:
        raise TenantPathError("path_escapes_root") from e
    return candidate


def write_tenant_file(user_id: str, rel_path: str, content: bytes) -> int:
    """Atomically write `content` to `rel_path` inside the tenant's data root.

    - Creates the tenant root + intermediate directories with 0o700.
    - Enforces MAX_FILE_BYTES on `content`.
    - Writes to a sibling `.tmp` file then renames (atomic on POSIX).
    - File mode is 0o600 (gateway-process-owner readable only).

    Returns the number of bytes written. Raises TenantPathError on
    validation failure or OSError on filesystem failure.
    """
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise TenantPathError("content_must_be_bytes")
    if len(content) > MAX_FILE_BYTES:
        raise TenantPathError("content_too_large")
    target = _resolve_tenant_file(user_id, rel_path, create_root=True)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.with_name(target.name + ".tmp")
    # Use os.open so we can set the mode atomically (chmod-after-write
    # would race with a reader that opened between create + chmod).
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, bytes(content))
    finally:
        os.close(fd)
    os.replace(tmp, target)
    return len(content)


def read_tenant_file(user_id: str, rel_path: str) -> Optional[bytes]:
    """Read a tenant file, or None if it doesn't exist.

    Raises TenantPathError on validation failure. Other filesystem
    errors (PermissionError, IsADirectoryError) propagate — those
    indicate a bug or hostile filesystem state, not normal client
    input.
    """
    target = _resolve_tenant_file(user_id, rel_path, create_root=False)
    if not target.is_file():
        return None
    if target.stat().st_size > MAX_FILE_BYTES:
        # Defence in depth: even if a write somehow bypassed the cap,
        # don't echo the over-large content back to a client. Return
        # None and log — caller surfaces as "not found".
        logger.warning(
            "read_tenant_file refusing oversize file: user=%s path=%s size=%d",
            user_id, rel_path, target.stat().st_size,
        )
        return None
    return target.read_bytes()


def delete_tenant_file(user_id: str, rel_path: str) -> bool:
    """Delete a tenant file. Returns True if deleted, False if absent.

    Raises TenantPathError on validation failure.
    """
    target = _resolve_tenant_file(user_id, rel_path, create_root=False)
    if not target.exists():
        return False
    if target.is_dir():
        # We don't currently support tenant subdirs at the API level
        # (write creates them as a side effect of the file path).
        # Reject directory deletes outright — tenants shouldn't be
        # able to recursively rm their own dir tree via this API.
        raise TenantPathError("path_is_directory")
    target.unlink()
    return True


class TenantDataSummary(TypedDict):
    """What /tenant/data returns to a caller."""
    user_id: str
    data_root: str
    exists: bool
    files: list[str]
    dirs: list[str]
    total_size_bytes: int
    cap_bytes: Optional[int]


# Conservative cap on how many entries we'll enumerate / size-sum before
# bailing out. A tenant with 100k files shouldn't be able to make a
# single /tenant/data call stat() every one of them on every dashboard
# refresh. Returning truncated counts is honest enough for "how full is
# my dir" UX; the dashboard can surface "(more — refresh to scan)".
_MAX_ENTRIES_PER_SUMMARY = 1000


def describe_tenant_data(user_id: str, *, create: bool = False) -> Optional[TenantDataSummary]:
    """Read-only summary of a tenant's on-disk data.

    Returns None if `user_id` is unsanitisable (same failure mode as
    tenant_paths.tenant_data_root). Caller treats that as "unauthorised".

    When `create=False` (default) and the dir doesn't exist yet, returns
    a summary with exists=False and empty lists. This is the normal
    first-call shape — operators see "no data yet, you're brand new."
    When `create=True`, the dir is mkdir'd and an empty summary returned
    (used by /tenant/setup-style flows; Phase 0.3 doesn't ship one yet).
    """
    root = tenant_paths.tenant_data_root(user_id, create=create)
    if root is None:
        return None

    summary: TenantDataSummary = {
        "user_id": user_id,
        "data_root": str(root),
        "exists": root.exists(),
        "files": [],
        "dirs": [],
        "total_size_bytes": 0,
        "cap_bytes": None,
    }

    if not summary["exists"]:
        return summary

    files: list[str] = []
    dirs: list[str] = []
    total = 0
    count = 0
    try:
        for entry in sorted(root.rglob("*")):
            count += 1
            if count > _MAX_ENTRIES_PER_SUMMARY:
                break
            rel = entry.relative_to(root)
            rel_str = str(rel)
            if entry.is_file():
                files.append(rel_str)
                try:
                    total += entry.stat().st_size
                except OSError:
                    # Race: file existed in glob but vanished by stat.
                    # Treat as zero-size and continue. Not a fatal error.
                    pass
            elif entry.is_dir():
                dirs.append(rel_str)
    except (OSError, PermissionError) as e:
        # Don't blow up the response — return what we have so the caller
        # at least sees the root + the readability problem in the log.
        logger.warning("describe_tenant_data partial: %s", e)

    summary["files"] = files
    summary["dirs"] = dirs
    summary["total_size_bytes"] = total
    return summary


def describe_shared_data() -> dict:
    """Read-only summary of the legacy single-tenant `~/.delimit/` view.

    Used by the shared-bearer (founder/system) path on /tenant/data.
    Returns the same shape as describe_tenant_data minus `user_id`
    (there is no user_id for the shared-bearer caller — it's the
    founder/system).
    """
    # Reuse the same _MAX_ENTRIES_PER_SUMMARY cap. Founder's `~/.delimit/`
    # typically has hundreds of files (memory.jsonl, ledger.jsonl,
    # evidence/, daemon/, etc), so truncation is realistic.
    home = os.environ.get("DELIMIT_HOME")
    root = Path(home).expanduser().resolve() if home else (Path.home() / ".delimit")
    summary: dict = {
        "user_id": "",  # shared-bearer: no tenant scope
        "data_root": str(root),
        "exists": root.is_dir(),
        "files": [],
        "dirs": [],
        "total_size_bytes": 0,
        "cap_bytes": None,
    }
    if not summary["exists"]:
        return summary

    files: list[str] = []
    dirs: list[str] = []
    total = 0
    count = 0
    try:
        for entry in sorted(root.rglob("*")):
            # Skip the tenants/ subdir from the shared view — that's the
            # per-tenant tree, which the founder views via the dashboard's
            # tenant-list / admin surface, not as part of her own data.
            try:
                if entry.relative_to(root).parts[:1] == ("tenants",):
                    continue
            except ValueError:
                pass
            count += 1
            if count > _MAX_ENTRIES_PER_SUMMARY:
                break
            rel_str = str(entry.relative_to(root))
            if entry.is_file():
                files.append(rel_str)
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
            elif entry.is_dir():
                dirs.append(rel_str)
    except (OSError, PermissionError) as e:
        logger.warning("describe_shared_data partial: %s", e)

    summary["files"] = files
    summary["dirs"] = dirs
    summary["total_size_bytes"] = total
    return summary
