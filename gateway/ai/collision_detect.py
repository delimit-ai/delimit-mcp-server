"""Duplicate work detection — prevent two AI models from editing the same file (STR-051).

Tracks which model is working on which files. Alerts before collision.
Adjacent problem nobody else solves.

Storage: ~/.delimit/agents/file_locks.json
"""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

AGENTS_DIR = Path.home() / ".delimit" / "agents"
LOCKS_FILE = AGENTS_DIR / "file_locks.json"

# Lock expires after 30 minutes of inactivity
LOCK_TTL_SECONDS = 1800


def _ensure_dir():
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)


def _load_locks() -> Dict[str, Any]:
    if not LOCKS_FILE.exists():
        return {}
    try:
        return json.loads(LOCKS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_locks(locks: Dict[str, Any]):
    _ensure_dir()
    LOCKS_FILE.write_text(json.dumps(locks, indent=2))


def _cleanup_expired(locks: Dict[str, Any]) -> Dict[str, Any]:
    now = time.time()
    return {
        path: lock for path, lock in locks.items()
        if now - lock.get("ts", 0) < LOCK_TTL_SECONDS
    }


def claim_file(
    file_path: str,
    model: str,
    task_id: str = "",
) -> Dict[str, Any]:
    """Claim a file for editing. Returns collision info if another model holds it."""
    if not file_path or not model:
        return {"error": "file_path and model are required"}

    file_path = str(Path(file_path).resolve())
    model = model.lower().strip()

    locks = _cleanup_expired(_load_locks())

    existing = locks.get(file_path)
    if existing and existing["model"] != model:
        return {
            "status": "collision",
            "file": file_path,
            "held_by": existing["model"],
            "held_since": existing.get("claimed_at", "unknown"),
            "task_id": existing.get("task_id", ""),
            "your_model": model,
            "message": f"COLLISION: {existing['model']} is already editing {Path(file_path).name}",
            "recommendation": "Coordinate with the other model or wait for them to finish.",
        }

    locks[file_path] = {
        "model": model,
        "task_id": task_id,
        "ts": time.time(),
        "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _save_locks(locks)

    return {
        "status": "claimed",
        "file": file_path,
        "model": model,
        "message": f"{model} claimed {Path(file_path).name}",
    }


def release_file(file_path: str, model: str = "") -> Dict[str, Any]:
    """Release a file lock."""
    file_path = str(Path(file_path).resolve())
    locks = _load_locks()

    if file_path in locks:
        if model and locks[file_path]["model"] != model.lower():
            return {"error": f"File held by {locks[file_path]['model']}, not {model}"}
        del locks[file_path]
        _save_locks(locks)
        return {"status": "released", "file": file_path}

    return {"status": "ok", "message": "File was not locked"}


# --- STR-2202: checkout/branch-level claims -------------------------------
# Per-file locks (above) solve simultaneous edits WITHIN one checkout. The
# real 2026 fleet hazard, documented in memory
# (feedback_parallel_agents_need_worktree_isolation), is CHECKOUT-STATE
# collisions: two agents in the same checkout, an agent dirtying a tree a
# restart-watcher needs clean, a branch left checked out that another process
# assumes is main. These helpers reuse the SAME lock store + TTL but key on a
# ``checkout:`` prefix and treat ANY existing holder from a DIFFERENT task as a
# CONFLICT (not just a different model) — because two arms of the same model in
# one checkout still collide on branch/tree state. Additive: the existing
# claim_file / release_file / check_collisions surfaces are unchanged.

_CHECKOUT_PREFIX = "checkout:"


def _checkout_key(checkout: str) -> str:
    return _CHECKOUT_PREFIX + str(Path(checkout).expanduser().resolve())


def claim_checkout(
    checkout: str,
    model: str,
    task_id: str = "",
) -> Dict[str, Any]:
    """Claim a checkout root / worktree for exclusive write use.

    Conflict semantics differ from claim_file: a checkout held by ANY task
    other than ``task_id`` is a collision, regardless of model. Re-claiming
    with the same task_id is idempotent (returns "claimed").
    """
    if not checkout or not model:
        return {"error": "checkout and model are required"}

    key = _checkout_key(checkout)
    model = model.lower().strip()
    task_id = (task_id or "").strip()

    locks = _cleanup_expired(_load_locks())

    existing = locks.get(key)
    if existing and existing.get("task_id", "") != task_id:
        return {
            "status": "collision",
            "checkout": key[len(_CHECKOUT_PREFIX):],
            "held_by": existing.get("model", "unknown"),
            "held_by_task": existing.get("task_id", ""),
            "held_since": existing.get("claimed_at", "unknown"),
            "your_model": model,
            "your_task": task_id,
            "message": (
                f"CHECKOUT COLLISION: {existing.get('task_id') or existing.get('model')} "
                f"holds {key[len(_CHECKOUT_PREFIX):]} since "
                f"{existing.get('claimed_at', 'unknown')}"
            ),
            "recommendation": "Use an isolated worktree for this dispatch, or wait for the holder to release.",
        }

    locks[key] = {
        "model": model,
        "task_id": task_id,
        "kind": "checkout",
        "ts": time.time(),
        "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _save_locks(locks)

    return {
        "status": "claimed",
        "checkout": key[len(_CHECKOUT_PREFIX):],
        "lock_key": key,
        "model": model,
        "task_id": task_id,
        "message": f"{model} claimed checkout {key[len(_CHECKOUT_PREFIX):]}",
    }


def release_checkout(checkout: str = "", task_id: str = "") -> Dict[str, Any]:
    """Release checkout lock(s). By ``checkout`` path, by ``task_id``, or both.

    When only ``task_id`` is given, every checkout lock held by that task is
    released (the completion/handoff path uses this — it need not know which
    checkout was claimed).
    """
    locks = _load_locks()
    task_id = (task_id or "").strip()
    removed: List[str] = []

    if checkout:
        key = _checkout_key(checkout)
        lock = locks.get(key)
        if lock and (not task_id or lock.get("task_id", "") == task_id):
            del locks[key]
            removed.append(key[len(_CHECKOUT_PREFIX):])
    elif task_id:
        for key in [k for k, v in locks.items()
                    if k.startswith(_CHECKOUT_PREFIX) and v.get("task_id", "") == task_id]:
            del locks[key]
            removed.append(key[len(_CHECKOUT_PREFIX):])

    if removed:
        _save_locks(locks)
        return {"status": "released", "checkouts": removed}
    return {"status": "ok", "message": "No matching checkout lock held"}


def check_collisions(model: str = "") -> Dict[str, Any]:
    """Check for active file locks and potential collisions."""
    locks = _cleanup_expired(_load_locks())
    _save_locks(locks)

    active = []
    by_model = {}
    for path, lock in locks.items():
        entry = {
            "file": Path(path).name,
            "full_path": path,
            "model": lock["model"],
            "claimed_at": lock.get("claimed_at", ""),
            "task_id": lock.get("task_id", ""),
        }
        active.append(entry)
        by_model.setdefault(lock["model"], []).append(entry)

    # Detect overlapping directories (two models in same folder)
    dir_models = {}
    for path, lock in locks.items():
        parent = str(Path(path).parent)
        dir_models.setdefault(parent, set()).add(lock["model"])

    hotspots = [
        {"directory": d, "models": list(models), "risk": "high"}
        for d, models in dir_models.items() if len(models) > 1
    ]

    return {
        "status": "ok",
        "active_locks": len(active),
        "locks": active,
        "by_model": {m: len(files) for m, files in by_model.items()},
        "hotspots": hotspots,
        "message": f"{len(active)} active lock(s), {len(hotspots)} hotspot(s)" if active else "No active locks",
    }
