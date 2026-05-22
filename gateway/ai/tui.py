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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# -- Data paths ---------------------------------------------------------------

# LED-1188: route through the canonical resolver so $DELIMIT_HOME /
# $DELIMIT_NAMESPACE_ROOT overrides apply uniformly across npm + gateway.
from .continuity import get_namespace_root  # noqa: E402

DELIMIT_HOME = get_namespace_root()
LEDGER_DIR = DELIMIT_HOME / "ledger"
SWARM_DIR = DELIMIT_HOME / "swarm"
MEMORY_DIR = DELIMIT_HOME / "memory"
SESSIONS_DIR = DELIMIT_HOME / "sessions"
NOTIFICATIONS_FILE = DELIMIT_HOME / "notifications.jsonl"
DAEMON_STATE_FILE = DELIMIT_HOME / "daemon" / "state.json"
DAEMON_LOG_FILE = DELIMIT_HOME / "daemon" / "daemon.log.jsonl"
ALERTS_DIR = DELIMIT_HOME / "alerts"


# -- Data loaders -------------------------------------------------------------

def _load_ledger_items(status: str = "open", limit: int = 20) -> List[Dict]:
    """Load deduplicated ledger items (append-only JSONL, last entry wins)."""
    by_id: Dict[str, Dict] = {}
    for fname in ("operations.jsonl", "strategy.jsonl"):
        path = LEDGER_DIR / fname
        if not path.exists():
            continue
        for line in path.read_text().strip().split("\n"):
            try:
                d = json.loads(line)
                item_id = d.get("id", "")
                if item_id:
                    by_id[item_id] = d
            except json.JSONDecodeError:
                continue
    items = [d for d in by_id.values() if d.get("status") == status]
    items.sort(key=lambda x: (0 if x.get("priority") == "P0" else 1 if x.get("priority") == "P1" else 2))
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


def _load_process_list() -> List[Dict[str, Any]]:
    """Build a list of known daemons with status from state files and alerts."""
    processes = []

    # 1. Inbox daemon — primary daemon
    daemon = _load_daemon_state()
    started = daemon.get("started_at", "")
    last_loop = daemon.get("last_loop_at", "")
    loops = daemon.get("loops", 0)
    items_proc = daemon.get("items_processed", 0)
    status = daemon.get("status", "unknown")

    # Check for alert overrides
    alert_file = ALERTS_DIR / "inbox_daemon.json"
    if alert_file.exists():
        try:
            alert = json.loads(alert_file.read_text())
            if alert.get("alert") == "inbox_daemon_stopped":
                status = "stopped (alert)"
        except (json.JSONDecodeError, OSError):
            pass

    uptime = ""
    if started and status in ("running", "idle"):
        try:
            start_dt = datetime.fromisoformat(started)
            delta = datetime.now(timezone.utc) - start_dt
            hours = int(delta.total_seconds() // 3600)
            minutes = int((delta.total_seconds() % 3600) // 60)
            uptime = f"{hours}h {minutes}m"
        except (ValueError, TypeError):
            uptime = "?"

    processes.append({
        "name": "inbox_daemon",
        "label": "Inbox Daemon",
        "status": status,
        "uptime": uptime,
        "detail": f"loops={loops} processed={items_proc}",
        "last_action": last_loop[:19] if last_loop else "",
    })

    # 2. Social scanner — check cron.log and social_drafts for activity
    social_status = "inactive"
    social_last = ""
    social_detail = ""
    cron_log = DELIMIT_HOME / "cron.log"
    if cron_log.exists():
        try:
            # Read last 2KB to find recent social scan entries
            with open(cron_log, "rb") as f:
                f.seek(0, 2)
                fsize = f.tell()
                read_size = min(fsize, 2048)
                f.seek(fsize - read_size)
                tail = f.read().decode("utf-8", errors="replace")
            # Look for social scan references
            for line in reversed(tail.strip().split("\n")):
                if "social" in line.lower() or "scan" in line.lower():
                    social_status = "active"
                    social_last = line[:19] if len(line) > 19 else line
                    social_detail = line.strip()[:60]
                    break
        except (OSError, UnicodeDecodeError):
            pass

    processes.append({
        "name": "social_scanner",
        "label": "Social Scanner",
        "status": social_status,
        "uptime": "",
        "detail": social_detail,
        "last_action": social_last,
    })

    # 3. Ledger watcher — check if ledger files were recently modified
    ledger_status = "inactive"
    ledger_last = ""
    for fname in ("operations.jsonl", "strategy.jsonl"):
        lpath = LEDGER_DIR / fname
        if lpath.exists():
            mtime = lpath.stat().st_mtime
            age_hours = (time.time() - mtime) / 3600
            if age_hours < 1:
                ledger_status = "active"
            ts = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
            if not ledger_last or ts > ledger_last:
                ledger_last = ts

    processes.append({
        "name": "ledger_watcher",
        "label": "Ledger Watcher",
        "status": ledger_status,
        "uptime": "",
        "detail": "monitors operations + strategy",
        "last_action": ledger_last,
    })

    # 4. Notification router
    notif_status = "inactive"
    notif_last = ""
    if NOTIFICATIONS_FILE.exists():
        mtime = NOTIFICATIONS_FILE.stat().st_mtime
        age_hours = (time.time() - mtime) / 3600
        if age_hours < 1:
            notif_status = "active"
        notif_last = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")

    processes.append({
        "name": "notify_router",
        "label": "Notification Router",
        "status": notif_status,
        "uptime": "",
        "detail": f"routing via {DELIMIT_HOME / 'notify_routing.yaml'}",
        "last_action": notif_last,
    })

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
        for item in _load_ledger_items("open", 25):
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
    """Ventures as app tiles -- each venture is an 'app' in the OS."""

    def compose(self) -> ComposeResult:
        yield Static(id="ventures-content")

    def on_mount(self) -> None:
        self._refresh_data()
        self.set_interval(30, self._refresh_data)

    def _refresh_data(self) -> None:
        content = self.query_one("#ventures-content", Static)
        swarm = _load_swarm_status()
        by_venture = swarm.get("by_venture", {})

        if not by_venture:
            content.update("[dim]No ventures registered. Run delimit_swarm(action='register').[/]")
            return

        all_items = _load_ledger_items("open", 999)
        venture_items = {}
        for item in all_items:
            v = item.get("venture", "root")
            venture_items[v] = venture_items.get(v, 0) + 1

        lines = [
            "[bold]Ventures[/] -- each venture is an app in Delimit OS\n",
        ]
        for venture, agent_count in sorted(by_venture.items()):
            open_count = venture_items.get(venture, venture_items.get(f"{venture}-mcp", 0))
            status_icon = "[green]>[/]" if agent_count > 0 else "[red]o[/]"
            lines.append(
                f"  {status_icon} [bold cyan]{venture}[/]"
                f"  |  {agent_count} agents"
                f"  |  {open_count} open items"
            )

        lines.append(f"\n[dim]Total: {len(by_venture)} ventures, {swarm['agents']} agents[/]")
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
        ledger_count = len(_load_ledger_items("open", 999))
        swarm = _load_swarm_status()
        mode_file = DELIMIT_HOME / "enforcement_mode"
        mode = mode_file.read_text().strip() if mode_file.exists() else "default"

        # Notification badge
        notif_count = NotificationPanel.get_unread_count()
        notif_badge = f"  |  [yellow]Notif:[/] {notif_count}" if notif_count > 0 else ""

        bar.update(
            f"  [bold magenta]</>[/] [bold]Delimit OS[/]  |  "
            f"[cyan]Ledger:[/] {ledger_count} open  |  "
            f"[cyan]Swarm:[/] {swarm['agents']} agents / {swarm['ventures']} ventures  |  "
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
            with TabPane("Ledger", id="tab-ledger"):
                yield LedgerPanel()
            with TabPane("Swarm", id="tab-swarm"):
                yield SwarmPanel()
            with TabPane("Notifications", id="tab-notifications"):
                yield NotificationPanel()
            with TabPane("Files", id="tab-files"):
                yield FilesystemPanel()
            with TabPane("Processes", id="tab-processes"):
                yield ProcessPanel()
            with TabPane("Ventures", id="tab-ventures"):
                yield VenturesPanel()
            with TabPane("Sessions", id="tab-sessions"):
                yield SessionPanel()
        yield Footer()

    # -- Tab focus actions -----------------------------------------------------

    def action_focus_ledger(self) -> None:
        self.query_one(TabbedContent).active = "tab-ledger"

    def action_focus_swarm(self) -> None:
        self.query_one(TabbedContent).active = "tab-swarm"

    def action_focus_notifications(self) -> None:
        self.query_one(TabbedContent).active = "tab-notifications"

    def action_focus_files(self) -> None:
        self.query_one(TabbedContent).active = "tab-files"

    def action_focus_processes(self) -> None:
        self.query_one(TabbedContent).active = "tab-processes"

    def action_focus_ventures(self) -> None:
        self.query_one(TabbedContent).active = "tab-ventures"

    def action_focus_sessions(self) -> None:
        self.query_one(TabbedContent).active = "tab-sessions"

    # -- Global actions --------------------------------------------------------

    def action_refresh(self) -> None:
        """Refresh all panels."""
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
