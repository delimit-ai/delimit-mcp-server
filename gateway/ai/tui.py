"""Delimit TUI — Terminal User Interface (Phase 5 of Delimit OS).

The proprietary terminal experience. Type 'delimit' and get an OS-like
environment with panels for ledger, swarm, notifications, filesystem,
process manager, and live logs.

Enterprise-ready: zero JS, pure Python, works over SSH, sub-2s boot.
Designed for devs who hate browser-based tools.

Usage:
    python -m ai.tui          # Full TUI
    python -m ai.tui --quick  # Quick status (no interactive mode)
"""

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Header, Footer, Static, DataTable, Log, TabbedContent, TabPane,
    Label, ProgressBar, Button, Input, Tree, RichLog,
)
from textual.timer import Timer
from textual import work
from textual.binding import Binding
import json
import os
import subprocess
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# -- Data paths ---------------------------------------------------------------

# LED-1188: route through the canonical resolver so $DELIMIT_HOME /
# $DELIMIT_NAMESPACE_ROOT overrides apply uniformly across npm + gateway.
from .continuity import get_namespace_root  # noqa: E402

DELIMIT_HOME = get_namespace_root()
# LED-4300: the ledger is read through ledger_manager (which resolves
# ledger-v2/<venture>/ from the project path). PROJECT_PATH is that resolution
# input; LEDGER_DIR remains only for legacy callers and must not gain new ones.
PROJECT_PATH = Path(os.environ.get("DELIMIT_PROJECT_PATH", os.getcwd()))
LEDGER_DIR = DELIMIT_HOME / "ledger"
LEDGER_V2_DIR = DELIMIT_HOME / "ledger-v2"
SWARM_DIR = DELIMIT_HOME / "swarm"
MEMORY_DIR = DELIMIT_HOME / "memory"
SESSIONS_DIR = DELIMIT_HOME / "sessions"
NOTIFICATIONS_FILE = DELIMIT_HOME / "notifications.jsonl"
DAEMON_STATE_FILE = DELIMIT_HOME / "daemon" / "state.json"
DAEMON_LOG_FILE = DELIMIT_HOME / "daemon" / "daemon.log.jsonl"
ALERTS_DIR = DELIMIT_HOME / "alerts"


# -- Provenance ---------------------------------------------------------------
#
# LED-4300, the anti-recurrence control. This panel reported false state for
# MONTHS — a June-era daemon log rendered as live process status, an April-era
# ledger dir rendered as the current board — and nothing made that visible,
# because no widget ever declared where its data came from or how old it was.
# Every widget now carries a source + age footer and GREYS OUT when its source
# is stale, so the next time a source dies the panel says so instead of
# confidently rendering fiction.

STALE_AFTER_SECONDS = 6 * 3600


class LedgerUnavailable(RuntimeError):
    """The ledger could not be read.

    LED-4300, panel-mandated. Returning [] on a read failure would render
    "Nothing is blocked on you" — a FALSE GREEN, and precisely the defect
    class this whole repair exists to remove. An empty board and a broken
    board must never look alike, so failure is raised and each view renders
    it as UNAVAILABLE rather than as calm.
    """



def _source_age(*paths: Path) -> Optional[float]:
    """Newest mtime across the given paths, in seconds. None if none exist."""
    newest: Optional[float] = None
    for p in paths:
        try:
            if p.exists():
                mtime = p.stat().st_mtime
                newest = mtime if newest is None else max(newest, mtime)
        except OSError:
            continue
    return None if newest is None else max(0.0, time.time() - newest)


def _fmt_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "missing"
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)}m ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _provenance(label: str, *paths: Path, stale_after: int = STALE_AFTER_SECONDS) -> str:
    """Render the source/age footer. Marks STALE loudly rather than silently."""
    age = _source_age(*paths)
    shown = " ".join(str(p).replace(str(Path.home()), "~") for p in paths[:2])
    if age is None:
        return f"[dim]source:[/] {label} [red]MISSING[/] [dim]{shown}[/]"
    if age > stale_after:
        return (
            f"[dim]source:[/] {label} [bold red]STALE {_fmt_age(age)}[/] "
            f"[dim]{shown} — this view may be wrong[/]"
        )
    return f"[dim]source: {label} · {_fmt_age(age)} · {shown}[/]"


# -- Data loaders -------------------------------------------------------------

def _load_ledger_items(status: str = "open", limit: int = 20) -> List[Dict]:
    """Load ledger items through the CANONICAL event-sourced reducer.

    LED-4300. This function used to keep its own reducer over the LEGACY
    ``~/.delimit/ledger/`` directory, deduplicating an append-only log with
    "last row wins". That is wrong twice over:

      * the live store is ``ledger-v2/<venture>/``, not the legacy dir (whose
        newest real file predates this by months); and
      * ``type:"update"`` rows carry only id/note/status — no title, no
        priority — so each update CLOBBERED its parent item, which the status
        filter then dropped. Measured before the fix: 1,457 deduped rows,
        1,419 of them update rows, leaving exactly 10 items with
        ``status == "open"`` against ~432 genuinely open across ventures.

    Net effect of the old code: the more an item was worked, the more certainly
    it vanished from the operator's board — the panel structurally hid active
    work and displayed only abandoned work.

    ``ledger_manager.list_items`` already replays events correctly (updates
    PATCH their parent, preserving title/priority). Delegating here keeps ONE
    reducer for the whole system rather than a second, divergent copy.
    """
    try:
        from .ledger_manager import list_items
    except ImportError:  # pragma: no cover - flat bundle layout
        from ledger_manager import list_items  # type: ignore

    try:
        result = list_items(
            status=status, limit=max(limit, 500), project_path=str(PROJECT_PATH)
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as UNAVAILABLE, never as empty
        raise LedgerUnavailable(f"ledger read failed: {exc}") from exc
    if not isinstance(result, dict) or not isinstance(result.get("items"), dict):
        raise LedgerUnavailable("ledger returned an unexpected shape")

    buckets = result.get("items") or {}
    items: List[Dict] = []
    for bucket in ("ops", "strategy"):
        items.extend(buckets.get(bucket) or [])

    items.sort(
        key=lambda x: (
            0 if x.get("priority") == "P0" else 1 if x.get("priority") == "P1" else 2,
            str(x.get("updated_at") or x.get("created_at") or ""),
        )
    )
    return items[:limit]


def _load_actionable(limit: int = 200) -> List[Dict]:
    """Open/in-progress/blocked work at P0-P1 — the operator's real queue.

    LED-4300. A raw "open" count is not an operator metric: on the live store
    it reads 4,156, of which 4,070 are strategy/ThinkTank BACKLOG items that
    are open by design and will never be "done". Reporting that as the
    headline would swap one useless number for another. Priority-scoping is
    what makes the count mean "work that wants a decision or a commit".
    """
    try:
        from .ledger_manager import list_items
    except ImportError:  # pragma: no cover - flat bundle layout
        from ledger_manager import list_items  # type: ignore
    try:
        result = list_items(
            status__in=["open", "in_progress", "blocked"],
            priority__in=["P0", "P1"],
            limit=max(limit, 2000),
            project_path=str(PROJECT_PATH),
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as UNAVAILABLE, never as empty
        raise LedgerUnavailable(f"ledger read failed: {exc}") from exc
    if not isinstance(result, dict) or not isinstance(result.get("items"), dict):
        raise LedgerUnavailable("ledger returned an unexpected shape")
    buckets = result["items"]
    items: List[Dict] = []
    for bucket in ("ops", "strategy"):
        items.extend(buckets.get(bucket) or [])
    items.sort(key=lambda x: (
        0 if x.get("priority") == "P0" else 1,
        str(x.get("updated_at") or x.get("created_at") or ""),
    ), reverse=False)
    return items[:limit]


# Items only the FOUNDER can resolve. Two signals, both already in use:
# an explicit ``founder-gated`` tag, or a blocked status. Kept deliberately
# narrow — an action queue that mixes in FYI items stops being an action queue.
_FOUNDER_GATE_TAGS = ("founder-gated", "founder-decision", "merge-hold")


def _load_needs_you(limit: int = 20) -> List[Dict]:
    """Ledger items awaiting a decision only the founder can make.

    LED-4300. Deliberation ruling (unanimous, 4 vendors): a founder authority
    decision NEVER silently expires — it escalates until decided, superseded,
    lane-retired, or withdrawn. So nothing here ages out on a timer; items
    leave only when their underlying state changes.
    """
    seen: Dict[str, Dict] = {}
    for status in ("blocked", "open", "in_progress"):
        for item in _load_ledger_items(status=status, limit=500):
            tags = {str(t).lower() for t in (item.get("tags") or [])}
            gated = bool(tags.intersection(_FOUNDER_GATE_TAGS))
            if status == "blocked" or gated:
                item = {**item, "_gate": "tagged" if gated else "blocked"}
                seen.setdefault(str(item.get("id")), item)
    items = list(seen.values())
    items.sort(
        key=lambda x: (
            0 if x.get("priority") == "P0" else 1 if x.get("priority") == "P1" else 2,
            str(x.get("updated_at") or x.get("created_at") or ""),
        )
    )
    return items[:limit]


def _load_swarm_status() -> Dict[str, Any]:
    registry = SWARM_DIR / "agent_registry.json"
    if not registry.exists():
        return {"agents": 0, "ventures": 0}
    try:
        data = json.loads(registry.read_text())
        agents = data.get("agents", {})
        ventures = set(a.get("venture", "") for a in agents.values())
        return {
            "agents": len(agents),
            "ventures": len(ventures),
            "by_venture": {v: sum(1 for a in agents.values() if a.get("venture") == v) for v in ventures},
        }
    except (json.JSONDecodeError, KeyError):
        return {"agents": 0, "ventures": 0}


def _load_recent_sessions(limit: int = 5) -> List[Dict]:
    if not SESSIONS_DIR.exists():
        return []
    sessions = []
    for f in sorted(SESSIONS_DIR.glob("*.json"), reverse=True)[:limit]:
        try:
            sessions.append(json.loads(f.read_text()))
        except (json.JSONDecodeError, KeyError):
            continue
    return sessions


def _load_notifications(limit: int = 50) -> List[Dict]:
    """Load recent notifications from JSONL, newest first."""
    if not NOTIFICATIONS_FILE.exists():
        return []
    # Read last N lines efficiently (tail)
    lines: List[str] = []
    try:
        with open(NOTIFICATIONS_FILE, "rb") as f:
            # Seek from end to find last `limit` lines
            f.seek(0, 2)
            fsize = f.tell()
            # Read at most 64KB from the end — enough for 50 notifications
            read_size = min(fsize, 65536)
            f.seek(fsize - read_size)
            data = f.read().decode("utf-8", errors="replace")
            lines = data.strip().split("\n")
    except (OSError, UnicodeDecodeError):
        return []

    notifications = []
    for line in reversed(lines[-limit:]):
        try:
            notifications.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return notifications


def _load_daemon_state() -> Dict[str, Any]:
    """Load inbox daemon state."""
    if not DAEMON_STATE_FILE.exists():
        return {"status": "unknown"}
    try:
        return json.loads(DAEMON_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"status": "unknown"}



def _load_pending_approvals(limit: int = 20) -> List[Dict]:
    """Load pending drafts from the SQLite registry (LED-1129)."""
    db_path = DELIMIT_HOME / "drafts.db"
    if not db_path.exists():
        return []
    
    approvals = []
    try:
        # Connect read-only to avoid locking issues with the daemon
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM drafts WHERE status IN ('pending', 'waiting_for_approval') "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,)
            )
            for row in cursor:
                d = dict(row)
                # Parse target_json for a summary
                try:
                    target = json.loads(d.get("target_json", "{}"))
                    d["target_summary"] = target.get("repo", target.get("venture", "unknown"))
                    if "issue" in target:
                        d["target_summary"] += f" #{target['issue']}"
                except:
                    d["target_summary"] = "unknown"
                
                # Calculate age
                created_at = d.get("created_at", 0)
                if created_at:
                    diff = int(time.time()) - created_at
                    if diff < 60: d["age_str"] = f"{diff}s"
                    elif diff < 3600: d["age_str"] = f"{diff//60}m"
                    elif diff < 86400: d["age_str"] = f"{diff//3600}h"
                    else: d["age_str"] = f"{diff//86400}d"
                else:
                    d["age_str"] = "n/a"
                
                approvals.append(d)
    except Exception:
        pass
    return approvals


def _load_process_list() -> List[Dict[str, Any]]:
    """Live service status from the HEARTBEAT registry (LED-4300).

    This used to read ``daemon/state.json`` + ``daemon/daemon.log.jsonl`` —
    files last written 2026-06-06 — and rendered them as current. It therefore
    reported "Inbox Daemon: stopped (alert)" while that daemon was demonstrably
    alive, and, worse, would have reported the same thing had it genuinely
    died: an ANTI-SIGNAL, silent in exactly the case it exists to catch.

    Every scheduled service already writes ``heartbeats/<service>.json`` with
    its own staleness threshold, and ``ai.heartbeat.check_staleness`` is the
    canonical classifier. Use it. A service the classifier cannot judge is
    reported as UNKNOWN — never as healthy.
    """
    try:
        from .heartbeat import check_staleness
    except ImportError:  # pragma: no cover - flat bundle layout
        try:
            from heartbeat import check_staleness  # type: ignore
        except ImportError:
            return []

    try:
        res = check_staleness(str(DELIMIT_HOME / "heartbeats"))
    except Exception:  # noqa: BLE001 - never let the board crash on a bad file
        return []

    order = {"failed": 0, "parse_error": 1, "stale": 2, "never_seen": 3,
             "degraded": 4, "unknown_age": 5, "ok": 6}
    processes: List[Dict[str, Any]] = []
    for svc in (res.get("services") or []):
        cls = str(svc.get("classification") or "unknown")
        last = str(svc.get("last_run") or "")
        age = svc.get("age_seconds")
        processes.append({
            "name": svc.get("service", "?"),
            "label": str(svc.get("service", "?")),
            "status": cls,
            "uptime": _fmt_age(age) if isinstance(age, (int, float)) and age >= 0 else "",
            "detail": str(svc.get("detail") or "")[:80],
            "last_action": last.replace("T", " ").replace("Z", ""),
            "_sort": order.get(cls, 7),
        })
    processes.sort(key=lambda p: (p["_sort"], p["name"]))
    return processes


def _build_dir_tree(root: Path, max_depth: int = 3, _depth: int = 0) -> List[Tuple[str, Path, bool]]:
    """Build a flat list of (name, path, is_dir) for the tree, respecting depth."""
    if _depth > max_depth or not root.is_dir():
        return []
    entries = []
    try:
        children = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return []
    for child in children:
        # Skip very large directories and hidden internals
        if child.name.startswith("__") or child.name == "venv":
            continue
        entries.append((child.name, child, child.is_dir()))
    return entries


# -- Widgets ------------------------------------------------------------------

class NeedsYouPanel(Static):
    """Decisions ONLY the founder can make. The board's first question.

    LED-4300. The tab that used to occupy this slot showed 25 dead social_post
    drafts aged 3-30 days from a lane the founder RATIFIED RETIRED — an action
    queue made entirely of tombstones. Items here never expire on a timer
    (unanimous panel ruling): a founder authority decision leaves this list
    only when it is decided, superseded, lane-retired, or withdrawn.
    """

    def compose(self) -> ComposeResult:
        yield Static(id="needs-you-content")

    def on_mount(self) -> None:
        self._refresh_data()
        self.set_interval(30, self._refresh_data)

    def _refresh_data(self) -> None:
        content = self.query_one("#needs-you-content", Static)
        try:
            items = _load_needs_you(20)
        except LedgerUnavailable as exc:
            # Never render "nothing is blocked on you" when the read failed.
            content.update(f"[bold red]NEEDS YOU unavailable:[/] {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            content.update(f"[bold red]NEEDS YOU unavailable:[/] {exc}")
            return

        lines = ["[bold]BLOCKED ON YOU[/] — decisions nobody else can make\n"]
        if not items:
            lines.append("  [green]Nothing is blocked on you.[/]")
        for item in items:
            pri = str(item.get("priority") or "")
            colour = "red" if pri == "P0" else "yellow" if pri == "P1" else "cyan"
            age = ""
            stamp = str(item.get("updated_at") or item.get("created_at") or "")
            if stamp:
                try:
                    when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    days = (datetime.now(timezone.utc) - when).days
                    age = f"  [dim]{days}d[/]" if days else "  [dim]today[/]"
                except ValueError:
                    age = ""
            lines.append(
                f"  [{colour}]{pri:3s}[/] [bold]{item.get('id','')}[/]  "
                f"{str(item.get('title',''))[:72]}{age}"
            )

        lines.append("")
        lines.append(_provenance(
            "ledger-v2 (event-sourced)",
            LEDGER_V2_DIR / "delimit" / "operations.jsonl",
        ))
        content.update("\n".join(lines))


class LedgerPanel(Static):
    """Live ledger view -- shows open items sorted by priority."""

    def compose(self) -> ComposeResult:
        yield DataTable(id="ledger-table")

    def on_mount(self) -> None:
        table = self.query_one("#ledger-table", DataTable)
        table.add_columns("ID", "P", "Title", "Venture", "Type")
        self._refresh_data()
        self.set_interval(30, self._refresh_data)

    def _refresh_data(self) -> None:
        table = self.query_one("#ledger-table", DataTable)
        table.clear()
        try:
            rows = _load_actionable(40)
        except LedgerUnavailable as exc:
            table.add_row("[red]UNAVAILABLE[/]", "", str(exc)[:60], "", "")
            return
        for item in rows:
            table.add_row(
                item.get("id", ""),
                item.get("priority", ""),
                item.get("title", "")[:60],
                item.get("venture", "")[:15],
                item.get("type", ""),
            )


class SwarmPanel(Static):
    """Swarm status -- agents, ventures, health."""

    def compose(self) -> ComposeResult:
        yield Static(id="swarm-content")

    def on_mount(self) -> None:
        self._refresh_data()
        self.set_interval(15, self._refresh_data)

    def _refresh_data(self) -> None:
        content = self.query_one("#swarm-content", Static)
        swarm = _load_swarm_status()
        lines = [
            f"[bold cyan]Agents:[/] {swarm['agents']}  |  [bold cyan]Ventures:[/] {swarm['ventures']}",
            "",
        ]
        for venture, count in swarm.get("by_venture", {}).items():
            lines.append(f"  [green]{venture}[/]: {count} agents")
        content.update("\n".join(lines))


class SessionPanel(Static):
    """Recent sessions -- handoff history."""

    def compose(self) -> ComposeResult:
        yield Static(id="session-content")

    def on_mount(self) -> None:
        self._refresh_data()

    def _refresh_data(self) -> None:
        content = self.query_one("#session-content", Static)
        sessions = _load_recent_sessions(5)
        if not sessions:
            content.update("[dim]No sessions recorded yet.[/]")
            return
        lines = []
        for s in sessions:
            ts = s.get("timestamp", s.get("closed_at", ""))[:16]
            summary = s.get("summary", "")[:80]
            completed = len(s.get("items_completed", []))
            lines.append(f"[dim]{ts}[/] -- {summary}")
            if completed:
                lines.append(f"  [green]{completed} items completed[/]")
        content.update("\n".join(lines))


class VenturesPanel(Static):
    """Portfolio derived from EVIDENCE, not from a hand-seeded roster.

    LED-4300. This used to read swarm/agent_registry.json — a static fixture of
    5 identical role-slots per venture (the fictional "25 agents") — so it
    listed dormant ventures while omitting every venture that had never been
    hand-registered, and reported "delimit | 0 open items" against 99 real
    open items.

    A venture now appears when it has left EVIDENCE: a ledger-v2/<slug>/ store
    with real items. Panel ruling: ONE signal is enough to be listed, because
    under-registration is the measured failure and board spam is speculative;
    junk is controlled by a deny-list, not by volume thresholds.
    """

    DENY = {"unsorted", "tmp", "test", "scratch", "backup", "_archived"}

    def compose(self) -> ComposeResult:
        yield Static(id="ventures-content")

    def on_mount(self) -> None:
        self._refresh_data()
        self.set_interval(60, self._refresh_data)

    def _refresh_data(self) -> None:
        content = self.query_one("#ventures-content", Static)
        try:
            from .ledger_manager import _replay_status_counts
        except ImportError:  # pragma: no cover - flat bundle layout
            from ledger_manager import _replay_status_counts  # type: ignore

        rows = []
        if LEDGER_V2_DIR.exists():
            for d in sorted(LEDGER_V2_DIR.iterdir()):
                if not d.is_dir() or d.name in self.DENY or d.name.startswith("_"):
                    continue
                try:
                    counts = _replay_status_counts(d)
                except Exception:  # noqa: BLE001
                    continue
                total = sum(counts.values())
                if not total:
                    continue
                active = (counts.get("open", 0) + counts.get("in_progress", 0)
                          + counts.get("blocked", 0))
                age = _source_age(d / "operations.jsonl", d / "strategy.jsonl")
                rows.append((d.name, active, counts.get("done", 0), age))

        lines = ["[bold]Portfolio[/] — listed on ledger evidence\n"]
        if not rows:
            lines.append("  [dim]No venture ledgers found.[/]")
        for name, active, done, age in sorted(rows, key=lambda r: -r[1]):
            dormant = age is not None and age > 90 * 86400
            label = f"[dim]{name}[/]" if dormant else f"[bold cyan]{name}[/]"
            mark = "[dim]-[/]" if dormant else "[green]>[/]"
            note = "  [dim](dormant 90d+)[/]" if dormant else ""
            lines.append(
                f"  {mark} {label}  |  {active} active  |  {done} done  "
                f"|  [dim]{_fmt_age(age)}[/]{note}"
            )
        lines.append(f"\n[dim]{len(rows)} venture(s) with ledger evidence.[/]")
        lines.append(_provenance("ledger-v2 stores", LEDGER_V2_DIR))
        content.update("\n".join(lines))


class NotificationPanel(Static):
    """Notification drawer -- recent events from notifications.jsonl."""

    DEFAULT_CSS = """
    NotificationPanel {
        height: 1fr;
    }
    #notif-log {
        height: 1fr;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("[bold]Notifications[/]  [dim]Auto-refreshes every 30s[/]\n", id="notif-header")
        yield RichLog(id="notif-log", highlight=True, markup=True, wrap=True)

    def on_mount(self) -> None:
        self._refresh_data()
        self.set_interval(30, self._refresh_data)

    def _refresh_data(self) -> None:
        log = self.query_one("#notif-log", RichLog)
        log.clear()
        notifications = _load_notifications(50)
        if not notifications:
            log.write("[dim]No notifications yet.[/]")
            return

        for n in notifications:
            ts = n.get("timestamp", "")[:19].replace("T", " ")
            channel = n.get("channel", "?")
            subject = n.get("subject", n.get("event_type", ""))
            success = n.get("success", None)
            reason = n.get("reason", "")

            # Color-code by status
            if success is True:
                icon = "[green]OK[/]"
            elif success is False:
                icon = "[red]FAIL[/]"
            else:
                icon = "[yellow]--[/]"

            line = f"[dim]{ts}[/]  {icon}  [{_channel_color(channel)}]{channel}[/]"
            if subject:
                line += f"  {subject[:50]}"
            if reason:
                line += f"  [dim]({reason})[/]"
            log.write(line)

    @staticmethod
    def get_unread_count() -> int:
        """Count notifications from the last hour."""
        if not NOTIFICATIONS_FILE.exists():
            return 0
        try:
            mtime = NOTIFICATIONS_FILE.stat().st_mtime
            age_hours = (time.time() - mtime) / 3600
            if age_hours > 1:
                return 0
            # Count lines in last 4KB
            with open(NOTIFICATIONS_FILE, "rb") as f:
                f.seek(0, 2)
                fsize = f.tell()
                read_size = min(fsize, 4096)
                f.seek(fsize - read_size)
                data = f.read().decode("utf-8", errors="replace")
            count = 0
            cutoff = time.time() - 3600
            for line in reversed(data.strip().split("\n")):
                try:
                    n = json.loads(line)
                    ts = n.get("timestamp", "")
                    if ts:
                        dt = datetime.fromisoformat(ts)
                        if dt.timestamp() < cutoff:
                            break
                    count += 1
                except (json.JSONDecodeError, ValueError):
                    continue
            return count
        except (OSError, UnicodeDecodeError):
            return 0


def _channel_color(channel: str) -> str:
    """Return a rich color name for a notification channel."""
    colors = {
        "email": "cyan",
        "social": "magenta",
        "github": "white",
        "deploy": "green",
        "security": "red",
        "test": "yellow",
    }
    return colors.get(channel, "white")



class ApprovalsPanel(Static):
    """Pending approvals view -- shows items from drafts.db."""

    BINDINGS = [
        Binding("y", "approve", "Approve", key_display="Y"),
        Binding("n", "reject", "Reject", key_display="N"),
    ]

    def compose(self) -> ComposeResult:
        yield DataTable(id="approvals-table")

    def on_mount(self) -> None:
        table = self.query_one("#approvals-table", DataTable)
        table.add_columns("ID", "Kind", "Target", "Status", "Age")
        table.cursor_type = "row"
        self._refresh_data()
        self.set_interval(10, self._refresh_data)

    def _refresh_data(self) -> None:
        table = self.query_one("#approvals-table", DataTable)
        table.clear()
        self.items = _load_pending_approvals(25)
        for item in self.items:
            table.add_row(
                item.get("draft_id", "")[:12],
                item.get("draft_kind", ""),
                item.get("target_summary", "")[:40],
                item.get("status", ""),
                item.get("age_str", ""),
            )

    def action_approve(self) -> None:
        self._handle_action("approve")

    def action_reject(self) -> None:
        self._handle_action("reject")

    def _handle_action(self, action: str) -> None:
        table = self.query_one("#approvals-table", DataTable)
        cursor_row = table.cursor_row
        if cursor_row is None or not hasattr(self, "items") or cursor_row >= len(self.items):
            self.app.notify("No draft selected.", severity="warning")
            return

        item = self.items[cursor_row]
        draft_id = item.get("draft_id")
        current_status = item.get("status")

        if not draft_id or not current_status:
            self.app.notify("Invalid draft data selected.", severity="error")
            return

        from .inbox_drafts import transition
        db_path = DELIMIT_HOME / "drafts.db"
        new_status = "approved" if action == "approve" else "cancelled"

        try:
            success = transition(
                draft_id,
                expected=current_status,
                new=new_status,
                db_path=db_path
            )
            if success:
                self.app.notify(
                    f"Draft {draft_id[:12]} {action}d successfully!",
                    title="Action Succeeded",
                    severity="information"
                )
                self._refresh_data()
            else:
                self.app.notify(
                    f"Failed to {action} draft {draft_id[:12]}: state mismatch.",
                    title="Action Failed",
                    severity="warning"
                )
        except Exception as e:
            self.app.notify(
                f"Error performing {action}: {e}",
                title="System Error",
                severity="error"
            )


class FilesystemPanel(Static):
    """Filesystem browser -- navigate .delimit/ directory tree."""

    DEFAULT_CSS = """
    FilesystemPanel {
        height: 1fr;
    }
    #fs-container {
        height: 1fr;
    }
    #fs-tree {
        width: 1fr;
        min-width: 30;
        height: 1fr;
    }
    #fs-preview {
        width: 2fr;
        height: 1fr;
        padding: 0 1;
        border-left: solid $primary;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="fs-container"):
            yield Tree("[bold].delimit/[/]", id="fs-tree")
            yield RichLog(id="fs-preview", highlight=True, markup=True, wrap=True)

    def on_mount(self) -> None:
        tree = self.query_one("#fs-tree", Tree)
        tree.root.expand()
        self._populate_tree(tree.root, DELIMIT_HOME, depth=0)
        tree.root.expand()

    def _populate_tree(self, node, path: Path, depth: int) -> None:
        """Populate tree nodes lazily up to depth 2."""
        if depth > 2 or not path.is_dir():
            return
        entries = _build_dir_tree(path, max_depth=0)
        for name, child_path, is_dir in entries:
            if is_dir:
                branch = node.add(f"[bold cyan]{name}/[/]", data=child_path)
                # Add a placeholder so it shows as expandable
                if depth < 2:
                    self._populate_tree(branch, child_path, depth + 1)
            else:
                # Show file size hint
                try:
                    size = child_path.stat().st_size
                    size_str = _human_size(size)
                except OSError:
                    size_str = "?"
                node.add_leaf(f"{name} [dim]({size_str})[/]", data=child_path)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Preview file contents on selection."""
        preview = self.query_one("#fs-preview", RichLog)
        preview.clear()

        path = event.node.data
        if path is None:
            return

        if isinstance(path, Path) and path.is_file():
            preview.write(f"[bold]{path.name}[/]  [dim]{_human_size(path.stat().st_size)}[/]\n")
            preview.write(f"[dim]{path}[/]\n")
            preview.write("[dim]" + "-" * 60 + "[/]\n")

            # Read file with size guard
            try:
                size = path.stat().st_size
                if size > 102400:  # 100KB limit
                    preview.write(f"[yellow]File too large to preview ({_human_size(size)}). Showing first 4KB.[/]\n\n")
                    content = path.read_bytes()[:4096].decode("utf-8", errors="replace")
                elif path.suffix in (".json", ".jsonl", ".yml", ".yaml", ".txt", ".md", ".py", ".log", ".sh"):
                    content = path.read_text(errors="replace")
                else:
                    preview.write(f"[dim]Binary file ({path.suffix}). Size: {_human_size(size)}[/]")
                    return
                # For JSONL, show last 20 lines
                if path.suffix == ".jsonl":
                    lines = content.strip().split("\n")
                    if len(lines) > 20:
                        preview.write(f"[dim]Showing last 20 of {len(lines)} lines[/]\n\n")
                        content = "\n".join(lines[-20:])
                # Pretty-print JSON
                if path.suffix == ".json":
                    try:
                        parsed = json.loads(content)
                        content = json.dumps(parsed, indent=2)
                    except json.JSONDecodeError:
                        pass
                preview.write(content)
            except (OSError, UnicodeDecodeError) as e:
                preview.write(f"[red]Error reading file: {e}[/]")
        elif isinstance(path, Path) and path.is_dir():
            preview.write(f"[bold]{path.name}/[/]\n")
            try:
                children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
                for c in children[:50]:
                    if c.is_dir():
                        preview.write(f"  [cyan]{c.name}/[/]\n")
                    else:
                        preview.write(f"  {c.name}  [dim]({_human_size(c.stat().st_size)})[/]\n")
                total = len(list(path.iterdir()))
                if total > 50:
                    preview.write(f"\n[dim]... and {total - 50} more[/]")
            except PermissionError:
                preview.write("[red]Permission denied[/]")


def _human_size(size: int) -> str:
    """Convert bytes to human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


class ProcessPanel(Static):
    """Process manager -- show running daemons with status and controls."""

    DEFAULT_CSS = """
    ProcessPanel {
        height: 1fr;
    }
    #proc-table {
        height: auto;
        max-height: 50%;
    }
    #proc-detail {
        height: 1fr;
        padding: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield DataTable(id="proc-table")
        yield Static(id="proc-detail")

    def on_mount(self) -> None:
        table = self.query_one("#proc-table", DataTable)
        table.add_columns("Name", "Status", "Uptime", "Last Action", "Detail")
        table.cursor_type = "row"
        self._refresh_data()
        self.set_interval(15, self._refresh_data)

    def _refresh_data(self) -> None:
        table = self.query_one("#proc-table", DataTable)
        detail = self.query_one("#proc-detail", Static)
        table.clear()

        processes = _load_process_list()
        for proc in processes:
            status = proc["status"]
            if status in ("running", "active"):
                status_display = f"[green]{status}[/]"
            elif status in ("stopped", "stopped (alert)", "unknown"):
                status_display = f"[red]{status}[/]"
            else:
                status_display = f"[yellow]{status}[/]"

            table.add_row(
                proc["label"],
                status_display,
                proc.get("uptime", ""),
                proc.get("last_action", ""),
                proc.get("detail", "")[:40],
            )

        # Show daemon log tail in detail area
        lines = ["[bold]Recent Daemon Activity[/]\n"]
        if DAEMON_LOG_FILE.exists():
            try:
                with open(DAEMON_LOG_FILE, "rb") as f:
                    f.seek(0, 2)
                    fsize = f.tell()
                    read_size = min(fsize, 4096)
                    f.seek(fsize - read_size)
                    tail = f.read().decode("utf-8", errors="replace")
                for log_line in tail.strip().split("\n")[-10:]:
                    try:
                        entry = json.loads(log_line)
                        ts = entry.get("ts", "")[:19].replace("T", " ")
                        action = entry.get("action", "")
                        item_id = entry.get("item_id", "")
                        log_detail = entry.get("detail", "")[:50]
                        risk = entry.get("risk", "")
                        risk_color = "red" if risk == "high" else "yellow" if risk == "medium" else "green"
                        lines.append(
                            f"  [dim]{ts}[/]  {action:<15}  {item_id:<10}  "
                            f"[{risk_color}]{risk}[/]  [dim]{log_detail}[/]"
                        )
                    except json.JSONDecodeError:
                        continue
            except (OSError, UnicodeDecodeError):
                lines.append("  [dim]Could not read daemon log.[/]")
        else:
            lines.append("  [dim]No daemon log found.[/]")

        # Show alerts
        lines.append("\n[bold]Active Alerts[/]\n")
        alert_count = 0
        if ALERTS_DIR.exists():
            for alert_file in sorted(ALERTS_DIR.glob("*.json")):
                try:
                    alert = json.loads(alert_file.read_text())
                    alert_name = alert.get("alert", alert_file.stem)
                    reason = alert.get("reason", "")[:60]
                    alert_ts = alert.get("timestamp", "")[:19].replace("T", " ")
                    lines.append(f"  [red]![/] [bold]{alert_name}[/]  [dim]{alert_ts}[/]")
                    if reason:
                        lines.append(f"    {reason}")
                    alert_count += 1
                except (json.JSONDecodeError, OSError):
                    continue
        if alert_count == 0:
            lines.append("  [green]No active alerts.[/]")

        detail.update("\n".join(lines))


class GovernanceBar(Static):
    """Top status bar -- governance health at a glance."""

    def compose(self) -> ComposeResult:
        yield Static(id="gov-bar")

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(60, self._refresh)

    def _refresh(self) -> None:
        bar = self.query_one("#gov-bar", Static)
        actionable = len(_load_actionable(9999))
        # LED-4300: the old bar advertised "N agents / M ventures" from a
        # hand-seeded roster. Replaced with the only number that should drive
        # the operator's attention: how many decisions are blocked on him.
        try:
            needs_you = len(_load_needs_you(99))
        except Exception:  # noqa: BLE001
            needs_you = -1
        mode_file = DELIMIT_HOME / "enforcement_mode"
        mode = mode_file.read_text().strip() if mode_file.exists() else "default"

        # Notification badge
        notif_count = NotificationPanel.get_unread_count()
        notif_badge = f"  |  [yellow]Notif:[/] {notif_count}" if notif_count > 0 else ""

        if needs_you > 0:
            attention = f"[bold red]NEEDS YOU:[/] {needs_you}"
        elif needs_you == 0:
            attention = "[green]Nothing blocked on you[/]"
        else:
            attention = "[red]NEEDS YOU: unavailable[/]"

        bar.update(
            f"  [bold magenta]</>[/] [bold]Delimit OS[/]  |  "
            f"{attention}  |  "
            f"[cyan]Active P0/P1:[/] {actionable}  |  "
            f"[cyan]Mode:[/] {mode}"
            f"{notif_badge}  |  "
            f"[dim]{time.strftime('%H:%M')}[/]"
        )


# -- Main App -----------------------------------------------------------------

class DelimitOS(App):
    """Delimit OS -- the AI developer operating system."""

    CSS = """
    Screen {
        background: $surface;
    }
    #gov-bar {
        height: 1;
        background: $primary-background;
        color: $text;
        padding: 0 1;
    }
    TabbedContent {
        height: 1fr;
    }
    DataTable {
        height: 1fr;
    }
    #swarm-content, #session-content, #ventures-content {
        padding: 1;
    }
    """

    TITLE = "Delimit OS"
    SUB_TITLE = "AI Developer Operating System"

    BINDINGS = [
        Binding("q", "quit", "Quit", key_display="Q"),
        Binding("l", "focus_ledger", "Ledger", key_display="L"),
        Binding("a", "focus_approvals", "Approvals", key_display="A"),
        Binding("s", "focus_swarm", "Swarm", key_display="S"),
        Binding("n", "focus_notifications", "Notifications", key_display="N"),
        Binding("f", "focus_files", "Files", key_display="F"),
        Binding("p", "focus_processes", "Processes", key_display="P"),
        Binding("v", "focus_ventures", "Ventures", key_display="V"),
        Binding("h", "focus_sessions", "History", key_display="H"),
        Binding("t", "think", "Think", key_display="T"),
        Binding("b", "build", "Build", key_display="B"),
        Binding("r", "refresh", "Refresh", key_display="R"),
    ]

    def compose(self) -> ComposeResult:
        yield GovernanceBar()
        with TabbedContent():
            with TabPane("Needs You", id="tab-needsyou"):
                yield NeedsYouPanel()
            with TabPane("Ledger", id="tab-ledger"):
                yield LedgerPanel()
            with TabPane("Notifications", id="tab-notifications"):
                yield NotificationPanel()
            with TabPane("Processes", id="tab-processes"):
                yield ProcessPanel()
            with TabPane("Ventures", id="tab-ventures"):
                yield VenturesPanel()
            with TabPane("Sessions", id="tab-sessions"):
                yield SessionPanel()
        yield Footer()

    # -- Tab focus actions -----------------------------------------------------

    def action_focus_approvals(self) -> None:
        self.query_one(TabbedContent).active = "tab-needsyou"
        try:
            self.query_one("#needs-you-content", Static).focus()
        except Exception:
            pass

    def action_focus_ledger(self) -> None:
        self.query_one(TabbedContent).active = "tab-ledger"

    def action_focus_swarm(self) -> None:
        self.query_one(TabbedContent).active = "tab-ventures"

    def action_focus_notifications(self) -> None:
        self.query_one(TabbedContent).active = "tab-notifications"

    def action_focus_files(self) -> None:
        self.query_one(TabbedContent).active = "tab-ledger"

    def action_focus_processes(self) -> None:
        self.query_one(TabbedContent).active = "tab-processes"

    def action_focus_ventures(self) -> None:
        self.query_one(TabbedContent).active = "tab-ventures"

    def action_focus_sessions(self) -> None:
        self.query_one(TabbedContent).active = "tab-sessions"

    # -- Global actions --------------------------------------------------------

    def action_refresh(self) -> None:
        """Refresh all panels."""
        for panel in self.query(ApprovalsPanel):
            panel._refresh_data()
        for panel in self.query(LedgerPanel):
            panel._refresh_data()
        for panel in self.query(SwarmPanel):
            panel._refresh_data()
        for panel in self.query(SessionPanel):
            panel._refresh_data()
        for panel in self.query(NotificationPanel):
            panel._refresh_data()
        for panel in self.query(ProcessPanel):
            panel._refresh_data()
        for panel in self.query(VenturesPanel):
            panel._refresh_data()
        self.query_one(GovernanceBar)._refresh()
        self.notify("All panels refreshed", title="Refresh")

    @work(thread=True)
    def action_think(self) -> None:
        """Trigger deliberation in background thread."""
        self.notify("Deliberation starting...", title="Think")
        try:
            from ai.deliberation import deliberate
            result = deliberate(
                "Based on the current ledger and recent signals, what should the swarm build next?",
                mode="dialogue",
                max_rounds=2,
            )
            if result.get("mode") == "single_model_reflection":
                verdict = result.get("synthesis", "No synthesis")[:200]
            else:
                verdict = result.get("final_verdict", "No consensus")
                if isinstance(verdict, str):
                    verdict = verdict[:200]
                else:
                    verdict = str(verdict)[:200]
            self.notify(verdict, title="Think Result", timeout=15)
        except Exception as e:
            self.notify(f"Deliberation failed: {e}", title="Think Error", severity="error")

    def action_build(self) -> None:
        """Show next buildable item from ledger."""
        items = _load_ledger_items("open", 5)
        if items:
            top = items[0]
            self.notify(
                f"{top.get('id', '?')} [{top.get('priority', '?')}]: {top.get('title', '?')[:60]}",
                title="Next Build Item",
                timeout=10,
            )
        else:
            self.notify("Ledger is clear -- nothing to build!", title="Build")


def main():
    """Entry point for 'delimit' command."""
    import sys
    if "--quick" in sys.argv:
        # Quick status mode -- no interactive TUI
        from rich.console import Console
        from rich.table import Table

        console = Console()
        console.print("\n[bold magenta]</>[/] [bold]Delimit OS[/]\n")

        swarm = _load_swarm_status()
        items = _load_ledger_items("open", 10)

        console.print(f"[cyan]Swarm:[/] {swarm['agents']} agents across {swarm['ventures']} ventures")
        console.print(f"[cyan]Ledger:[/] {len(items)} open items\n")

        if items:
            table = Table(title="Open Items")
            table.add_column("ID", style="dim")
            table.add_column("P", style="bold")
            table.add_column("Title")
            table.add_column("Venture", style="green")
            for item in items[:10]:
                table.add_row(
                    item.get("id", ""),
                    item.get("priority", ""),
                    item.get("title", "")[:60],
                    item.get("venture", "")[:15],
                )
            console.print(table)

        # Quick notification summary
        notif_count = NotificationPanel.get_unread_count()
        if notif_count > 0:
            console.print(f"\n[yellow]Notifications:[/] {notif_count} in the last hour")

        # Quick process summary
        processes = _load_process_list()
        running = [p for p in processes if p["status"] in ("running", "active")]
        stopped = [p for p in processes if p["status"] not in ("running", "active", "inactive")]
        if running:
            console.print(f"[green]Running:[/] {', '.join(p['label'] for p in running)}")
        if stopped:
            console.print(f"[red]Stopped:[/] {', '.join(p['label'] for p in stopped)}")

        return

    app = DelimitOS()
    app.run()


if __name__ == "__main__":
    main()
