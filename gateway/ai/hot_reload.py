"""Cross-session MCP hot reload (LED-799).

Solves the pain where one Claude session edits ai/*.py and other sessions
have to restart the MCP server to pick up the change. There are three
distinct cases this module handles:

1. **Edited helper module** (e.g. ai/content_intel.py changed):
   importlib.reload() the module so tools that lazily `from ai.X import Y`
   inside their function body pick up the new code on the next call.

2. **New helper module** (e.g. ai/foo.py added by another session):
   importlib.import_module() to bring it into sys.modules so subsequent
   lazy imports inside tool bodies succeed.

3. **New @mcp.tool() decoration** in a freshly added module (ai/tools/*.py):
   walk the module globals for fastmcp.tools.tool.FunctionTool instances
   and add them to the live FastMCP tool_manager via add_tool(). New tool
   files become callable without a server restart.

Out of scope (still requires restart):
- Edits to ai/server.py itself. That module is too large, has too many
  side effects on import, and reloading it would create a NEW FastMCP
  instance disconnected from the running server. Convention: put NEW
  tools in ai/tools/<name>.py, not in ai/server.py.

Dead-letter behavior: every reload/import is wrapped in try/except. Failures
are logged to ~/.delimit/logs/hot_reload.jsonl and never crash the server.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("delimit.ai.hot_reload")

LOG_DIR = Path.home() / ".delimit" / "logs"
LOG_FILE = LOG_DIR / "hot_reload.jsonl"

# Modules whose reload would do more harm than good. server.py defines the
# live FastMCP instance — reloading it would create a fresh disconnected
# instance. Tests confirm reload of these modules creates duplicate state.
RELOAD_DENY_LIST: Set[str] = {
    "ai.server",
    "ai.hot_reload",  # don't reload self
    "ai",  # the package itself
}


# ── logging ──────────────────────────────────────────────────────────


def _log(event: Dict[str, Any]) -> None:
    """Append a structured event to the hot-reload audit log. Never raises."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        event = {
            **event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
        }
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except OSError as e:
        logger.debug("hot_reload log write failed: %s", e)


# ── tool re-registration ──────────────────────────────────────────────


def _is_function_tool(obj: Any) -> bool:
    """True if `obj` is a fastmcp FunctionTool (registered tool)."""
    cls = type(obj)
    return cls.__module__.startswith("fastmcp.") and cls.__name__ == "FunctionTool"


def register_module_tools(mcp: Any, module: Any) -> List[str]:
    """Walk a module's globals and register every FunctionTool against the live mcp.

    Returns the list of tool keys registered. Existing tools with the same
    key are *replaced* — that lets edits to a tool's metadata or schema
    take effect without a restart.
    """
    if mcp is None or module is None:
        return []
    registered: List[str] = []
    try:
        tool_manager = getattr(mcp, "_tool_manager", None)
        if tool_manager is None or not hasattr(tool_manager, "_tools"):
            return []
        for name, value in list(vars(module).items()):
            if not _is_function_tool(value):
                continue
            try:
                key = getattr(value, "key", name)
                tool_manager._tools[key] = value
                registered.append(key)
            except Exception as e:
                _log({
                    "event": "tool_register_failed",
                    "module": module.__name__,
                    "name": name,
                    "error": str(e),
                })
    except Exception as e:  # noqa: BLE001
        _log({
            "event": "register_module_tools_failed",
            "module": getattr(module, "__name__", "?"),
            "error": str(e),
            "traceback": traceback.format_exc(limit=3),
        })
    if registered:
        _log({
            "event": "tools_registered",
            "module": getattr(module, "__name__", "?"),
            "count": len(registered),
            "keys": registered,
        })
    return registered


def reload_module(mcp: Any, module_name: str) -> Dict[str, Any]:
    """Reload an existing module and re-register any tools it defines.

    Returns a status dict with the module name, whether the reload succeeded,
    and the list of tool keys registered. Reload failures keep the previous
    module in place (importlib.reload either replaces atomically or raises).
    """
    if module_name in RELOAD_DENY_LIST:
        return {"module": module_name, "ok": False, "skipped": "deny_list"}
    if module_name not in sys.modules:
        return {"module": module_name, "ok": False, "skipped": "not_loaded"}
    try:
        module = importlib.reload(sys.modules[module_name])
        tools = register_module_tools(mcp, module)
        _log({
            "event": "module_reloaded",
            "module": module_name,
            "tools_registered": tools,
        })
        return {"module": module_name, "ok": True, "tools_registered": tools}
    except Exception as e:  # noqa: BLE001
        _log({
            "event": "module_reload_failed",
            "module": module_name,
            "error": str(e),
            "traceback": traceback.format_exc(limit=5),
        })
        return {"module": module_name, "ok": False, "error": str(e)}


def import_new_module(
    mcp: Any,
    file_path: Path,
    package_root: Path,
    package_prefix: str = "ai",
) -> Dict[str, Any]:
    """Import a freshly added file under the watched package and register its tools.

    `file_path` must live under `package_root`. The module name is derived
    from the relative path: ai/tools/foo.py → ai.tools.foo.
    """
    try:
        rel = file_path.relative_to(package_root)
    except ValueError:
        return {"file": str(file_path), "ok": False, "error": "outside_package_root"}

    parts = list(rel.with_suffix("").parts)
    if not parts:
        return {"file": str(file_path), "ok": False, "error": "invalid_path"}
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if package_prefix:
        # The package_root is the directory CONTAINING the package (e.g. delimit-gateway/),
        # so the relative path already starts with the package name. If not, prepend.
        if not parts or parts[0] != package_prefix:
            parts = [package_prefix] + parts
    module_name = ".".join(parts)

    if module_name in RELOAD_DENY_LIST:
        return {"file": str(file_path), "module": module_name, "ok": False, "skipped": "deny_list"}

    try:
        # Critical: drop cached finders so a new file inside an already-imported
        # package becomes visible. Without this, importlib's package finder
        # uses a stale directory listing.
        importlib.invalidate_caches()
        if module_name in sys.modules:
            module = importlib.reload(sys.modules[module_name])
            action = "reloaded"
        else:
            module = importlib.import_module(module_name)
            action = "imported"
        tools = register_module_tools(mcp, module)
        _log({
            "event": "new_module_handled",
            "module": module_name,
            "action": action,
            "tools_registered": tools,
        })
        return {
            "file": str(file_path),
            "module": module_name,
            "action": action,
            "ok": True,
            "tools_registered": tools,
        }
    except Exception as e:  # noqa: BLE001
        _log({
            "event": "new_module_import_failed",
            "module": module_name,
            "error": str(e),
            "traceback": traceback.format_exc(limit=5),
        })
        return {
            "file": str(file_path),
            "module": module_name,
            "ok": False,
            "error": str(e),
        }


# ── file watcher ──────────────────────────────────────────────────────


class HotReloadWatcher:
    """Polling-based file watcher (no inotify dependency).

    Tracks mtimes for every .py file under `watch_dir`. On each tick:
    - New files trigger import_new_module().
    - Changed files trigger reload_module() (unless on the deny list).
    - Deleted files are noted in the log but no action is taken (the
      cached sys.modules entry stays — that's safer than fighting against
      another session that may be mid-edit).
    """

    def __init__(
        self,
        mcp: Any,
        watch_dir: Path,
        package_root: Path,
        package_prefix: str = "ai",
        interval: float = 2.0,
    ) -> None:
        self.mcp = mcp
        self.watch_dir = Path(watch_dir)
        self.package_root = Path(package_root)
        self.package_prefix = package_prefix
        self.interval = interval
        self._mtimes: Dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._snapshot_initial()

    def _snapshot_initial(self) -> None:
        """Record current mtimes so the first tick doesn't reload everything."""
        for path in self.watch_dir.rglob("*.py"):
            try:
                self._mtimes[str(path)] = path.stat().st_mtime
            except OSError:
                pass

    def tick(self) -> Dict[str, Any]:
        """Run a single scan pass. Returns counts of actions taken."""
        new_files: List[Path] = []
        changed_files: List[Path] = []
        seen: Set[str] = set()

        try:
            for path in self.watch_dir.rglob("*.py"):
                key = str(path)
                seen.add(key)
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                prev = self._mtimes.get(key)
                if prev is None:
                    new_files.append(path)
                elif mtime > prev:
                    changed_files.append(path)
                self._mtimes[key] = mtime
        except OSError as e:
            _log({"event": "watch_scan_error", "error": str(e)})
            return {"new": 0, "changed": 0, "errors": 1}

        results: Dict[str, Any] = {"new": [], "changed": [], "errors": 0}
        for path in new_files:
            r = import_new_module(self.mcp, path, self.package_root, self.package_prefix)
            results["new"].append(r)
            if not r.get("ok"):
                results["errors"] += 1

        for path in changed_files:
            module_name = self._path_to_module(path)
            if module_name is None:
                continue
            if module_name in RELOAD_DENY_LIST:
                continue
            r = reload_module(self.mcp, module_name)
            results["changed"].append(r)
            if not r.get("ok") and r.get("skipped") is None:
                results["errors"] += 1

        return results

    def _path_to_module(self, path: Path) -> Optional[str]:
        try:
            rel = path.relative_to(self.package_root)
        except ValueError:
            return None
        parts = list(rel.with_suffix("").parts)
        if not parts:
            return None
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if self.package_prefix and (not parts or parts[0] != self.package_prefix):
            parts = [self.package_prefix] + parts
        return ".".join(parts)

    def _loop(self) -> None:
        _log({"event": "watcher_started", "watch_dir": str(self.watch_dir), "interval": self.interval})
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                _log({
                    "event": "watcher_tick_error",
                    "error": str(e),
                    "traceback": traceback.format_exc(limit=3),
                })
            self._stop.wait(timeout=self.interval)
        _log({"event": "watcher_stopped"})

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="delimit-hot-reload", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


# ── module-level singleton + bootstrap helper ─────────────────────────


_singleton: Optional[HotReloadWatcher] = None
_singleton_lock = threading.Lock()


def start_hot_reload(
    mcp: Any,
    watch_dir: Optional[Path] = None,
    package_root: Optional[Path] = None,
    interval: float = 2.0,
) -> Dict[str, Any]:
    """Start the global hot-reload watcher. Idempotent.

    Args:
        mcp: The live FastMCP instance from ai/server.py.
        watch_dir: Directory to watch. Defaults to the directory containing
            ai/server.py (i.e. the ai/ package directory).
        package_root: Directory whose first child is the package. Used to
            derive module names from file paths. Defaults to the parent of
            watch_dir.
        interval: Poll interval in seconds. Default 2.0.

    Returns a status dict. Will not raise — failures are logged.
    """
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            return {"status": "already_running"}
        try:
            if watch_dir is None:
                watch_dir = Path(__file__).parent
            if package_root is None:
                package_root = Path(watch_dir).parent
            _singleton = HotReloadWatcher(
                mcp=mcp,
                watch_dir=Path(watch_dir),
                package_root=Path(package_root),
                interval=interval,
            )
            _singleton.start()
            _log({
                "event": "hot_reload_started",
                "watch_dir": str(watch_dir),
                "package_root": str(package_root),
                "interval": interval,
            })
            return {
                "status": "started",
                "watch_dir": str(watch_dir),
                "package_root": str(package_root),
                "interval": interval,
            }
        except Exception as e:  # noqa: BLE001
            _log({
                "event": "hot_reload_start_failed",
                "error": str(e),
                "traceback": traceback.format_exc(limit=5),
            })
            return {"status": "failed", "error": str(e)}


def stop_hot_reload() -> Dict[str, Any]:
    """Stop the global watcher. Idempotent."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            return {"status": "not_running"}
        _singleton.stop()
        _singleton = None
        _log({"event": "hot_reload_stopped_via_api"})
        return {"status": "stopped"}


def hot_reload_status() -> Dict[str, Any]:
    """Inspect the watcher state."""
    with _singleton_lock:
        if _singleton is None:
            return {"running": False}
        return {
            "running": True,
            "watch_dir": str(_singleton.watch_dir),
            "package_root": str(_singleton.package_root),
            "interval": _singleton.interval,
            "tracked_files": len(_singleton._mtimes),
        }
