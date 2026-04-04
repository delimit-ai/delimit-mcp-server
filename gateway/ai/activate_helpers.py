"""LED-269 / LED-270: Activation checklist helpers.

Extracted so they can be tested independently of ai.server (which has
heavy MCP decorator dependencies).
"""

import json
import os
from pathlib import Path
from typing import Dict, Any


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
    applicable = [c for c in checklist if not c["status"].startswith("Skip")]
    passed_total = sum(1 for c in applicable if c["status"] == "Pass")
    total = len(applicable)
    score = f"{passed_total}/{total}"

    result: Dict[str, Any] = {
        "tool": "activate",
        "status": "complete",
        "score": score,
        "passed": passed_total,
        "total": total,
        "skipped": len(checklist) - total,
        "checklist": checklist,
        "tier": get_license().get("tier", "free"),
        "project": str(p),
    }
    if passed_total == total and total > 0:
        result["message"] = f"All {total} checks passed. Delimit is fully operational."
    elif passed_total < total:
        failed_items = [c["item"] for c in applicable if c["status"] == "Fail"]
        result["message"] = f"{passed_total}/{total} checks passed. Fix: {', '.join(failed_items)}"

    return result
