"""Governed Executor for Continuous Build (LED-239).

Requirements (Consensus 123):
- root ledger in /root/.delimit is authoritative
- select only build-safe open items (feat, fix, task)
- resolve venture + repo before dispatch
- use Delimit swarm/governance as control plane
- every iteration must update ledger, audit trail, and session state
- no deploy/secrets/destructive actions without explicit gate
- enforce max-iteration, max-error, and max-cost safeguards
"""

import json
import logging
from datetime import datetime, timezone
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.ai.loop_engine")

# ── Configuration ────────────────────────────────────────────────────
ROOT_LEDGER_PATH = Path("/root/.delimit")
BUILD_SAFE_TYPES = ["feat", "fix", "task"]
SOCIAL_SAFE_TYPES = ["social", "outreach", "content", "sensor", "strategy"]
SIGNAL_TYPES = ["strategy"]  # Web scanner signals eligible for triage
MAX_ITERATIONS_DEFAULT = 10
MAX_COST_DEFAULT = 2.0
MAX_ERRORS_DEFAULT = 2
SOCIAL_SCAN_PLATFORMS = ["reddit", "x", "hn", "devto", "github", "web"]
SOCIAL_SCAN_VENTURES = ["delimit"]

# Web scanner signal prefixes (from social_target._scan_web)
WEB_SIGNAL_PREFIXES = {
    "competitor": "[COMPETITOR RELEASE]",
    "ecosystem": "[ECOSYSTEM]",
    "npm": "[NPM]",
    "venture": "[VENTURE SIGNAL]",
}

# LED-788: timeouts + observability for the social loop
SOCIAL_ITERATION_TIMEOUT = int(os.environ.get("DELIMIT_SOCIAL_ITERATION_TIMEOUT", "300"))  # 5 min
SOCIAL_STRATEGY_TIMEOUT = int(os.environ.get("DELIMIT_SOCIAL_STRATEGY_TIMEOUT", "120"))  # 2 min
SOCIAL_SCAN_TIMEOUT = int(os.environ.get("DELIMIT_SOCIAL_SCAN_TIMEOUT", "180"))  # 3 min total for all platform scans

# ── Session State ────────────────────────────────────────────────────
SESSION_DIR = Path.home() / ".delimit" / "loop" / "sessions"
HEARTBEAT_DIR = Path.home() / ".delimit" / "loop" / "heartbeat"


def _ensure_heartbeat_dir():
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)


def _write_heartbeat(session_id: str, stage: str, extra: Optional[Dict[str, Any]] = None) -> None:
    """LED-788: record the current loop stage + elapsed time.

    delimit_loop_status reads this so callers can see where an in-flight
    iteration is actually spending its time instead of staring at a stale
    snapshot of the last completed iteration.
    """
    try:
        _ensure_heartbeat_dir()
        payload = {
            "session_id": session_id,
            "stage": stage,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "ts": time.time(),
        }
        if extra:
            payload.update(extra)
        path = HEARTBEAT_DIR / f"{session_id}.json"
        path.write_text(json.dumps(payload, indent=2))
    except OSError as e:
        logger.debug("heartbeat write failed: %s", e)


def _read_heartbeat(session_id: str) -> Optional[Dict[str, Any]]:
    try:
        path = HEARTBEAT_DIR / f"{session_id}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        # Augment with elapsed seconds for the currently-running stage
        if "ts" in data:
            data["elapsed_seconds"] = round(time.time() - data["ts"], 1)
        return data
    except (OSError, json.JSONDecodeError):
        return None


def _run_stage_with_timeout(
    stage: str,
    fn,
    timeout_s: int,
    session_id: str = "",
) -> Dict[str, Any]:
    """Run a callable with a wall-clock timeout and timing instrumentation.

    Uses ThreadPoolExecutor so a hung HTTP client can be abandoned without
    killing the whole loop process. Returns a dict with keys:
      - ok: bool
      - value: return value on success
      - error: error string on failure
      - elapsed_seconds: wall-clock time
      - timed_out: True if the wall-clock deadline was hit
    """
    import threading

    start = time.time()
    if session_id:
        _write_heartbeat(session_id, stage)
    logger.info("[loop] stage=%s start timeout=%ss", stage, timeout_s)

    container: Dict[str, Any] = {"value": None, "error": None}

    def _runner():
        try:
            container["value"] = fn()
        except Exception as _exc:  # noqa: BLE001 — intentional broad catch
            container["error"] = _exc

    # Daemon thread so a hung worker cannot block interpreter shutdown.
    worker = threading.Thread(target=_runner, name=f"loop-stage-{stage}", daemon=True)
    worker.start()
    worker.join(timeout=timeout_s)
    elapsed = time.time() - start

    if worker.is_alive():
        logger.error("[loop] stage=%s TIMEOUT after %.1fs (limit=%ss)", stage, elapsed, timeout_s)
        return {
            "ok": False,
            "error": f"{stage} exceeded {timeout_s}s timeout",
            "elapsed_seconds": round(elapsed, 1),
            "timed_out": True,
        }
    if container["error"] is not None:
        logger.error("[loop] stage=%s failed after %.1fs: %s", stage, elapsed, container["error"])
        return {
            "ok": False,
            "error": str(container["error"]),
            "elapsed_seconds": round(elapsed, 1),
            "timed_out": False,
        }
    logger.info("[loop] stage=%s done elapsed=%.1fs", stage, elapsed)
    return {
        "ok": True,
        "value": container["value"],
        "elapsed_seconds": round(elapsed, 1),
        "timed_out": False,
    }


def _ensure_session_dir():
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

def _save_session(session: Dict[str, Any]):
    _ensure_session_dir()
    path = SESSION_DIR / f"{session['session_id']}.json"
    path.write_text(json.dumps(session, indent=2))

def create_governed_session(loop_type: str = "build") -> Dict[str, Any]:
    prefix = loop_type if loop_type in ("build", "social", "deploy") else "build"
    session_id = f"{prefix}-{uuid.uuid4().hex[:8]}"
    session = {
        "session_id": session_id,
        "type": f"governed_{prefix}",
        "loop_type": prefix,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "iterations": 0,
        "max_iterations": MAX_ITERATIONS_DEFAULT,
        "cost_incurred": 0.0,
        "cost_cap": MAX_COST_DEFAULT,
        "errors": 0,
        "error_threshold": MAX_ERRORS_DEFAULT,
        "tasks_completed": [],
        "status": "running"
    }
    _save_session(session)
    return session

# ── Venture & Repo Resolution ─────────────────────────────────────────

def resolve_venture_context(venture_name: str) -> Dict[str, str]:
    """Resolve a venture name to its project path and repo URL."""
    from ai.ledger_manager import list_ventures
    
    ventures = list_ventures().get("ventures", {})
    context = {"path": ".", "repo": "", "name": venture_name or "root"}
    
    if not venture_name or venture_name == "root":
        context["path"] = str(ROOT_LEDGER_PATH)
        return context

    if venture_name in ventures:
        v = ventures[venture_name]
        context["path"] = v.get("path", ".")
        context["repo"] = v.get("repo", "")
        return context
    
    # Fallback to fuzzy match
    for name, info in ventures.items():
        if venture_name.lower() in name.lower():
            context["path"] = info.get("path", ".")
            context["repo"] = info.get("repo", "")
            context["name"] = name
            return context
            
    return context

# ── Web Signal Triage (think→build pipeline) ────────────────────────

def _classify_web_signal(item: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Classify a web scanner strategy item into a triage action.

    Returns dict with keys: action, build_type, priority, title, description
    or None if the signal should be skipped.
    """
    title = item.get("title", "")
    desc = item.get("description", "")
    snippet = f"{title} {desc}".lower()

    # Competitor releases → assess feature parity need
    if WEB_SIGNAL_PREFIXES["competitor"].lower() in snippet or "competitor release" in snippet:
        return {
            "action": "build",
            "build_type": "task",
            "priority": "P1",
            "title": f"Assess: {title}",
            "description": (
                f"Web scanner detected competitor activity. Assess whether Delimit "
                f"needs a matching feature or response.\n\nOriginal signal: {desc[:500]}"
            ),
            "venture": item.get("venture", "delimit"),
            "source_signal": item.get("id", ""),
        }

    # Ecosystem build signals → assess threat or opportunity
    if WEB_SIGNAL_PREFIXES["ecosystem"].lower() in snippet:
        return {
            "action": "build",
            "build_type": "task",
            "priority": "P2",
            "title": f"Evaluate: {title}",
            "description": (
                f"Ecosystem signal detected. Assess if this is a threat, opportunity, "
                f"or integration target for Delimit.\n\nOriginal signal: {desc[:500]}"
            ),
            "venture": item.get("venture", "delimit"),
            "source_signal": item.get("id", ""),
        }

    # npm packages → check compete or complement
    if WEB_SIGNAL_PREFIXES["npm"].lower() in snippet:
        return {
            "action": "build",
            "build_type": "task",
            "priority": "P2",
            "title": f"npm scout: {title}",
            "description": (
                f"New npm package detected in Delimit's space. Determine if it "
                f"competes with or complements Delimit.\n\nOriginal signal: {desc[:500]}"
            ),
            "venture": "delimit",
            "source_signal": item.get("id", ""),
        }

    # Venture discovery → flag for founder review (never auto-build)
    if WEB_SIGNAL_PREFIXES["venture"].lower() in snippet:
        return {
            "action": "notify",
            "venture": item.get("venture", "jamsons"),
            "source_signal": item.get("id", ""),
        }

    return None


def triage_web_signals(session: Dict[str, Any], max_signals: int = 5) -> List[Dict[str, Any]]:
    """Consume strategy items created by the web scanner and convert to build tasks.

    This is the think→build pipeline:
    1. Find open strategy items with web scanner fingerprints
    2. Classify each signal (competitor, ecosystem, npm, venture)
    3. For build signals: create a feat/task item in the ledger
    4. For venture signals: send founder notification
    5. Mark the original strategy item as triaged

    Returns list of actions taken.
    """
    from ai.ledger_manager import list_items, add_item, update_item

    result = list_items(status="open", project_path=str(ROOT_LEDGER_PATH))
    items = []
    for ledger_items in result.get("items", {}).values():
        items.extend(ledger_items)

    # Find untriaged web scanner signals
    web_signals = []
    for item in items:
        if item.get("type") not in SIGNAL_TYPES:
            continue
        tags = item.get("tags", [])
        if "web-triaged" in tags:
            continue
        title = item.get("title", "")
        desc = item.get("description", "")
        snippet = f"{title} {desc}".lower()
        # Match web scanner output patterns
        if any(prefix.lower() in snippet for prefix in WEB_SIGNAL_PREFIXES.values()):
            web_signals.append(item)

    if not web_signals:
        return []

    actions = []
    for signal in web_signals[:max_signals]:
        classification = _classify_web_signal(signal)
        if not classification:
            continue

        if classification["action"] == "build":
            # Create a build-safe ledger item from the signal
            try:
                new_item = add_item(
                    title=classification["title"],
                    item_type=classification["build_type"],
                    priority=classification["priority"],
                    description=classification["description"],
                    venture=classification.get("venture", "delimit"),
                    project_path=str(ROOT_LEDGER_PATH),
                    tags=["web-signal", f"from:{classification.get('source_signal', '')}"],
                )
                actions.append({
                    "action": "created_build_task",
                    "source": signal.get("id"),
                    "new_item": new_item.get("id", "unknown"),
                    "type": classification["build_type"],
                    "priority": classification["priority"],
                })
            except Exception as e:
                logger.warning("Failed to create build item from signal %s: %s", signal.get("id"), e)
                continue

        elif classification["action"] == "notify":
            # Venture signals → founder review
            actions.append({
                "action": "notify_founder",
                "source": signal.get("id"),
                "venture": classification.get("venture", "jamsons"),
                "title": signal.get("title", ""),
            })

        # Mark signal as triaged so we don't process it again
        try:
            existing_tags = signal.get("tags", [])
            update_item(
                item_id=signal["id"],
                status="done",
                note=f"Triaged by build loop → {classification['action']}",
                project_path=str(ROOT_LEDGER_PATH),
            )
        except Exception as e:
            logger.warning("Failed to mark signal %s as triaged: %s", signal.get("id"), e)

    return actions


# ── Governed Selection ───────────────────────────────────────────────

def next_task(venture: str = "", max_risk: str = "", session_id: str = "") -> Dict[str, Any]:
    """Get the next task to work on. Wrapper for server.py compatibility."""
    session = create_governed_session() if not session_id else {"session_id": session_id, "status": "running", "iterations": 0, "max_iterations": 50, "cost_incurred": 0, "cost_cap": 5, "errors": 0, "error_threshold": 3, "tasks_done": 0, "auto_consensus": False}
    task = get_next_build_task(session)
    if task is None:
        from ai.ledger_manager import list_items
        result = list_items(status="open", project_path=str(ROOT_LEDGER_PATH))
        open_count = sum(len(v) for v in result.get("items", {}).values())
        return {"action": "CONSENSUS", "reason": f"No build-safe items found ({open_count} open items, none actionable)", "remaining_items": open_count, "session": session}
    return {"action": "BUILD", "task": task, "remaining_items": 0, "session": session}


def get_next_build_task(session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Select the next build-safe item from the authoritative root ledger."""
    from ai.ledger_manager import list_items
    
    # Authoritative root ledger check
    result = list_items(status="open", project_path=str(ROOT_LEDGER_PATH))
    items = []
    for ledger_items in result.get("items", {}).values():
        items.extend(ledger_items)
        
    # Filter build-safe items only
    actionable = []
    for item in items:
        if item.get("type") not in BUILD_SAFE_TYPES:
            continue
        # Skip items that explicitly require owner action or are not for AI
        tags = item.get("tags", [])
        if "owner-action" in tags or "manual" in tags:
            continue
        actionable.append(item)
        
    if not actionable:
        return None
        
    # Sort by priority
    priority_map = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    actionable.sort(key=lambda x: priority_map.get(x.get("priority", "P2"), 9))
    
    return actionable[0]

# ── Social Loop Task Selection ────────────────────────────────────────

def get_next_social_task(session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Select the next social/outreach item from the root ledger."""
    from ai.ledger_manager import list_items

    result = list_items(status="open", project_path=str(ROOT_LEDGER_PATH))
    items = []
    for ledger_items in result.get("items", {}).values():
        items.extend(ledger_items)

    actionable = []
    for item in items:
        if item.get("type") not in SOCIAL_SAFE_TYPES:
            continue
        tags = item.get("tags", [])
        if "manual" in tags:
            continue
        actionable.append(item)

    if not actionable:
        return None

    priority_map = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    actionable.sort(key=lambda x: priority_map.get(x.get("priority", "P2"), 9))
    return actionable[0]


def run_social_iteration(session_id: str) -> Dict[str, Any]:
    """Execute one governed social/think loop iteration.

    Cycle: scan platforms → draft replies → notify founder → handle social ledger items.
    """
    path = SESSION_DIR / f"{session_id}.json"
    if not path.exists():
        return {"error": f"Session {session_id} not found"}
    session = json.loads(path.read_text())

    if session["status"] != "running":
        return {"status": "stopped", "reason": f"Session status is {session['status']}"}
    if session["iterations"] >= session["max_iterations"]:
        session["status"] = "finished"
        _save_session(session)
        return {"status": "finished", "reason": "Max iterations reached"}
    if session["cost_incurred"] >= session["cost_cap"]:
        session["status"] = "stopped"
        _save_session(session)
        return {"status": "stopped", "reason": "Cost cap reached"}

    results = {"scans": [], "drafts_sent": 0, "ledger_task": None, "triage": [], "stage_timings": {}}
    iteration_start = time.time()
    _write_heartbeat(session_id, "iteration_start", {"iteration": session["iterations"] + 1})

    # 1. Scan all platforms via social_target pipeline (scan + draft + ledger)
    # LED-788: wall-clock timeout prevents a hung platform from eating the session
    def _do_scan_and_process():
        from ai.social_target import scan_targets, process_targets
        _targets = scan_targets(
            platforms=SOCIAL_SCAN_PLATFORMS,
            ventures=SOCIAL_SCAN_VENTURES,
            limit=10,
        )
        _processed = None
        if _targets:
            _processed = process_targets(_targets, draft_replies=True, create_ledger=True)
        return _targets, _processed

    scan_result = _run_stage_with_timeout(
        "social_scan_and_process",
        _do_scan_and_process,
        SOCIAL_SCAN_TIMEOUT,
        session_id=session_id,
    )
    results["stage_timings"]["scan_and_process"] = scan_result["elapsed_seconds"]
    if scan_result["ok"]:
        targets, processed = scan_result["value"]
        results["scans"] = [
            {"platform": t.get("platform"), "title": t.get("title", "")[:80]}
            for t in targets[:5]
        ]
        results["targets_found"] = len(targets)
        if processed:
            drafted_list = processed.get("drafted", []) or []
            ledger_list = processed.get("ledger_items", []) or []
            notifs_sent = sum(1 for d in drafted_list if d.get("notification_sent"))
            results["processed"] = {
                "drafts": len(drafted_list),
                "drafts_ready": notifs_sent,
                "drafts_suppressed": sum(1 for d in drafted_list if d.get("suppressed_reason")),
                "ledger_items": len(ledger_list),
                "notifications": notifs_sent,
            }
            results["drafts_sent"] = notifs_sent
    else:
        logger.error("Social scan failed: %s", scan_result.get("error"))
        session["errors"] += 1
        results["scan_error"] = scan_result.get("error")
        results["scan_timed_out"] = scan_result.get("timed_out", False)

    # 3. Triage web signals (think→build pipeline)
    _write_heartbeat(session_id, "triage_web_signals")
    triage_actions = triage_web_signals(session)
    if triage_actions:
        results["triage"] = [
            {"action": a.get("action"), "title": a.get("title", "")[:60]}
            for a in triage_actions
        ]

    # 4. Pick up social-typed ledger items
    social_task = get_next_social_task(session)
    if social_task:
        results["ledger_task"] = {"id": social_task["id"], "title": social_task.get("title", "")}
        try:
            from ai.ledger_manager import update_item
            update_item(
                item_id=social_task["id"],
                status="in_progress",
                note="Picked up by think loop",
                project_path=str(ROOT_LEDGER_PATH),
            )
        except Exception:
            pass

    # 5. Strategy deliberation (think): every 4th iteration to avoid rate limits
    # LED-788: strategy cycle wraps delimit_deliberate which easily hangs on
    # a single slow model — wall-clock cap so it can't eat the whole iteration.
    results["strategy"] = None
    if session["iterations"] % 4 == 0:
        strat_result = _run_stage_with_timeout(
            "strategy_cycle",
            lambda: _run_strategy_cycle(session),
            SOCIAL_STRATEGY_TIMEOUT,
            session_id=session_id,
        )
        results["stage_timings"]["strategy_cycle"] = strat_result["elapsed_seconds"]
        if strat_result["ok"]:
            results["strategy"] = strat_result["value"]
        else:
            logger.error("Strategy cycle failed: %s", strat_result.get("error"))
            results["strategy"] = {
                "error": strat_result.get("error"),
                "timed_out": strat_result.get("timed_out", False),
            }

    # LED-788: total iteration time — if we've overrun, mark the session so
    # the next iteration runs lighter (strategy cycle will still be rate-gated
    # by the %4 check, but the warning surfaces to operators).
    total_elapsed = round(time.time() - iteration_start, 1)
    results["stage_timings"]["total"] = total_elapsed
    if total_elapsed > SOCIAL_ITERATION_TIMEOUT:
        logger.error(
            "[loop] iteration %d took %.1fs, exceeding soft cap of %ss",
            session["iterations"] + 1, total_elapsed, SOCIAL_ITERATION_TIMEOUT,
        )
        results["iteration_overrun"] = True

    # 6. Update session
    _write_heartbeat(session_id, "iteration_complete", {"elapsed_seconds": total_elapsed})
    session["iterations"] += 1
    cost = 0.01 if not results.get("strategy") else 0.15  # deliberations cost more
    session["cost_incurred"] += cost
    session["tasks_completed"].append({
        "iteration": session["iterations"],
        "drafts_sent": results["drafts_sent"],
        "targets_scanned": len(results["scans"]),
        "ledger_task": results.get("ledger_task"),
        "strategy": results.get("strategy"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    _save_session(session)

    return {"status": "continued", "session_id": session_id, "results": results}


# ── Strategy Deliberation (think cycle) ───────────────────────────────

STRATEGY_LEDGER = Path("/root/.delimit/ledger/strategy.jsonl")
DELIBERATION_DIR = Path("/home/delimit/delimit-private/decisions")

def _get_open_strategy_items(limit: int = 6) -> List[Dict[str, Any]]:
    """Read open strategy items from the strategy ledger."""
    if not STRATEGY_LEDGER.exists():
        return []
    items = []
    for line in STRATEGY_LEDGER.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            if item.get("status", "open") == "open":
                items.append(item)
        except json.JSONDecodeError:
            continue
    priority_map = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    items.sort(key=lambda x: priority_map.get(x.get("priority", "P2"), 9))
    return items[:limit]


def _group_strategy_items(items: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Group related strategy items by venture/topic for batch deliberation."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        key = item.get("venture", item.get("tags", ["general"])[0] if item.get("tags") else "general")
        groups.setdefault(key, []).append(item)
    # Cap each group at 4 items
    return [g[:4] for g in groups.values()]


def _run_strategy_cycle(session: Dict[str, Any]) -> Dict[str, Any]:
    """Run one strategy deliberation cycle: pull items → group → deliberate → build tasks."""
    items = _get_open_strategy_items(limit=6)
    if not items:
        return {"status": "idle", "reason": "No open strategy items"}

    groups = _group_strategy_items(items)
    result = {"deliberations": 0, "build_tasks_created": 0, "items_closed": 0}

    # Process at most 1 group per cycle to stay within rate limits
    group = groups[0]
    item_refs = ", ".join(f"{i.get('id', '?')}: {i.get('title', '')[:40]}" for i in group)
    titles = " + ".join(i.get("id", "?") for i in group)

    question = (
        f"{titles}: {' | '.join(i.get('title', '') for i in group)}. "
        "What are the specific next steps to move these forward? "
        "Output as 3-5 specific operational tasks with titles and descriptions."
    )

    context = (
        f"Items: {item_refs}\n"
        f"Venture: {group[0].get('venture', 'delimit')}\n"
        f"Session: think loop iteration {session['iterations']}\n"
        f"Constraint: solo founder, all ventures parallel, ledger-based dev"
    )

    try:
        from ai.deliberation import deliberate as run_deliberation
        date_str = datetime.now(timezone.utc).strftime("%Y_%m_%d")
        topic = group[0].get("venture", "strategy").upper()
        save_path = str(DELIBERATION_DIR / f"DELIBERATION_{topic}_{date_str}.md")

        delib_result = run_deliberation(
            question=question,
            context=context,
            mode="debate",
            save_path=save_path,
        )
        result["deliberations"] = 1
        result["save_path"] = save_path

        # Close the strategy items
        from ai.ledger_manager import update_item
        for item in group:
            try:
                update_item(
                    item_id=item["id"],
                    status="done",
                    note=f"Deliberated in think loop. Transcript: {save_path}",
                    project_path=str(ROOT_LEDGER_PATH),
                )
                result["items_closed"] += 1
            except Exception:
                pass

    except Exception as e:
        logger.error("Deliberation failed for %s: %s", titles, e)
        result["error"] = str(e)

    return result


# ── Deploy Handoff (build→deploy pipeline) ──────────────────────────

DEPLOY_QUEUE_DIR = Path.home() / ".delimit" / "loop" / "deploy-queue"

def _ensure_deploy_queue():
    DEPLOY_QUEUE_DIR.mkdir(parents=True, exist_ok=True)


def _notify_deploy_loop(task: Dict[str, Any], venture: str, project_path: str,
                        session_id: str = "") -> Dict[str, Any]:
    """Signal the deploy loop that a build task completed and code is ready.

    Writes a deploy-ready item to the deploy queue. The deploy loop picks these
    up and runs commit → push → deploy gates → deploy for each venture.
    """
    _ensure_deploy_queue()

    item = {
        "task_id": task.get("id", "unknown"),
        "title": task.get("title", ""),
        "venture": venture,
        "project_path": project_path,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
    }

    queue_file = DEPLOY_QUEUE_DIR / "pending.jsonl"
    with open(queue_file, "a") as f:
        f.write(json.dumps(item) + "\n")

    logger.info("Deploy queue: added %s (%s) for %s", task.get("id"), venture, project_path)
    return item


def get_deploy_ready(venture: str = "") -> List[Dict[str, Any]]:
    """Get pending deploy-ready items, optionally filtered by venture.

    Called by the deploy loop to discover what the build loop produced.
    """
    _ensure_deploy_queue()
    queue_file = DEPLOY_QUEUE_DIR / "pending.jsonl"
    if not queue_file.exists():
        return []

    items = []
    for line in queue_file.read_text().strip().split("\n"):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if item.get("status") != "pending":
                continue
            if venture and item.get("venture", "") != venture:
                continue
            items.append(item)
        except json.JSONDecodeError:
            continue

    return items


def mark_deployed(task_id: str) -> bool:
    """Mark a deploy-queue item as deployed. Called by deploy loop after successful deploy."""
    _ensure_deploy_queue()
    queue_file = DEPLOY_QUEUE_DIR / "pending.jsonl"
    if not queue_file.exists():
        return False

    lines = queue_file.read_text().strip().split("\n")
    updated = False
    new_lines = []
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if item.get("task_id") == task_id and item.get("status") == "pending":
                item["status"] = "deployed"
                item["deployed_at"] = datetime.now(timezone.utc).isoformat()
                updated = True
            new_lines.append(json.dumps(item))
        except json.JSONDecodeError:
            new_lines.append(line)

    if updated:
        queue_file.write_text("\n".join(new_lines) + "\n")
    return updated


# ── Swarm Dispatch & Execution ───────────────────────────────────────

def loop_config(session_id: str = "", max_iterations: int = 0,
                cost_cap: float = 0.0, auto_consensus: bool = False,
                error_threshold: int = 0, status: str = "",
                require_approval_for: list = None) -> Dict[str, Any]:
    """Configure or create a loop session with safeguards."""
    _ensure_session_dir()

    # Load existing or create new
    if session_id:
        path = SESSION_DIR / f"{session_id}.json"
        if path.exists():
            session = json.loads(path.read_text())
        else:
            session = {
                "session_id": session_id,
                "type": "governed_build",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "iterations": 0,
                "max_iterations": max_iterations or MAX_ITERATIONS_DEFAULT,
                "cost_incurred": 0.0,
                "cost_cap": cost_cap or MAX_COST_DEFAULT,
                "errors": 0,
                "error_threshold": error_threshold or MAX_ERRORS_DEFAULT,
                "tasks_completed": [],
                "status": status or "running",
            }
    else:
        session = create_governed_session()

    # Apply non-zero/non-empty overrides
    if max_iterations > 0:
        session["max_iterations"] = max_iterations
    if cost_cap > 0:
        session["cost_cap"] = cost_cap
    if error_threshold > 0:
        session["error_threshold"] = error_threshold
    if status:
        session["status"] = status
    if auto_consensus:
        session["auto_consensus"] = True
    if require_approval_for is not None:
        session["require_approval_for"] = require_approval_for

    _save_session(session)
    return {
        "session_id": session["session_id"],
        "status": session["status"],
        "max_iterations": session["max_iterations"],
        "iterations": session.get("iterations", 0),
        "cost_cap": session["cost_cap"],
        "cost_incurred": session.get("cost_incurred", 0.0),
        "error_threshold": session["error_threshold"],
        "errors": session.get("errors", 0),
    }


def run_governed_iteration(session_id: str, hardening: Optional[Any] = None) -> Dict[str, Any]:
    """Execute one governed build iteration.

    Args:
        session_id: The session to advance.
        hardening: Optional GovernanceHardeningConfig from ai.governance_hardening.
                   When provided, dispatch calls are wrapped with retry, debounce,
                   and circuit-breaker protection.  When None (default), behavior
                   is unchanged from the original implementation.
    """
    from datetime import datetime, timezone
    import importlib
    import ai.swarm as _swarm_mod
    importlib.reload(_swarm_mod)
    from ai.swarm import dispatch_task

    # 1. Load Session & Check Safeguards
    path = SESSION_DIR / f"{session_id}.json"
    if not path.exists():
        return {"error": f"Session {session_id} not found"}
    session = json.loads(path.read_text())

    if session["status"] != "running":
        return {"status": "stopped", "reason": f"Session status is {session['status']}"}

    if session["iterations"] >= session["max_iterations"]:
        session["status"] = "finished"
        _save_session(session)
        return {"status": "finished", "reason": "Max iterations reached"}

    if session["cost_incurred"] >= session["cost_cap"]:
        session["status"] = "stopped"
        _save_session(session)
        return {"status": "stopped", "reason": "Cost cap reached"}

    # 1b. Triage web scanner signals (think→build pipeline)
    triage_actions = triage_web_signals(session)
    if triage_actions:
        logger.info("Web signal triage: %d actions taken", len(triage_actions))
        # If we created new build tasks, they'll be picked up in task selection below
        # If we need to notify founder for venture signals, do it now
        for action in triage_actions:
            if action.get("action") == "notify_founder":
                try:
                    from ai.notify import send_notification
                    send_notification(
                        message=(
                            f"[VENTURE SIGNAL] {action.get('title', 'New venture opportunity')}\n"
                            f"Source: {action.get('source', 'web scanner')}\n"
                            f"Venture: {action.get('venture', 'jamsons')}\n"
                            f"Action: Founder review needed before acting"
                        ),
                        channel="email",
                        priority="P1",
                    )
                except Exception as e:
                    logger.warning("Failed to notify founder for venture signal: %s", e)

    # 2. Select Task
    task = get_next_build_task(session)
    if not task:
        return {"status": "idle", "reason": "No build-safe items in ledger", "triage_actions": triage_actions}

    # 3. Resolve Context
    v_name = task.get("venture", "root")
    ctx = resolve_venture_context(v_name)

    # 4. Dispatch through Swarm (Control Plane)
    logger.info(f"Dispatching build task {task['id']} for venture {v_name}")

    start_time = time.time()
    try:
        # LED-661: Route through governance hardening stack when configured
        dispatch_kwargs = dict(
            title=task["title"],
            description=task["description"],
            context=f"Executing governed build loop for {v_name}. Ledger ID: {task['id']}",
            project_path=ctx["path"],
            priority=task["priority"],
        )

        if hardening is not None and hardening.is_active():
            from ai.governance_hardening import hardened_dispatch
            dispatch_result = hardened_dispatch(
                hardening, dispatch_task,
                tool_name="dispatch_task",
                **dispatch_kwargs,
            )
            # hardened_dispatch may return a control dict (debounced/circuit_open)
            if isinstance(dispatch_result, dict) and dispatch_result.get("status") in ("debounced", "circuit_open"):
                session["tasks_completed"].append({
                    "id": task["id"],
                    "status": dispatch_result["status"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                _save_session(session)
                return {"status": dispatch_result["status"], "task_id": task["id"], "detail": dispatch_result}
        else:
            # Original path: direct dispatch, no hardening
            dispatch_result = dispatch_task(**dispatch_kwargs)

        # 5. Update State & Ledger
        duration = time.time() - start_time
        cost = dispatch_result.get("estimated_cost", 0.05) # Default placeholder if missing

        session["iterations"] += 1
        session["cost_incurred"] += cost

        from ai.ledger_manager import update_item
        if dispatch_result.get("status") == "completed":
            update_item(
                item_id=task["id"],
                status="done",
                note=f"Completed via governed build loop. Result: {dispatch_result.get('summary', 'OK')}",
                project_path=str(ROOT_LEDGER_PATH)
            )
            session["tasks_completed"].append({
                "id": task["id"],
                "status": "success",
                "duration": duration,
                "cost": cost
            })
            # 5b. Signal deploy loop that code is ready
            try:
                _notify_deploy_loop(
                    task=task,
                    venture=v_name,
                    project_path=ctx["path"],
                    session_id=session_id,
                )
            except Exception as e:
                logger.warning("Failed to notify deploy loop for %s: %s", task.get("id"), e)
        else:
            session["errors"] += 1
            if session["errors"] >= session["error_threshold"]:
                session["status"] = "circuit_broken"
            session["tasks_completed"].append({
                "id": task["id"],
                "status": "failed",
                "error": dispatch_result.get("error", "Dispatch failed")
            })

        _save_session(session)
        return {"status": "continued", "task_id": task["id"], "result": dispatch_result}

    except Exception as e:
        session["errors"] += 1
        _save_session(session)
        return {"error": str(e)}

def loop_status(session_id: str = "") -> Dict[str, Any]:
    """Check autonomous loop metrics for a session."""
    _ensure_session_dir()
    if session_id:
        path = SESSION_DIR / f"{session_id}.json"
        if not path.exists():
            return {"error": f"Session {session_id} not found"}
        session = json.loads(path.read_text())
    else:
        # Find most recent session
        sessions = sorted(SESSION_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not sessions:
            return {"error": "No loop sessions found"}
        session = json.loads(sessions[0].read_text())

    heartbeat = _read_heartbeat(session["session_id"])  # LED-788: live stage + elapsed
    return {
        "session_id": session["session_id"],
        "status": session.get("status", "unknown"),
        "iterations": session.get("iterations", 0),
        "max_iterations": session.get("max_iterations", MAX_ITERATIONS_DEFAULT),
        "cost_incurred": session.get("cost_incurred", 0.0),
        "cost_cap": session.get("cost_cap", MAX_COST_DEFAULT),
        "errors": session.get("errors", 0),
        "error_threshold": session.get("error_threshold", MAX_ERRORS_DEFAULT),
        "tasks_completed": session.get("tasks_completed", []),
        "started_at": session.get("started_at", ""),
        "heartbeat": heartbeat,
    }


def task_complete(task_id: str, status: str = "done", note: str = "", session_id: str = "") -> Dict[str, Any]:
    """Mark a task as complete within a loop session."""
    from ai.ledger_manager import update_item

    result = update_item(
        item_id=task_id,
        status=status,
        note=note or f"Completed via governed build loop",
        project_path=str(ROOT_LEDGER_PATH),
    )

    # Update session if provided
    if session_id:
        path = SESSION_DIR / f"{session_id}.json"
        if path.exists():
            session = json.loads(path.read_text())
            session["tasks_completed"].append({
                "id": task_id,
                "status": status,
                "note": note,
            })
            _save_session(session)

    return {"task_id": task_id, "status": status, "ledger_update": result}


if __name__ == "__main__":
    pass
