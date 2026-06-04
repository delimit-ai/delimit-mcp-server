"""
Bridge to repo-level tools: repodoctor, configsentry, evidencepack, securitygate.
Tier 3 Extended — repository health, config audit, evidence, security.
"""

import os
import sys
import json
import asyncio
import importlib
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from .async_utils import run_async

logger = logging.getLogger("delimit.ai.repo_bridge")

PACKAGES = Path(os.environ.get("DELIMIT_HOME", str(Path.home() / ".delimit"))) / "server" / "packages"

# Add PACKAGES dir so `from shared.base_server import BaseMCPServer` resolves
_packages = str(PACKAGES)
if _packages not in sys.path:
    sys.path.insert(0, _packages)

_servers = {}


def _call(pkg: str, factory_name: str, method: str, args: Dict, tool_label: str) -> Dict[str, Any]:
    try:
        srv = _servers.get(pkg)
        if srv is None:
            mod = importlib.import_module(f"{pkg}.server")
            factory = getattr(mod, factory_name)
            srv = factory()
            # Some servers need async initialization (e.g. evidencepack)
            init_fn = getattr(srv, "initialize", None)
            if init_fn and asyncio.iscoroutinefunction(init_fn):
                run_async(init_fn())
            _servers[pkg] = srv
        fn = getattr(srv, method, None)
        if fn is None:
            return {"tool": tool_label, "status": "not_implemented", "error": f"Method {method} not found"}
        result = run_async(fn(args, None))
        return json.loads(result) if isinstance(result, str) else result
    except Exception as e:
        return {"tool": tool_label, "error": str(e)}


# ─── RepoDoctor ────────────────────────────────────────────────────────

def diagnose(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    """Check for common repository issues."""
    import subprocess
    root = Path(target).resolve()
    issues = []
    if not (root / ".gitignore").exists():
        issues.append({"severity": "warning", "issue": "No .gitignore file found"})
    if not any((root / d).exists() for d in ["tests", "test", "__tests__", "spec"]):
        issues.append({"severity": "warning", "issue": "No test directory found"})
    if not any((root / f).exists() for f in [".github/workflows", ".gitlab-ci.yml", "Jenkinsfile", ".circleci"]):
        issues.append({"severity": "info", "issue": "No CI configuration detected"})
    # Check for large files
    try:
        result = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            for f in result.stdout.strip().splitlines():
                fp = root / f
                if fp.exists() and fp.stat().st_size > 5_000_000:
                    issues.append({"severity": "warning", "issue": f"Large file ({fp.stat().st_size // 1_000_000}MB): {f}"})
    except Exception:
        pass
    status = "healthy" if not issues else ("warning" if all(i["severity"] != "error" for i in issues) else "unhealthy")
    return {"tool": "repo.diagnose", "status": status, "target": str(root), "issues": issues, "total_issues": len(issues)}


def analyze(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    """Analyze repository structure: file counts by type, configs, health."""
    root = Path(target).resolve()
    skip = {"node_modules", "dist", ".next", ".git", "__pycache__", "build", ".cache", "venv", ".venv"}
    ext_counts: Dict[str, int] = {}
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for f in filenames:
            ext = Path(f).suffix or "(no ext)"
            ext_counts[ext] = ext_counts.get(ext, 0) + 1
            total += 1
    top = sorted(ext_counts.items(), key=lambda x: -x[1])[:15]
    configs = {c: (root / c).exists() for c in [
        ".gitignore", "package.json", "pyproject.toml", "Makefile", "Dockerfile",
        "tsconfig.json", ".eslintrc.json", "jest.config.js", "pytest.ini", "setup.py",
    ]}
    has_tests = any((root / d).exists() for d in ["tests", "test", "__tests__", "spec"])
    has_ci = any((root / f).exists() for f in [".github/workflows", ".gitlab-ci.yml", "Jenkinsfile"])
    return {"tool": "repo.analyze", "status": "ok", "target": str(root), "total_files": total,
            "file_types": dict(top), "configs_found": {k: v for k, v in configs.items() if v},
            "has_tests": has_tests, "has_ci": has_ci}


# ─── ConfigSentry ───────────────────────────────────────────────────────

def config_validate(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    """Validate JSON/YAML config files parse correctly."""
    root = Path(target).resolve()
    results = []
    for ext, loader in [(".json", "json"), (".yaml", "yaml"), (".yml", "yaml")]:
        for fp in root.glob(f"*{ext}"):
            if fp.name.startswith(".") and ext == ".json" and "lock" in fp.name:
                continue
            try:
                text = fp.read_text()
                if loader == "json":
                    json.loads(text)
                else:
                    try:
                        import yaml as _yaml
                        _yaml.safe_load(text)
                    except ImportError:
                        pass  # skip YAML validation if pyyaml not installed
                results.append({"file": fp.name, "valid": True})
            except Exception as e:
                results.append({"file": fp.name, "valid": False, "error": str(e)[:200]})
    valid = sum(1 for r in results if r["valid"])
    invalid = sum(1 for r in results if not r["valid"])
    return {"tool": "config.validate", "status": "ok" if invalid == 0 else "issues_found",
            "target": str(root), "files_checked": len(results), "valid": valid, "invalid": invalid, "details": results}


def config_audit(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    """Audit config files for security issues and staleness."""
    root = Path(target).resolve()
    findings = []
    # Check for secrets in config files
    secret_patterns = ["password", "secret", "api_key", "apikey", "token", "private_key"]
    for ext in [".json", ".yaml", ".yml", ".env", ".toml", ".ini", ".cfg"]:
        for fp in root.glob(f"*{ext}"):
            try:
                text = fp.read_text().lower()
                for pat in secret_patterns:
                    if pat in text and fp.name != ".gitignore":
                        findings.append({"file": fp.name, "severity": "warning",
                                         "issue": f"Possible secret pattern '{pat}' found"})
                        break
            except Exception:
                pass
    # Check .env not in .gitignore
    gitignore = root / ".gitignore"
    if (root / ".env").exists() and gitignore.exists():
        gi_text = gitignore.read_text()
        if ".env" not in gi_text:
            findings.append({"file": ".env", "severity": "error", "issue": ".env exists but not in .gitignore"})
    elif (root / ".env").exists() and not gitignore.exists():
        findings.append({"file": ".env", "severity": "error", "issue": ".env exists with no .gitignore"})
    return {"tool": "config.audit", "status": "ok" if not findings else "issues_found",
            "target": str(root), "findings": findings, "total_findings": len(findings)}


# ─── EvidencePack ───────────────────────────────────────────────────────

def evidence_collect(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    """Collect project evidence: git log, test files, configs, governance data.

    Accepts either a local filesystem path (repo directory) or a remote
    reference (GitHub URL, owner/repo#N, or any non-filesystem string).
    Remote targets skip the filesystem walk and store reference metadata.
    """
    import re
    import subprocess
    import time as _time

    opts = options or {}
    evidence_type = opts.get("evidence_type", "")

    # Detect non-filesystem targets: URLs, owner/repo#N, bare issue refs, etc.
    is_remote = (
        "://" in target
        or target.startswith("http")
        or re.match(r"^[\w.-]+/[\w.-]+#\d+$", target) is not None
        or "#" in target
    )

    evidence: Dict[str, Any] = {"collected_at": _time.time(), "target": target}
    if evidence_type:
        evidence["evidence_type"] = evidence_type

    if is_remote:
        # Remote/reference target — no filesystem walk, just record metadata.
        evidence["target_type"] = "remote"
        evidence["git_log"] = []
        evidence["test_directories"] = []
        evidence["configs"] = []
        m = re.match(r"^([\w.-]+)/([\w.-]+)#(\d+)$", target)
        if m:
            evidence["repo"] = f"{m.group(1)}/{m.group(2)}"
            evidence["issue_number"] = int(m.group(3))
    else:
        root = Path(target).resolve()
        evidence["target"] = str(root)
        evidence["target_type"] = "local"

        if not root.exists():
            return {
                "tool": "evidence.collect",
                "status": "error",
                "error": "target_not_found",
                "message": f"Path {root} does not exist. For remote targets, pass a URL or owner/repo#N.",
                "target": target,
            }

        # Git log (safe for non-git dirs)
        try:
            r = subprocess.run(
                ["git", "-C", str(root), "log", "--oneline", "-10"],
                capture_output=True, text=True, timeout=10,
            )
            evidence["git_log"] = r.stdout.strip().splitlines() if r.returncode == 0 else []
        except Exception:
            evidence["git_log"] = []

        # Test dirs + configs (only if target is a directory)
        if root.is_dir():
            test_dirs = [d for d in ["tests", "test", "__tests__", "spec"] if (root / d).exists()]
            evidence["test_directories"] = test_dirs
            try:
                evidence["configs"] = [
                    f.name for f in root.iterdir()
                    if f.is_file() and (f.suffix in [".json", ".yaml", ".yml", ".toml"] or f.name.startswith("."))
                ]
            except (PermissionError, OSError):
                evidence["configs"] = []
        else:
            evidence["test_directories"] = []
            evidence["configs"] = []

    # Save bundle
    ev_dir = Path(os.environ.get("DELIMIT_HOME", str(Path.home() / ".delimit"))) / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)
    bundle_id = f"ev-{int(_time.time())}"
    bundle_path = ev_dir / f"{bundle_id}.json"
    evidence["bundle_id"] = bundle_id
    bundle_path.write_text(json.dumps(evidence, indent=2))
    return {
        "tool": "evidence.collect",
        "status": "ok",
        "bundle_id": bundle_id,
        "bundle_path": str(bundle_path),
        "summary": {k: len(v) if isinstance(v, list) else v for k, v in evidence.items()},
    }


def evidence_verify(bundle_id: Optional[str] = None, bundle_path: Optional[str] = None, options: Optional[Dict] = None) -> Dict[str, Any]:
    """Verify the integrity and authenticity of a collected evidence bundle."""
    args = {**(options or {})}
    if bundle_id:
        args["bundle_id"] = bundle_id
    if bundle_path:
        args["bundle_path"] = bundle_path
    if not bundle_id and not bundle_path:
        return {"tool": "evidence.verify", "status": "no_input", "message": "Provide bundle_id or bundle_path to verify"}
    try:
        importlib.import_module("evidencepack.server")
    except (ImportError, ModuleNotFoundError):
        return {
            "tool": "evidence.verify",
            "status": "not_available",
            "error": "evidencepack backend is not installed or not available in this environment.",
            "hint": "Install evidencepack to enable evidence bundle verification.",
            **args,
        }
    return _call("evidencepack", "create_evidencepack_server", "_tool_validate",
                 args, "evidence.verify")


# ─── SecurityGate ───────────────────────────────────────────────────────

_INTERNAL_TOKEN = os.environ.get("DELIMIT_INTERNAL_BRIDGE_TOKEN", "delimit-internal-bridge")


def _fallback_security_result(target: str, tool_label: str) -> Dict[str, Any]:
    """Run the built-in local security audit when securitygate is unavailable."""
    from .tools_infra import security_audit as local_security_audit

    result = local_security_audit(target=target)
    result["tool"] = tool_label
    result.setdefault("status", "ok" if "error" not in result else "error")
    result["fallback"] = True
    result["hint"] = (
        "Running basic security audit (free fallback). "
        "For enhanced scanning with CVE detection, install securitygate: "
        "pip install securitygate"
    )
    return result


def security_scan(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    """Perform an enhanced security scan for CVEs and vulnerabilities."""
    try:
        importlib.import_module("securitygate.server")
    except (ImportError, ModuleNotFoundError):
        logger.warning("securitygate module not found, falling back to local security audit")
        return _fallback_security_result(target=target, tool_label="security.scan")
    result = _call("securitygate", "create_securitygate_server", "_tool_scan",
                   {"target": target, "authorization_token": _INTERNAL_TOKEN, **(options or {})}, "security.scan")
    # Guard against fabricated/hardcoded CVE data from stub implementations
    vulns = result.get("vulnerabilities", [])
    if vulns and any("CVE-2023-12345" in str(v.get("id", "")) for v in vulns):
        return {"tool": "security.scan", "status": "not_available",
                "error": "Security scanner returned placeholder data. Install a real vulnerability scanner."}
    return result


def security_audit(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    """Audit source code for dangerous patterns and hardcoded secrets."""
    try:
        importlib.import_module("securitygate.server")
    except (ImportError, ModuleNotFoundError):
        logger.warning("securitygate module not found, using built-in security audit")
        return _fallback_security_result(target=target, tool_label="security.audit")
    return _call("securitygate", "create_securitygate_server", "_tool_audit",
                 {"target": target, "authorization_token": _INTERNAL_TOKEN, **(options or {})}, "security.audit")
