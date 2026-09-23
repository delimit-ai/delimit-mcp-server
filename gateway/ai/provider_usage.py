"""Cached provider meters and quota-aware pooled worker selection (LED-5672)."""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import time
from typing import Any
import urllib.request

from ai.tenant_paths import _delimit_home

_RPC_TIMEOUT_S = 8.0
_MUSE_TIMEOUT_S = 8.0
_MUSE_CACHE: dict[str, tuple[float, dict]] = {}
_HTTP_TIMEOUT_S = 8.0
_ORDER = ("muse", "copilot", "antigravity", "codex")
_CLI = {"muse": "muse", "copilot": "copilot", "antigravity": "agy", "codex": "codex"}
_ALL = ("codex", "claude", "muse", "deepseek", "x_api", "copilot", "antigravity", "gemini")


def _state_path(name: str) -> Path:
    return _delimit_home() / "state" / name


def _policy() -> dict:
    try:
        data = json.loads(_state_path("provider_budget.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _copilot_hold_until(policy: dict) -> float:
    """Local-policy Copilot hold: provider_budget.json `copilot_exhausted_until`
    (ISO date/datetime or epoch seconds). Absent/invalid = no hold."""
    raw = policy.get("copilot_exhausted_until")
    if raw in (None, ""):
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        pass
    try:
        when = dt.datetime.fromisoformat(str(raw))
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        return when.timestamp()
    except ValueError:
        return 0.0


def _unknown(provider: str, reason: str) -> dict:
    return {"provider": provider, "windows": [], "status": "unknown", "reason": reason,
            "observed_at": time.time(), "source": "none"}


def _iso_epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _x_reset_epoch(value: Any) -> float | None:
    """X's cap_reset_day is a UTC day of month, not an epoch timestamp."""
    if isinstance(value, int) and 1 <= value <= 31:
        now = dt.datetime.now(dt.timezone.utc)
        for offset in range(0, 3):
            year = now.year + (now.month + offset - 1) // 12
            month = (now.month + offset - 1) % 12 + 1
            try:
                reset = dt.datetime(year, month, value, tzinfo=dt.timezone.utc)
            except ValueError:
                continue
            if reset.timestamp() > now.timestamp():
                return reset.timestamp()
        return None
    return _iso_epoch(value)


def _window(name: str, used: Any, reset: Any, window_hours: float | None = None) -> dict:
    window = {"name": name, "used_percent": float(used), "resets_at": _iso_epoch(reset)}
    if window_hours is not None:
        window["window_hours"] = window_hours
    return window


def _http_json(url: str, headers: dict, timeout: float = _HTTP_TIMEOUT_S) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def _rpc_process(command: list[str], calls: list[tuple[str, dict, bool]],
                 timeout: float = _RPC_TIMEOUT_S) -> list[dict]:
    """One bounded JSON-RPC stdio exchange; never expose host stderr."""
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, bufsize=0)
    replies = []
    deadline = time.monotonic() + timeout
    buffer = b""
    try:
        for number, (method, params, notification) in enumerate(calls, 1):
            message = {"jsonrpc": "2.0", "method": method, "params": params}
            if not notification:
                message["id"] = number
            proc.stdin.write((json.dumps(message) + "\n").encode())
            proc.stdin.flush()
            if notification:
                continue
            while True:
                while b"\n" not in buffer:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("JSON-RPC timeout")
                    ready, _, _ = select.select([proc.stdout], [], [], remaining)
                    if not ready:
                        raise TimeoutError("JSON-RPC timeout")
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    if not chunk:
                        raise RuntimeError("JSON-RPC host closed")
                    buffer += chunk
                line, buffer = buffer.split(b"\n", 1)
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if data.get("id") != number:
                    continue
                if "error" in data:
                    raise RuntimeError("JSON-RPC error: " + str(data["error"].get("message", "unknown"))[:100])
                replies.append(data.get("result") or {})
                break
        return replies
    finally:
        proc.kill()
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _codex() -> dict:
    result = _rpc_process(["codex", "app-server"], [
        ("initialize", {"clientInfo": {"name": "delimit_usage_probe", "version": "0.1"}}, False),
        ("initialized", {}, True), ("account/rateLimits/read", {}, False),
    ])[-1]
    limits = result.get("rateLimits") or {}
    windows = []
    for key in ("primary", "secondary"):
        item = limits.get(key)
        if isinstance(item, dict) and item.get("usedPercent") is not None:
            duration = item.get("windowDurationMins")
            windows.append(_window(key, item["usedPercent"], item.get("resetsAt"),
                                   float(duration) / 60 if duration else None))
    if not windows:
        return _unknown("codex", "no rate limits returned")
    return {"provider": "codex", "windows": windows, "status": "ok",
            "plan_type": limits.get("planType"), "observed_at": time.time(), "source": "codex_app_server"}


def _claude() -> dict:
    path = Path.home() / ".claude" / ".credentials.json"
    token = json.loads(path.read_text())["claudeAiOauth"]["accessToken"]
    data = _http_json("https://api.anthropic.com/api/oauth/usage",
                      {"Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20"})
    # The endpoint reports utilization already in percent (e.g. 35.0), verified
    # live 2026-09-23; scaling it again read as 3500%.
    windows = [_window(name, float(data[name]["utilization"]), data[name].get("resets_at"),
                       5 if name == "five_hour" else 168)
               for name in ("five_hour", "seven_day") if isinstance(data.get(name), dict)
               and data[name].get("utilization") is not None]
    if not windows:
        return _unknown("claude", "no usage windows returned")
    return {"provider": "claude", "windows": windows, "status": "ok",
            "observed_at": time.time(), "source": "anthropic_oauth"}


def _deepseek() -> dict:
    secret = json.loads((_delimit_home() / "secrets" / "DEEPSEEK_API_KEY.json").read_text())
    token = base64.b64decode(secret["encoded_value"]).decode()
    data = _http_json("https://api.deepseek.com/user/balance", {"Authorization": "Bearer " + token})
    balances = data.get("balance_infos") or []
    usd = sum(float(row["total_balance"]) for row in balances if row.get("currency", "USD") == "USD")
    return {"provider": "deepseek", "windows": [], "balance_usd": usd, "status": "ok",
            "observed_at": time.time(), "source": "deepseek_balance"}


def _x_api() -> dict:
    # Which stored secret holds the X API bearer token is local configuration:
    # DELIMIT_X_USAGE_SECRET, then provider_budget.json `x_usage_secret`,
    # then the generic `x_api.json`.
    name = (os.environ.get("DELIMIT_X_USAGE_SECRET") or _policy().get("x_usage_secret")
            or "x_api.json")
    path = _delimit_home() / "secrets" / os.path.basename(str(name))
    if not path.exists():
        return _unknown("x_api", "no X usage credential configured")
    secret = json.loads(path.read_text())
    data = _http_json("https://api.twitter.com/2/usage/tweets?usage.fields=project_cap,project_usage,cap_reset_day",
                      {"Authorization": "Bearer " + secret["bearer_token"]})
    body = data.get("data", data)
    if isinstance(body, list):
        body = body[0] if body else {}
    cap, used = body.get("project_cap"), body.get("project_usage")
    if not cap:
        return _unknown("x_api", "project cap unavailable")
    return {"provider": "x_api", "windows": [_window("project", 100 * float(used or 0) / float(cap),
                                                     _x_reset_epoch(body.get("cap_reset_day")))],
            "status": "ok", "observed_at": time.time(), "source": "x_usage"}


def _muse() -> dict:
    """Read MSP's last observation without starting a quota-consuming turn."""
    data = _rpc_process(["muse", "serve", "--no-session-log"], [
        ("initialize", {"clientInfo": {"name": "delimit_usage_probe", "version": "0.1"}}, False),
        ("initialized", {}, True), ("usage/read", {}, False),
    ], timeout=_MUSE_TIMEOUT_S)[-1].get("usage") or {}
    if not data:
        return _unknown("muse", "no usage observed by MSP host")
    windows = []
    for key in ("window", "weekly"):
        item = data.get(key)
        if isinstance(item, dict) and item.get("usedPercent") is not None:
            duration = item.get("windowDurationMins")
            windows.append(_window(key, item["usedPercent"],
                                   float(item["resetsAtMs"]) / 1000 if item.get("resetsAtMs") else None,
                                   float(duration) / 60 if duration else None))
    if not windows:
        return _unknown("muse", "usage windows unavailable")
    return {"provider": "muse", "windows": windows, "status": "ok", "tier": data.get("tier"),
            "observed_at": float(data.get("observedAtMs", time.time() * 1000)) / 1000,
            "source": "muse_msp"}


def _muse_ok_ttl(item: dict, cap_s: float = 60.0) -> float:
    """Remaining freshness of an 'ok' Muse reading: never more than cap_s
    after the host observed it, so caching cannot extend a stale reading."""
    try:
        age = time.time() - float(item.get("observed_at"))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(cap_s, cap_s - age))


def read_usage(provider: str, max_age_s: int = 1800) -> dict:
    """Return a cached, normalized meter; every adapter fails closed to unknown."""
    provider = str(provider).lower()
    if provider not in _ALL:
        return _unknown(provider, "unsupported provider")
    path = _state_path("provider_usage.json")
    muse_key = str(path) if provider == "muse" else None
    if muse_key is not None and max_age_s > 0:
        cached = _MUSE_CACHE.get(muse_key)
        if cached and time.monotonic() < cached[0]:
            item = cached[1]
            # Honour the caller's freshness bound: an 'ok' reading older than
            # max_age_s is not served from memory even if its TTL remains.
            try:
                fresh = item.get("status") != "ok" or time.time() - float(item.get("observed_at")) <= max_age_s
            except (TypeError, ValueError):
                fresh = False
            if fresh:
                return item
    try:
        cache = json.loads(path.read_text())
        item = cache.get(provider)
        if (isinstance(item, dict) and item.get("status") == "ok"
                and 0 <= time.time() - float(item["observed_at"]) <=
                (min(max_age_s, 60) if provider == "muse" else max_age_s)):
            if muse_key is not None and max_age_s > 0:
                _MUSE_CACHE[muse_key] = (time.monotonic() + _muse_ok_ttl(item), item)
            return item
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        cache = {}
    try:
        if provider == "copilot":
            item = _unknown(provider, "no usage meter (Copilot usage needs the gh 'user' scope)")
        elif provider in {"antigravity", "gemini"}:
            item = _unknown(provider, "no meter known")
        else:
            item = {"codex": _codex, "claude": _claude, "muse": _muse,
                    "deepseek": _deepseek, "x_api": _x_api}[provider]()
        if item.get("status") == "ok":
            cache = cache if isinstance(cache, dict) else {}
            cache[provider] = item
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(cache))
                os.replace(tmp, path)
            except OSError:
                # A read-only home must not erase a successful live reading.
                pass
        if muse_key is not None and max_age_s > 0:
            _MUSE_CACHE[muse_key] = (time.monotonic() + (_muse_ok_ttl(item) if item.get("status") == "ok" else 120), item)
        return item
    except Exception as exc:
        # Exception strings from HTTP clients can contain URLs but never credential headers.
        item = _unknown(provider, type(exc).__name__ + ": " + str(exc)[:100])
        if muse_key is not None and max_age_s > 0:
            _MUSE_CACHE[muse_key] = (time.monotonic() + 120, item)
        return item


def read_muse_usage(max_age_s: int = 1800) -> dict:
    return read_usage("muse", max_age_s)


def budget_tier(usage: dict, thresholds: dict | None = None, *, background: bool = True) -> str:
    """unknown/stale blocks P2 background only; reserve blocks every priority."""
    thresholds = thresholds or _policy()
    if usage.get("status") != "ok" or time.time() - float(usage.get("observed_at", 0)) > 1800:
        return "priority_only" if background else "normal"
    percentages = [float(w["used_percent"]) for w in usage.get("windows", [])]
    if not percentages:
        return "normal"
    peak = max(percentages)
    if peak >= float(thresholds.get("reserve", 95)):
        return "reserve"
    if peak >= float(thresholds.get("priority_only", 80)):
        return "priority_only"
    return "normal"


def _budget_reason(usage: dict, tier: str, policy: dict) -> str:
    if usage.get("status") != "ok":
        return "budget: " + usage.get("reason", "unknown usage")
    windows = usage.get("windows") or []
    if not windows:
        return "budget: no usage window"
    top = max(windows, key=lambda w: float(w.get("used_percent", 0)))
    threshold = policy.get("reserve" if tier == "reserve" else "priority_only", 95 if tier == "reserve" else 80)
    return f"budget: {top['name']} {top['used_percent']:g}% ≥ {threshold}%"


def muse_background_admitted(priority: str) -> bool:
    # TODO(LED-5672): call from background Muse consumers before they start a turn.
    tier = budget_tier(read_muse_usage())
    return tier != "reserve" and (tier != "priority_only" or priority.upper() in {"P0", "P1"})


def deepseek_background_admitted(task_type: str, expected_usd: float = 0) -> bool:
    """Balance gate for future non-coding API lanes; no contained worker exists.

    TODO(LED-5672): add DeepSeek to auto only after a contained launcher exists.
    """
    if task_type.lower() in {"feat", "fix", "refactor", "test", "coding"}:
        return False
    cap = float(_policy().get("deepseek_dollar_cap", 0))
    balance = read_usage("deepseek")
    return (cap > 0 and 0 <= expected_usd <= cap and balance.get("status") == "ok"
            and float(balance.get("balance_usd", 0)) >= expected_usd)


def choose_runtime(task_priority: str, task_type: str = "", exclude=None,
                   readings: dict | None = None) -> dict:
    """Pick the lowest use-it-or-lose-it pressure among eligible non-lead workers."""
    policy = _policy()
    lead = str(policy.get("lead", "claude")).lower()
    excluded = set(exclude or ())
    skipped = {}
    ranked = []
    now = time.time()
    for runtime in _ORDER:
        if runtime in excluded:
            continue
        if runtime == lead:
            skipped[runtime] = "budget: reserved lead provider"
            continue
        if runtime == "copilot" and now < _copilot_hold_until(policy):
            skipped[runtime] = "budget: copilot exhausted (local policy)"
            continue
        if shutil.which(_CLI[runtime]) is None:
            skipped[runtime] = f"{_CLI[runtime]} not installed"
            continue
        # Existing shared hold is authoritative; import lazily to avoid a cycle.
        try:
            from ai.agent_dispatch import _dispatch_runtime_hold
            hold = _dispatch_runtime_hold(runtime)
        except ImportError:
            hold = None
        if hold:
            skipped[runtime] = f"on {hold.get('reason', 'quota')} hold until {hold.get('until')}"
            continue
        usage = readings[runtime] if readings is not None and runtime in readings else read_usage(runtime)
        windows = usage.get("windows") or []
        if any(float(w["used_percent"]) >= float(policy.get("reserve", 95)) for w in windows):
            skipped[runtime] = _budget_reason(usage, "reserve", policy)
            continue
        tier = budget_tier(usage, policy)
        if tier == "reserve" or (tier == "priority_only" and task_priority.upper() == "P2"):
            skipped[runtime] = _budget_reason(usage, tier, policy)
            continue
        fresh = 0 <= now - float(usage.get("observed_at", 0)) <= 1800
        if usage.get("status") == "ok" and fresh and windows:
            expected = float(policy.get("expected_spend_percent", 1))
            def pressure(window: dict) -> float:
                reset = window.get("resets_at")
                if reset is None:
                    return float("inf")
                name = window.get("name", "")
                fallback_hours = 5 if name in {"primary", "window", "five_hour"} else 168
                window_hours = float(window.get("window_hours") or fallback_hours)
                hours_to_reset = max(0, (float(reset) - now) / 3600)
                return max(0, (float(window["used_percent"]) + expected)
                           * hours_to_reset / window_hours)
            score = max(pressure(w) for w in windows)
        else:
            score = float("inf")
        ranked.append((score, _ORDER.index(runtime), runtime))
    ranked.sort()
    chosen = ranked[0][2] if ranked else None
    reason = (f"lowest quota pressure ({ranked[0][0]:.2f})" if chosen else "no eligible pooled runtime")
    return {"runtime": chosen, "reason": reason, "skipped": skipped,
            "ranked": [r for _, _, r in ranked]}


def usage_report() -> dict:
    readings = {p: read_usage(p) for p in _ALL}
    choice = choose_runtime("P2", readings=readings)
    return {"providers": readings, "tiers": {p: budget_tier(u) for p, u in readings.items()},
            "auto_next": choice}


if __name__ == "__main__":
    print(json.dumps(usage_report(), sort_keys=True))
