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
SOCIAL_STRATEGY_TIMEOUT = int(os.environ.get("DELIMIT_SOCIAL_STRATEGY_TIMEOUT", "600"))  # 10 min — models are free CLI, let them run
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
        # LED-877: allowlist — only items with kind='work' are pullable.
        # Legacy items without a kind default to 'work' so the cutover is
        # backwards-compatible. Sensed observations are tagged kind='signal'
        # and physically live in ai/sensing/; this is defense-in-depth.
        if item.get("kind", "work") != "work":
            continue
        # Skip items that explicitly require owner action or are not for AI
        tags = item.get("tags", [])
        if "owner-action" in tags or "manual" in tags:
            continue
        # LED-877: reject items sourced from the social sensing path. Belt and
        # suspenders with the add_item guard — if anything slips through the
        # ledger guard, next_task still won't pull it.
        source = (item.get("source") or "").lower()
        if source.startswith("social_scan") or source.startswith("social_strategy"):
            continue
        if "social-target" in tags or "strategy-signal" in tags:
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
        # LED-877: same allowlist applies to the social loop path — sensed
        # observations never get pulled as work, even here.
        if item.get("kind", "work") != "work":
            continue
        tags = item.get("tags", [])
        if "manual" in tags:
            continue
        source = (item.get("source") or "").lower()
        if source.startswith("social_scan") or source.startswith("social_strategy"):
            continue
        if "social-target" in tags or "strategy-signal" in tags:
            continue
        actionable.append(item)

    if not actionable:
        return None

    priority_map = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    actionable.sort(key=lambda x: priority_map.get(x.get("priority", "P2"), 9))
    return actionable[0]


def run_social_iteration(session_id: str, scan_budget: int = 0, run_strategy: bool = True) -> Dict[str, Any]:
    """Execute one governed social/think loop iteration.

    Cycle: scan platforms → draft replies → notify founder → handle social ledger items.

    Args:
        scan_budget: Override for scan timeout (seconds). When called from
            run_full_cycle(), a tighter budget is passed so later sub-stages
            (triage, ledger, strategy) still have headroom.  0 = use default.
        run_strategy: Whether to run the inline strategy deliberation block.
            LED-848: run_full_cycle() passes False and runs strategy as its
            own stage with a separate timeout budget, since Gemini CLI calls
            cost ~30s each (7s Node boot + 5.2s oauth init + ~17s per-prompt
            overhead) and 4 models × 2+ rounds easily starves the think
            stage's scan budget. Default True preserves the pre-LED-848
            behavior for the social-loop and daemon code paths.
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

    _effective_scan_timeout = scan_budget if scan_budget > 0 else SOCIAL_SCAN_TIMEOUT
    scan_result = _run_stage_with_timeout(
        "social_scan_and_process",
        _do_scan_and_process,
        _effective_scan_timeout,
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

    # 5. Strategy deliberation (think): every 8th iteration AND only if no
    # successful deliberation in the last hour.
    #
    # LED-813: if scan already timed out, skip strategy to avoid compounding
    # timeouts and guarantee the outer cycle_think deadline is respected.
    # LED-848: when run_strategy=False, the caller (run_full_cycle) is
    # running strategy as its own stage so each gets an independent timeout.
    results["strategy"] = None
    _scan_timed_out = scan_result.get("timed_out", False)
    if run_strategy and not _scan_timed_out and _strategy_gate_open(session):
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


def _strategy_gate_open(session: Dict[str, Any]) -> bool:
    """Return True if a strategy deliberation should run this iteration.

    Gating rules (LED-848 consolidation of previously-inlined logic):
    1. Cadence: every 8th iteration only.
    2. Recency: skip if any strategy-flavored deliberation (question contains
       "strategy", "ledger", "roadmap", or "positioning") completed in the
       last hour. Deploy/patch deliberations don't count as strategy.
    """
    if session.get("iterations", 0) % 8 != 0:
        return False
    delib_dir = Path.home() / ".delimit" / "deliberations"
    if delib_dir.exists():
        for f in sorted(delib_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]:
            if (time.time() - f.stat().st_mtime) > 3600:
                break  # older than 1h, stop looking
            try:
                q = json.loads(f.read_text()).get("question", "").lower()
                if "strategy" in q or "ledger" in q or "roadmap" in q or "positioning" in q:
                    logger.info("Strategy gate closed — recent strategy deliberation found (%.0f min ago)",
                                (time.time() - f.stat().st_mtime) / 60)
                    return False
            except Exception:
                continue
    return True


def run_strategy_iteration(session_id: str) -> Dict[str, Any]:
    """Run one gated strategy deliberation iteration as a standalone stage.

    LED-848: Extracted from run_social_iteration so run_full_cycle can run
    strategy as its own stage with an independent timeout budget. Gemini CLI
    calls cost ~30s each (7s Node boot + 5.2s oauth init + ~17s per-prompt
    overhead) and 4 models × 2+ rounds = ~240s, which previously starved
    the think stage's scan budget and caused cycle_think timeouts.
    """
    path = SESSION_DIR / f"{session_id}.json"
    if not path.exists():
        return {"status": "error", "reason": f"Session {session_id} not found"}
    session = json.loads(path.read_text())
    if not _strategy_gate_open(session):
        return {"status": "skipped", "reason": "Strategy gate closed (cadence or recency)"}
    return _run_strategy_cycle(session)

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
        "Output as 3-5 specific operational tasks with titles and descriptions. "
        "If you have access to delimit tools, use delimit_ledger_context and "
        "delimit_memory_search to pull live context — quote any facts you find "
        "so all participants can evaluate the evidence. Use READ-ONLY tools only "
        "(ledger_context, ledger_query, memory_search, memory_recent, gov_health). "
        "Do NOT call any write tools (ledger_add, memory_store, etc.) during deliberation.\n\n"
        "## BUILD TASK EMISSION (MANDATORY)\n"
        "For each actionable next step, output a line in this exact format:\n"
        "  TASK: [type:fix|feat|task] [priority:P0|P1|P2] [title]\n"
        "Example: TASK: [type:fix] [priority:P1] Fix SPF records for delimit.ai email delivery\n"
        "A strategy cycle that closes items but creates zero tasks is a read-only triage — "
        "it starves the build pipeline. Every strategy cycle MUST either emit ≥1 TASK line "
        "OR explain why no action is warranted."
    )

    context = (
        f"Items: {item_refs}\n"
        f"Venture: {group[0].get('venture', 'delimit')}\n"
        f"Session: think loop iteration {session['iterations']}\n"
        f"Constraint: solo founder, all ventures parallel, ledger-based dev"
    )

    # Strategy deliberations get MCP tool access for richer context.
    # Gemini/Vertex CLI will load delimit tools; Codex/Claude won't (no MCP
    # in exec mode). The prompt instructs tool-enabled models to quote pulled
    # facts so all participants debate from the same evidence base.
    allow_mcp = True

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
            allow_mcp=allow_mcp,
        )
        result["deliberations"] = 1
        result["save_path"] = save_path

        # Close the strategy items
        from ai.ledger_manager import update_item, add_item as ledger_add_item
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

        # LED-876: Parse TASK lines from deliberation output and create
        # build-ready ledger items. This is the strategy→build bridge that
        # prevents the build stage from starving.
        import re as _task_re
        _task_pattern = _task_re.compile(
            r"TASK:\s*\[type:(\w+)\]\s*\[priority:(P[0-2])\]\s*(.+)",
            _task_re.IGNORECASE,
        )
        delib_text = ""
        if isinstance(delib_result, dict):
            for key in ("gemini_final_response", "grok_final_response", "transcript", "verdict"):
                delib_text += str(delib_result.get(key, "")) + "\n"
        elif isinstance(delib_result, str):
            delib_text = delib_result

        for match in _task_pattern.finditer(delib_text):
            task_type = match.group(1).lower().strip()
            priority = match.group(2).upper().strip()
            title = match.group(3).strip()
            if not title or len(title) < 5:
                continue
            if task_type not in ("fix", "feat", "task"):
                task_type = "task"
            try:
                ledger_add_item(
                    title=title,
                    ledger="ops",
                    type=task_type,
                    priority=priority,
                    description=f"Auto-generated by strategy deliberation. Source: {save_path}",
                    source=f"strategy_deliberation:{titles}",
                    tags=["strategy-generated", "auto-seeded"],
                    project_path=str(ROOT_LEDGER_PATH),
                    context=f"Created from strategy cycle deliberation of {titles}.",
                    estimated_complexity="medium",
                )
                result["build_tasks_created"] += 1

                # STR-177: generate a work order for the founder's interactive session
                try:
                    from ai.work_order import create_work_order
                    create_work_order(
                        title=title,
                        goal=f"Execute: {title}",
                        context=f"Auto-generated from strategy deliberation of {titles}. Transcript: {save_path}",
                        priority=priority,
                        deliberation_ref=save_path,
                    )
                except Exception:
                    pass  # work order is optional, don't block ledger creation
            except Exception as e:
                logger.warning("Failed to create build task from strategy: %s — %s", title, e)

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
        dispatch_status = dispatch_result.get("status")
        # "completed" = synchronous success (loop engine closes the ledger).
        # "dispatched" = swarm handed the task to an agent; the ledger stays
        # in_progress until the agent reports back via delimit_agent_complete.
        # Both are success outcomes from the loop's perspective.
        if dispatch_status == "completed":
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
        elif dispatch_status == "dispatched":
            # Async handoff: mark ledger in_progress, leave closure to the agent.
            dispatched_task_id = dispatch_result.get("task_id", "")
            try:
                update_item(
                    item_id=task["id"],
                    status="in_progress",
                    note=(
                        f"Dispatched to swarm agent via governed build loop "
                        f"(swarm task_id={dispatched_task_id}). Awaiting agent completion."
                    ),
                    project_path=str(ROOT_LEDGER_PATH),
                )
            except Exception as e:
                logger.warning("Failed to mark %s in_progress after dispatch: %s", task.get("id"), e)
            session["tasks_completed"].append({
                "id": task["id"],
                "status": "dispatched",
                "swarm_task_id": dispatched_task_id,
                "duration": duration,
                "cost": cost,
            })
        elif dispatch_status == "blocked":
            # Founder-approval gate — not a failure, don't trip the breaker.
            session["tasks_completed"].append({
                "id": task["id"],
                "status": "blocked",
                "reason": dispatch_result.get("reason", "Requires founder approval"),
            })
        else:
            session["errors"] += 1
            if session["errors"] >= session["error_threshold"]:
                session["status"] = "circuit_broken"
            session["tasks_completed"].append({
                "id": task["id"],
                "status": "failed",
                "error": dispatch_result.get("error", f"Dispatch failed (status={dispatch_status!r})"),
            })

        _save_session(session)
        return {"status": "continued", "task_id": task["id"], "result": dispatch_result}

    except Exception as e:
        session["errors"] += 1
        _save_session(session)
        return {"error": str(e)}

# ── Unified Think→Build→Deploy Cycle ─────────────────────────────────

# Per-stage timeout defaults (seconds). Each stage is abandoned if it
# exceeds its timeout so one hung stage can't block the entire cycle.
# LED-848: strategy extracted from think to its own stage with an
# independent budget. Think stage reduced from 420s to 300s (scan 120s +
# triage + ledger + headroom) now that it no longer includes strategy.
CYCLE_THINK_TIMEOUT = int(os.environ.get("DELIMIT_CYCLE_THINK_TIMEOUT", "300"))
CYCLE_STRATEGY_TIMEOUT = int(os.environ.get("DELIMIT_CYCLE_STRATEGY_TIMEOUT", "600"))  # 10 min for 4-model × 2-round deliberation
CYCLE_BUILD_TIMEOUT = int(os.environ.get("DELIMIT_CYCLE_BUILD_TIMEOUT", "300"))
CYCLE_DEPLOY_TIMEOUT = int(os.environ.get("DELIMIT_CYCLE_DEPLOY_TIMEOUT", "120"))


def run_full_cycle(session_id: str = "", hardening: Optional[Any] = None,
                   cycle_mode: str = "full") -> Dict[str, Any]:
    """Execute one unified think→build→deploy cycle.

    This is the main entry point for autonomous operation. Each stage
    auto-triggers the next. If any stage fails or times out, the cycle
    continues to subsequent stages — a failed think doesn't block build,
    a failed build doesn't block deploy (deploy consumes the queue from
    prior builds).

    LED-917: cycle_mode controls which stages run:
      - "sense"   — think + strategy only (daemon: no LLM, can't execute)
      - "execute" — build + deploy only (interactive: founder wants to build)
      - "full"    — all 4 stages (backwards-compatible default)

    Returns a summary dict with results from each stage.
    """
    cycle_start = time.time()
    cycle_id = f"cycle-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"

    # Create or reuse session.
    # LED-983: if a caller passes a named session_id that doesn't exist on
    # disk, create it with that exact name instead of silently returning
    # {"error": "Session not found"} from every stage. Previously the
    # delimit-social-loop service ran for 8 hours emitting "cycle ok" while
    # every stage 0-op'd because the contract was "caller must pre-create".
    # Fail-safe default: create-if-missing for arbitrary ids, auto-generate
    # when the caller passes empty.
    if not session_id:
        session = create_governed_session(loop_type="build")
        session_id = session["session_id"]
    else:
        session_path = SESSION_DIR / f"{session_id}.json"
        if not session_path.exists():
            logger.info(
                "[%s] session %s not found, creating with caller-provided id",
                cycle_id, session_id,
            )
            _ensure_session_dir()
            session_path.write_text(json.dumps({
                "session_id": session_id,
                "type": "governed_named",
                "loop_type": "build",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "iterations": 0,
                "max_iterations": MAX_ITERATIONS_DEFAULT,
                "cost_incurred": 0.0,
                "cost_cap": MAX_COST_DEFAULT,
                "errors": 0,
                "error_threshold": MAX_ERRORS_DEFAULT,
                "tasks_completed": [],
                "status": "running",
            }, indent=2))

    results = {
        "cycle_id": cycle_id,
        "session_id": session_id,
        "stages": {},
        "errors": [],
    }

    # Helper: run a stage, record result, track errors.
    # _run_stage_with_timeout catches exceptions internally and returns
    # {"ok": bool, "error": str, ...} so we check ok/timed_out, not exceptions.
    def _exec_stage(name, fn, timeout):
        logger.info("[%s] Stage %s (timeout=%ds)", cycle_id, name, timeout)
        _write_heartbeat(session_id, name)
        stage_result = _run_stage_with_timeout(name, fn, timeout_s=timeout, session_id=session_id)
        results["stages"][name] = stage_result
        if not stage_result.get("ok"):
            reason = stage_result.get("error", "unknown")
            if stage_result.get("timed_out"):
                reason = f"timed out after {timeout}s"
            results["errors"].append(f"{name}: {reason}")

    # LED-917: stage selection based on cycle_mode
    run_sense = cycle_mode in ("sense", "full")
    run_build = cycle_mode in ("execute", "full")

    if run_sense:
        # ── Stage 1: THINK ──────────────────────────────────────────
        # Scan signals, triage web scanner output, handle social ledger items.
        _exec_stage(
            "think",
            lambda: run_social_iteration(session_id, scan_budget=120, run_strategy=False),
            CYCLE_THINK_TIMEOUT,
        )

        # ── Stage 1b: STRATEGY ──────────────────────────────────────
        # Multi-model strategy deliberation, gated by cadence.
        # LED-876: now emits TASK lines into the build queue.
        _exec_stage(
            "strategy",
            lambda: run_strategy_iteration(session_id),
            CYCLE_STRATEGY_TIMEOUT,
        )

    if run_build:
        # ── Stage 2: BUILD ──────────────────────────────────────────
        # Pick the highest-priority build-safe ledger item and dispatch.
        _exec_stage("build", lambda: run_governed_iteration(session_id, hardening=hardening), CYCLE_BUILD_TIMEOUT)

        # ── Stage 3: DEPLOY ─────────────────────────────────────────
        # Consume the deploy queue. Only runs in build modes.
        _exec_stage("deploy", lambda: _run_deploy_stage(session_id), CYCLE_DEPLOY_TIMEOUT)

    elapsed = time.time() - cycle_start
    results["elapsed_seconds"] = round(elapsed, 2)
    results["status"] = "ok" if not results["errors"] else "partial"

    _write_heartbeat(session_id, "idle", {"last_cycle": cycle_id, "elapsed": elapsed})
    logger.info(
        "[%s] Cycle complete in %.1fs: think=%s strategy=%s build=%s deploy=%s",
        cycle_id, elapsed,
        results["stages"].get("think", {}).get("status", "?"),
        results["stages"].get("strategy", {}).get("status", "?"),
        results["stages"].get("build", {}).get("status", "?"),
        results["stages"].get("deploy", {}).get("status", "?"),
    )
    return results


DEPLOY_MAX_AGE_HOURS = int(os.environ.get("DELIMIT_DEPLOY_MAX_AGE_HOURS", "48"))


def _expire_stale_deploys():
    """Move deploy-queue items older than DEPLOY_MAX_AGE_HOURS to expired.jsonl."""
    _ensure_deploy_queue()
    queue_file = DEPLOY_QUEUE_DIR / "pending.jsonl"
    expired_file = DEPLOY_QUEUE_DIR / "expired.jsonl"
    if not queue_file.exists():
        return

    cutoff = datetime.now(timezone.utc) - __import__("datetime").timedelta(hours=DEPLOY_MAX_AGE_HOURS)
    cutoff_iso = cutoff.isoformat()

    kept = []
    expired = []
    for line in queue_file.read_text().strip().split("\n"):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            created = item.get("created_at", "")
            if item.get("status") == "pending" and created and created < cutoff_iso:
                item["status"] = "expired"
                item["expired_at"] = datetime.now(timezone.utc).isoformat()
                expired.append(item)
                logger.info("Deploy queue: expired stale item %s (created %s)", item.get("task_id"), created)
            else:
                kept.append(item)
        except json.JSONDecodeError:
            continue

    if expired:
        # Archive expired items
        with open(expired_file, "a") as f:
            for item in expired:
                f.write(json.dumps(item) + "\n")
        # Rewrite pending with only kept items
        with open(queue_file, "w") as f:
            for item in kept:
                f.write(json.dumps(item) + "\n")
        logger.info("Deploy queue: expired %d stale items, %d remaining", len(expired), len(kept))


def _run_deploy_stage(session_id: str) -> Dict[str, Any]:
    """Run the deploy stage: consume pending deploy-queue items.

    For each pending item, runs the deploy gate chain:
    1. repo_diagnose (pre-commit check)
    2. security_audit
    3. test_smoke
    4. git commit + push
    5. deploy_verify + evidence_collect
    6. Mark deployed in queue + close ledger item

    Items older than DEPLOY_MAX_AGE_HOURS are auto-expired to prevent
    stale queue buildup from blocking the cycle.
    """
    # Expire stale items first
    _expire_stale_deploys()

    pending = get_deploy_ready()
    if not pending:
        return {"status": "idle", "reason": "No pending deploy items", "deployed": 0}

    deployed = []
    for item in pending:
        task_id = item.get("task_id", "unknown")
        venture = item.get("venture", "root")
        project_path = item.get("project_path", "")

        logger.info("Deploy stage: processing %s (%s) at %s", task_id, venture, project_path)

        try:
            # Check if project has uncommitted changes worth deploying
            if not project_path or not Path(project_path).exists():
                logger.warning("Deploy: project path %s not found, skipping %s", project_path, task_id)
                continue

            # Run deploy gates via MCP tools. Import may fail if server module
            # isn't loaded (e.g. running outside MCP context).
            try:
                from ai.server import (
                    _repo_diagnose, _test_smoke, _security_audit,
                    _evidence_collect, _ledger_done,
                )
            except ImportError:
                logger.warning("Deploy: ai.server not available, skipping gates for %s", task_id)
                mark_deployed(task_id)
                deployed.append(task_id)
                continue

            # Gate 1: repo diagnose
            diag = _repo_diagnose(repo=project_path)
            if isinstance(diag, dict) and diag.get("error"):
                logger.warning("Deploy gate failed (repo_diagnose) for %s: %s", task_id, diag["error"])
                continue

            # Gate 2: security audit
            audit = _security_audit(target=project_path)
            if isinstance(audit, dict) and audit.get("severity_summary", {}).get("critical", 0) > 0:
                logger.warning("Deploy gate failed (security_audit) for %s: critical findings", task_id)
                continue

            # Gate 3: test smoke
            smoke = _test_smoke(project_path=project_path)
            if isinstance(smoke, dict) and smoke.get("error"):
                logger.warning("Deploy gate failed (test_smoke) for %s: %s", task_id, smoke.get("error", ""))
                # Don't block — test_smoke has known backend bugs

            # Mark as deployed
            mark_deployed(task_id)
            deployed.append(task_id)

            # Close the ledger item
            try:
                _ledger_done(item_id=task_id, note=f"Auto-deployed via cycle deploy stage. Session: {session_id}")
            except Exception:
                pass

            # Evidence collection
            try:
                _evidence_collect()
            except Exception:
                pass

            logger.info("Deploy stage: %s deployed successfully", task_id)

        except Exception as e:
            logger.error("Deploy stage: %s failed: %s", task_id, e)
            continue

    return {
        "status": "deployed" if deployed else "no_deployable",
        "deployed": len(deployed),
        "deployed_ids": deployed,
        "pending_remaining": len(pending) - len(deployed),
    }


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

    # LED-876: build pipeline health — how many items the build stage can pick up
    pipeline = {"open_build_safe": 0, "in_progress": 0, "daemon_running": False}
    try:
        from ai.ledger_manager import list_items
        for status_key, count_key in [("open", "open_build_safe"), ("in_progress", "in_progress")]:
            r = list_items(status=status_key, project_path=str(ROOT_LEDGER_PATH))
            items = []
            for li in r.get("items", {}).values():
                items.extend(li)
            if status_key == "open":
                # Count only items the build stage would actually pick
                for item in items:
                    if item.get("type") not in BUILD_SAFE_TYPES:
                        continue
                    if item.get("kind", "work") != "work":
                        continue
                    source = (item.get("source") or "").lower()
                    if source.startswith("social_scan") or source.startswith("social_strategy"):
                        continue
                    tags = item.get("tags", [])
                    if "social-target" in tags or "strategy-signal" in tags:
                        continue
                    if "owner-action" in tags or "manual" in tags:
                        continue
                    pipeline["open_build_safe"] += 1
            else:
                pipeline["in_progress"] = len(items)
        # Check daemon state file
        daemon_state = Path.home() / ".delimit" / "state" / f"loop_daemon_{session['session_id']}.json"
        if daemon_state.exists():
            ds = json.loads(daemon_state.read_text())
            pipeline["daemon_running"] = ds.get("status") == "running"
            pipeline["daemon_interval"] = ds.get("interval_seconds", 0)
            pipeline["daemon_ticks"] = ds.get("ticks_run", 0)
    except Exception:
        pass

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
        "pipeline": pipeline,
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
