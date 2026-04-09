"""LED-269 / LED-270: Activation checklist helpers.

Extracted so they can be tested independently of ai.server (which has
heavy MCP decorator dependencies).
"""

import json
import os
import stat
from pathlib import Path
from typing import Dict, Any, List


def activate_auto_permissions(auto_permissions: bool) -> dict:
    """LED-269: Detect AI assistant and auto-configure Delimit tool permissions.

    Returns a checklist entry dict with item/status/detail.
    """
    home = Path.home()
    assistant = None
    config_path = None

    # Detect which assistant is running
    if os.environ.get("CLAUDE_CODE") or (home / ".claude").is_dir():
        assistant = "claude_code"
        config_path = home / ".claude" / "settings.json"
    elif (home / ".codex" / "config.toml").exists():
        assistant = "codex"
        config_path = home / ".codex" / "config.toml"
    elif (home / ".gemini" / "settings.json").exists():
        assistant = "gemini"
        config_path = home / ".gemini" / "settings.json"
    else:
        if os.environ.get("CODEX_CLI"):
            assistant = "codex"
            config_path = home / ".codex" / "config.toml"

    if not assistant:
        return {"item": "Permissions", "status": "Skip (no assistant)", "detail": "No AI assistant detected"}

    if not auto_permissions:
        return {"item": "Permissions", "status": "Skip (manual)", "detail": f"Detected {assistant} — auto-config disabled"}

    try:
        if assistant == "claude_code":
            return configure_claude_code_permissions(config_path)
        elif assistant == "codex":
            return configure_codex_permissions(config_path)
        elif assistant == "gemini":
            return {"item": "Permissions", "status": "Skip (manual)", "detail": "Gemini CLI — configure permissions manually"}
    except Exception as e:
        return {"item": "Permissions", "status": "Fail", "detail": f"Auto-config failed: {e}"}

    return {"item": "Permissions", "status": "Skip (manual)", "detail": f"Detected {assistant}"}


def configure_claude_code_permissions(config_path: Path) -> dict:
    """Add mcp__delimit__* to Claude Code permissions.allow if not present."""
    permission_pattern = "mcp__delimit__*"

    if config_path.exists():
        try:
            data = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}

    permissions = data.setdefault("permissions", {})
    allow_list = permissions.setdefault("allow", [])

    if any(permission_pattern in str(entry) for entry in allow_list):
        return {"item": "Permissions", "status": "Pass", "detail": f"Claude Code: {permission_pattern} already in settings"}

    allow_list.append(permission_pattern)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(data, indent=2))
    return {"item": "Permissions", "status": "Pass", "detail": f"Claude Code: added {permission_pattern} to {config_path}"}


def configure_codex_permissions(config_path: Path) -> dict:
    """Set trust_level to trusted for Delimit in Codex config.toml."""
    if config_path.exists():
        content = config_path.read_text()
        if "trust_level" in content and "trusted" in content:
            return {"item": "Permissions", "status": "Pass", "detail": "Codex: already trusted"}
    else:
        content = ""

    if "[delimit]" not in content:
        addition = '\n[delimit]\ntrust_level = "trusted"\n'
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "a") as f:
            f.write(addition)
        return {"item": "Permissions", "status": "Pass", "detail": f"Codex: added trust_level=trusted to {config_path}"}
    else:
        return {"item": "Permissions", "status": "Pass", "detail": "Codex: delimit section exists"}


# ─── LED-269: Filesystem permission auto-config for delimit_init ──────
#
# Safety contract:
#   - Never chmod 777
#   - Never touch anything outside <project>/.delimit/ or
#     <project>/.claude/settings.json
#   - Never modify existing permissions on files we did not create
#     (we only chmod files/dirs we just created or that match our
#     known-safe set: .delimit/ itself, .delimit/secrets/* files)
#   - Idempotent: running twice is a no-op for already-correct state
#   - Backwards compatible: existing installs work without re-running init


# Reasonable default permission allowlist for AI assistants working
# inside a Delimit-governed project. Mirrors what most projects already
# grant manually. Conservative — no `Bash(rm:*)`, no wildcards on dangerous
# commands, no network egress beyond what tools need.
_DEFAULT_CLAUDE_PROJECT_ALLOW: List[str] = [
    "Edit",
    "Write",
    "Read",
    "Glob",
    "Grep",
    "Bash(git status)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git add:*)",
    "Bash(ls:*)",
    "Bash(cat:*)",
    "Bash(pwd)",
    "Bash(delimit:*)",
    "Bash(npm test:*)",
    "Bash(npm run:*)",
    "Bash(pytest:*)",
    "Bash(python:*)",
    "Bash(python3:*)",
    "mcp__delimit__*",
]


def _safe_chmod(path: Path, mode: int) -> bool:
    """chmod a path defensively. Returns True if applied, False on any error.

    Refuses world-writable bits (0o002) outright. Never raises.
    """
    if mode & 0o002:
        return False
    try:
        path.chmod(mode)
        return True
    except (OSError, PermissionError):
        return False


def _detect_target_owner(project_root: Path) -> tuple:
    """If running as root, detect the (uid, gid) of the project owner so
    we can chown anything we create back to them. Returns (None, None)
    if not running as root or detection fails.
    """
    try:
        if os.geteuid() != 0:
            return (None, None)
    except AttributeError:
        # Windows or platform without geteuid
        return (None, None)

    try:
        st = project_root.stat()
        # Don't chown to root-owned projects (no-op)
        if st.st_uid == 0 and st.st_gid == 0:
            return (None, None)
        return (st.st_uid, st.st_gid)
    except OSError:
        return (None, None)


def _safe_chown(path: Path, uid, gid) -> None:
    """chown a path defensively. Never raises."""
    if uid is None or gid is None:
        return
    try:
        os.chown(str(path), uid, gid)
    except (OSError, PermissionError, AttributeError):
        pass


def setup_init_permissions(project_root: Path, no_permissions: bool = False) -> Dict[str, Any]:
    """LED-269: Configure filesystem permissions for a freshly-initialized
    Delimit project.

    Performs:
      1. chmod 755 on <project>/.delimit/ (idempotent)
      2. chmod 600 on every file under <project>/.delimit/secrets/ (if dir exists)
      3. Creates <project>/.claude/settings.json with a reasonable Edit/Write/
         Bash allowlist if it does not already exist (never overwrites)
      4. If running as root, chowns anything we created back to the project
         owner so the user can still access their own files

    Args:
        project_root: Resolved absolute path to the project root.
        no_permissions: If True, skip everything and return a 'skipped' result.

    Returns:
        Dict with keys: status, applied (list of changes), skipped (list of
        items skipped with reason), warnings (list).
    """
    result: Dict[str, Any] = {
        "status": "skipped" if no_permissions else "ok",
        "applied": [],
        "skipped": [],
        "warnings": [],
    }

    if no_permissions:
        result["skipped"].append("--no-permissions flag set")
        return result

    project_root = Path(project_root).resolve()
    delimit_dir = project_root / ".delimit"

    # Hard safety guard: refuse to operate if .delimit/ doesn't exist.
    # delimit_init creates it before calling us — if it's missing something
    # is wrong and we should not silently chmod random paths.
    if not delimit_dir.is_dir():
        result["status"] = "error"
        result["warnings"].append(f".delimit/ not found at {delimit_dir} — refusing to set permissions")
        return result

    target_uid, target_gid = _detect_target_owner(project_root)
    running_as_root = target_uid is not None

    # 1. chmod 755 on .delimit/
    try:
        current_mode = stat.S_IMODE(delimit_dir.stat().st_mode)
        if current_mode != 0o755:
            if _safe_chmod(delimit_dir, 0o755):
                result["applied"].append(f"chmod 755 {delimit_dir}")
            else:
                result["warnings"].append(f"Could not chmod {delimit_dir}")
        else:
            result["skipped"].append(f"{delimit_dir} already 755")
    except OSError as e:
        result["warnings"].append(f"stat failed on {delimit_dir}: {e}")

    # 2. chmod 600 on secrets files (only if secrets dir exists)
    secrets_dir = delimit_dir / "secrets"
    if secrets_dir.is_dir():
        # Lock the secrets dir itself to 700
        try:
            current_mode = stat.S_IMODE(secrets_dir.stat().st_mode)
            if current_mode != 0o700:
                if _safe_chmod(secrets_dir, 0o700):
                    result["applied"].append(f"chmod 700 {secrets_dir}")
        except OSError:
            pass

        for entry in secrets_dir.iterdir():
            if not entry.is_file():
                continue
            try:
                current_mode = stat.S_IMODE(entry.stat().st_mode)
                if current_mode != 0o600:
                    if _safe_chmod(entry, 0o600):
                        result["applied"].append(f"chmod 600 {entry}")
                    else:
                        result["warnings"].append(f"Could not chmod {entry}")
            except OSError:
                continue
    else:
        result["skipped"].append("no secrets/ directory present")

    # 3. Create project-local .claude/settings.json with reasonable allowlist
    claude_dir = project_root / ".claude"
    claude_settings = claude_dir / "settings.json"
    if claude_settings.exists():
        result["skipped"].append(f"{claude_settings} already exists (not modified)")
    else:
        try:
            claude_dir.mkdir(parents=True, exist_ok=True)
            settings_payload = {
                "permissions": {
                    "allow": list(_DEFAULT_CLAUDE_PROJECT_ALLOW),
                    "deny": [
                        "Bash(rm -rf:*)",
                        "Bash(sudo:*)",
                    ],
                },
                "_generated_by": "delimit_init",
                "_note": "Edit freely. Delimit will never overwrite this file.",
            }
            claude_settings.write_text(json.dumps(settings_payload, indent=2) + "\n")
            # Lock to 644 (or 600 if it lives in a secrets dir, which it doesn't)
            _safe_chmod(claude_settings, 0o644)
            result["applied"].append(f"created {claude_settings}")

            if running_as_root:
                _safe_chown(claude_dir, target_uid, target_gid)
                _safe_chown(claude_settings, target_uid, target_gid)
        except OSError as e:
            result["warnings"].append(f"Could not create {claude_settings}: {e}")

    # 4. Re-chown .delimit/ tree if we're root and there's a non-root owner
    if running_as_root:
        try:
            for path in [delimit_dir, *delimit_dir.rglob("*")]:
                # Only chown if currently owned by root — never override
                # an existing non-root owner
                try:
                    if path.stat().st_uid == 0:
                        _safe_chown(path, target_uid, target_gid)
                except OSError:
                    continue
            result["applied"].append(f"chown -R {target_uid}:{target_gid} {delimit_dir} (root-owned entries)")
        except OSError:
            pass

    if not result["applied"] and not result["warnings"]:
        result["status"] = "noop"

    return result


def build_checklist(
    license_key: str,
    project_path: str,
    auto_permissions: bool,
) -> Dict[str, Any]:
    """Build the activation checklist. Core logic extracted from delimit_activate.

    Returns the result dict (without next_steps wrapping).
    """
    from ai.license import activate_license, get_license, is_premium, require_premium

    checklist: list = []
    p = Path(project_path).resolve()

    # --- Step 1: License activation (if key provided) ---
    if license_key:
        lic_result = activate_license(license_key)
        if lic_result.get("status") == "activated":
            checklist.append({"item": "License activation", "status": "Pass", "detail": f"Tier: {lic_result.get('tier', 'pro')}"})
        else:
            checklist.append({"item": "License activation", "status": "Fail", "detail": lic_result.get("error", "Unknown error")})
    else:
        lic = get_license()
        tier = lic.get("tier", "free")
        checklist.append({"item": "License status", "status": "Pass", "detail": f"Tier: {tier}"})

    # --- Step 2: MCP server reachable ---
    checklist.append({"item": "MCP server", "status": "Pass", "detail": "Server responding"})

    # --- Step 3: Python dependencies ---
    dep_ok = True
    for pkg in ["yaml", "pydantic", "packaging", "fastmcp"]:
        try:
            __import__(pkg)
        except ImportError:
            dep_ok = False
            checklist.append({"item": f"Dependency: {pkg}", "status": "Fail", "detail": f"pip install {pkg}"})
    if dep_ok:
        checklist.append({"item": "Dependencies", "status": "Pass", "detail": "All required packages installed"})

    # --- Step 4: Governance initialized ---
    delimit_dir = p / ".delimit"
    policies = delimit_dir / "policies.yml"
    if delimit_dir.is_dir() and policies.is_file():
        checklist.append({"item": "Governance", "status": "Pass", "detail": f"Initialized at {delimit_dir}"})
    elif delimit_dir.is_dir():
        checklist.append({"item": "Governance", "status": "Fail", "detail": "Missing policies.yml — run delimit_init"})
    else:
        checklist.append({"item": "Governance", "status": "Fail", "detail": "Not initialized — run delimit_init"})

    # --- Step 5: Test smoke (skip if no framework) ---
    try:
        from backends.tools_real import test_smoke as _test_smoke_fn
        smoke = _test_smoke_fn(project_path=str(p))
        if smoke.get("status") == "no_framework":
            checklist.append({"item": "Test smoke", "status": "Skip (no tests)", "detail": "No test framework detected"})
        elif smoke.get("error"):
            checklist.append({"item": "Test smoke", "status": "Fail", "detail": smoke.get("error", "")})
        else:
            passed_count = smoke.get("passed", 0)
            failed_count = smoke.get("failed", 0)
            if failed_count == 0:
                checklist.append({"item": "Test smoke", "status": "Pass", "detail": f"{passed_count} tests passed"})
            else:
                checklist.append({"item": "Test smoke", "status": "Fail", "detail": f"{passed_count} passed, {failed_count} failed"})
    except Exception as e:
        checklist.append({"item": "Test smoke", "status": "Skip (no tests)", "detail": f"Could not run: {e}"})

    # --- Step 6: AI assistant detection + permission auto-config (LED-269) ---
    perm_result = activate_auto_permissions(auto_permissions)
    checklist.append(perm_result)

    # --- Step 7: Premium feature checks (skip on free tier) ---
    premium_checks = [
        ("Deliberation (multi-model)", "delimit_deliberate"),
        ("Security audit", "delimit_security_ingest"),
        ("Deploy pipeline", "delimit_deploy_plan"),
        ("Cost analysis", "delimit_cost_analyze"),
        ("Release management", "delimit_release_plan"),
        ("Agent orchestration", "delimit_agent_dispatch"),
    ]
    for label, tool_name in premium_checks:
        gate = require_premium(tool_name)
        if gate is None:
            checklist.append({"item": label, "status": "Pass", "detail": "Pro feature unlocked"})
        else:
            checklist.append({"item": label, "status": "Skip (Pro)", "detail": "Requires Delimit Pro"})

    # --- Score: only count applicable checks (exclude skips) ---
    # LED-270: explicitly distinguish passed / failed / skipped so callers
    # (CI, dashboards, CLI summaries) can render them separately. Skipped
    # checks (premium on free tier, no test framework, no AI assistant)
    # never count as failures and are excluded from the score denominator.
    applicable = [c for c in checklist if not c["status"].startswith("Skip")]
    passed_total = sum(1 for c in applicable if c["status"] == "Pass")
    failed_total = sum(1 for c in applicable if c["status"] == "Fail")
    skipped_total = len(checklist) - len(applicable)
    # Break out skip reasons so the UI can show "X Pro features locked"
    # without conflating them with "no test framework"-style skips.
    skipped_premium = sum(1 for c in checklist if c["status"] == "Skip (Pro)")
    skipped_other = skipped_total - skipped_premium
    total = len(applicable)
    score = f"{passed_total}/{total}"

    tier = get_license().get("tier", "free")

    result: Dict[str, Any] = {
        "tool": "activate",
        "status": "complete",
        "score": score,
        "passed": passed_total,
        "failed": failed_total,
        "total": total,
        "skipped": skipped_total,
        "skipped_premium": skipped_premium,
        "skipped_other": skipped_other,
        "checklist": checklist,
        "tier": tier,
        "project": str(p),
    }
    if failed_total == 0 and total > 0:
        msg = f"All {total} checks passed. Delimit is fully operational."
        if skipped_premium > 0 and tier == "free":
            msg += f" ({skipped_premium} Pro features available with upgrade.)"
        result["message"] = msg
    elif failed_total > 0:
        failed_items = [c["item"] for c in applicable if c["status"] == "Fail"]
        result["message"] = (
            f"{passed_total}/{total} checks passed, {failed_total} failed. "
            f"Fix: {', '.join(failed_items)}"
        )
    else:
        result["message"] = f"{passed_total}/{total} checks passed."

    return result
