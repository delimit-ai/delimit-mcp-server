"""Remote-input resolution helpers for MCP tools (LED-1237).

Tools like delimit_repo_analyze and delimit_lint historically only
accepted local filesystem paths. When a multi-model deliberation panel
emits ``[TOOL: delimit_repo_analyze target="calcom/cal.com"]`` the
target was resolved against the cwd and silently returned an empty
analysis (total_files=0).

This module adds **additive** remote-input handling:

* ``resolve_repo_target(target)`` — context manager that accepts either
  a local path, a ``<owner>/<repo>`` shorthand, or a full
  https/ssh GitHub URL, and yields ``(local_path, metadata)``.

* ``resolve_spec_input(spec)`` — context manager that accepts either a
  local path or an http(s) URL, fetches the URL into a tempfile with
  the right extension, and yields ``(local_path, metadata)``.

Both helpers are no-ops on local input (passthrough). All cleanup is
handled automatically. Network/clone failures raise
``RemoteResolveError`` which callers should convert into a clean
``{"error": "...", ...}`` response.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("delimit.ai.remote_resolve")

# ─── Constants ───────────────────────────────────────────────────────────

GIT_CLONE_TIMEOUT_S = 120
HTTP_FETCH_TIMEOUT_S = 60
HTTP_FETCH_MAX_BYTES = 10 * 1024 * 1024  # 10 MB

# <owner>/<repo> regex: each segment is a non-empty token without
# whitespace, slashes (other than the single separator), or path
# traversal. We deliberately keep this conservative — anything fancy
# falls back to local-path semantics.
_OWNER_REPO_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9][A-Za-z0-9._-]*)$")

_GITHUB_URL_PREFIXES = (
    "https://github.com/",
    "http://github.com/",
    "git@github.com:",
    "ssh://git@github.com/",
)


# ─── Exceptions ──────────────────────────────────────────────────────────


class RemoteResolveError(Exception):
    """Raised when a remote target cannot be resolved.

    Carries an ``error`` code and a human-readable detail. Callers
    should translate this into a structured response dict — never let
    it bubble up as a stack trace into an MCP response.
    """

    def __init__(self, error: str, detail: str, target: Optional[str] = None) -> None:
        super().__init__(detail)
        self.error = error
        self.detail = detail
        self.target = target

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"error": self.error, "detail": self.detail}
        if self.target is not None:
            out["target"] = self.target
        return out


# ─── Repo target classification ──────────────────────────────────────────


def _is_github_url(target: str) -> bool:
    return any(target.startswith(p) for p in _GITHUB_URL_PREFIXES)


def _normalize_github_url(target: str) -> str:
    """Convert any accepted GitHub URL form to an https clone URL.

    Handles ``git@github.com:owner/repo(.git)`` and
    ``ssh://git@github.com/owner/repo(.git)`` plus pre-formed
    ``https://github.com/owner/repo``.
    """
    if target.startswith("git@github.com:"):
        path = target[len("git@github.com:"):]
        return "https://github.com/" + path.rstrip("/")
    if target.startswith("ssh://git@github.com/"):
        path = target[len("ssh://git@github.com/"):]
        return "https://github.com/" + path.rstrip("/")
    if target.startswith("http://github.com/"):
        return "https://" + target[len("http://"):]
    return target.rstrip("/")


def _looks_like_owner_repo(target: str) -> bool:
    """Detect ``owner/repo`` shorthand.

    Required:
    * exactly one '/' separator
    * both halves non-empty and match a conservative slug pattern
    * no path traversal ('..'); does not start with '/' or '.'
    * the literal ``./<owner>/<repo>`` does not exist on disk
      (backwards-compat guard so a real local subdir wins)
    """
    if not target or target.startswith(("/", ".")):
        return False
    if ".." in target.split("/"):
        return False
    if not _OWNER_REPO_RE.match(target):
        return False
    if Path(target).exists():
        # A real local dir/file with this name exists — treat as local.
        return False
    return True


def classify_repo_target(target: str) -> str:
    """Return one of: ``"github_url"``, ``"owner_repo"``, ``"local"``.

    Pure function, no I/O beyond a single ``Path.exists`` check for the
    backwards-compat guard. Exposed for tests.
    """
    if _is_github_url(target):
        return "github_url"
    if _looks_like_owner_repo(target):
        return "owner_repo"
    return "local"


# ─── Git clone ───────────────────────────────────────────────────────────


def _git_clone_shallow(url: str, dest: str) -> None:
    """Run ``git clone --depth 1 <url> <dest>``.

    Raises ``RemoteResolveError`` on failure (non-zero exit, timeout, or
    git binary missing).
    """
    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", url, dest],
            capture_output=True,
            text=True,
            timeout=GIT_CLONE_TIMEOUT_S,
        )
    except FileNotFoundError as e:
        raise RemoteResolveError(
            "clone_failed",
            f"git binary not found: {e}",
            target=url,
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RemoteResolveError(
            "clone_failed",
            f"git clone timed out after {GIT_CLONE_TIMEOUT_S}s",
            target=url,
        ) from e
    except Exception as e:  # pragma: no cover - defensive
        raise RemoteResolveError(
            "clone_failed",
            f"git clone failed: {e}",
            target=url,
        ) from e

    if proc.returncode != 0:
        # stderr tends to carry the useful "Repository not found" /
        # "Authentication failed" message. Trim it so we don't dump
        # huge output into MCP responses.
        stderr = (proc.stderr or "").strip()
        if len(stderr) > 500:
            stderr = stderr[:500] + "..."
        raise RemoteResolveError(
            "clone_failed",
            stderr or f"git clone exited with code {proc.returncode}",
            target=url,
        )


@contextlib.contextmanager
def resolve_repo_target(target: str) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Resolve a repo target to a local path.

    Yields ``(path, metadata)``. Metadata always includes
    ``resolved_from`` (one of ``"local"``, ``"remote_clone"``) and may
    include ``upstream_url`` for remote clones.

    Cleanup of any temporary clone happens automatically when the
    ``with`` block exits, even on exception inside the block.

    Raises ``RemoteResolveError`` if the input cannot be resolved
    (clone failure, malformed URL). Local-path targets always succeed
    here — the existence check stays the responsibility of the
    downstream backend so that ``analyze("nonexistent/")`` keeps its
    pre-existing behavior.
    """
    kind = classify_repo_target(target)

    if kind == "local":
        yield target, {"resolved_from": "local"}
        return

    # Remote: normalize to an https URL.
    if kind == "github_url":
        url = _normalize_github_url(target)
    else:  # owner_repo
        url = f"https://github.com/{target}"

    tmp = tempfile.mkdtemp(prefix="delimit-repo-")
    # mkdtemp creates the dir; git clone refuses to clone into an
    # existing non-empty dir, so we point it at a child path.
    clone_dir = os.path.join(tmp, "repo")
    try:
        _git_clone_shallow(url, clone_dir)
        meta: Dict[str, Any] = {
            "resolved_from": "remote_clone",
            "upstream_url": url,
        }
        yield clone_dir, meta
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── Spec (URL) input resolution ─────────────────────────────────────────


_ALLOWED_SPEC_SCHEMES = ("http", "https")


def _classify_spec_input(spec: str) -> str:
    """Return ``"url"`` or ``"local"``.

    Anything starting with ``http://`` or ``https://`` is a URL.
    Everything else is a local path. Unknown schemes (file://, ftp://,
    javascript:) are rejected up front — but only if they actually look
    like a URL, otherwise a local path beginning with ``foo:`` would
    misclassify on Windows-style drives etc. We're strict: any
    ``<scheme>://`` prefix that isn't http(s) is rejected.
    """
    if spec.startswith(("http://", "https://")):
        return "url"
    # Reject other URL-ish schemes outright.
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+\-.]*)://", spec)
    if m:
        raise RemoteResolveError(
            "invalid_scheme",
            f"only http(s) URLs are accepted; got scheme '{m.group(1)}'",
            target=spec,
        )
    # javascript: / data: — no '//' after the colon, treat as local
    # path candidate (will fail downstream if invalid).
    return "local"


def _ensure_public_host(url: str) -> None:
    """SSRF guard: reject URLs that resolve to private/loopback IPs.

    Resolves the hostname and checks the resulting IP against the
    standard private/loopback/link-local/reserved ranges. Raises
    ``RemoteResolveError`` on any disallowed host.
    """
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise RemoteResolveError(
            "invalid_url",
            "URL is missing a hostname",
            target=url,
        )
    try:
        ip_str = socket.gethostbyname(host)
    except OSError as e:
        raise RemoteResolveError(
            "fetch_failed",
            f"DNS resolution failed for {host}: {e}",
            target=url,
        ) from e
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError as e:  # pragma: no cover - defensive
        raise RemoteResolveError(
            "fetch_failed",
            f"could not parse resolved IP {ip_str}: {e}",
            target=url,
        ) from e
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise RemoteResolveError(
            "blocked_host",
            f"refusing to fetch from non-public host {host} ({ip_str})",
            target=url,
        )


def _spec_extension_for(url: str, content_type: str) -> str:
    """Pick a tempfile extension based on URL or response Content-Type."""
    parsed = urlparse(url)
    path = parsed.path.lower()
    for ext in (".yaml", ".yml", ".json"):
        if path.endswith(ext):
            return ext
    ct = (content_type or "").lower()
    if "yaml" in ct or "yml" in ct:
        return ".yaml"
    if "json" in ct:
        return ".json"
    # Fall back: many OpenAPI specs are served with text/plain — use
    # .json because the loader auto-detects, and json parsers fail
    # fast and clean on yaml.
    return ".json"


def _http_fetch(url: str) -> Tuple[bytes, str]:
    """Fetch ``url`` with size/timeout caps. Returns (body, content_type).

    Raises ``RemoteResolveError`` on any failure (network, timeout,
    oversize body, non-2xx status).
    """
    # Lazy import so test envs without urllib quirks don't choke at
    # module import time.
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    req = Request(url, headers={"User-Agent": "delimit-gateway-remote-resolve/1"})
    try:
        with urlopen(req, timeout=HTTP_FETCH_TIMEOUT_S) as resp:
            content_type = resp.headers.get("Content-Type", "")
            # Read up to MAX+1 to detect oversize.
            body = resp.read(HTTP_FETCH_MAX_BYTES + 1)
    except HTTPError as e:
        raise RemoteResolveError(
            "fetch_failed",
            f"HTTP {e.code}: {e.reason}",
            target=url,
        ) from e
    except URLError as e:
        raise RemoteResolveError(
            "fetch_failed",
            f"network error: {e.reason}",
            target=url,
        ) from e
    except Exception as e:
        raise RemoteResolveError(
            "fetch_failed",
            f"fetch failed: {e}",
            target=url,
        ) from e

    if len(body) > HTTP_FETCH_MAX_BYTES:
        raise RemoteResolveError(
            "fetch_failed",
            f"response body exceeds {HTTP_FETCH_MAX_BYTES} bytes",
            target=url,
        )
    return body, content_type


@contextlib.contextmanager
def resolve_spec_input(spec: str) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Resolve a spec input (local path or URL) to a local path.

    Yields ``(path, metadata)``. Metadata always includes
    ``resolved_from`` (``"local"`` or ``"url"``) and, for URLs,
    ``upstream_url`` and ``content_type``.

    Tempfiles are cleaned up automatically when the ``with`` block
    exits.
    """
    kind = _classify_spec_input(spec)

    if kind == "local":
        yield spec, {"resolved_from": "local"}
        return

    # URL path: SSRF guard, fetch, write to tempfile.
    _ensure_public_host(spec)
    body, content_type = _http_fetch(spec)
    ext = _spec_extension_for(spec, content_type)
    fd, tmp_path = tempfile.mkstemp(prefix="delimit-spec-", suffix=ext)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        meta: Dict[str, Any] = {
            "resolved_from": "url",
            "upstream_url": spec,
            "content_type": content_type,
        }
        yield tmp_path, meta
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


__all__ = [
    "RemoteResolveError",
    "classify_repo_target",
    "resolve_repo_target",
    "resolve_spec_input",
]
