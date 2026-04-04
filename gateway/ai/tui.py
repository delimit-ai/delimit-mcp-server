"""Delimit TUI — Terminal User Interface (Phase 2 of Delimit OS).

The proprietary terminal experience. Type 'delimit' and get an OS-like
environment with panels for ledger, swarm, memory, and live logs.

Enterprise-ready: zero JS, pure Python, works over SSH, sub-2s boot.
Designed for devs who hate browser-based tools.

Usage:
    python -m ai.tui          # Full TUI
    python -m ai.tui --quick  # Quick status (no interactive mode)
"""

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import (
    Header, Footer, Static, DataTable, Log, TabbedContent, TabPane,
    Label, ProgressBar, Button, Input,
)
from textual.timer import Timer
from textual import work
import json
import time
from pathlib import Path
from typing import Any, Dict, List


# ── Data loaders ─────────────────────────────────────────────────────

LEDGER_DIR = Path.home() / ".delimit" / "ledger"
SWARM_DIR = Path.home() / ".delimit" / "swarm"
MEMORY_DIR = Path.home() / ".delimit" / "memory"
SESSIONS_DIR = Path.home() / ".delimit" / "sessions"


def _load_ledger_items(status: str = "open", limit: int = 20) -> List[Dict]:
    # Deduplicate by ID — last entry wins (append-only JSONL)
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


# ── Widgets ──────────────────────────────────────────────────────────

class LedgerPanel(Static):
    """Live ledger view — shows open items sorted by priority."""

    def compose(self) -> ComposeResult:
        yield DataTable(id="ledger-table")

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

    def on_mount(self) -> None:
        table = self.query_one("#ledger-table", DataTable)
        table.add_columns("ID", "P", "Title", "Venture", "Type")
        self._refresh_data()
        self.set_interval(30, self._refresh_data)


class SwarmPanel(Static):
    """Swarm status — agents, ventures, health."""

    def compose(self) -> ComposeResult:
        yield Static(id="swarm-content")

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

    def on_mount(self) -> None:
        self._refresh_data()
        self.set_interval(15, self._refresh_data)


class SessionPanel(Static):
    """Recent sessions — handoff history."""

    def compose(self) -> ComposeResult:
        yield Static(id="session-content")

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
            lines.append(f"[dim]{ts}[/] — {summary}")
            if completed:
                lines.append(f"  [green]✓ {completed} items completed[/]")
        content.update("\n".join(lines))

    def on_mount(self) -> None:
        self._refresh_data()


class VenturesPanel(Static):
    """Ventures as app tiles — each venture is an 'app' in the OS."""

    def compose(self) -> ComposeResult:
        yield Static(id="ventures-content")

    def _refresh_data(self) -> None:
        content = self.query_one("#ventures-content", Static)
        swarm = _load_swarm_status()
        by_venture = swarm.get("by_venture", {})

        if not by_venture:
            content.update("[dim]No ventures registered. Run delimit_swarm(action='register').[/]")
            return

        # Count open items per venture
        all_items = _load_ledger_items("open", 999)
        venture_items = {}
        for item in all_items:
            v = item.get("venture", "root")
            venture_items[v] = venture_items.get(v, 0) + 1

        lines = [
            "[bold]Ventures[/] — each venture is an app in Delimit OS\n",
        ]
        for venture, agent_count in sorted(by_venture.items()):
            open_count = venture_items.get(venture, venture_items.get(f"{venture}-mcp", 0))
            status_icon = "[green]●[/]" if agent_count > 0 else "[red]○[/]"
            lines.append(
                f"  {status_icon} [bold cyan]{venture}[/]"
                f"  |  {agent_count} agents"
                f"  |  {open_count} open items"
            )

        lines.append(f"\n[dim]Total: {len(by_venture)} ventures, {swarm['agents']} agents[/]")
        content.update("\n".join(lines))

    def on_mount(self) -> None:
        self._refresh_data()
        self.set_interval(30, self._refresh_data)


class GovernanceBar(Static):
    """Top status bar — governance health at a glance."""

    def compose(self) -> ComposeResult:
        yield Static(id="gov-bar")

    def _refresh(self) -> None:
        bar = self.query_one("#gov-bar", Static)
        ledger_count = len(_load_ledger_items("open", 999))
        swarm = _load_swarm_status()
        mode_file = Path.home() / ".delimit" / "enforcement_mode"
        mode = mode_file.read_text().strip() if mode_file.exists() else "default"

        bar.update(
            f"  [bold magenta]</>[/] [bold]Delimit OS[/]  |  "
            f"[cyan]Ledger:[/] {ledger_count} open  |  "
            f"[cyan]Swarm:[/] {swarm['agents']} agents / {swarm['ventures']} ventures  |  "
            f"[cyan]Mode:[/] {mode}  |  "
            f"[dim]{time.strftime('%H:%M')}[/]"
        )

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(60, self._refresh)


# ── Main App ─────────────────────────────────────────────────────────

class DelimitOS(App):
    """Delimit OS — the AI developer operating system."""

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
    #swarm-content, #session-content {
        padding: 1;
    }
    """

    TITLE = "Delimit OS"
    SUB_TITLE = "AI Developer Operating System"

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("l", "focus_ledger", "Ledger"),
        ("s", "focus_swarm", "Swarm"),
        ("v", "focus_ventures", "Ventures"),
        ("h", "focus_sessions", "History"),
        ("r", "refresh", "Refresh"),
        ("t", "think", "Think"),
        ("b", "build", "Build"),
    ]

    def compose(self) -> ComposeResult:
        yield GovernanceBar()
        with TabbedContent():
            with TabPane("Ledger", id="tab-ledger"):
                yield LedgerPanel()
            with TabPane("Swarm", id="tab-swarm"):
                yield SwarmPanel()
            with TabPane("Ventures", id="tab-ventures"):
                yield VenturesPanel()
            with TabPane("Sessions", id="tab-sessions"):
                yield SessionPanel()
        yield Footer()

    def action_focus_ledger(self) -> None:
        self.query_one(TabbedContent).active = "tab-ledger"

    def action_focus_swarm(self) -> None:
        self.query_one(TabbedContent).active = "tab-swarm"

    def action_focus_ventures(self) -> None:
        self.query_one(TabbedContent).active = "tab-ventures"

    def action_focus_sessions(self) -> None:
        self.query_one(TabbedContent).active = "tab-sessions"

    def action_refresh(self) -> None:
        for panel in self.query(LedgerPanel):
            panel._refresh_data()
        for panel in self.query(SwarmPanel):
            panel._refresh_data()
        for panel in self.query(SessionPanel):
            panel._refresh_data()
        self.query_one(GovernanceBar)._refresh()

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
            self.notify("Ledger is clear — nothing to build!", title="Build")


def main():
    """Entry point for 'delimit' command."""
    import sys
    if "--quick" in sys.argv:
        # Quick status mode — no interactive TUI
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
        return

    app = DelimitOS()
    app.run()


if __name__ == "__main__":
    main()
