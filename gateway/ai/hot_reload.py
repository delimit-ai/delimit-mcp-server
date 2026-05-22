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
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

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


def _get_tool_dict(mcp: Any) -> Optional[Dict[str, Any]]:
    """Return a name → tool dict view of the live FastMCP registry.

    Handles three schemas:
      - fastmcp 2.x:  `mcp._tool_manager._tools`         keys = bare names
      - fastmcp 3.x:  `mcp._local_provider._components`  keys = "tool:<name>@<scope>"
      - any future:   probe `_tools` / `tools` attrs directly

    For 3.x the returned dict is a *projected view* — the keys are bare tool
    names (so callers can do `name in d` against a tool name), but writes
    through that view propagate to the underlying components dict using the
    correct namespaced key. That keeps the hot-reload code path unchanged
    across fastmcp versions.

    Returns None if no compatible registry is found.
    """
    # 2.x path
    tm = getattr(mcp, "_tool_manager", None)
    if tm is not None and isinstance(getattr(tm, "_tools", None), dict):
        return tm._tools  # type: ignore[return-value]

    # 3.x path: _local_provider._components is the live registry, but keys
    # are "tool:<name>@<scope>". Wrap with a projected-name view.
    lp = getattr(mcp, "_local_provider", None)
    if lp is not None:
        comps = getattr(lp, "_components", None)
        if isinstance(comps, dict):
            return _LocalProviderToolView(comps)

    # Unknown schemas — try common attribute names directly
    for attr in ("_tools", "tools"):
        candidate = getattr(mcp, attr, None)
        if isinstance(candidate, dict):
            return candidate
    for mgr_attr in ("_tool_manager", "tool_manager"):
        mgr = getattr(mcp, mgr_attr, None)
        if mgr is None:
            continue
        for inner in ("_tools", "tools"):
            candidate = getattr(mgr, inner, None)
            if isinstance(candidate, dict):
                return candidate
    return None


class _LocalProviderToolView(dict):
    """fastmcp-3.x compatibility shim.

    The 3.x `_local_provider._components` dict stores tools under keys of
    the form `"tool:<name>@<scope>"`. Hot reload code expects to write
    `d[name] = tool` and read `name in d` against bare tool names.

    This view sits in front of the components dict and translates between
    the two schemas. Reads find the matching `tool:NAME@*` key, writes
    insert under `tool:NAME@<existing_scope_if_any_else_empty>`.
    """

    def __init__(self, backing: Dict[str, Any]):
        super().__init__()
        # Don't store the backing in `super()` storage; just keep a reference.
        self._backing = backing

    @staticmethod
    def _bare_name(key: str) -> str:
        # "tool:foo@scope" -> "foo"; non-tool keys ignored
        if not key.startswith("tool:"):
            return ""
        rest = key[len("tool:"):]
        return rest.split("@", 1)[0]

    def _find_key(self, name: str) -> Optional[str]:
        """Find the existing components key for a bare tool name."""
        for k in self._backing:
            if self._bare_name(k) == name:
                return k
        return None

    def __contains__(self, name: object) -> bool:  # type: ignore[override]
        return isinstance(name, str) and self._find_key(name) is not None

    def __getitem__(self, name: str) -> Any:
        k = self._find_key(name)
        if k is None:
            raise KeyError(name)
        return self._backing[k]

    def __setitem__(self, name: str, value: Any) -> None:
        existing = self._find_key(name)
        if existing is not None:
            # Replace in place — preserves any scope suffix the original used.
            self._backing[existing] = value
        else:
            self._backing[f"tool:{name}@"] = value

    def __delitem__(self, name: str) -> None:
        k = self._find_key(name)
        if k is None:
            raise KeyError(name)
        del self._backing[k]

    def __iter__(self):
        for k in self._backing:
            bn = self._bare_name(k)
            if bn:
                yield bn

    def __len__(self) -> int:
        return sum(1 for k in self._backing if k.startswith("tool:"))


def register_module_tools(mcp: Any, module: Any) -> List[str]:
    """Walk a module's globals and ensure every decorated tool is in the live mcp.

    Two schemas in play:

    fastmcp 2.x  — `@mcp.tool()` wraps the decorated function as a
                   FunctionTool instance and replaces the module global.
                   We find them in `vars(module)` via `_is_function_tool`
                   and write them into the live registry dict.

    fastmcp 3.x  — `@mcp.tool()` registers the tool with the server at
                   decoration time and leaves the module global as a plain
                   function. By the time `register_module_tools` is called,
                   the registration has ALREADY happened. Our job is just
                   to enumerate the resulting tool names.

    Returns the list of tool keys registered.
    """
    if mcp is None or module is None:
        return []
    registered: List[str] = []
    try:
        tool_dict = _get_tool_dict(mcp)
        if tool_dict is None:
            return []

        # 2.x path: explicit FunctionTool instances in the module globals
        any_function_tool_found = False
        for name, value in list(vars(module).items()):
            if not _is_function_tool(value):
                continue
            any_function_tool_found = True
            try:
                key = getattr(value, "key", name)
                tool_dict[key] = value
                registered.append(key)
            except Exception as e:
                _log({
                    "event": "tool_register_failed",
                    "module": module.__name__,
                    "name": name,
                    "error": str(e),
                })

        # 3.x fallback: no FunctionTool in module globals; the decorator
        # already registered the tools. Walk module globals for plain
        # functions whose name appears in the registry.
        if not any_function_tool_found:
            for name, value in list(vars(module).items()):
                if name.startswith("_"):
                    continue
                if not callable(value):
                    continue
                # Skip imports — only count things actually defined in this module
                value_mod = getattr(value, "__module__", "")
                if value_mod and value_mod != module.__name__:
                    continue
                if name in tool_dict:
                    registered.append(name)
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
