"""Swarm shared infrastructure — auth, logging, namespace isolation, directory jailing.

LED-274: Phase 1 shared infrastructure for multi-agent orchestration.
Provides enforcement primitives that the swarm dispatch system can wire into
agent operations. All classes are lightweight and stateless where possible;
persistent state lives in ~/.delimit/swarm/ alongside the existing registry.

Design:
  - SwarmAuth: venture-scoped token issue/validate for agent identity
  - SwarmLogger: centralized structured logging with venture+agent context
  - VentureNamespace: resolve venture paths and enforce boundaries
  - DirectoryJail: hard enforcement — raises on out-of-scope writes
  - SharedLibs: read-only access registry for common utilities
"""

import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# Re-use the swarm directory for shared state
SWARM_DIR = Path.home() / ".delimit" / "swarm"
AUTH_DIR = SWARM_DIR / "auth"
INFRA_LOG = SWARM_DIR / "infra_log.jsonl"


# =========================================================================
#  SwarmAuth — venture-scoped tokens for agent identity
# =========================================================================

class SwarmAuth:
    """Issue and validate HMAC-based tokens scoped to a venture+agent pair.

    Tokens are short-lived (default 1 hour) and tied to a specific venture
    namespace. The signing secret is derived per-venture so a token from
    venture A cannot be used in venture B.

    Token format (hex):  hmac_hex : issued_ts : venture : agent_id
    """

    TOKEN_FILE = AUTH_DIR / "tokens.json"
    # Default to 1 hour; caller can override
    DEFAULT_TTL = 3600

    def __init__(self, master_secret: str = ""):
        """Initialize with a master secret. Falls back to a machine-derived key."""
        self._master = master_secret or self._derive_machine_secret()
        AUTH_DIR.mkdir(parents=True, exist_ok=True)

    # -- public API -------------------------------------------------------

    def issue_token(
        self,
        venture: str,
        agent_id: str,
        ttl: int = 0,
    ) -> Dict[str, Any]:
        """Issue a venture-scoped token for an agent."""
        if not venture or not agent_id:
            return {"error": "venture and agent_id are required"}

        ttl = ttl or self.DEFAULT_TTL
        issued = int(time.time())
        expires = issued + ttl
        payload = f"{venture}:{agent_id}:{issued}:{expires}"
        sig = self._sign(venture, payload)

        token = f"{sig}:{issued}:{expires}:{venture}:{agent_id}"

        # Persist for revocation checks
        self._store_token(token, venture, agent_id, expires)

        return {
            "token": token,
            "venture": venture,
            "agent_id": agent_id,
            "issued_at": issued,
            "expires_at": expires,
            "ttl_seconds": ttl,
        }

    def validate_token(self, token: str) -> Dict[str, Any]:
        """Validate a token. Returns agent identity or error."""
        parts = token.split(":")
        if len(parts) != 5:
            return {"valid": False, "error": "Malformed token"}

        sig, issued_str, expires_str, venture, agent_id = parts

        try:
            issued = int(issued_str)
            expires = int(expires_str)
        except ValueError:
            return {"valid": False, "error": "Invalid timestamp in token"}

        # Expiry check
        now = int(time.time())
        if now > expires:
            return {
                "valid": False,
                "error": "Token expired",
                "expired_at": expires,
                "venture": venture,
                "agent_id": agent_id,
            }

        # Signature check
        payload = f"{venture}:{agent_id}:{issued}:{expires}"
        expected_sig = self._sign(venture, payload)
        if not hmac.compare_digest(sig, expected_sig):
            return {"valid": False, "error": "Invalid signature"}

        # Revocation check
        if self._is_revoked(token):
            return {"valid": False, "error": "Token has been revoked"}

        return {
            "valid": True,
            "venture": venture,
            "agent_id": agent_id,
            "issued_at": issued,
            "expires_at": expires,
            "remaining_seconds": expires - now,
        }

    def revoke_token(self, token: str) -> Dict[str, Any]:
        """Revoke a token so it can no longer be used."""
        tokens = self._load_tokens()
        token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
        if token_hash in tokens:
            tokens[token_hash]["revoked"] = True
            tokens[token_hash]["revoked_at"] = int(time.time())
            self._save_tokens(tokens)
            return {"status": "revoked", "token_hash": token_hash}
        return {"status": "not_found", "token_hash": token_hash}

    # -- internals --------------------------------------------------------

    @staticmethod
    def _derive_machine_secret() -> str:
        """Derive a stable secret from machine identity (hostname + uid)."""
        identity = f"{os.uname().nodename}:{os.getuid()}:delimit-swarm-v1"
        return hashlib.sha256(identity.encode()).hexdigest()

    def _venture_key(self, venture: str) -> bytes:
        """Derive a per-venture signing key from the master secret."""
        return hashlib.sha256(f"{self._master}:{venture}".encode()).digest()

    def _sign(self, venture: str, payload: str) -> str:
        key = self._venture_key(venture)
        return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:32]

    def _load_tokens(self) -> Dict[str, Any]:
        if not self.TOKEN_FILE.exists():
            return {}
        try:
            return json.loads(self.TOKEN_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_tokens(self, tokens: Dict[str, Any]):
        self.TOKEN_FILE.write_text(json.dumps(tokens, indent=2))

    def _store_token(self, token: str, venture: str, agent_id: str, expires: int):
        tokens = self._load_tokens()
        token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
        tokens[token_hash] = {
            "venture": venture,
            "agent_id": agent_id,
            "expires": expires,
            "revoked": False,
        }
        # Prune expired tokens while we are here
        now = int(time.time())
        tokens = {k: v for k, v in tokens.items() if v.get("expires", 0) > now or v.get("revoked")}
        self._save_tokens(tokens)

    def _is_revoked(self, token: str) -> bool:
        tokens = self._load_tokens()
        token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
        entry = tokens.get(token_hash, {})
        return entry.get("revoked", False)


# =========================================================================
#  SwarmLogger — centralized structured logging with venture/agent context
# =========================================================================

class SwarmLogger:
    """Structured JSON-line logger with venture and agent context tags.

    All log entries go to a shared JSONL file that the governor can query.
    Also integrates with Python's logging module for standard stderr output.
    """

    def __init__(self, log_file: Optional[Path] = None):
        self._log_file = log_file or INFRA_LOG
        SWARM_DIR.mkdir(parents=True, exist_ok=True)
        self._py_logger = logging.getLogger("delimit.swarm")
        if not self._py_logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(
                "[%(asctime)s] %(levelname)s %(name)s — %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            ))
            self._py_logger.addHandler(handler)
            self._py_logger.setLevel(logging.DEBUG)

    def log(
        self,
        level: str,
        message: str,
        venture: str = "",
        agent_id: str = "",
        action: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Write a structured log entry."""
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "epoch": time.time(),
            "level": level.upper(),
            "message": message,
            "venture": venture,
            "agent_id": agent_id,
            "action": action,
        }
        if extra:
            entry["extra"] = extra

        # Write to JSONL
        try:
            with open(self._log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass

        # Mirror to Python logging
        py_level = getattr(logging, level.upper(), logging.INFO)
        ctx = f"[{venture}/{agent_id}]" if venture else ""
        self._py_logger.log(py_level, f"{ctx} {message}")

        return entry

    def info(self, message: str, **kwargs) -> Dict[str, Any]:
        return self.log("INFO", message, **kwargs)

    def warn(self, message: str, **kwargs) -> Dict[str, Any]:
        return self.log("WARNING", message, **kwargs)

    def error(self, message: str, **kwargs) -> Dict[str, Any]:
        return self.log("ERROR", message, **kwargs)

    def audit(self, message: str, **kwargs) -> Dict[str, Any]:
        """Audit-level log entry (always persisted, never filtered)."""
        return self.log("AUDIT", message, **kwargs)

    def query(
        self,
        venture: str = "",
        agent_id: str = "",
        level: str = "",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Query recent log entries with optional filters."""
        if not self._log_file.exists():
            return []

        entries: List[Dict[str, Any]] = []
        try:
            with open(self._log_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if venture and entry.get("venture") != venture:
                        continue
                    if agent_id and entry.get("agent_id") != agent_id:
                        continue
                    if level and entry.get("level") != level.upper():
                        continue
                    entries.append(entry)
        except OSError:
            pass

        # Return most recent first, capped at limit
        return entries[-limit:][::-1]


# =========================================================================
#  VentureNamespace — resolve venture paths and enforce boundaries
# =========================================================================

class VentureNamespace:
    """Resolve venture-specific paths and enforce namespace boundaries.

    Each venture gets an isolated directory tree under ~/.delimit/ventures/<ns>/
    plus its registered repo_path. Agents can only operate within their
    venture's resolved paths.
    """

    VENTURES_DIR = Path.home() / ".delimit" / "ventures"
    VENTURES_FILE = SWARM_DIR / "ventures.json"

    def __init__(self):
        self.VENTURES_DIR.mkdir(parents=True, exist_ok=True)

    def resolve(self, venture: str) -> Dict[str, Any]:
        """Resolve all paths for a venture namespace."""
        if not venture:
            return {"error": "venture name is required"}

        ns = venture.strip().lower().replace(" ", "_").replace("-", "_")
        venture_root = self.VENTURES_DIR / ns
        venture_root.mkdir(parents=True, exist_ok=True)

        # Also check for a registered repo_path
        repo_path = self._get_repo_path(venture)

        return {
            "venture": venture,
            "namespace": ns,
            "venture_root": str(venture_root),
            "repo_path": repo_path,
            "data_dir": str(venture_root / "data"),
            "logs_dir": str(venture_root / "logs"),
            "tmp_dir": str(venture_root / "tmp"),
            "allowed_roots": self._allowed_roots(ns, repo_path),
        }

    def is_within_namespace(self, venture: str, target_path: str) -> bool:
        """Check if a path falls within the venture's namespace."""
        info = self.resolve(venture)
        if "error" in info:
            return False

        target = Path(target_path).resolve()
        for root in info["allowed_roots"]:
            if str(target).startswith(root):
                return True
        return False

    def _get_repo_path(self, venture: str) -> str:
        """Look up the repo_path from the swarm ventures registry."""
        if not self.VENTURES_FILE.exists():
            return ""
        try:
            ventures = json.loads(self.VENTURES_FILE.read_text())
            name = venture.strip().lower()
            return ventures.get(name, {}).get("repo_path", "")
        except (json.JSONDecodeError, OSError):
            return ""

    def _allowed_roots(self, ns: str, repo_path: str) -> List[str]:
        """Build the list of allowed root paths for a namespace."""
        roots = [str(self.VENTURES_DIR / ns)]
        if repo_path:
            roots.append(str(Path(repo_path).resolve()))
        return roots


# =========================================================================
#  DirectoryJail — enforce that agents only write to their venture dirs
# =========================================================================

class DirectoryJailViolation(Exception):
    """Raised when an agent attempts to write outside its venture scope."""
    pass


class DirectoryJail:
    """Enforce write-path restrictions for agents.

    Validates every write path against the venture's allowed roots.
    Raises DirectoryJailViolation on any violation -- this is hard
    enforcement, not advisory logging.
    """

    def __init__(self, namespace: Optional[VentureNamespace] = None):
        self._ns = namespace or VentureNamespace()
        self._logger = SwarmLogger()

    def check_write(self, venture: str, agent_id: str, target_path: str) -> bool:
        """Validate a write path. Returns True if allowed, raises on violation."""
        resolved = Path(target_path).resolve()

        # Block obvious escapes
        if ".." in str(target_path):
            self._raise_violation(venture, agent_id, target_path, "Path contains '..' traversal")

        # Check namespace membership
        if not self._ns.is_within_namespace(venture, str(resolved)):
            self._raise_violation(
                venture,
                agent_id,
                target_path,
                f"Path is outside venture '{venture}' namespace",
            )

        # Block sensitive system paths regardless of namespace
        blocked_prefixes = ["/etc", "/usr", "/bin", "/sbin", "/boot", "/proc", "/sys"]
        for prefix in blocked_prefixes:
            if str(resolved).startswith(prefix):
                self._raise_violation(
                    venture,
                    agent_id,
                    target_path,
                    f"Writes to system path '{prefix}' are always blocked",
                )

        self._logger.info(
            f"Write allowed: {target_path}",
            venture=venture,
            agent_id=agent_id,
            action="jail_check_pass",
        )
        return True

    def check_read(self, venture: str, agent_id: str, target_path: str) -> bool:
        """Validate a read path. More permissive than writes -- allows shared libs."""
        resolved = Path(target_path).resolve()

        # Reads within namespace are always fine
        if self._ns.is_within_namespace(venture, str(resolved)):
            return True

        # Reads from shared libs are fine (handled by SharedLibs)
        # Reads from /tmp are fine
        if str(resolved).startswith("/tmp"):
            return True

        self._logger.warn(
            f"Read outside namespace: {target_path}",
            venture=venture,
            agent_id=agent_id,
            action="jail_read_warning",
        )
        # Reads are advisory warnings, not hard blocks
        return True

    def _raise_violation(self, venture: str, agent_id: str, path: str, reason: str):
        self._logger.error(
            f"JAIL VIOLATION: {reason} (path={path})",
            venture=venture,
            agent_id=agent_id,
            action="jail_violation",
            extra={"path": path, "reason": reason},
        )
        raise DirectoryJailViolation(
            f"Agent '{agent_id}' (venture: {venture}) blocked: {reason}. Path: {path}"
        )


# =========================================================================
#  SharedLibs — read-only access to common utilities
# =========================================================================

class SharedLibs:
    """Registry of shared libraries that agents can read but not modify.

    The governor controls which paths are exposed as shared libs.
    Agents get read-only access; any write attempt through the jail
    will be blocked.
    """

    SHARED_LIBS_FILE = SWARM_DIR / "shared_libs.json"

    # Default shared lib paths that all ventures can read
    DEFAULT_LIBS: List[Dict[str, str]] = [
        {
            "name": "delimit_core",
            "path": str(Path.home() / ".delimit" / "server"),
            "description": "Core Delimit MCP server (read-only)",
        },
        {
            "name": "governance_policies",
            "path": str(Path.home() / ".delimit" / "governance"),
            "description": "Governance policy definitions",
        },
        {
            "name": "shared_schemas",
            "path": str(Path.home() / ".delimit" / "schemas"),
            "description": "Shared data schemas across ventures",
        },
    ]

    def __init__(self):
        SWARM_DIR.mkdir(parents=True, exist_ok=True)

    def register_lib(
        self,
        name: str,
        path: str,
        description: str = "",
        ventures: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Register a shared library path. Only the governor should call this."""
        if not name or not path:
            return {"error": "name and path are required"}

        resolved = str(Path(path).resolve())
        libs = self._load_libs()
        libs[name] = {
            "name": name,
            "path": resolved,
            "description": description,
            "ventures": ventures or ["*"],  # "*" means all ventures
            "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        self._save_libs(libs)

        return {"status": "registered", "name": name, "path": resolved}

    def unregister_lib(self, name: str) -> Dict[str, Any]:
        """Remove a shared library from the registry."""
        libs = self._load_libs()
        if name not in libs:
            return {"status": "not_found", "name": name}
        del libs[name]
        self._save_libs(libs)
        return {"status": "removed", "name": name}

    def list_libs(self, venture: str = "") -> List[Dict[str, str]]:
        """List available shared libraries, optionally filtered by venture."""
        libs = self._load_libs()
        result = []
        for lib in libs.values():
            allowed = lib.get("ventures", ["*"])
            if venture and "*" not in allowed and venture not in allowed:
                continue
            result.append({
                "name": lib["name"],
                "path": lib["path"],
                "description": lib.get("description", ""),
            })
        return result

    def can_access(self, venture: str, path: str) -> bool:
        """Check if a venture can access a path via shared libs."""
        resolved = str(Path(path).resolve())
        libs = self._load_libs()
        for lib in libs.values():
            allowed = lib.get("ventures", ["*"])
            if "*" not in allowed and venture not in allowed:
                continue
            if resolved.startswith(lib["path"]):
                return True
        return False

    def get_lib_paths(self, venture: str = "") -> Set[str]:
        """Get all shared lib root paths accessible to a venture."""
        paths: Set[str] = set()
        for lib in self.list_libs(venture=venture):
            paths.add(lib["path"])
        return paths

    def _load_libs(self) -> Dict[str, Any]:
        if not self.SHARED_LIBS_FILE.exists():
            # Bootstrap with defaults
            libs = {}
            for d in self.DEFAULT_LIBS:
                libs[d["name"]] = {**d, "ventures": ["*"], "registered_at": "bootstrap"}
            return libs
        try:
            return json.loads(self.SHARED_LIBS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_libs(self, libs: Dict[str, Any]):
        self.SHARED_LIBS_FILE.write_text(json.dumps(libs, indent=2))


# =========================================================================
#  Convenience: single entry point for swarm infra validation
# =========================================================================

def validate_agent_operation(
    venture: str,
    agent_id: str,
    token: str,
    target_path: str,
    operation: str = "write",
) -> Dict[str, Any]:
    """Full validation pipeline: auth -> namespace -> jail -> shared libs.

    Returns a structured result with pass/fail and details.
    Raises DirectoryJailViolation on write violations.
    """
    auth = SwarmAuth()
    jail = DirectoryJail()
    shared = SharedLibs()
    logger = SwarmLogger()

    # 1. Authenticate
    auth_result = auth.validate_token(token)
    if not auth_result.get("valid"):
        logger.error(
            f"Auth failed: {auth_result.get('error')}",
            venture=venture,
            agent_id=agent_id,
            action="validate_operation",
        )
        return {"allowed": False, "stage": "auth", "error": auth_result.get("error")}

    # 2. Verify token matches claimed identity
    if auth_result["venture"] != venture or auth_result["agent_id"] != agent_id:
        logger.error(
            f"Identity mismatch: token is for {auth_result['venture']}/{auth_result['agent_id']}",
            venture=venture,
            agent_id=agent_id,
            action="validate_operation",
        )
        return {"allowed": False, "stage": "identity", "error": "Token does not match claimed identity"}

    # 3. Check operation
    if operation == "write":
        # This raises DirectoryJailViolation on failure
        jail.check_write(venture, agent_id, target_path)
    elif operation == "read":
        # Check shared libs for cross-namespace reads
        ns = VentureNamespace()
        if not ns.is_within_namespace(venture, target_path):
            if not shared.can_access(venture, target_path):
                logger.warn(
                    f"Read denied: {target_path} not in namespace or shared libs",
                    venture=venture,
                    agent_id=agent_id,
                    action="validate_operation",
                )
                return {
                    "allowed": False,
                    "stage": "namespace",
                    "error": "Path not in namespace or shared libs",
                }

    logger.info(
        f"Operation '{operation}' approved for {target_path}",
        venture=venture,
        agent_id=agent_id,
        action="validate_operation",
    )
    return {
        "allowed": True,
        "venture": venture,
        "agent_id": agent_id,
        "operation": operation,
        "target_path": target_path,
    }
