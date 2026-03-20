"""
Bridge to deploy tracking — file-based deploy plan management.
Tier 3 Extended — tracks deploy plans, builds, and rollbacks locally.

No external server required. Plans stored at ~/.delimit/deploys/.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.ai.deploy_bridge")

DEPLOY_DIR = Path.home() / ".delimit" / "deploys"


def _ensure_dir():
    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)


def _list_plans(app: Optional[str] = None, env: Optional[str] = None) -> List[Dict]:
    """List all deploy plans, optionally filtered by app and/or env."""
    _ensure_dir()
    plans = []
    for f in sorted(DEPLOY_DIR.glob("PLAN-*.json"), reverse=True):
        try:
            data = json.loads(f.read_text())
            if app and data.get("app") != app:
                continue
            if env and data.get("env") != env:
                continue
            plans.append(data)
        except Exception:
            continue
    return plans


def plan(app: str, env: str, git_ref: Optional[str] = None) -> Dict[str, Any]:
    """Create a deploy plan."""
    _ensure_dir()
    plan_id = f"PLAN-{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    data = {
        "plan_id": plan_id,
        "app": app,
        "env": env,
        "git_ref": git_ref or "HEAD",
        "status": "planned",
        "created_at": now,
        "updated_at": now,
        "history": [{"status": "planned", "at": now}],
    }
    (DEPLOY_DIR / f"{plan_id}.json").write_text(json.dumps(data, indent=2))
    return data


def status(app: str, env: str) -> Dict[str, Any]:
    """Get latest deploy status for an app+env."""
    plans = _list_plans(app=app, env=env)
    if not plans:
        return {
            "app": app,
            "env": env,
            "status": "no_deploys",
            "message": f"No deploy plans found for {app} in {env}.",
        }
    latest = plans[0]
    return {
        "app": app,
        "env": env,
        "latest_plan": latest["plan_id"],
        "status": latest["status"],
        "git_ref": latest.get("git_ref"),
        "updated_at": latest.get("updated_at"),
        "total_plans": len(plans),
    }


def build(app: str, git_ref: Optional[str] = None) -> Dict[str, Any]:
    """Check if a Dockerfile exists and return build info."""
    dockerfile = Path.cwd() / "Dockerfile"
    if not dockerfile.exists():
        # Check app-specific paths
        for candidate in [Path.home() / app / "Dockerfile", Path(f"./{app}/Dockerfile")]:
            if candidate.exists():
                dockerfile = candidate
                break

    if dockerfile.exists():
        return {
            "app": app,
            "git_ref": git_ref or "HEAD",
            "dockerfile": str(dockerfile),
            "status": "ready",
            "message": f"Dockerfile found at {dockerfile}. Ready to build.",
        }
    return {
        "app": app,
        "git_ref": git_ref or "HEAD",
        "status": "no_dockerfile",
        "message": f"No Dockerfile found for {app}. Create one to enable Docker builds.",
    }


def publish(app: str, git_ref: Optional[str] = None) -> Dict[str, Any]:
    """Update latest plan status to published."""
    plans = _list_plans(app=app)
    if not plans:
        return {"error": f"No deploy plans found for {app}"}
    latest = plans[0]
    now = datetime.now(timezone.utc).isoformat()
    latest["status"] = "published"
    latest["updated_at"] = now
    latest["history"].append({"status": "published", "at": now})
    (DEPLOY_DIR / f"{latest['plan_id']}.json").write_text(json.dumps(latest, indent=2))
    return latest


def verify(app: str, env: str, git_ref: Optional[str] = None) -> Dict[str, Any]:
    """Verify deployment health (stub — returns plan status)."""
    plans = _list_plans(app=app, env=env)
    if not plans:
        return {"app": app, "env": env, "status": "no_deploys", "healthy": False}
    latest = plans[0]
    return {
        "app": app,
        "env": env,
        "plan_id": latest["plan_id"],
        "status": latest["status"],
        "healthy": latest["status"] in ("published", "planned"),
        "message": "Health check is a stub — no real endpoint verification yet.",
    }


def rollback(app: str, env: str, to_sha: Optional[str] = None) -> Dict[str, Any]:
    """Mark latest plan as rolled back."""
    plans = _list_plans(app=app, env=env)
    if not plans:
        return {"error": f"No deploy plans found for {app} in {env}"}
    latest = plans[0]
    now = datetime.now(timezone.utc).isoformat()
    latest["status"] = "rolled_back"
    latest["updated_at"] = now
    latest["rolled_back_to"] = to_sha
    latest["history"].append({"status": "rolled_back", "at": now, "to_sha": to_sha})
    (DEPLOY_DIR / f"{latest['plan_id']}.json").write_text(json.dumps(latest, indent=2))
    return latest
