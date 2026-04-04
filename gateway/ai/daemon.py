"""Delimit Autonomous Daemon — processes ledger items without human prompting.

The daemon continuously:
1. Checks the ledger for open items
2. Classifies each item as automatable vs needs-human
3. Executes automatable items
4. Updates results
5. Escalates high-risk items via approval gates
6. Loops

Risk tiers:
- LOW: auto-execute immediately (lint, diff, scan, test, docs)
- MEDIUM: execute and notify after (deploy to staging, security audit)
- HIGH: create approval request, wait for human (deploy to prod, data migration)
- CRITICAL: never auto-execute (delete data, change auth, billing changes)
"""
import json
import time
import logging
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("delimit.daemon")

DAEMON_LOG = Path.home() / ".delimit" / "daemon" / "daemon.log.jsonl"
DAEMON_STATE = Path.home() / ".delimit" / "daemon" / "state.json"

# Risk classification for automated execution
AUTO_EXECUTE_TOOLS = {
    # LOW risk — safe to run anytime
    "lint", "diff", "scan", "diagnose", "doctor", "version", "help",
    "ledger_context", "ledger_list", "gov_health", "gov_status",
    "security_scan", "security_audit", "test_smoke", "test_generate",
    "docs_generate", "docs_validate", "zero_spec", "explain", "semver",
    "policy", "impact", "license_status", "content_schedule",
    "social_generate", "resource_drivers", "context_list",
    "secret_list", "obs_status", "obs_metrics",
}

NOTIFY_AFTER_TOOLS = {
    # MEDIUM risk — execute then notify
    "ledger_add", "ledger_done", "init", "social_post",
    "content_publish", "context_write", "context_snapshot",
    "gov_evaluate", "evidence_collect",
}

APPROVAL_REQUIRED_TOOLS = {
    # HIGH risk — needs approval gate
    "deploy_publish", "deploy_npm", "deploy_site", "deploy_rollback",
    "secret_store", "secret_revoke", "data_migrate", "data_backup",
}

# Keywords indicating the item requires human action (forums, outreach, etc.)
HUMAN_REQUIRED_KEYWORDS = [
    "recruit", "beta testers", "namepros", "contact", "email", "call",
    "manual", "approve", "review", "decision", "meeting", "interview",
    "negotiate", "sign", "contract", "payment", "purchase",
    "sensor_github_issue",
]

# Tools that should never be auto-called by the daemon (polling/sensor tools)
SKIP_TOOLS = {
    "sensor_github_issue",
}

# Cooldown: don't re-call the same tool within this window
TOOL_COOLDOWN_SECONDS = 3600  # 1 hour

# Ledger item patterns that can be auto-processed
AUTO_PATTERNS = {
    "lint": ["lint", "breaking change", "api check", "spec check"],
    "scan": ["scan", "security", "audit", "vulnerability"],
    "test": ["test", "coverage", "smoke"],
    "docs": ["docs", "documentation", "readme"],
    "governance": ["governance", "policy", "compliance"],
}


class DaemonState:
    """Tracks daemon execution state across runs."""

    def _load(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "loops": 0,
            "items_processed": 0,
            "items_skipped": 0,
            "items_escalated": 0,
            "last_loop_at": None,
            "status": "idle",
            "errors": 0,
            "processed_ids": [],
        }

    def __init__(self, state_path: Optional[Path] = None):
        self.state_path = state_path or DAEMON_STATE
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load()

    def save(self):
        self.state_path.write_text(json.dumps(self.state, indent=2))

    def increment(self, key: str):
        self.state[key] = self.state.get(key, 0) + 1
        self.save()

    def set(self, key: str, value):
        self.state[key] = value
        self.save()

    def mark_processed(self, item_id: str):
        """Record that an item has been attempted so it is skipped next loop."""
        ids = set(self.state.get("processed_ids", []))
        ids.add(item_id)
        self.state["processed_ids"] = sorted(ids)
        self.save()

    def is_processed(self, item_id: str) -> bool:
        return item_id in set(self.state.get("processed_ids", []))

    def record_tool_call(self, tool_name: str):
        """Record when a tool was last called for cooldown enforcement."""
        cooldowns = self.state.get("tool_cooldowns", {})
        cooldowns[tool_name] = datetime.now(timezone.utc).isoformat()
        self.state["tool_cooldowns"] = cooldowns
        self.save()

    def is_tool_on_cooldown(self, tool_name: str,
                            cooldown_seconds: int = TOOL_COOLDOWN_SECONDS) -> bool:
        """Return True if the tool was called within the cooldown window."""
        cooldowns = self.state.get("tool_cooldowns", {})
        last_call = cooldowns.get(tool_name)
        if not last_call:
            return False
        try:
            last_dt = datetime.fromisoformat(last_call)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) - last_dt < timedelta(seconds=cooldown_seconds)
        except (ValueError, TypeError):
            return False


def log_action(action: str, item_id: str = "", detail: str = "",
               risk: str = "low", log_path: Optional[Path] = None):
    """Log daemon action to JSONL file."""
    target = log_path or DAEMON_LOG
    target.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "item_id": item_id,
        "detail": detail[:500],
        "risk": risk,
    }
    with open(target, "a") as f:
        f.write(json.dumps(entry) + "\n")


def classify_item(item: dict) -> Tuple[str, str]:
    """Classify a ledger item into risk tier and suggested tool.

    Returns: (risk_tier, suggested_tool)
    """
    title = item.get("title", "").lower()
    description = item.get("description", "").lower()
    text = f"{title} {description}"

    # Check for skip-tools — sensor/polling tools the daemon must never call
    if any(kw in text for kw in SKIP_TOOLS):
        return ("high", "human_required")

    # Check for high-risk keywords
    high_risk_keywords = [
        "deploy", "publish", "production", "rollback",
        "delete", "migrate", "billing", "auth",
    ]
    if any(kw in text for kw in high_risk_keywords):
        return ("high", "deploy_publish")

    # Check for human-required keywords — these override low-risk patterns
    if any(kw in text for kw in HUMAN_REQUIRED_KEYWORDS):
        return ("high", "human_required")

    # Check for automatable patterns
    for tool, keywords in AUTO_PATTERNS.items():
        if any(kw in text for kw in keywords):
            return ("low", tool)

    # Default: medium risk, needs review
    return ("medium", "unknown")


def get_open_ledger_items(ledger_dir: Optional[Path] = None) -> List[dict]:
    """Read open items from ledger JSONL files.

    Handles both simple entries and update entries that modify existing items.
    """
    ledger_dir = ledger_dir or (Path.home() / ".delimit" / "ledger")
    if not ledger_dir.exists():
        return []

    items: Dict[str, dict] = {}
    for fname in ["operations.jsonl", "strategy.jsonl"]:
        fpath = ledger_dir / fname
        if not fpath.exists():
            continue
        for line in fpath.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                if not e.get("id"):
                    continue
                if e.get("type") == "update" and e["id"] in items:
                    items[e["id"]].update(e)
                elif e.get("type") != "update":
                    items[e["id"]] = e
            except (json.JSONDecodeError, KeyError):
                pass

    # Filter to open items
    return [i for i in items.values() if i.get("status") == "open"]


def get_next_automatable_item(
    ledger_dir: Optional[Path] = None,
    state: Optional[DaemonState] = None,
) -> Optional[dict]:
    """Get the next ledger item that can be auto-executed (low risk only).

    If *state* is provided, items already in ``state.processed_ids`` are
    skipped so the daemon does not re-process the same item every loop.
    """
    open_items = get_open_ledger_items(ledger_dir)

    # Sort by priority
    priority_order = {"P0": 0, "P1": 1, "P2": 2}
    open_items.sort(key=lambda x: priority_order.get(x.get("priority", "P2"), 3))

    # Find first automatable item that hasn't been processed already
    for item in open_items:
        item_id = item.get("id", "")
        if state and state.is_processed(item_id):
            continue
        risk, tool = classify_item(item)
        if risk == "low":
            # Enforce tool cooldown — don't call the same tool repeatedly
            if state and tool and state.is_tool_on_cooldown(tool):
                continue
            item["_risk"] = risk
            item["_suggested_tool"] = tool
            return item

    return None


def _run_lint(item: dict) -> dict:
    """Run lint on any spec files mentioned in the item."""
    import glob as globmod
    try:
        from ai.backends.gateway_core import run_lint
    except ImportError:
        return {"status": "import_error", "detail": "gateway_core not available"}
    specs = (
        globmod.glob("**/openapi.yaml", recursive=True)
        + globmod.glob("**/openapi.yml", recursive=True)
        + globmod.glob("**/openapi.json", recursive=True)
    )
    if specs:
        return run_lint(specs[0], specs[0])
    return {"status": "no_specs_found"}


def _run_scan(item: dict) -> dict:
    """Run a lightweight project scan by discovering key files."""
    scan_result = {"status": "scanned", "files": {}}
    for pattern, label in [
        ("**/openapi.yaml", "openapi_specs"),
        ("**/openapi.yml", "openapi_specs"),
        ("**/package.json", "node_projects"),
        ("**/pyproject.toml", "python_projects"),
        ("**/.delimit/policies.yml", "delimit_policies"),
    ]:
        import glob as globmod
        found = globmod.glob(pattern, recursive=True)
        if found:
            scan_result["files"][label] = found[:10]
    return scan_result


def _run_test(item: dict) -> dict:
    """Run test discovery (not execution) to count available tests."""
    import subprocess
    try:
        result = subprocess.run(
            ["python3", "-m", "pytest", "--co", "-q"],
            capture_output=True, text=True, timeout=30,
        )
        lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
        return {"tests_found": len(lines), "status": "discovered"}
    except FileNotFoundError:
        return {"status": "pytest_not_found"}
    except subprocess.TimeoutExpired:
        return {"status": "timeout"}
    except Exception as e:
        return {"status": "error", "detail": str(e)[:200]}


def _run_governance(item: dict) -> dict:
    """Run a governance health check by inspecting project structure."""
    result = {"status": "checked", "findings": []}
    cwd = Path(".")
    if (cwd / ".delimit" / "policies.yml").exists():
        result["findings"].append("policies.yml found")
    else:
        result["findings"].append("no policies.yml — run delimit init")
    if (cwd / "delimit.yml").exists():
        result["findings"].append("delimit.yml found")
    if any(cwd.glob("**/openapi.yaml")) or any(cwd.glob("**/openapi.yml")):
        result["findings"].append("OpenAPI spec found")
    else:
        result["findings"].append("no OpenAPI spec detected")
    return result


def _run_docs(item: dict) -> dict:
    """Run docs validation by checking for common documentation files."""
    result = {"status": "checked", "files_found": [], "missing": []}
    cwd = Path(".")
    for doc_file in ["README.md", "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE"]:
        if (cwd / doc_file).exists():
            result["files_found"].append(doc_file)
        else:
            result["missing"].append(doc_file)
    return result


def process_item(item: dict, log_path: Optional[Path] = None) -> dict:
    """Process a single ledger item by running the suggested tool.

    For high/critical risk items, creates an escalation instead of executing.
    For low risk items, actually runs the tool. Medium risk items run then notify.
    """
    item_id = item.get("id", "unknown")
    risk = item.get("_risk", "medium")
    tool = item.get("_suggested_tool", "unknown")

    log_action("processing", item_id, f"risk={risk}, tool={tool}", risk,
               log_path=log_path)

    if risk in ("high", "critical"):
        # Create approval request instead of executing
        log_action("escalated", item_id, "Requires human approval", risk,
                   log_path=log_path)
        return {
            "status": "escalated",
            "item_id": item_id,
            "reason": "high-risk action requires human approval",
        }

    # ACTUALLY RUN THE TOOL
    tool_map = {
        "lint": _run_lint,
        "scan": _run_scan,
        "test": _run_test,
        "governance": _run_governance,
        "docs": _run_docs,
    }

    runner = tool_map.get(tool)
    if runner:
        try:
            result = runner(item)
            log_action("completed", item_id, json.dumps(result)[:200], risk,
                       log_path=log_path)
            return {"status": "executed", "item_id": item_id, "result": result}
        except Exception as e:
            log_action("error", item_id, str(e)[:200], risk,
                       log_path=log_path)
            return {"status": "error", "item_id": item_id, "error": str(e)}

    # Fallback for tools without a runner
    result = {
        "status": "processed",
        "item_id": item_id,
        "tool": tool,
        "risk": risk,
        "action": f"No runner for {tool}: {item.get('title', '')[:100]}",
    }
    log_action("completed", item_id, json.dumps(result)[:200], risk,
               log_path=log_path)
    return result


def run_loop(max_iterations: int = 0, interval_seconds: int = 60,
             dry_run: bool = True, state_path: Optional[Path] = None,
             log_path: Optional[Path] = None,
             ledger_dir: Optional[Path] = None) -> dict:
    """Run the daemon loop.

    Args:
        max_iterations: 0 = infinite loop, >0 = stop after N iterations
        interval_seconds: seconds between checks
        dry_run: if True, log but don't execute
        state_path: override state file location (for testing)
        log_path: override log file location (for testing)
        ledger_dir: override ledger directory (for testing)
    """
    state = DaemonState(state_path=state_path)
    state.set("status", "running")
    state.set("started_at", datetime.now(timezone.utc).isoformat())

    iteration = 0
    logger.info(f"Daemon started (dry_run={dry_run}, interval={interval_seconds}s)")
    log_action("daemon_start", detail=f"dry_run={dry_run}", log_path=log_path)

    try:
        while True:
            iteration += 1
            state.increment("loops")
            state.set("last_loop_at", datetime.now(timezone.utc).isoformat())

            # Get next automatable item (skip already-processed ones)
            item = get_next_automatable_item(
                ledger_dir=ledger_dir, state=state,
            )

            if item:
                item_id = item.get("id", "")
                tool = item.get("_suggested_tool", "unknown")
                if dry_run:
                    risk, tool = classify_item(item)
                    log_action(
                        "dry_run", item.get("id", ""),
                        f"Would process: {item.get('title', '')[:80]} "
                        f"(tool={tool}, risk={risk})",
                        log_path=log_path,
                    )
                    state.increment("items_skipped")
                else:
                    result = process_item(item, log_path=log_path)
                    if result.get("status") == "escalated":
                        state.increment("items_escalated")
                    else:
                        state.increment("items_processed")
                # Mark item as processed so it is not retried next loop
                state.mark_processed(item_id)
                # Record tool cooldown so it is not called again within the window
                if tool and tool != "unknown":
                    state.record_tool_call(tool)
            else:
                log_action("idle", detail="No automatable items found",
                           log_path=log_path)

            # Check iteration limit
            if max_iterations > 0 and iteration >= max_iterations:
                break

            time.sleep(interval_seconds)

    except KeyboardInterrupt:
        logger.info("Daemon stopped by user")
    finally:
        state.set("status", "stopped")
        log_action("daemon_stop", detail=f"iterations={iteration}",
                   log_path=log_path)

    return state.state


def get_daemon_status(state_path: Optional[Path] = None,
                      log_path: Optional[Path] = None) -> dict:
    """Get current daemon status including recent log entries."""
    state = DaemonState(state_path=state_path)
    target_log = log_path or DAEMON_LOG

    # Read recent log entries
    recent = []
    if target_log.exists():
        lines = target_log.read_text().splitlines()
        for line in lines[-20:]:
            try:
                recent.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                pass

    return {
        **state.state,
        "recent_actions": recent[-10:],
        "log_path": str(target_log),
    }
