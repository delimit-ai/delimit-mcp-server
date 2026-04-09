"""
Continuity State Resolution (LED-240).

Resolves user identity, project, venture, and private state namespace
at startup so all tools bind to the correct local state under ~/.delimit/.

Design:
  - Single-user (root) today: namespace collapses to ~/.delimit/ (backwards compat)
  - Multi-user (future): each user gets ~/.delimit/users/{user_hash}/
  - Private state never leaks into git-tracked dirs, npm payloads, or public repos

Resolution chain:
  resolve_user()      -> whoami + git config + gh auth
  resolve_project()   -> git remote + .delimit/ dir + package.json/pyproject.toml
  resolve_venture()   -> map project to venture via ventures.json registry
  resolve_namespace() -> compute private state path
  auto_bind()         -> run all, set env vars for downstream tools
"""

import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("delimit.continuity")

DELIMIT_HOME = Path.home() / ".delimit"
VENTURES_FILE = DELIMIT_HOME / "ventures.json"

# Directories that hold private continuity state (must never be git-tracked or published)
PRIVATE_STATE_DIRS = [
    "souls",
    "handoff_receipts",
    "ledger",
    "evidence",
    "agent_actions",
    "events",
    "traces",
    "deliberations",
    "audit",
    "audits",
    "vault",
    "secrets",
    "credentials",
    "context",
    "continuity",
]


def _run_cmd(args: list, timeout: int = 5) -> str:
    """Run a command and return stdout, or empty string on failure."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return ""


def _stable_hash(value: str) -> str:
    """Deterministic short hash for namespace isolation."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


# ---- resolve_user -------------------------------------------------------

def resolve_user() -> Dict[str, str]:
    """Detect the current user from OS, git config, and GitHub auth.

    Returns a dict with:
      - os_user: system username (whoami)
      - git_email: git config user.email (may be empty)
      - git_name: git config user.name (may be empty)
      - gh_user: GitHub username from gh auth status (may be empty)
      - user_hash: stable hash derived from the best available identity
      - identity_source: which signal produced the hash
    """
    os_user = os.environ.get("USER", "") or _run_cmd(["whoami"])
    git_email = _run_cmd(["git", "config", "--global", "user.email"])
    git_name = _run_cmd(["git", "config", "--global", "user.name"])

    # gh auth status --active outputs the logged-in user
    gh_user = ""
    gh_raw = _run_cmd(["gh", "auth", "status"])
    if gh_raw:
        # Parse "Logged in to github.com account <user> ..."
        for line in gh_raw.splitlines():
            stripped = line.strip()
            if "account" in stripped.lower():
                parts = stripped.split()
                for i, tok in enumerate(parts):
                    if tok.lower() == "account" and i + 1 < len(parts):
                        candidate = parts[i + 1].strip("()")
                        if candidate and candidate not in ("as", "to"):
                            gh_user = candidate
                            break
                if gh_user:
                    break

    # Pick the strongest identity signal for hashing
    if gh_user:
        identity_key = f"gh:{gh_user}"
        identity_source = "github"
    elif git_email:
        identity_key = f"email:{git_email}"
        identity_source = "git_email"
    elif os_user:
        identity_key = f"os:{os_user}"
        identity_source = "os_user"
    else:
        identity_key = "unknown"
        identity_source = "none"

    return {
        "os_user": os_user,
        "git_email": git_email,
        "git_name": git_name,
        "gh_user": gh_user,
        "user_hash": _stable_hash(identity_key),
        "identity_source": identity_source,
    }


# ---- resolve_project -----------------------------------------------------

def resolve_project(project_path: str = ".") -> Dict[str, str]:
    """Detect the current project from the working directory.

    Returns a dict with:
      - path: resolved absolute path
      - name: project name (from package.json, pyproject.toml, or dir name)
      - repo_url: git remote origin URL (may be empty)
      - project_hash: stable hash of the resolved path
      - has_delimit_dir: whether .delimit/ exists in the project
      - project_type: node | python | unknown
    """
    p = Path(project_path).resolve()
    info: Dict[str, str] = {
        "path": str(p),
        "name": p.name,
        "repo_url": "",
        "project_hash": _stable_hash(str(p)),
        "has_delimit_dir": str((p / ".delimit").is_dir()),
        "project_type": "unknown",
    }

    # package.json
    pkg_file = p / "package.json"
    if pkg_file.exists():
        try:
            pkg = json.loads(pkg_file.read_text())
            info["name"] = pkg.get("name", p.name)
            info["project_type"] = "node"
        except Exception:
            pass

    # pyproject.toml
    pyproj = p / "pyproject.toml"
    if pyproj.exists() and info["project_type"] == "unknown":
        try:
            for line in pyproj.read_text().splitlines():
                if line.strip().startswith("name"):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        info["name"] = val
                        info["project_type"] = "python"
                        break
        except Exception:
            pass

    # git remote
    remote = _run_cmd(["git", "-C", str(p), "remote", "get-url", "origin"])
    if remote:
        info["repo_url"] = remote

    return info


# ---- resolve_venture -----------------------------------------------------

def resolve_venture(project_path: str = ".") -> Dict[str, str]:
    """Map a project to its venture using the global ventures registry.

    Returns a dict with:
      - venture_name: matched venture name (or "unregistered")
      - venture_match: how the match was made (exact | path | repo | none)
      - registered_ventures: count of ventures in registry
    """
    project = resolve_project(project_path)
    resolved_path = project["path"]
    repo_url = project["repo_url"]

    ventures = _load_ventures()
    result: Dict[str, str] = {
        "venture_name": "unregistered",
        "venture_match": "none",
        "registered_ventures": str(len(ventures)),
    }

    # Exact path match
    for name, vinfo in ventures.items():
        v_path = vinfo.get("path", "")
        if v_path and os.path.realpath(v_path) == os.path.realpath(resolved_path):
            result["venture_name"] = name
            result["venture_match"] = "exact"
            return result

    # Path-prefix match (project is a subdirectory of a venture)
    for name, vinfo in ventures.items():
        v_path = vinfo.get("path", "")
        if v_path:
            try:
                if Path(resolved_path).is_relative_to(Path(v_path).resolve()):
                    result["venture_name"] = name
                    result["venture_match"] = "path"
                    return result
            except (ValueError, TypeError):
                pass

    # Repo URL match
    if repo_url:
        normalized_repo = repo_url.rstrip("/").replace(".git", "").lower()
        for name, vinfo in ventures.items():
            v_repo = vinfo.get("repo", "").rstrip("/").replace(".git", "").lower()
            if v_repo and v_repo == normalized_repo:
                result["venture_name"] = name
                result["venture_match"] = "repo"
                return result

    return result


def _load_ventures() -> Dict[str, Any]:
    """Load the global ventures registry."""
    if not VENTURES_FILE.exists():
        return {}
    try:
        return json.loads(VENTURES_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


# ---- resolve_namespace ---------------------------------------------------

def resolve_namespace(
    user_hash: str = "",
    venture_name: str = "",
    project_hash: str = "",
) -> Dict[str, str]:
    """Compute the private state path for a user/venture/project combination.

    Namespace layout:
      Single-user (default): ~/.delimit/                  (backwards compat)
      Multi-user (future):   ~/.delimit/users/{user_hash}/

    Within a namespace, state is organized by type:
      souls/{project_hash}/
      handoff_receipts/{project_hash}/
      ledger/
      evidence/
      ...

    Returns a dict with:
      - namespace_root: the base path for this user's private state
      - is_multi_user: whether multi-user scoping is active
      - souls_dir: where soul captures go
      - receipts_dir: where handoff receipts go
      - ledger_dir: central ledger
      - evidence_dir: audit evidence
      - events_dir: event log
    """
    # Determine if we are in multi-user mode.
    # For now, multi-user activates only if DELIMIT_MULTI_USER=1 is set,
    # keeping backwards compat for the single-user (root) case.
    multi_user = os.environ.get("DELIMIT_MULTI_USER", "") == "1"

    if multi_user and user_hash:
        namespace_root = DELIMIT_HOME / "users" / user_hash
    else:
        namespace_root = DELIMIT_HOME

    result = {
        "namespace_root": str(namespace_root),
        "is_multi_user": str(multi_user),
        "souls_dir": str(namespace_root / "souls"),
        "receipts_dir": str(namespace_root / "handoff_receipts"),
        "ledger_dir": str(namespace_root / "ledger"),
        "evidence_dir": str(namespace_root / "evidence"),
        "events_dir": str(namespace_root / "events"),
        "traces_dir": str(namespace_root / "traces"),
        "agent_actions_dir": str(namespace_root / "agent_actions"),
    }

    # If project_hash is provided, the per-project subdirs get it
    if project_hash:
        result["souls_dir"] = str(namespace_root / "souls" / project_hash)
        result["receipts_dir"] = str(
            namespace_root / "handoff_receipts" / project_hash
        )

    return result


# ---- auto_bind -----------------------------------------------------------

def auto_bind(project_path: str = ".") -> Dict[str, Any]:
    """Run full resolution chain and set environment variables.

    This is the single entry point for startup. It:
    1. Resolves user identity
    2. Resolves current project
    3. Maps project to venture
    4. Computes the namespace
    5. Sets DELIMIT_* env vars so all downstream tools use the right paths
    6. Ensures the namespace directories exist

    Returns the full resolution context for logging/debugging.
    """
    user = resolve_user()
    project = resolve_project(project_path)
    venture = resolve_venture(project_path)
    namespace = resolve_namespace(
        user_hash=user["user_hash"],
        venture_name=venture["venture_name"],
        project_hash=project["project_hash"],
    )

    # Set env vars for downstream consumption
    _env_vars = {
        "DELIMIT_USER_HASH": user["user_hash"],
        "DELIMIT_USER_IDENTITY": user.get("gh_user") or user.get("git_email") or user.get("os_user", ""),
        "DELIMIT_PROJECT_PATH": project["path"],
        "DELIMIT_PROJECT_HASH": project["project_hash"],
        "DELIMIT_PROJECT_NAME": project["name"],
        "DELIMIT_VENTURE": venture["venture_name"],
        "DELIMIT_NAMESPACE_ROOT": namespace["namespace_root"],
        "DELIMIT_LEDGER_DIR": namespace["ledger_dir"],
        "DELIMIT_SOULS_DIR": namespace["souls_dir"],
        "DELIMIT_EVIDENCE_DIR": namespace["evidence_dir"],
    }

    for key, value in _env_vars.items():
        os.environ[key] = value

    # Ensure critical namespace directories exist
    for dir_key in ("namespace_root", "souls_dir", "receipts_dir", "ledger_dir",
                    "evidence_dir", "events_dir", "traces_dir", "agent_actions_dir"):
        Path(namespace[dir_key]).mkdir(parents=True, exist_ok=True)

    # Verify private state is not inside a git worktree that would be committed
    leak_warnings = _check_for_leaks(namespace["namespace_root"])

    context = {
        "user": user,
        "project": project,
        "venture": venture,
        "namespace": namespace,
        "env_vars_set": list(_env_vars.keys()),
        "leak_warnings": leak_warnings,
    }

    logger.info(
        "Continuity bound: user=%s project=%s venture=%s namespace=%s",
        user.get("gh_user") or user.get("os_user", "?"),
        project["name"],
        venture["venture_name"],
        namespace["namespace_root"],
    )

    return context


# ---- Safety checks -------------------------------------------------------

def _check_for_leaks(namespace_root: str) -> list:
    """Check that the namespace root is not inside a git worktree or npm package.

    Returns a list of warning strings (empty if clean).
    """
    warnings = []
    ns_path = Path(namespace_root).resolve()

    # Check 1: namespace should be under ~/.delimit/ (home directory)
    home = Path.home().resolve()
    expected_base = home / ".delimit"
    if not str(ns_path).startswith(str(expected_base)):
        warnings.append(
            f"Namespace root {ns_path} is outside ~/.delimit/ -- state may leak"
        )

    # Check 2: namespace should not be inside a git worktree
    git_root = _run_cmd(["git", "-C", str(ns_path), "rev-parse", "--show-toplevel"])
    if git_root:
        # If the git root is the home dir itself, that is fine (some users have ~/ as a repo)
        # But if it is a project repo, that is a problem.
        git_root_path = Path(git_root).resolve()
        if git_root_path != home and str(ns_path).startswith(str(git_root_path)):
            warnings.append(
                f"Namespace root {ns_path} is inside git worktree {git_root_path} "
                "-- private state could be committed. Add to .gitignore."
            )

    # Check 3: verify .gitignore coverage in the gateway repo
    gateway_gitignore = Path("/home/delimit/delimit-gateway/.gitignore")
    if gateway_gitignore.exists():
        content = gateway_gitignore.read_text()
        if ".delimit/" not in content and ".delimit/ledger/" not in content:
            warnings.append(
                "Gateway .gitignore does not exclude .delimit/ -- state may be committed"
            )

    return warnings


def verify_npm_exclusion() -> Dict[str, Any]:
    """Verify that private state directories are excluded from npm publish.

    Checks .npmignore in the npm package for coverage of state dirs.
    Returns a report.
    """
    npmignore = Path("/home/delimit/npm-delimit/.npmignore")
    result: Dict[str, Any] = {
        "npmignore_exists": npmignore.exists(),
        "covered": [],
        "missing": [],
    }

    if not npmignore.exists():
        result["missing"] = PRIVATE_STATE_DIRS[:]
        return result

    content = npmignore.read_text()
    for dirname in PRIVATE_STATE_DIRS:
        # Check if the dir or a pattern covering it appears in .npmignore
        if dirname in content or ".delimit" in content or f"**/{dirname}" in content:
            result["covered"].append(dirname)
        else:
            result["missing"].append(dirname)

    return result


# ---- Convenience for other modules ---------------------------------------

def get_namespace_root() -> Path:
    """Return the current namespace root from env or default.

    Other modules can call this instead of hardcoding Path.home() / ".delimit".
    """
    env_root = os.environ.get("DELIMIT_NAMESPACE_ROOT", "")
    if env_root:
        return Path(env_root)
    return DELIMIT_HOME
