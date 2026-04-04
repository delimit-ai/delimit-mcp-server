"""Ledger Propose — AI-driven item generation from signals.

Analyzes repo state, sensing signals, completed work, and venture priorities
to propose 3-5 new ledger items with rationale. Runs at end of build loops
when the queue is empty, or on-demand.

Works across all AI models via MCP — no model-specific code.
"""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


SIGNALS_DIR = Path.home() / ".delimit" / "signals"
PROPOSALS_DIR = Path.home() / ".delimit" / "proposals"


def _gather_context(venture: str = "") -> Dict[str, Any]:
    """Collect context from multiple sources for proposal generation."""
    context = {
        "timestamp": time.time(),
        "venture": venture or "all",
        "sources": {},
    }

    # 1. Recent ledger completions (what just shipped)
    ledger_path = Path.home() / ".delimit" / "ledger.jsonl"
    if ledger_path.exists():
        recent_done = []
        for line in ledger_path.read_text().strip().split("\n")[-100:]:
            try:
                item = json.loads(line)
                if item.get("status") == "done":
                    recent_done.append({
                        "id": item.get("id", ""),
                        "title": item.get("title", ""),
                        "venture": item.get("venture", ""),
                    })
            except json.JSONDecodeError:
                continue
        context["sources"]["completed_work"] = recent_done[-10:]

    # 2. Open items (what's already tracked)
    open_items = []
    if ledger_path.exists():
        for line in ledger_path.read_text().strip().split("\n"):
            try:
                item = json.loads(line)
                if item.get("status") == "open":
                    open_items.append(item.get("title", ""))
            except json.JSONDecodeError:
                continue
    context["sources"]["open_items_count"] = len(open_items)
    context["sources"]["open_item_titles"] = open_items[:20]

    # 3. Sensing signals (GitHub issues, Reddit, migrations)
    signals_file = Path.home() / ".delimit" / "signals" / "recent.jsonl"
    if signals_file.exists():
        signals = []
        for line in signals_file.read_text().strip().split("\n")[-20:]:
            try:
                sig = json.loads(line)
                signals.append({
                    "type": sig.get("type", ""),
                    "title": sig.get("title", ""),
                    "source": sig.get("source", ""),
                    "relevance": sig.get("relevance", 0),
                })
            except json.JSONDecodeError:
                continue
        context["sources"]["signals"] = signals

    # 4. Git recent activity
    try:
        import subprocess
        result = subprocess.run(
            ["git", "log", "--oneline", "-10"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            context["sources"]["recent_commits"] = result.stdout.strip().split("\n")
    except Exception:
        pass

    # 5. Swarm state
    swarm_registry = Path.home() / ".delimit" / "swarm" / "agent_registry.json"
    if swarm_registry.exists():
        try:
            reg = json.loads(swarm_registry.read_text())
            context["sources"]["swarm_agents"] = len(reg.get("agents", {}))
        except json.JSONDecodeError:
            pass

    return context


def propose_items(
    venture: str = "",
    focus: str = "",
    max_items: int = 5,
) -> Dict[str, Any]:
    """Generate proposed ledger items based on current context.

    Analyzes completed work, open items, sensing signals, and repo state
    to suggest what to work on next. Returns structured proposals that
    can be added to the ledger with delimit_ledger_add.

    This is the AI's "what should I do next?" engine. Works with any model.

    Args:
        venture: Focus proposals on a specific venture.
        focus: Optional focus area (e.g., "outreach", "engineering", "security").
        max_items: Maximum number of proposals to generate.
    """
    context = _gather_context(venture)

    # Build proposal prompt from context
    proposals = []
    open_titles = set(context["sources"].get("open_item_titles", []))

    # Strategy 1: Follow-through on completed work
    for done in context["sources"].get("completed_work", []):
        title = done.get("title", "")
        if venture and done.get("venture", "") != venture:
            continue
        # Suggest follow-up actions
        if "deploy" in title.lower() or "publish" in title.lower():
            follow_up = f"Verify deployment: {title}"
            if follow_up not in open_titles:
                proposals.append({
                    "title": follow_up,
                    "rationale": f"Follow-through: '{title}' was shipped but may need verification",
                    "priority": "P1",
                    "type": "task",
                    "source": "ledger_propose:follow_through",
                })
        if "outreach" in title.lower() or "issue" in title.lower():
            follow_up = f"Monitor engagement: {title}"
            if follow_up not in open_titles:
                proposals.append({
                    "title": follow_up,
                    "rationale": f"Outreach needs monitoring for responses and engagement",
                    "priority": "P1",
                    "type": "task",
                    "source": "ledger_propose:follow_through",
                })

    # Strategy 2: Act on unprocessed signals
    for sig in context["sources"].get("signals", []):
        sig_title = sig.get("title", "")
        if sig_title and sig_title not in open_titles:
            proposals.append({
                "title": f"Evaluate signal: {sig_title[:80]}",
                "rationale": f"Unprocessed {sig.get('type', 'signal')} from {sig.get('source', 'unknown')}",
                "priority": "P1",
                "type": "strategy",
                "source": "ledger_propose:signal",
            })

    # Strategy 3: Detect gaps
    has_tests = any("test" in t.lower() for t in open_titles)
    has_docs = any("doc" in t.lower() or "readme" in t.lower() for t in open_titles)
    has_security = any("security" in t.lower() or "audit" in t.lower() for t in open_titles)

    if not has_tests and focus != "outreach":
        proposals.append({
            "title": f"Run test coverage analysis{f' for {venture}' if venture else ''}",
            "rationale": "No test-related items in queue — ensure coverage hasn't regressed",
            "priority": "P1",
            "type": "task",
            "source": "ledger_propose:gap_detection",
        })

    if not has_security and focus != "outreach":
        proposals.append({
            "title": f"Security audit{f' for {venture}' if venture else ''}",
            "rationale": "No security items in queue — periodic audits prevent surprises",
            "priority": "P2",
            "type": "task",
            "source": "ledger_propose:gap_detection",
        })

    if not has_docs:
        proposals.append({
            "title": f"Documentation freshness check{f' for {venture}' if venture else ''}",
            "rationale": "No doc items in queue — ensure README/CHANGELOG reflect current state",
            "priority": "P2",
            "type": "task",
            "source": "ledger_propose:gap_detection",
        })

    # Apply focus filter
    if focus:
        focus_lower = focus.lower()
        proposals = [p for p in proposals if focus_lower in p.get("title", "").lower()
                     or focus_lower in p.get("rationale", "").lower()
                     or focus_lower in p.get("type", "").lower()]

    # Deduplicate and limit
    seen = set()
    unique = []
    for p in proposals:
        key = p["title"][:50]
        if key not in seen:
            seen.add(key)
            unique.append(p)
    proposals = unique[:max_items]

    # Save proposals for audit trail
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    proposal_file = PROPOSALS_DIR / f"proposal_{int(time.time())}.json"
    proposal_file.write_text(json.dumps({
        "proposals": proposals,
        "context_summary": {
            "completed_work": len(context["sources"].get("completed_work", [])),
            "open_items": context["sources"].get("open_items_count", 0),
            "signals": len(context["sources"].get("signals", [])),
            "swarm_agents": context["sources"].get("swarm_agents", 0),
        },
        "venture": venture,
        "focus": focus,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, indent=2))

    return {
        "status": "ok",
        "proposals": proposals,
        "total": len(proposals),
        "venture": venture or "all",
        "focus": focus or "none",
        "context": {
            "completed_work_analyzed": len(context["sources"].get("completed_work", [])),
            "open_items": context["sources"].get("open_items_count", 0),
            "signals_analyzed": len(context["sources"].get("signals", [])),
        },
        "message": f"Generated {len(proposals)} proposal(s). "
                   "Use delimit_ledger_add to add approved items.",
    }
