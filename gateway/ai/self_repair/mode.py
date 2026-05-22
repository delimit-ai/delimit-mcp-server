"""
Self-repair mode machine.

The mode ladder controls how aggressively the self-repair loop acts on a
function. This module owns:

  - The `Mode` enum (ordered, ascending intensity)
  - Loading the per-machine config (`~/.delimit/self_repair.yaml`) with
    fallback to the bundled `default_self_repair.yaml`
  - Resolving the effective mode for a function honoring:
      1. `DELIMIT_SELF_REPAIR_PAUSE=1` env var (forces OFF globally)
      2. `DELIMIT_SELF_REPAIR_MODE=<mode>` env var (forces global mode)
      3. Global `pause: true` in config (forces OFF)
      4. Per-function `pause: true` (forces OFF for that function)
      5. Per-function `mode` override
      6. Top-level `default_mode`
      7. Hard fallback: OFF
  - Writing a per-function mode change back to disk

NO writes happen on import. The mode change writer creates the user
config directory only when explicitly invoked.
"""

from __future__ import annotations

import logging
import os
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover - PyYAML is in gateway requirements
    _yaml = None  # type: ignore[assignment]

logger = logging.getLogger("delimit.ai.self_repair.mode")

# Bundled default config — used when ~/.delimit/self_repair.yaml is missing.
_BUNDLED_DEFAULT = Path(__file__).parent / "default_self_repair.yaml"

# User-level override (founder / pro customer machine).
USER_CONFIG_PATH = Path.home() / ".delimit" / "self_repair.yaml"


class Mode(str, Enum):
    """Self-repair intensity ladder.

    Order is meaningful: callers compare with `>=` / `<=` to short-circuit
    behaviors. e.g. a watcher only runs if `mode >= Mode.ALERT`; a
    diagnoser only runs if `mode >= Mode.DIAGNOSE`.
    """

    OFF = "off"
    ALERT = "alert"
    DIAGNOSE = "diagnose"
    DELIBERATE = "deliberate"
    ASSIST = "assist"
    FULL = "full"

    # Ordered list — populated below — used for comparison ops.
    @property
    def _rank(self) -> int:
        return _MODE_ORDER.index(self)

    def __ge__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, Mode):
            return self._rank >= other._rank
        return NotImplemented

    def __gt__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, Mode):
            return self._rank > other._rank
        return NotImplemented

    def __le__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, Mode):
            return self._rank <= other._rank
        return NotImplemented

    def __lt__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, Mode):
            return self._rank < other._rank
        return NotImplemented


_MODE_ORDER = [
    Mode.OFF,
    Mode.ALERT,
    Mode.DIAGNOSE,
    Mode.DELIBERATE,
    Mode.ASSIST,
    Mode.FULL,
]


def _coerce_mode(raw: Any) -> Mode:
    """Best-effort coerce a yaml/string value to a Mode. Falls back to OFF.

    YAML 1.1 quirk: bare `off` parses as boolean False. We treat both
    True and False as OFF (the only sane interpretation given the
    contract — booleans are not modes), but emit a clear warning so the
    user knows their config needs quoting.
    """
    if raw is None:
        return Mode.OFF
    if isinstance(raw, Mode):
        return raw
    if isinstance(raw, bool):
        # YAML parsed `off` / `on` as boolean. Always falls back to OFF
        # to fail closed; warn so the user fixes their config.
        logger.debug(
            "self_repair: mode value parsed as bool (%s) — quote it as a "
            "string in yaml. Treating as OFF.",
            raw,
        )
        return Mode.OFF
    s = str(raw).strip().lower()
    for m in _MODE_ORDER:
        if m.value == s:
            return m
    logger.warning("self_repair: unknown mode '%s' — falling back to OFF", raw)
    return Mode.OFF


def _read_yaml(path: Path) -> Dict[str, Any]:
    """Read a yaml file. Returns {} on missing file or parse failure."""
    if not path.exists():
        return {}
    if _yaml is None:
        logger.warning(
            "self_repair: PyYAML not available; cannot read %s", path
        )
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            logger.warning("self_repair: %s did not parse to a mapping", path)
            return {}
        return data
    except (OSError, _yaml.YAMLError) as exc:  # type: ignore[attr-defined]
        logger.warning("self_repair: failed to read %s: %s", path, exc)
        return {}


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the self-repair config.

    Resolution order:
      1. Explicit `path` argument (used by tests + CLI override).
      2. `~/.delimit/self_repair.yaml` (user override).
      3. Bundled `default_self_repair.yaml` (always-present fallback).

    Returns a dict that always contains a `default_mode` key. If every
    candidate file is missing or unreadable, returns
    `{"default_mode": "off", "functions": {}, "pause": False}`.
    """
    if path is not None:
        data = _read_yaml(path)
        if data:
            return data

    if path is None:
        user_data = _read_yaml(USER_CONFIG_PATH)
        if user_data:
            return user_data

    bundled = _read_yaml(_BUNDLED_DEFAULT)
    if bundled:
        return bundled

    return {"default_mode": "off", "functions": {}, "pause": False}


def get_mode(
    function_name: str, config: Optional[Dict[str, Any]] = None
) -> Mode:
    """Resolve the effective `Mode` for `function_name`.

    See module docstring for the full precedence order. The two pause
    flags (env + per-function) and the `DELIMIT_SELF_REPAIR_MODE` env
    override are resolved here so callers don't have to re-check.
    """
    # Hard kill switch — overrides every other mode setting.
    if os.environ.get("DELIMIT_SELF_REPAIR_PAUSE", "").strip() == "1":
        return Mode.OFF

    # Global runtime override (incident response / forced-mode demos).
    env_mode = os.environ.get("DELIMIT_SELF_REPAIR_MODE", "").strip()
    if env_mode:
        return _coerce_mode(env_mode)

    if config is None:
        config = load_config()

    # Global pause flag.
    if bool(config.get("pause", False)):
        return Mode.OFF

    functions = config.get("functions") or {}
    if not isinstance(functions, dict):
        functions = {}

    fn_cfg = functions.get(function_name)
    if isinstance(fn_cfg, dict):
        # Per-function pause overrides the per-function mode.
        if bool(fn_cfg.get("pause", False)):
            return Mode.OFF
        if "mode" in fn_cfg:
            return _coerce_mode(fn_cfg.get("mode"))

    # Fall back to top-level default_mode, then OFF.
    if "default_mode" in config:
        return _coerce_mode(config.get("default_mode"))

    return Mode.OFF


def set_mode(
    function_name: str,
    mode: Mode,
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist a mode change for one function.

    Writes to `config_path` if given, otherwise to `~/.delimit/self_repair.yaml`.
    Creates the parent directory if needed (this is the only place in this
    module that writes to disk; it must be called explicitly, never on
    import).

    Returns the updated config dict.
    """
    if not isinstance(mode, Mode):
        mode = _coerce_mode(mode)

    target = config_path or USER_CONFIG_PATH

    # Start from existing user config if present, else bundle default,
    # else minimal scaffold. This preserves customer pause flags + rate
    # limits across mode changes.
    base: Dict[str, Any] = {}
    if target.exists():
        base = _read_yaml(target)
    if not base:
        base = _read_yaml(_BUNDLED_DEFAULT)
    if not base:
        base = {"version": 1, "default_mode": "off", "pause": False, "functions": {}}

    functions = base.setdefault("functions", {})
    if not isinstance(functions, dict):
        functions = {}
        base["functions"] = functions

    fn_cfg = functions.setdefault(function_name, {})
    if not isinstance(fn_cfg, dict):
        fn_cfg = {}
        functions[function_name] = fn_cfg

    fn_cfg["mode"] = mode.value

    target.parent.mkdir(parents=True, exist_ok=True)
    if _yaml is None:
        raise RuntimeError(
            "PyYAML is required to persist self-repair mode changes"
        )
    with open(target, "w", encoding="utf-8") as f:
        _yaml.safe_dump(base, f, sort_keys=False)
    return base


def set_global_pause(
    paused: bool, config_path: Optional[Path] = None
) -> Dict[str, Any]:
    """Toggle the global `pause` flag in config.

    Used by `delimit self-repair pause` / `... resume`. Same on-disk
    rules as `set_mode` — explicit invocation only, no import-time
    side-effects.
    """
    target = config_path or USER_CONFIG_PATH
    base: Dict[str, Any] = {}
    if target.exists():
        base = _read_yaml(target)
    if not base:
        base = _read_yaml(_BUNDLED_DEFAULT)
    if not base:
        base = {"version": 1, "default_mode": "off", "pause": False, "functions": {}}
    base["pause"] = bool(paused)
    target.parent.mkdir(parents=True, exist_ok=True)
    if _yaml is None:
        raise RuntimeError(
            "PyYAML is required to persist self-repair pause changes"
        )
    with open(target, "w", encoding="utf-8") as f:
        _yaml.safe_dump(base, f, sort_keys=False)
    return base
