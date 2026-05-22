"""LED-1279: dispatcher anti-duplicate gate.

Before creating a new agent task tagged with an LED ID, check whether any
local repository's git history already contains a commit referencing that
LED. If it does, refuse the dispatch and auto-close the LED — yesterday's
AGT-65A61AD5 wasted three subagent cycles on LEDs already shipped in
commit 014fb5c (PR #106) on 2026-05-03.

Cost model: each duplicate dispatch burns 5-30 minutes of subagent + orchestrator
attention. This gate pays for itself within 1-2 future dispatches.

Design notes:
  - LED-id parsing is conservative: r"LED-\\d+" only. AGT-... and STR-...
    do not trigger the gate — only operational ledger items.
  - Repo discovery: prefer caller-supplied list, then fall back to a small
    static list of canonical Delimit / wire-report / livetube / dv repos.
    A missing repo logs a warning and is skipped (don't fail dispatch on
    infra issues — that's a worse failure mode than a false negative).
  - Match scope: only commits on the *first-parent* line of `main` count
    as "shipped" so feature-branch WIP that mentions an LED but never
    merged doesn't trigger a false positive. PR-merge commits qualify.
  - Time window: only commits with author/commit date >= LED.created_at
    are considered. An LED can't have been "shipped" before it existed.
  - Multiple matches: the FIRST match (oldest commit since created_at) wins.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Default repos to search. Discovered at runtime first via the venture
# registry; this list is the safety net if discovery returns nothing.
DEFAULT_REPOS = (
    "/home/delimit/delimit-gateway",
    "/home/delimit/delimit-action",
    "/home/delimit/npm-delimit",
    "/home/delimit/delimit-ui",
)

# Conservative LED-id matcher. Plain LED-NNN format.
LED_ID_RE = re.compile(r"\bLED-(\d+)\b", re.IGNORECASE)


def extract_led_id(*texts: str) -> Optional[str]:
    """Return the first LED-NNN id found in any of the supplied strings.

    Used by the dispatcher to grab the LED tag from title/description/context
    without forcing callers to plumb it as a separate parameter.
    """
    for text in texts:
        if not text:
            continue
        m = LED_ID_RE.search(text)
        if m:
            return f"LED-{m.group(1)}"
    return None


def discover_repos() -> list[str]:
    """Return the list of repo paths to search.

    Reads the venture registry (~/.delimit/ventures.json) and unions in the
    DEFAULT_REPOS safety net. Filters to existing directories that actually
    contain a .git subdir — pseudo-ventures pointing at /tmp paths or stale
    entries get dropped silently.
    """
    seen: list[str] = []
    seen_set: set[str] = set()

    def _add(path: str) -> None:
        if not path:
            return
        p = Path(path).resolve()
        s = str(p)
        if s in seen_set:
            return
        if not (p / ".git").exists():
            return
        seen_set.add(s)
        seen.append(s)

    # 1. Venture registry — ~/.delimit/ventures.json
    try:
        from ai.ledger_manager import VENTURES_FILE  # late import: tests stub VENTURES_FILE
        import json

        if VENTURES_FILE.exists():
            ventures = json.loads(VENTURES_FILE.read_text())
            for info in ventures.values():
                _add(info.get("path", ""))
    except Exception as e:  # pragma: no cover — best effort
        logger.debug("dispatch_gate: venture registry read failed: %s", e)

    # 2. Safety net defaults
    for path in DEFAULT_REPOS:
        _add(path)

    return seen


def _parse_iso_z(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (with optional trailing Z) to a tz-aware UTC datetime."""
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _git_log_first_parent_grep(
    repo: str,
    led_id: str,
    since_iso: Optional[str],
    timeout_sec: float = 8.0,
) -> list[tuple[str, str, str]]:
    """Run `git log --first-parent main` filtered by --grep=<led_id>.

    Returns a list of (sha, iso_date, subject) tuples, oldest first. Empty
    list if the repo isn't a git checkout, the ref doesn't exist, or git
    times out / errors. Errors are logged at DEBUG; we never raise — a
    missing repo must NOT fail dispatch.
    """
    repo_path = Path(repo)
    if not (repo_path / ".git").exists():
        logger.debug("dispatch_gate: repo missing or not a git checkout: %s", repo)
        return []

    cmd = [
        "git",
        "-C",
        str(repo_path),
        "log",
        "--first-parent",
        "main",
        f"--grep={led_id}",
        "-i",  # case-insensitive grep — match LED-1208 / led-1208 / Led-1208
        "--pretty=%H%x09%cI%x09%s",
        "--reverse",  # oldest first so [0] is the FIRST match
    ]
    if since_iso:
        cmd.append(f"--since={since_iso}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning("dispatch_gate: git log failed for %s: %s", repo, e)
        return []

    if result.returncode != 0:
        # Try `master` as fallback for repos that haven't renamed yet, but
        # only for the specific "unknown revision" failure — anything else
        # is a real error we should silently skip.
        stderr = result.stderr or ""
        if "unknown revision" in stderr.lower() or "ambiguous argument 'main'" in stderr.lower():
            cmd[5] = "master"
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout_sec, check=False
                )
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
                logger.warning("dispatch_gate: git log master fallback failed for %s: %s", repo, e)
                return []
            if result.returncode != 0:
                logger.debug("dispatch_gate: git log non-zero for %s: %s", repo, result.stderr)
                return []
        else:
            logger.debug("dispatch_gate: git log non-zero for %s: %s", repo, result.stderr)
            return []

    matches: list[tuple[str, str, str]] = []
    for line in (result.stdout or "").splitlines():
        if not line:
            continue
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        matches.append((parts[0], parts[1], parts[2]))
    return matches


def is_led_already_shipped(
    led_id: str,
    created_at: str = "",
    repos: Optional[Iterable[str]] = None,
) -> tuple[bool, Optional[dict]]:
    """Search the given repos for a commit on `main`'s first-parent line that
    mentions ``led_id`` and was committed at or after ``created_at``.

    Args:
        led_id: e.g. "LED-1208" — must be the bare ID, not "LED-1208 fix something".
        created_at: ISO-8601 timestamp of when the LED was opened. Commits
            older than this are treated as unrelated mentions and ignored.
            Empty string disables the time filter (used by the sweep, which
            is willing to accept older matches at the cost of false positives).
        repos: optional iterable of repo paths. Defaults to discover_repos().

    Returns:
        (shipped: bool, details: dict | None)
        details, when shipped, is:
            {
                "repo": "/home/delimit/delimit-gateway",
                "sha": "014fb5c...",
                "short_sha": "014fb5c",
                "date": "2026-05-03T17:13:45-04:00",
                "subject": "fix(self-repair): ...",
            }
    """
    if not led_id or not LED_ID_RE.fullmatch(led_id):
        return False, None

    # Normalize LED ID to canonical "LED-NNN" so the grep is consistent.
    norm_id = led_id.upper()

    repo_list = list(repos) if repos is not None else discover_repos()
    if not repo_list:
        return False, None

    since_iso: Optional[str] = None
    since_dt = _parse_iso_z(created_at) if created_at else None
    if since_dt is not None:
        since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%S%z")

    # Collect first match per repo, then pick the globally-oldest.
    candidates: list[tuple[str, str, str, str]] = []
    for repo in repo_list:
        rows = _git_log_first_parent_grep(repo, norm_id, since_iso)
        if not rows:
            continue
        # rows are oldest-first; defensively re-check the bare-id token is
        # in the subject — `git log --grep` would already match but a stray
        # "LED-12080" could match LED-1208's pattern if regex was loose.
        # Our LED_ID_RE uses \b boundaries so we're safe; double-check anyway.
        for sha, date_iso, subject in rows:
            if not LED_ID_RE.search(subject):
                continue
            if since_dt is not None:
                commit_dt = _parse_iso_z(date_iso)
                if commit_dt is not None and commit_dt < since_dt:
                    continue
            # Verify the LED token in the subject actually equals our target.
            tokens = {f"LED-{m.group(1)}" for m in LED_ID_RE.finditer(subject)}
            if norm_id not in tokens:
                continue
            candidates.append((repo, sha, date_iso, subject))
            break  # first match per repo

    if not candidates:
        return False, None

    # Pick the oldest (smallest commit date) across all repos.
    candidates.sort(key=lambda t: t[2])
    repo, sha, date_iso, subject = candidates[0]
    return True, {
        "repo": repo,
        "sha": sha,
        "short_sha": sha[:7],
        "date": date_iso,
        "subject": subject,
    }


def auto_close_shipped_led(led_id: str, details: dict) -> dict:
    """Mark an already-shipped LED as done with a note pointing to the commit.

    Wraps ``ai.ledger_manager.update_item`` so the dispatcher's refusal path
    has a single call site. Errors here MUST NOT propagate — if ledger update
    fails for whatever reason (stale registry, missing file), we still want
    to refuse the dispatch.
    """
    try:
        from ai.ledger_manager import update_item

        note = (
            f"Auto-closed by dispatcher gate (LED-1279): shipped in "
            f"{details.get('repo','?')}@{details.get('short_sha','?')} on "
            f"{details.get('date','?')}. "
            f"Refused duplicate AGT dispatch."
        )
        result = update_item(
            item_id=led_id,
            status="done",
            note=note,
            worked_by="dispatcher-gate",
        )
        return {"updated": True, "result": result}
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("dispatch_gate: auto-close of %s failed: %s", led_id, e)
        return {"updated": False, "error": str(e)}


def evaluate_dispatch(
    title: str,
    description: str = "",
    context: str = "",
    led_created_at: str = "",
    repos: Optional[Iterable[str]] = None,
) -> Optional[dict]:
    """Run the gate: extract an LED id, check if shipped, return refusal payload or None.

    Returns:
        - None when dispatch should proceed (no LED tag, or LED not shipped).
        - A refusal dict when dispatch should be blocked:
            {
                "status": "refused",
                "reason": "led_already_shipped",
                "led_id": "LED-1208",
                "shipped_in": {"repo": ..., "sha": ..., "short_sha": ..., "date": ..., "subject": ...},
                "auto_close": {...},
                "message": "...",
            }
    """
    led_id = extract_led_id(title, description, context)
    if not led_id:
        return None  # No LED tag — orchestrator may dispatch generic work.

    shipped, details = is_led_already_shipped(
        led_id, created_at=led_created_at, repos=repos
    )
    if not shipped or not details:
        return None

    auto_close = auto_close_shipped_led(led_id, details)
    return {
        "status": "refused",
        "reason": "led_already_shipped",
        "led_id": led_id,
        "shipped_in": details,
        "auto_close": auto_close,
        "message": (
            f"Refused: {led_id} already shipped in "
            f"{details['repo']}@{details['short_sha']} on {details['date'][:10]} "
            f"({details['subject'][:80]}). LED auto-closed."
        ),
    }


def lookup_led_created_at(led_id: str) -> str:
    """Look up the LED's created_at across all known ledger files.

    Returns the original-create timestamp (genesis row, not the latest update).
    Empty string when the LED isn't found anywhere — the gate then runs without
    the time filter, accepting the small false-positive risk.
    """
    if not led_id:
        return ""
    norm = led_id.upper()
    try:
        from ai.ledger_manager import LEDGER_V2_DIR
    except Exception:
        return ""

    if not LEDGER_V2_DIR.exists():
        return ""

    import json

    # Walk every ledger file under ledger-v2/* looking for the genesis row.
    for jsonl in LEDGER_V2_DIR.rglob("*.jsonl"):
        try:
            with open(jsonl, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("id") != norm:
                        continue
                    if row.get("type") == "update":
                        continue  # only the genesis row carries created_at-of-LED
                    ts = row.get("created_at", "")
                    if ts:
                        return ts
        except OSError:
            continue
    return ""
