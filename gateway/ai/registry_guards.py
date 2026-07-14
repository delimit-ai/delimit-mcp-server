"""Reusable registry write-guards (LED-3733 prevention-first).

Two small, dependency-free guards that any store which persists
user/venture/agent records to a shared ~/.delimit/*.json file can reuse
BEFORE it writes:

  strip_url_userinfo(url)  -> drop embedded `user:token@` credentials from
                              http(s) remote URLs so PATs never land in a
                              registry, a session read, or a transcript.
  is_ephemeral_path(path)  -> True for /tmp, tempfile roots, and pytest
                              scratch trees, so test-suite fixtures cannot
                              auto-register junk "ventures" into the real
                              registry.

Why a shared module: the same leak class (embedded credentials + pytest
tmp pollution) affects more than the ledger venture registry — the swarm
venture registry (~/.delimit/swarm/ventures.json), and later the souls
and agents/tasks.json stores. Centralizing the logic here means every
polluted store applies the identical, tested guard instead of a
copy-pasted variant that can drift.

Design notes:
  * Fails safe. On any parse issue both helpers return the least
    surprising thing (input unchanged / not-ephemeral) rather than raise.
  * Pure functions, no I/O, no side effects — trivially reusable + tested.
  * Backward compatible: no storage-format change; guards only ever remove
    a secret from a URL or refuse an ephemeral write.
"""

import re
import tempfile

__all__ = ["strip_url_userinfo", "is_ephemeral_path"]


def strip_url_userinfo(url: str) -> str:
    """Remove embedded credentials (``user:token@``) from a remote URL.

    GitHub PAT-embedded https remotes (e.g.
    ``https://user:ghp_XXX@github.com/org/repo.git``) leak the token into
    the registry, into every session that reads it, and into transcripts.
    This strips the ``user:pass@`` userinfo component from http(s) URLs so
    only ``https://host/path`` is persisted. Non-http URLs (scp-style
    ``git@github.com:org/repo.git`` SSH remotes) carry no secret and are
    returned unchanged. Fails safe: on any parse issue, returns the input.
    """
    if not url:
        return url
    m = re.match(r"^(https?://)[^/@]*@(.*)$", url)
    if m:
        return m.group(1) + m.group(2)
    return url


def is_ephemeral_path(path: str) -> bool:
    """True for auto-register-forbidden transient paths.

    Rejects anything under ``/tmp`` (including bare ``/tmp``), the system
    tempfile root (which may differ from ``/tmp``), and pytest temp trees
    (``pytest-of-*``, ``tmp*`` tempfile dirs, ``test_*`` fixtures). These
    are test-suite scratch dirs that historically leaked ~250 garbage
    ventures into the shared registry (STR-2169 / LED-3733).
    """
    if not path:
        return False
    p = path.rstrip("/")
    if p == "/tmp" or p.startswith("/tmp/"):
        return True
    # System temp root from tempfile.gettempdir() may differ from /tmp.
    tmproot = tempfile.gettempdir().rstrip("/")
    if tmproot and (p == tmproot or p.startswith(tmproot + "/")):
        return True
    # pytest / tempfile signature segments anywhere in the path.
    if re.search(r"(^|/)(pytest-of-[^/]+|tmp[0-9a-z_]{4,}|test_[^/]+)(/|$)", p):
        return True
    return False
