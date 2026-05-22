"""LED-193 ledger-item picker.

Selects the next eligible item to execute. One at a time (concurrency=1
is enforced upstream by the lockfile in scripts/led193_cron.py).

Eligibility (ALL must hold):
    1. Tagged ``auto_execute=class_a:<profile>`` where profile is in
       the whitelist {format_fix, lockfile_refresh, docs_typo}.
    2. Status is ``open`` (not done, in_progress, blocked, cancelled).
    3. Item is NOT marked ``worked_by=founder`` (founder claimed back).
    4. Item is younger than 7 days (created_at within window).
    5. No remote branch matching ``auto/{profile}-{item_id}-*``
       already exists (in-flight check).

Preference order (top of list first):
    P0 → P1 → P2 → P3, ties broken by oldest-first.

Returns ``None`` when no eligible item exists.
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("delimit.ai.led193_daemon.picker")

PROFILE_WHITELIST = {"format_fix", "lockfile_refresh", "docs_typo"}
TAG_PREFIX = "auto_execute=class_a:"
STALE_DAYS = 7
PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


# ── Tag parsing ────────────────────────────────────────────────────────


def parse_auto_execute_tag(tags: Iterable[str]) -> Optional[str]:
    """Return the profile name from an ``auto_execute=class_a:<profile>``
    tag, or ``None`` if no valid Class A tag is present.

    Rejects unknown profiles even if the prefix matches — so a typo or
    a future profile that hasn't been enrolled doesn't accidentally
    pick up.
    """
    if not tags:
        return None
    for tag in tags:
        if not isinstance(tag, str):
            continue
        if not tag.startswith(TAG_PREFIX):
            continue
        profile = tag[len(TAG_PREFIX):].strip()
        if profile in PROFILE_WHITELIST:
            return profile
    return None


# ── Age check ──────────────────────────────────────────────────────────


def _parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        # Accept both "...Z" and "...+00:00"
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def is_stale(item: Dict[str, Any], *, now: Optional[datetime] = None) -> bool:
    """True iff item is older than ``STALE_DAYS`` from ``created_at``.

    Falls back to ``updated_at`` when ``created_at`` is missing. If
    NEITHER is parseable, treat as stale (fail-closed: don't run on
    items with unknown age).
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=STALE_DAYS)
    created = _parse_iso(item.get("created_at") or "") or _parse_iso(item.get("updated_at") or "")
    if created is None:
        return True
    return created < cutoff


# ── In-flight check (remote branch presence) ───────────────────────────


def in_flight_branch_pattern(profile: str, item_id: str) -> str:
    return f"auto/{profile}-{item_id}-"


def has_in_flight_branch(
    *,
    profile: str,
    item_id: str,
    repo_path: Path,
    runner=None,
) -> bool:
    """Run ``git ls-remote --heads`` and return True iff any branch
    matches the in-flight prefix.

    ``runner`` is a test hook: callable taking (cmd_list, cwd) and
    returning a ``subprocess.CompletedProcess``-shaped object with
    ``stdout`` (str) and ``returncode`` (int). Defaults to real subprocess.
    """
    pattern = in_flight_branch_pattern(profile, item_id)
    cmd = ["git", "ls-remote", "--heads", "origin"]
    try:
        if runner is not None:
            res = runner(cmd, cwd=str(repo_path))
            stdout = getattr(res, "stdout", "") or ""
            rc = getattr(res, "returncode", 0)
        else:
            proc = subprocess.run(
                cmd,
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            stdout = proc.stdout or ""
            rc = proc.returncode
    except (subprocess.TimeoutExpired, OSError) as exc:
        # Fail-closed: if we can't tell whether a branch is in flight, skip
        # the item. A spurious skip is recoverable; a duplicate PR is not.
        logger.warning("led193_daemon: ls-remote failed (%s) — treating as in-flight", exc)
        return True
    if rc != 0:
        return True  # fail-closed
    for line in stdout.splitlines():
        # ls-remote line format: "<sha>\trefs/heads/<branch>"
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        ref = parts[1].strip()
        if not ref.startswith("refs/heads/"):
            continue
        branch = ref[len("refs/heads/"):]
        if branch.startswith(pattern):
            return True
    return False


# ── Sort + filter ──────────────────────────────────────────────────────


def _priority_rank(item: Dict[str, Any]) -> int:
    return PRIORITY_RANK.get(item.get("priority", "P3"), 99)


def _created_ts(item: Dict[str, Any]) -> str:
    # Lexical ISO compare — older is smaller.
    return str(item.get("created_at") or item.get("updated_at") or "")


def filter_eligible(
    items: Iterable[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> List[Tuple[str, Dict[str, Any]]]:
    """Return [(profile, item)] for items that pass the static gates.

    Static gates here = everything EXCEPT the in-flight remote-branch
    check, which requires git network access and is applied per
    candidate by the caller (cheaper to do it lazily on the top
    candidates only).
    """
    out: List[Tuple[str, Dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # Status gate — only "open" items execute.
        if (item.get("status") or "").strip().lower() != "open":
            continue
        # Founder claimed-back gate.
        worked_by = (item.get("worked_by") or "").strip().lower()
        if worked_by == "founder":
            continue
        # Tag gate.
        profile = parse_auto_execute_tag(item.get("tags") or [])
        if profile is None:
            continue
        # Age gate.
        if is_stale(item, now=now):
            continue
        out.append((profile, item))
    # Sort: priority asc (P0 first), then created_at asc (oldest first).
    out.sort(key=lambda pair: (_priority_rank(pair[1]), _created_ts(pair[1])))
    return out


def pick_next_item(
    items: Iterable[Dict[str, Any]],
    *,
    repo_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    runner=None,
    skip_in_flight_check: bool = False,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Return the next eligible (profile, item) or ``None``.

    The in-flight remote-branch check runs against ``repo_path`` (the
    target repo). When ``repo_path`` is None or
    ``skip_in_flight_check=True``, the check is bypassed (test hook).
    """
    eligible = filter_eligible(items, now=now)
    for profile, item in eligible:
        if skip_in_flight_check or repo_path is None:
            return profile, item
        item_id = item.get("id") or ""
        if not item_id:
            continue
        if has_in_flight_branch(
            profile=profile,
            item_id=item_id,
            repo_path=repo_path,
            runner=runner,
        ):
            logger.info(
                "led193_daemon: skipping %s — in-flight branch %s* exists",
                item_id,
                in_flight_branch_pattern(profile, item_id),
            )
            continue
        return profile, item
    return None
