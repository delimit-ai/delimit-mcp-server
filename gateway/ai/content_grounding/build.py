"""
Ingestion + validation for the grounding layer (LED-1084 Week 1).

Reads three canonical sources:
  - ~/.delimit/ledger/*.jsonl  → decisions / incidents / outreach / releases
  - ~/.delimit/attestations/*.json → HMAC-signed delimit wrap bundles
  - `git log` on delimit-gateway → commit events

Produces a `GroundingIndex` snapshot that downstream generators consume.

Week 1 posture: ingestion + validation only. No generation, no publishing.
`_PUBLISH_DISABLED = True` in `__init__` enforces this at import time.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .schemas import (
    Claim,
    ClaimType,
    EventType,
    GroundedEvent,
    GroundingIndex,
    Visibility,
)

logger = logging.getLogger("delimit.ai.content_grounding")

# Default paths — overridable via env for testing.
LEDGER_DIR = Path(os.environ.get("DELIMIT_LEDGER_DIR", str(Path.home() / ".delimit" / "ledger")))
ATTESTATIONS_DIR = Path(os.environ.get("DELIMIT_ATTESTATIONS_DIR", str(Path.home() / ".delimit" / "attestations")))
GATEWAY_REPO = Path(os.environ.get("DELIMIT_GATEWAY_REPO", "/home/delimit/delimit-gateway"))
GROUNDING_OUT = Path(os.environ.get("DELIMIT_GROUNDING_OUT", str(Path.home() / ".delimit" / "content" / "grounding")))


# ---------------------------------------------------------------------------
# Ledger ingestion
# ---------------------------------------------------------------------------

# Ledger item_type → grounded EventType. Items with types not in this map
# fall into DECISION as a safe default.
_LEDGER_TYPE_MAP: Dict[str, EventType] = {
    "release": EventType.RELEASE,
    "feature": EventType.FEATURE_SHIPPED,
    "fix": EventType.INCIDENT_RESOLVED,
    "incident": EventType.INCIDENT,
    "audit": EventType.DECISION,
    "strategy": EventType.DECISION,
    "watch": EventType.OUTREACH_EVENT,
    "outreach": EventType.OUTREACH_EVENT,
}


def _ledger_item_to_event(item: Dict[str, Any]) -> Optional[GroundedEvent]:
    """Normalize a ledger JSONL record into a GroundedEvent. Skip on malformed."""
    try:
        led_id = item.get("id") or item.get("ledger_id") or ""
        if not led_id:
            return None
        title = (item.get("title") or "").strip()
        date = item.get("created_at") or item.get("timestamp") or ""
        if not date:
            return None
        venture = (item.get("venture") or "delimit").lower()
        item_type = (item.get("item_type") or item.get("type") or "decision").lower()

        event_type = _LEDGER_TYPE_MAP.get(item_type, EventType.DECISION)

        # A ledger item has at minimum its own LED-id as evidence. Link
        # field also counts if present.
        evidence: List[str] = [f"LED-{led_id.replace('LED-', '')}"]
        link = item.get("link") or ""
        if link and link.startswith("http"):
            evidence.append(f"url:{link}")

        # Build a FEATURE or INCIDENT claim from the title. Claim text
        # is the exact title (no paraphrase permitted by Week 1/2 rules).
        claims: List[Claim] = []
        if title and event_type in (EventType.FEATURE_SHIPPED, EventType.INCIDENT_RESOLVED):
            ctype = ClaimType.FEATURE if event_type == EventType.FEATURE_SHIPPED else ClaimType.INCIDENT
            claims.append(Claim(
                claim_id=f"CLM-{led_id}-title",
                type=ctype,
                text=title,
                evidence_refs=list(evidence),
                visibility=Visibility.INTERNAL,  # default private; author must promote
            ))

        return GroundedEvent(
            event_id=f"evt-ledger-{led_id}",
            type=event_type,
            date=date,
            venture=venture,
            evidence_refs=list(evidence),
            claims=claims,
            visibility=Visibility.INTERNAL,
            source=f"ledger:{item_type}",
            raw={"ledger_id": led_id, "status": item.get("status"), "priority": item.get("priority")},
        )
    except Exception as e:
        logger.debug("skipping malformed ledger item: %s", e)
        return None


def _ingest_ledger(since: Optional[datetime] = None) -> List[GroundedEvent]:
    events: List[GroundedEvent] = []
    if not LEDGER_DIR.is_dir():
        logger.warning("ledger dir not found: %s", LEDGER_DIR)
        return events
    for p in sorted(LEDGER_DIR.glob("*.jsonl")):
        try:
            for line in p.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event = _ledger_item_to_event(item)
                if not event:
                    continue
                if since:
                    try:
                        evt_dt = datetime.fromisoformat(event.date.replace("Z", "+00:00"))
                        if evt_dt < since:
                            continue
                    except ValueError:
                        continue
                events.append(event)
        except Exception as e:
            logger.warning("failed to read %s: %s", p, e)
    return events


# ---------------------------------------------------------------------------
# Attestation ingestion
# ---------------------------------------------------------------------------

def _attestation_to_event(record: Dict[str, Any]) -> Optional[GroundedEvent]:
    try:
        att_id = record.get("id") or ""
        if not att_id.startswith("att_"):
            return None
        bundle = record.get("bundle") or {}
        started = bundle.get("started_at") or bundle.get("completed_at") or ""
        if not started:
            return None
        kind = bundle.get("kind", "merge_attestation")
        event_type = EventType.ATTESTATION
        gates = (bundle.get("governance") or {}).get("gates", [])
        gate_names = ",".join(g.get("name", "?") for g in gates if isinstance(g, dict))
        title = f"{kind}: {bundle.get('wrapped_command', '?')[:60]} | gates: {gate_names or 'none'}"
        evidence: List[str] = [f"attest:{att_id}"]
        before = bundle.get("before_head")
        after = bundle.get("after_head")
        if before and len(before) >= 7:
            evidence.append(f"git:{before[:12]}")
        if after and after != before and len(after) >= 7:
            evidence.append(f"git:{after[:12]}")
        return GroundedEvent(
            event_id=f"evt-att-{att_id}",
            type=event_type,
            date=started,
            venture="delimit",  # attestations are all delimit-venture for now
            evidence_refs=evidence,
            claims=[],  # attestations don't produce direct claim text
            visibility=Visibility.INTERNAL,
            source="attestation",
            raw={
                "attestation_id": att_id,
                "kind": kind,
                "wrapped_exit": bundle.get("wrapped_exit"),
                "signature_alg": record.get("signature_alg"),
            },
        )
    except Exception as e:
        logger.debug("skipping malformed attestation: %s", e)
        return None


def _ingest_attestations(since: Optional[datetime] = None) -> List[GroundedEvent]:
    events: List[GroundedEvent] = []
    if not ATTESTATIONS_DIR.is_dir():
        return events
    for p in sorted(ATTESTATIONS_DIR.glob("att_*.json")):
        try:
            record = json.loads(p.read_text(errors="replace"))
        except Exception:
            continue
        event = _attestation_to_event(record)
        if not event:
            continue
        if since:
            try:
                evt_dt = datetime.fromisoformat(event.date.replace("Z", "+00:00"))
                if evt_dt < since:
                    continue
            except ValueError:
                continue
        events.append(event)
    return events


# ---------------------------------------------------------------------------
# Git log ingestion
# ---------------------------------------------------------------------------

_RELEASE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")


def _ingest_git_commits(since: Optional[datetime] = None, limit: int = 200) -> List[GroundedEvent]:
    """Recent commits on delimit-gateway. `since` cuts by date."""
    events: List[GroundedEvent] = []
    if not (GATEWAY_REPO / ".git").is_dir():
        return events
    after_arg = []
    if since:
        after_arg = [f"--since={since.strftime('%Y-%m-%d')}"]
    try:
        result = subprocess.run(
            [
                "git", "-C", str(GATEWAY_REPO),
                "log", f"--max-count={limit}",
                "--pretty=format:%H%x00%aI%x00%s",
                *after_arg,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.warning("git log failed: %s", result.stderr[:200])
            return events
        for line in result.stdout.splitlines():
            parts = line.split("\x00")
            if len(parts) != 3:
                continue
            sha, iso_date, subject = parts
            events.append(GroundedEvent(
                event_id=f"evt-git-{sha[:12]}",
                type=EventType.COMMIT,
                date=iso_date,
                venture="delimit",
                evidence_refs=[f"git:{sha[:12]}"],
                claims=[],  # commit subject is NOT a claim — subjects paraphrase
                visibility=Visibility.INTERNAL,
                source="git-log",
                raw={"subject": subject[:200], "sha": sha},
            ))
    except Exception as e:
        logger.warning("git log exception: %s", e)
    return events


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_grounding_index(
    venture: str = "delimit",
    days: int = 30,
    whitelist: Optional[frozenset] = None,
) -> GroundingIndex:
    """Build a fresh grounding index over the last `days`.

    Week 1: ingest + normalize + validate. No publishing, no generation.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    events: List[GroundedEvent] = []
    events.extend(_ingest_ledger(since=since))
    events.extend(_ingest_attestations(since=since))
    events.extend(_ingest_git_commits(since=since))

    # Filter to the requested venture. Attestations + git commits are
    # `delimit`-venture by construction; ledger items carry their own.
    events = [e for e in events if e.venture == venture]

    index = GroundingIndex(
        venture=venture,
        built_at=datetime.now(timezone.utc).isoformat(),
        events=sorted(events, key=lambda e: e.date, reverse=True),
    )
    # Validation is best-effort at build time — errors get logged but
    # do not block index construction. Caller can call `validate_claims`
    # for a strict pass.
    errs = index.validate(whitelist=whitelist)
    if errs:
        logger.info(
            "build_grounding_index: %d validation warnings (first 5): %s",
            len(errs), errs[:5],
        )
    return index


def load_grounded_events(
    venture: str = "delimit",
    days: int = 30,
    visibility: Optional[Visibility] = None,
    event_type: Optional[EventType] = None,
    whitelist: Optional[frozenset] = None,
) -> List[GroundedEvent]:
    """Filtered view. Generators use this — not `build_grounding_index`."""
    idx = build_grounding_index(venture=venture, days=days, whitelist=whitelist)
    events = idx.events
    if visibility is not None:
        events = [e for e in events if e.visibility == visibility]
    if event_type is not None:
        events = [e for e in events if e.type == event_type]
    return events


def validate_claims(
    claims: List[Claim],
    whitelist: Optional[frozenset] = None,
) -> List[Dict[str, Any]]:
    """Strict per-claim validation. Returns a list of {claim_id, errors}.

    Used as the gate in front of any generator output (A9). Callers
    MUST fail-closed on any non-empty errors.
    """
    out: List[Dict[str, Any]] = []
    wl = whitelist or frozenset()
    for claim in claims:
        errs = claim.validate(whitelist=wl)
        out.append({"claim_id": claim.claim_id, "errors": errs, "valid": not errs})
    return out


def persist_grounding_index(index: GroundingIndex, out_dir: Path = GROUNDING_OUT) -> Path:
    """Write the index as events.jsonl for consumption. Week 1 artifact."""
    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / f"events-{index.venture}.jsonl"
    with open(events_path, "w") as f:
        for event in index.events:
            f.write(json.dumps(event.to_dict()) + "\n")
    meta = {
        "venture": index.venture,
        "built_at": index.built_at,
        "event_count": len(index.events),
        "canon_version": index.canon_version,
    }
    (out_dir / f"meta-{index.venture}.json").write_text(json.dumps(meta, indent=2))
    return events_path
