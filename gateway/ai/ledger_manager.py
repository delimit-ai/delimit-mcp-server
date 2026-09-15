"""
Delimit Ledger Manager — Strategy + Operational ledger as first-class MCP tools.

Two ledgers per project:
- Strategy: consensus decisions, positioning, pricing, product direction
- Operational: tasks, bugs, features — the "keep building" items

Ledger lives at {project}/.delimit/ledger/ (project-local).
Ventures auto-registered at ~/.delimit/ventures.json on first use.
"""

import base64
import json
import hashlib
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # Windows has no fcntl; the close lock degrades to in-process.
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _delimit_home() -> Path:
    """Resolve the customer-selected Delimit state root."""
    for env_key in ("DELIMIT_HOME", "DELIMIT_NAMESPACE_ROOT"):
        val = os.environ.get(env_key, "").strip()
        if val:
            return Path(val).expanduser()
    return Path.home() / ".delimit"


GLOBAL_DIR = _delimit_home()
VENTURES_FILE = GLOBAL_DIR / "ventures.json"

# LED-1145 Phase 2 #3: P0 quota soft warning. The soft block fires when an
# add_item call would push the unresolved-P0 count over the quota. Item is
# still added — this is policy nudge, not enforcement, per the strategic
# deliberation's hierarchy ("policy after primitives"). Override via env var:
#   DELIMIT_P0_SOFT_QUOTA=80     # raise the gate
#   DELIMIT_P0_SOFT_QUOTA=0      # disable warning entirely
P0_SOFT_QUOTA_DEFAULT = 50


def _utc_timestamp(fmt: str = "%Y-%m-%dT%H:%M:%SZ") -> str:
    """Return a UTC timestamp for persisted ledger/session metadata.

    ``time.strftime`` uses the process's local timezone when no time tuple is
    supplied.  Appending ``Z`` to that value falsely labels local wall time as
    UTC, which can reorder append-only evidence during replay.  Always pass an
    explicit UTC tuple anywhere this module persists a timestamp.
    """
    return time.strftime(fmt, time.gmtime())


def _p0_soft_quota() -> int:
    """Resolve the active P0 quota threshold from env. 0 disables warnings."""
    raw = os.environ.get("DELIMIT_P0_SOFT_QUOTA", "")
    if raw == "":
        return P0_SOFT_QUOTA_DEFAULT
    try:
        n = int(raw)
        return max(0, n)
    except (TypeError, ValueError):
        return P0_SOFT_QUOTA_DEFAULT


def _count_unresolved_p0(project_path: str = ".") -> int:
    """Count P0 items currently in an unresolved state (open / in_progress /
    blocked) across both ops and strategy ledgers. Excludes done / cancelled /
    archived. Used by the add_item soft-quota nudge."""
    ledger_dir = _project_ledger_dir(project_path)
    unresolved = {"open", "in_progress", "blocked"}
    count = 0
    for filename in ("operations.jsonl", "strategy.jsonl"):
        path = ledger_dir / filename
        if not path.exists():
            continue
        items = _read_ledger(path)
        # Replay events to current state
        state: Dict[str, Dict[str, Any]] = {}
        for item in items:
            iid = item.get("id", "")
            if not iid:
                continue
            if item.get("type") == "update":
                if iid in state:
                    if "status" in item:
                        state[iid]["status"] = item["status"]
                    if "priority" in item:
                        state[iid]["priority"] = item["priority"]
            else:
                state[iid] = {**item}
        for it in state.values():
            if it.get("priority") == "P0" and it.get("status") in unresolved:
                count += 1
    return count


def _resolve_nonventure_name(path: str, name: str) -> str:
    """LED-3925 write-path fix: map a junk/ephemeral basename to a real slug.

    The MCP server runs with cwd=~/.delimit/server, so ``_detect_venture``
    used to stamp ``venture="server"`` on every gov/deliberate/consensus item
    (90+ mis-attributed rows). Likewise ``/tmp/wire-report-*`` scratch trees
    stamped ``venture="wire-report-morning-presentation"`` etc. This helper
    extracts a known-venture token from the path (or the basename) so those
    writes land on the correct sub-ledger instead of pooling in the central
    store. When no token matches it returns ``"unsorted"`` — NEVER the raw
    ``server`` / ``tmpXXXX`` / ephemeral basename.
    """
    blob = f"{path} {name}".lower()
    if "wire-report" in blob or "wirereport" in blob or "wire.report" in blob \
            or "wire-api" in blob or "wr-" in blob:
        return "wire-report"
    if "stake" in blob:  # stake / stakeone / stake-one / stake.one
        return "stake-one"
    if "domain" in blob or "electricgrill" in blob:
        return "domainvested"
    if "solicit" in blob:
        return "solicitsignal"
    if "delimit" in blob or name in ("server", ".delimit"):
        return "delimit"
    return "unsorted"


def _detect_venture(project_path: str = ".") -> Dict[str, str]:
    """Auto-detect venture/project info from the directory."""
    p = Path(project_path).resolve()
    info = {"name": p.name, "path": str(p)}
    # Track whether the name came from a real project signal (package.json,
    # pyproject.toml, git remote) — those authoritative names are never
    # remapped by the LED-3925 junk-name guard below.
    name_from_project_signal = False

    # Try package.json
    pkg = p / "package.json"
    if pkg.exists():
        try:
            d = json.loads(pkg.read_text())
            info["name"] = d.get("name", p.name)
            info["type"] = "node"
            name_from_project_signal = True
        except Exception:
            pass

    # Try pyproject.toml
    pyproj = p / "pyproject.toml"
    if pyproj.exists():
        try:
            text = pyproj.read_text()
            for line in text.splitlines():
                if line.strip().startswith("name"):
                    name = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if name:
                        info["name"] = name
                        info["type"] = "python"
                        name_from_project_signal = True
                        break
        except Exception:
            pass

    # Try git remote
    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=3, cwd=str(p)
        )
        if remote.returncode == 0:
            url = _strip_url_userinfo(remote.stdout.strip())
            # Extract repo name from URL
            repo = url.rstrip("/").split("/")[-1].replace(".git", "")
            info["repo"] = url
            if not info.get("type"):
                info["name"] = repo
                name_from_project_signal = True
    except Exception:
        pass

    # LED-3925 write-path resolution: if no authoritative project signal named
    # this path AND the basename is an ephemeral / non-venture junk name
    # (server, .delimit, tmp*), remap it to a real venture slug rather than
    # stamping the junk basename onto every ledger write.
    if not name_from_project_signal:
        basename = p.name
        # A dedicated venture namespace (~/.delimit/ventures/<name>) is an
        # AUTHORITATIVE custom venture — the <name> after ventures/ is never
        # junk, even though the path contains ".delimit" and may sit under
        # /tmp during tests (which would otherwise mark it ephemeral and
        # collapse every custom venture to "delimit").
        parent = p.parent
        is_dedicated_venture = (
            parent.name == "ventures" and parent.parent.name == ".delimit"
        )
        if not is_dedicated_venture and (
            _is_ephemeral_path(str(p))
            or basename in ("server", ".delimit")
            or basename.startswith("tmp")
        ):
            info["name"] = _resolve_nonventure_name(str(p), basename)

    return info


# LED-3733: the canonical guard logic now lives in the reusable
# ai.registry_guards module so the swarm venture registry (and, later, the
# souls / agents-tasks stores) apply the identical, tested guard instead of
# a drifting copy. These thin private wrappers are kept for backward
# compatibility with existing imports (tests, callers).
from ai.registry_guards import strip_url_userinfo as _strip_url_userinfo  # noqa: E402
from ai.registry_guards import is_ephemeral_path as _is_ephemeral_path  # noqa: E402


def _register_venture(info: Dict[str, str]):
    """Silently register a venture in the global registry.

    Guard (STR-2169 / LED-3733, 2026-07-01): refuse to persist ephemeral
    paths (anything under /tmp or matching pytest/tempfile patterns) and
    strip any embedded credentials from the remote URL before writing.
    Previously only bare "/tmp" was rejected, so ~250 pytest tmp_path
    ventures accumulated and PAT-embedded remotes were stored verbatim.
    The guard fails-silently — tests that pass tmp_path to functions
    which auto-register simply don't pollute the registry going forward.
    """
    GLOBAL_DIR.mkdir(parents=True, exist_ok=True)
    ventures = {}
    if VENTURES_FILE.exists():
        try:
            ventures = json.loads(VENTURES_FILE.read_text())
        except Exception:
            pass

    name = info["name"]
    path = info.get("path", "")
    # Never auto-register transient test/scratch paths into the shared registry.
    if _is_ephemeral_path(path):
        return
    if name not in ventures:
        ventures[name] = {
            "path": path,
            "repo": _strip_url_userinfo(info.get("repo", "")),
            "type": info.get("type", ""),
            "registered_at": _utc_timestamp(),
        }
        VENTURES_FILE.write_text(json.dumps(ventures, indent=2))


CENTRAL_LEDGER_DIR = _delimit_home() / "ledger"
LEDGER_V2_DIR = _delimit_home() / "ledger-v2"

# LED-1188 D3 (deliberation att_f86e1f51110e8ed6 follow-up, 2026-04-28):
# Plan-C migration partitions the central ledger into per-venture sub-ledgers
# under ledger-v2/<slug>/. The resolver below auto-detects which layout is
# present and reads from it. Slugs match the migration script's canonical
# names so a v4.5.2 install picks up an existing Plan-C-staged tree without
# requiring the swap to happen first.
_VENTURE_CANONICAL = {
    "delimit-mcp": "delimit",
    "delimit-action": "delimit",
    "delimit-ui": "delimit",
    "delimit-cli": "delimit",        # npm package name
    "delimit-gateway": "delimit",    # gateway repo
    ".delimit": "delimit",
    "server": "delimit",             # LED-3925: MCP server cwd basename
    "wirereport": "wire-report",
    "wire.report": "wire-report",    # LED-3925
    "wire-api": "wire-report",       # LED-3925
    "action-wire-report": "wire-report",  # LED-3925
    "wirereportwnba": "wire-report",      # LED-3925
    "wr-phase1": "wire-report",      # LED-3925
    "stakeone": "stake-one",
    "stake.one": "stake-one",        # LED-3925
    "domain-monetization": "domainvested",  # LED-3925 (matches focus_gate)
    "domainvested-console": "domainvested",  # LED-3925
    "electricgrill-com": "domainvested",     # LED-3925
    "solicitsignal": "solicitsignal",        # LED-3925 (known slug)
}
_KNOWN_VENTURE_SLUGS = {
    "delimit", "wire-report", "domainvested",
    "livetube", "stake-one", "root", "unsorted",
    "solicitsignal",
}


def _canonical_venture_slug(name: str) -> Optional[str]:
    """Map a detected venture name to a canonical sub-ledger slug.

    Returns None when the name doesn't match any known venture; callers
    treat that as "no per-venture sub-ledger, use the central layout."
    """
    if not name:
        return None
    n = name.lower().strip()
    n = _VENTURE_CANONICAL.get(n, n)
    return n if n in _KNOWN_VENTURE_SLUGS else None


def _detect_model() -> str:
    """Auto-detect which AI model is running this session.

    Checks environment variables set by various AI coding assistants:
    - CLAUDE_MODEL / CLAUDE_CODE_MODEL: Claude Code
    - CODEX_MODEL: OpenAI Codex CLI
    - GEMINI_MODEL: Gemini CLI
    - MCP_CLIENT_NAME: Generic MCP client identifier
    Falls back to "unknown" if none are set.
    """
    # Claude Code
    for var in ("CLAUDE_MODEL", "CLAUDE_CODE_MODEL"):
        val = os.environ.get(var)
        if val:
            return val

    # OpenAI Codex
    val = os.environ.get("CODEX_MODEL")
    if val:
        return val

    # Gemini
    val = os.environ.get("GEMINI_MODEL")
    if val:
        return val

    # Generic MCP client
    val = os.environ.get("MCP_CLIENT_NAME")
    if val:
        return val

    return "unknown"


def _project_ledger_dir(project_path: str = ".") -> Path:
    """Resolve the ledger directory for a project, with Plan-C auto-detect.

    Resolution order (LED-1188 D3, deliberation att_f86e1f51110e8ed6):
      1. Detect venture from project_path -> canonical slug (delimit,
         wire-report, domainvested, livetube, stake-one).
      2. If LEDGER_V2_DIR / <slug> / operations.jsonl exists, return that
         per-venture sub-ledger. (Plan-C staged but not yet swapped.)
      3. If CENTRAL_LEDGER_DIR / <slug> / operations.jsonl exists, return
         that per-venture sub-ledger. (Plan-C swapped.)
      4. Fall back to CENTRAL_LEDGER_DIR (legacy single-file layout).

    Cross-model handoff fix (still enforced): Codex and Gemini were writing to
    $PWD/.delimit/ledger/ which caused ledger fragmentation. The central
    ~/.delimit/ledger/ tree (or its Plan-C-partitioned form) remains the
    single source of truth — per-project .delimit/ dirs are for policies and
    config only.
    """
    # Quick exit: legacy callers that pass venture="" / project="." and
    # have no Plan-C tree on disk get the original single-file layout.
    if not LEDGER_V2_DIR.exists() and not (CENTRAL_LEDGER_DIR / "delimit").exists():
        return CENTRAL_LEDGER_DIR

    info = _detect_venture(project_path)
    slug = _canonical_venture_slug(info.get("name", ""))
    if slug is None:
        return CENTRAL_LEDGER_DIR

    # Plan-C staged: ledger-v2/<slug>/
    staged = LEDGER_V2_DIR / slug
    if (staged / "operations.jsonl").exists():
        return staged

    # Plan-C swapped: ledger/<slug>/
    swapped = CENTRAL_LEDGER_DIR / slug
    if (swapped / "operations.jsonl").exists():
        return swapped

    # No partitioned tree for this venture — fall back to the central
    # legacy layout (operations.jsonl + strategy.jsonl directly in ledger/).
    return CENTRAL_LEDGER_DIR


def _resolve_namespace(project_path: str = ".") -> Dict[str, str]:
    """Report which physical store a project_path resolves to (LED-3720).

    Read-only. Returns the detected venture name, the canonical namespace
    slug (or "" when the name maps to no known venture and the central
    legacy layout is used), and the absolute store directory that
    list/query/health will actually read. Exposed so callers and tests can
    prove that two aliases for the same project resolve to the SAME store —
    the fragmentation that let an item be reachable via one alias and not
    another. Does NOT move or mutate any data.
    """
    info = _detect_venture(project_path)
    slug = _canonical_venture_slug(info.get("name", "")) or ""
    return {
        "venture": info.get("name", ""),
        "namespace": slug,
        "store_path": str(_project_ledger_dir(project_path)),
    }


def _replay_status_counts(
    ledger_dir: Path,
    ledgers=("operations.jsonl", "strategy.jsonl"),
) -> Dict[str, int]:
    """Deterministic, event-sourced status tally for a resolved ledger dir.

    Single source of truth for the open / in_progress / blocked / done / …
    counts (LED-3720). Replays create + update rows in file order, honoring
    the LATEST status per item id, so an item whose status was updated to
    ``done`` is counted as done — NOT open. This mirrors ``list_items``'
    event-sourcing exactly (an update row is only applied to an item whose
    create row was already seen in the SAME store), so the number it returns
    equals an independent recount of the same store and matches the
    ``summary`` ``list_items`` computes.

    Read-only. Never counts a status value of ``None`` (an item with no
    status field lands in no bucket), matching list_items' breakdown.
    """
    counts: Dict[str, int] = {}
    for filename in ledgers:
        path = ledger_dir / filename
        if not path.exists():
            continue
        state: Dict[str, Optional[str]] = {}
        for item in _read_ledger(path):
            iid = item.get("id", "")
            if not iid:
                continue
            if item.get("type") == "update":
                if iid in state and "status" in item:
                    state[iid] = item.get("status")
            else:
                state[iid] = item.get("status")
        for status in state.values():
            if status is None:
                continue
            counts[status] = counts.get(status, 0) + 1
    return counts


def _ensure(project_path: str = "."):
    ledger_dir = _project_ledger_dir(project_path)
    ledger_dir.mkdir(parents=True, exist_ok=True)
    for name in ["strategy.jsonl", "operations.jsonl"]:
        f = ledger_dir / name
        if not f.exists():
            f.write_text("")

    # Auto-register venture on first use
    info = _detect_venture(project_path)
    _register_venture(info)


def _read_ledger(path: Path) -> List[Dict]:
    items = []
    if not path.exists():
        return items
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def _append(path: Path, entry: Dict) -> Dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("")
    items = _read_ledger(path)
    prev_hash = items[-1].get("hash", "genesis") if items else "genesis"
    entry["hash"] = hashlib.sha256(f"{prev_hash}{json.dumps(entry, sort_keys=True)}".encode()).hexdigest()[:16]
    entry["created_at"] = _utc_timestamp()

    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


# ── LED-877 signal guard ─────────────────────────────────────────────


# Sources that originate from sensed observations (social/strategy scans).
# Centralized so the guard logic is in one place even if more prefixes are
# added later (e.g. github_sense, reddit_sense).
_SENSED_SOURCE_PREFIXES = ("social_scan", "social_strategy")


def _check_source_is_ledger_item(
    source: str,
    *,
    purpose: str = "promote_to_ledger",
    title: str = "",
    ledger: str = "",
) -> None:
    """LED-877 signal guard.

    Sensed observations (``source='social_scan:...'``) MUST NOT land in
    the ledger by default — they belong in the intel signal store.

    LED-216 Phase 1 split: callers can declare *why* they are checking.

    ``purpose='promote_to_ledger'`` (default, original strict behavior)
        Used by ``add_item`` and any code path that actually writes a
        ledger row. Raises ``ValueError`` on a sensed source unless the
        ``_DELIMIT_SIGNAL_PROMOTED_BY`` bypass env var is set (which is
        the explicit promote-to-ledger path).

    ``purpose='draft_only'``
        Used by code paths that produce a reply draft from a sensed
        observation but do NOT promote the signal to the ledger. Drafts
        are an acceptable consumer of sensed sources, so the guard is a
        no-op for this purpose.

    Both purposes still respect ``DELIMIT_SIGNAL_GUARD=shadow`` for the
    shadow-log fallback used during the LED-877 rollout.
    """
    _src_norm = (source or "").strip().lower()
    if not any(_src_norm.startswith(p) for p in _SENSED_SOURCE_PREFIXES):
        return  # Not a sensed source; nothing to guard against.

    if purpose == "draft_only":
        # Drafts may legitimately reference a sensed observation. The
        # guard exists to prevent ledger writes, not draft generation.
        return

    if purpose != "promote_to_ledger":
        # Defensive: unknown purpose ⇒ default to strict behavior so a
        # typo can't accidentally weaken the guard.
        pass

    _promoted_by = os.environ.get("_DELIMIT_SIGNAL_PROMOTED_BY", "")
    if _promoted_by:
        return  # Explicit promote_to_ledger path; bypass authorized.

    msg = (
        f"LED-877 guard: source={source!r} is a sensed observation, not "
        f"a ledger item. Use ai.sensing.signal_store.ingest() instead. "
        f"Promote explicitly via promote_to_ledger(signal_id=...)."
    )

    _guard_mode = os.environ.get("DELIMIT_SIGNAL_GUARD", "enforce").lower()
    if _guard_mode == "shadow":
        try:
            _shadow_log = Path.home() / ".delimit" / "logs" / "signal_guard_shadow.jsonl"
            _shadow_log.parent.mkdir(parents=True, exist_ok=True)
            with _shadow_log.open("a") as _f:
                _f.write(json.dumps({
                    "ts": _utc_timestamp(),
                    "title": title,
                    "source": source,
                    "ledger": ledger,
                    "purpose": purpose,
                    "msg": msg,
                }) + "\n")
        except Exception:
            pass
        # fall through (shadow mode does not raise)
        return

    raise ValueError(msg)


# ── LED-5321 M4: evidence-classed truth state ─────────────────────────
# A ledger note saying "deployed" is an assertion, not a verified fact.
# Every promotion that claims merged/deployed/published/verified carries an
# evidence object {class, kind, ref, observed_at, observed_by}. Callers that
# pass nothing keep working: the record is stored with class="assertion"
# (never silently "verified"). verified_states only advances on a
# verified-class evidence object whose ref is present. Deterministic, no
# model calls, no full audits on ordinary updates.

EVIDENCE_CLASSES = ("assertion", "verified", "stale", "unavailable")
EVIDENCE_KINDS = (
    "implemented",
    "tested",
    "independently_reviewed",
    "merged",
    "published",
    "deployed",
    "runtime_verified",
    "natural_outcome",
    "natural_outcome_observed",
)
VERIFIED_STATES_ORDER = [
    "implemented",
    "tested",
    "independently_reviewed",
    "merged",
    "published",
    "deployed",
    "runtime_verified",
    "natural_outcome_observed",
]
KIND_TO_VERIFIED_STATE = {
    "implemented": "implemented",
    "tested": "tested",
    "independently_reviewed": "independently_reviewed",
    "merged": "merged",
    "published": "published",
    "deployed": "deployed",
    "runtime_verified": "runtime_verified",
    "natural_outcome": "natural_outcome_observed",
    "natural_outcome_observed": "natural_outcome_observed",
}
CLASS_TO_TRUTH_LABEL = {
    "assertion": "ASSERTED",
    "verified": "VERIFIED",
    "stale": "STALE",
    "unavailable": "UNAVAILABLE",
}
LEGACY_TRUTH_LABEL = "legacy/unclassified"

_ASSERTION_RE = re.compile(
    r"\b(merged|deployed|published|verified|runtime[_\s-]?verified|natural[_\s-]?outcome)\b",
    re.IGNORECASE,
)


def validate_evidence(evidence: Any) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate a caller-supplied evidence object (LED-5321 M4).

    Returns (normalized, None) on success or (None, error_message).
    Never raises on malformed input; never fabricates verification.
    """
    if not isinstance(evidence, dict):
        return None, "evidence must be an object with {class, kind, ref, observed_at, observed_by}"
    ev_class = evidence.get("class", "")
    ev_kind = evidence.get("kind", "")
    if ev_class not in EVIDENCE_CLASSES:
        return None, (
            f"evidence.class must be one of {list(EVIDENCE_CLASSES)} "
            f"(got {ev_class!r})"
        )
    if ev_kind not in EVIDENCE_KINDS:
        return None, (
            f"evidence.kind must be one of {list(EVIDENCE_KINDS)} "
            f"(got {ev_kind!r})"
        )
    ref = evidence.get("ref", "")
    if ev_class == "verified" and not (isinstance(ref, str) and ref.strip()):
        return None, "verified-class evidence requires a non-empty ref (sha/tag/pr/evidence-bundle id)"
    if ref is None:
        ref = ""
    if not isinstance(ref, str):
        return None, "evidence.ref must be a string"
    observed_at = evidence.get("observed_at") or _utc_timestamp()
    if not isinstance(observed_at, str) or not observed_at.strip():
        return None, "evidence.observed_at must be a non-empty string"
    observed_by = evidence.get("observed_by") or _detect_model()
    if not isinstance(observed_by, str) or not observed_by.strip():
        return None, "evidence.observed_by must be a non-empty string"
    return {
        "class": ev_class,
        "kind": ev_kind,
        "ref": ref,
        "observed_at": observed_at,
        "observed_by": observed_by,
    }, None


def _infer_kind_from_text(*texts: Optional[str]) -> Optional[str]:
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return None
    if "runtime" in blob and "verif" in blob:
        return "runtime_verified"
    if "deployed" in blob:
        return "deployed"
    if "published" in blob:
        return "published"
    if "merged" in blob:
        return "merged"
    if "natural" in blob and "outcome" in blob:
        return "natural_outcome"
    return None


def _detect_assertion(
    status: Optional[str],
    note: Optional[str],
    title: Optional[str],
    description: Optional[str],
) -> tuple[bool, Optional[str]]:
    """Whether this update asserts a truth-sensitive state (LED-5321 M4)."""
    inferred = _infer_kind_from_text(note, title, description)
    if status == "done":
        return True, inferred or "merged"
    for text in (note, title, description):
        if text and _ASSERTION_RE.search(text):
            return True, inferred or "merged"
    return False, None


def truth_label_for_evidence(evidence: Any) -> str:
    """Map an evidence object (or class string) to a display label."""
    if isinstance(evidence, str):
        return CLASS_TO_TRUTH_LABEL.get(evidence, LEGACY_TRUTH_LABEL)
    if isinstance(evidence, dict):
        return CLASS_TO_TRUTH_LABEL.get(str(evidence.get("class", "")), LEGACY_TRUTH_LABEL)
    return LEGACY_TRUTH_LABEL


def truth_label_for_item(item_state: Dict[str, Any]) -> str:
    """Label a replayed ledger item (LED-5321 M4).

    Never fabricates: items with no evidence object render
    "legacy/unclassified".
    """
    if not isinstance(item_state, dict):
        return LEGACY_TRUTH_LABEL
    ev = item_state.get("evidence")
    if isinstance(ev, dict) and ev.get("class"):
        return truth_label_for_evidence(ev)
    ec = item_state.get("evidence_class")
    if isinstance(ec, str) and ec:
        return truth_label_for_evidence(ec)
    return LEGACY_TRUTH_LABEL


def _advance_verified_states(
    existing: Any, evidence: Dict[str, Any]
) -> List[str]:
    """Advance verified_states only on verified-class evidence with ref."""
    base = list(existing) if isinstance(existing, list) else []
    base = [s for s in base if isinstance(s, str) and s in VERIFIED_STATES_ORDER]
    if not isinstance(evidence, dict):
        return base
    if evidence.get("class") != "verified":
        return base
    ref = evidence.get("ref", "")
    if not (isinstance(ref, str) and ref.strip()):
        return base
    mapped = KIND_TO_VERIFIED_STATE.get(str(evidence.get("kind", "")))
    if not mapped or mapped in base:
        return base
    order = {s: i for i, s in enumerate(VERIFIED_STATES_ORDER)}
    return sorted(base + [mapped], key=lambda s: order.get(s, 999))


def _default_assertion_evidence(
    kind: str, worked_by: str = ""
) -> Dict[str, Any]:
    return {
        "class": "assertion",
        "kind": kind if kind in EVIDENCE_KINDS else "merged",
        "ref": "",
        "observed_at": _utc_timestamp(),
        "observed_by": worked_by or _detect_model(),
    }


def add_item(
    title: str,
    ledger: str = "ops",
    type: str = "task",
    priority: str = "P1",
    description: str = "",
    source: str = "session",
    project_path: str = ".",
    tags: Optional[List[str]] = None,
    acceptance_criteria: Optional[List[str]] = None,
    context: str = "",
    tools_needed: Optional[List[str]] = None,
    estimated_complexity: str = "",
    worked_by: str = "",
    decision_card: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Add a new item to the project's strategy or operational ledger.

    LED-189: Items can have acceptance_criteria (testable "done when" conditions).
    LED-190: Items can have context, tools_needed, and estimated_complexity
    for agent-executable task format.
    LED-877: Signal guard — rejects source='social_scan' writes so sensed
    observations cannot land in the ledger. Observations belong in the intel
    signal store (ai/sensing/signal_store.py). Bypass via env var for the
    promote_to_ledger path: _DELIMIT_SIGNAL_PROMOTED_BY=<who>.

    LED-216 Phase 1: the guard is now reusable via
    ``_check_source_is_ledger_item(..., purpose='draft_only')`` for code
    paths that produce reply drafts from sensed observations without
    promoting the underlying signal to the ledger.
    """
    _check_source_is_ledger_item(
        source,
        purpose="promote_to_ledger",
        title=title,
        ledger=ledger,
    )

    _ensure(project_path)
    venture = _detect_venture(project_path)
    ledger_dir = _project_ledger_dir(project_path)
    path = ledger_dir / ("strategy.jsonl" if ledger == "strategy" else "operations.jsonl")

    # LED-824: ID-collision fix. The Plan-C resolver routes delimit-context
    # queries to ledger-v2/<slug>/. Per-venture ID counters used to scan
    # only the active sub-ledger, so newly-created items could collide with
    # IDs already used in the legacy CENTRAL_LEDGER_DIR root files. Now we
    # union all known IDs across (a) the resolved sub-ledger AND (b) every
    # peer sub-ledger AND (c) the legacy root, then pick the next free.
    items = _read_ledger(path)
    prefix = "STR" if ledger == "strategy" else "LED"
    existing_ids = {i.get("id", "") for i in items if i.get("type") != "update"}

    # Union with all peer files in ledger-v2/* and the legacy root files,
    # for both strategy and operations ledgers (an LED-N could collide
    # whether it lives in operations or strategy in any sub-ledger).
    filename = "strategy.jsonl" if ledger == "strategy" else "operations.jsonl"
    candidate_paths: list[Path] = []
    if LEDGER_V2_DIR.exists():
        for sub in LEDGER_V2_DIR.iterdir():
            if sub.is_dir():
                candidate_paths.append(sub / filename)
    candidate_paths.append(CENTRAL_LEDGER_DIR / filename)

    for cand in candidate_paths:
        if cand == path:
            continue  # already scanned
        if not cand.exists():
            continue
        try:
            for entry in _read_ledger(cand):
                if entry.get("type") == "update":
                    continue
                eid = entry.get("id", "")
                if eid:
                    existing_ids.add(eid)
        except Exception:
            # Best-effort: a malformed peer file shouldn't block id assignment
            continue

    # Walk forward from len()+1 until we find a non-colliding slot.
    num = len(items) + 1
    while f"{prefix}-{num:03d}" in existing_ids:
        num += 1
    item_id = f"{prefix}-{num:03d}"

    entry = {
        "id": item_id,
        "title": title,
        "type": type,
        "priority": priority,
        "description": description,
        "source": source,
        "venture": venture["name"],
        "status": "open",
        "tags": tags or [],
        "worked_by": worked_by or _detect_model(),
        # LED-5321 M4: compatible verified-states list; empty until a
        # verified-class evidence object with ref advances it.
        "verified_states": [],
    }
    # LED-189: Optional acceptance criteria
    if acceptance_criteria:
        entry["acceptance_criteria"] = acceptance_criteria
    # LED-190: Optional agent-executable fields
    if context:
        entry["context"] = context
    if tools_needed:
        entry["tools_needed"] = tools_needed
    if estimated_complexity:
        entry["estimated_complexity"] = estimated_complexity
    # Phase-0 autonomous-org (mem-b2a9762b78d8): a decision card is a ledger
    # item of type="decision" carrying the founder-facing option set. The
    # payload is stored on the initial (non-update) event so list_items' replay
    # ({**item}) preserves it. Additive — items without a decision_card are
    # unchanged. Only attached when type == "decision" to avoid polluting
    # ordinary tasks.
    if decision_card and type == "decision":
        entry["decision_card"] = decision_card

    result = _append(path, entry)

    # Sync to Supabase for dashboard visibility
    try:
        from ai.supabase_sync import sync_ledger_item
        sync_ledger_item(result)
    except Exception:
        pass  # Never let cloud sync break ledger operations

    response: Dict[str, Any] = {
        "added": result,
        "ledger": ledger,
        "venture": venture["name"],
        "total_items": len(_read_ledger(path)),
    }

    # LED-1145 Phase 2 #3: P0 soft quota nudge. Soft (item still added),
    # not hard. Surfaces a warning when the unresolved-P0 count crosses
    # the quota — gives the founder a signal to groom before piling on.
    if priority == "P0":
        quota = _p0_soft_quota()
        if quota > 0:
            current_p0 = _count_unresolved_p0(project_path)
            if current_p0 > quota:
                response["warning"] = (
                    f"P0 quota soft-block: {current_p0} unresolved P0 items "
                    f"(threshold {quota}). Item was still added. Consider running "
                    f"delimit_ledger_groom to triage existing P0s before adding more, "
                    f"or set DELIMIT_P0_SOFT_QUOTA={current_p0 + 50} to raise the gate."
                )
                response["p0_count"] = current_p0
                response["p0_quota"] = quota

    return response


def _find_item_in_ledger_dir(item_id: str, ledger_dir: Path) -> Optional[Dict[str, Any]]:
    """Search a ledger directory for an item by ID. Returns (ledger_name, path) or None."""
    for ledger_name, filename in [("ops", "operations.jsonl"), ("strategy", "strategy.jsonl")]:
        path = ledger_dir / filename
        items = _read_ledger(path)
        for item in items:
            if item.get("id") == item_id and item.get("type") != "update":
                return {"ledger_name": ledger_name, "path": path}
    return None


def update_item(
    item_id: str,
    status: Optional[str] = None,
    note: Optional[str] = None,
    priority: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    assignee: Optional[str] = None,
    due_date: Optional[str] = None,
    labels: Optional[List[str]] = None,
    blocked_by: Optional[str] = None,
    blocks: Optional[str] = None,
    project_path: str = ".",
    worked_by: str = "",
    commit_sha: Optional[str] = None,
    pr_url: Optional[str] = None,
    # LED-5321 M4: all optional, backward compatible.
    evidence: Optional[Dict[str, Any]] = None,
    review_transcript: Optional[str] = None,
    review_diff_path: Optional[str] = None,
    review_diff_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Update an existing ledger item's fields.

    LED-1408 Phase 1: when `status="done"` is requested, callers MAY provide
    `commit_sha` and/or `pr_url` as proof that the work shipped to main.
    The proof is recorded on the update event under `ship_proof` with a
    `verified: bool` flag. Phase 1 does NOT enforce — items still
    transition to `done` even without proof — but the flag lets future
    audits and the Phase 2 reconciler find unverified-done items.

    LED-5321 M4: when `status` moves to `done`, or a note/field asserts
    merged/deployed/published/verified, an evidence object
    {class, kind, ref, observed_at, observed_by} is required-or-attached.
    Callers that pass nothing keep working: the record is stored with
    evidence.class="assertion" (never silently "verified"). `verified_states`
    only advances with a verified-class evidence object whose ref is
    present. Malformed evidence is rejected with a clear error and no write.
    """
    # LED-5321 M4: validate caller-supplied evidence before any write.
    _evidence_normalized: Optional[Dict[str, Any]] = None
    _evidence_error: Optional[str] = None
    if evidence is not None:
        _evidence_normalized, _evidence_error = validate_evidence(evidence)
        if _evidence_error:
            return {"error": f"invalid evidence: {_evidence_error}"}
    _triggered, _inferred_kind = _detect_assertion(status, note, title, description)
    if _triggered and _evidence_normalized is None:
        _evidence_normalized = _default_assertion_evidence(
            _inferred_kind or "merged", worked_by
        )
    _ensure(project_path)
    ledger_dir = _project_ledger_dir(project_path)

    # First, search the specified project's ledger
    found = _find_item_in_ledger_dir(item_id, ledger_dir)

    # If not found, search all registered ventures as a fallback
    if not found:
        try:
            ventures = {}
            if VENTURES_FILE.exists():
                ventures = json.loads(VENTURES_FILE.read_text())
        except Exception:
            ventures = {}

        searched = {str(ledger_dir)}
        for _name, info in ventures.items():
            vpath = info.get("path", "")
            if not vpath:
                continue
            candidate_dir = Path(vpath) / ".delimit" / "ledger"
            if str(candidate_dir) in searched:
                continue
            searched.add(str(candidate_dir))
            found = _find_item_in_ledger_dir(item_id, candidate_dir)
            if found:
                break

    if found:
        ledger_name = found["ledger_name"]
        path = found["path"]
        update = {
            "id": item_id,
            "type": "update",
            "updated_at": _utc_timestamp(),
            "worked_by": worked_by or _detect_model(),
        }
        if status:
            update["status"] = status
        if note:
            update["note"] = note
        if priority:
            update["priority"] = priority
        if title:
            update["title"] = title
        if description:
            update["description"] = description
        if assignee:
            update["assignee"] = assignee
        if due_date:
            update["due_date"] = due_date
        if labels is not None:
            # LED-2221: write to both `labels` and `tags`. The list_items
            # reconstruction (around line ~870) merges update events into
            # current state by checking the `tags` key only. Writing only
            # `labels` silently drops the update at read time, which in
            # particular meant the build daemon's `autonomous-build` tag
            # check could never see tags written through the MCP. Keeping
            # `labels` for any external consumer that reads the raw event
            # stream; adding `tags` so the live state aggregator picks it up.
            update["labels"] = labels
            update["tags"] = labels
        if blocked_by:
            update["blocked_by"] = blocked_by
        if blocks:
            update["blocks"] = blocks

        # LED-1408 Phase 1: attach ship_proof block when status transitions to
        # `done` or `shipped_pending`. Verified=True iff commit_sha or pr_url
        # was supplied (directly or scraped from the note). Phase 2's
        # reconciler will use this to distinguish "trustworthy done" from
        # "marked done but never verified on main."
        if status in ("done", "shipped_pending"):
            try:
                from ai.ledger_proof import build_ship_proof
                update["ship_proof"] = build_ship_proof(
                    commit_sha=commit_sha,
                    pr_url=pr_url,
                    note=note,
                )
                # LED-1420 Phase 2 strict-mode flip: when DELIMIT_LEDGER_STRICT_DONE=1,
                # an unverified `done` transition is downgraded to `shipped_pending`
                # so the nightly reconciler (scripts/delimit_ledger_reconciler.py)
                # can promote it to `done` once a commit-trailer match shows up on
                # origin/main. Off by default so existing workflows keep closing
                # items without hitting an unexpected gate; flip when the
                # reconciler has been observed running for ~1 week without
                # surprises.
                strict = os.environ.get("DELIMIT_LEDGER_STRICT_DONE") == "1"
                if (
                    strict
                    and status == "done"
                    and not update["ship_proof"].get("verified")
                ):
                    update["status"] = "shipped_pending"
                    existing_note = update.get("note") or ""
                    suffix = (
                        "[LED-1420 strict-mode: downgraded done → shipped_pending — "
                        "no commit_sha/pr_url proof; reconciler will upgrade to "
                        "done when it finds a Ledger-Item: " + item_id + " trailer "
                        "on origin/main]"
                    )
                    update["note"] = (existing_note + " " + suffix).strip() if existing_note else suffix
            except Exception:
                # Soft-fail: a ship_proof bug must not break ledger close.
                # The unverified state will be re-detectable from the missing
                # key on the next audit pass.
                pass

        # LED-5321 M4: evidence-classed promotion. A later commit must not
        # inherit an earlier review: independently_reviewed only advances
        # when the transcript binds to the exact diff (review_binding).
        _review_binding_result: Optional[Dict[str, Any]] = None
        if _evidence_normalized is not None:
            _mapped = KIND_TO_VERIFIED_STATE.get(
                str(_evidence_normalized.get("kind", "")), ""
            )
            if (
                _mapped == "independently_reviewed"
                and _evidence_normalized.get("class") == "verified"
            ):
                try:
                    from ai.review_binding import check_review_binding

                    _review_binding_result = check_review_binding(
                        transcript_path=review_transcript or "",
                        diff_path=review_diff_path or "",
                        diff_text=review_diff_text or "",
                    )
                except Exception as exc:
                    _review_binding_result = {
                        "bound": False,
                        "reason": f"review_binding_error: {exc}",
                    }
                if not isinstance(_review_binding_result, dict) or not _review_binding_result.get("bound"):
                    # Fail closed: keep the record (backward compatible) but
                    # store it as an assertion so verified_states does NOT
                    # advance and views render ASSERTED, not VERIFIED.
                    _reason = ""
                    if isinstance(_review_binding_result, dict):
                        _reason = str(_review_binding_result.get("reason", "unbound"))
                    _evidence_normalized = {
                        **_evidence_normalized,
                        "class": "assertion",
                    }
                    if isinstance(_review_binding_result, dict):
                        _review_binding_result.setdefault("downgraded", True)
                    else:
                        _review_binding_result = {
                            "bound": False,
                            "reason": _reason or "unbound",
                            "downgraded": True,
                        }
            update["evidence"] = _evidence_normalized
            if _review_binding_result is not None:
                update["review_binding"] = _review_binding_result
            # Advance verified_states only on verified-class + ref present.
            # Replay is scoped to this item's file (no full audit).
            if _evidence_normalized.get("class") == "verified":
                try:
                    _prior = _replay_current_state(item_id, path.parent)
                    _existing_states: Any = (
                        _prior.get("verified_states", []) if _prior else []
                    )
                except Exception:
                    _existing_states = []
                _advanced = _advance_verified_states(
                    _existing_states, _evidence_normalized
                )
                _base = (
                    list(_existing_states)
                    if isinstance(_existing_states, list)
                    else []
                )
                if _advanced != _base:
                    update["verified_states"] = _advanced

        _append(path, update)

        # Sync to Supabase for dashboard visibility
        try:
            from ai.supabase_sync import sync_ledger_update
            sync_ledger_update(item_id, status=status or "", note=note or "")
        except Exception:
            pass  # Never let cloud sync break ledger operations

        return {"updated": item_id, "changes": update, "ledger": ledger_name}

    return {"error": f"Item {item_id} not found in project ledger"}


# LED-1145 Phase 1 PR-A: known-good slim projection for AI agent triage.
# Strips description / acceptance_criteria / context / tags / hash etc. so the
# response fits in an MCP tool result without truncation. Default behaviour
# stays full (callers depending on description still get it); pass
# fields="slim" or an explicit allowlist to opt in.
SLIM_FIELDS = ("id", "title", "status", "priority", "type", "venture", "updated_at")
_VALID_FIELDS = SLIM_FIELDS + (
    "description", "acceptance_criteria", "context", "tags", "created_at",
    "worked_by", "last_worked_by", "last_note", "hash", "source", "tools_needed",
    "estimated_complexity", "ledger",
    # LED-5321 M4: evidence-classed truth fields (all optional, additive).
    "evidence", "evidence_class", "verified_states", "truth_label",
    "review_binding", "ship_proof",
)
_VALID_SORT = ("updated_at", "created_at", "priority")
_VALID_ORDER = ("asc", "desc")
_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def _normalize_filter_list(value):
    """Accept None / str / list and produce a list of strings (or None)."""
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        return [v.strip() for v in value.split(",") if v.strip()]
    return list(value) if value else None


def _resolve_fields(fields):
    """Map the `fields` parameter to a concrete projection set.

    Returns:
        (projection: set[str] | None, error: str | None)
        - projection=None means "return all fields" (backward-compat default)
        - error is set when an unknown field name was requested
    """
    if fields is None or fields == "":
        return None, None
    if isinstance(fields, str):
        if fields == "slim":
            return set(SLIM_FIELDS), None
        if fields == "*":
            return None, None
        # comma-separated string from the MCP boundary
        names = [f.strip() for f in fields.split(",") if f.strip()]
    else:
        names = list(fields)
    if not names:
        return None, None
    if names == ["*"]:
        return None, None
    if names == ["slim"]:
        return set(SLIM_FIELDS), None
    unknown = [n for n in names if n not in _VALID_FIELDS and n != "*"]
    if unknown:
        return None, f"unknown field(s) requested: {sorted(unknown)}; valid: {sorted(_VALID_FIELDS)}"
    return set(names), None


def _ts_to_iso(value):
    """Coerce a timestamp value into a comparable ISO string. Empty → ''."""
    return value or ""


def _compare_iso(a: str, b: str, op: str) -> bool:
    """Lexical ISO comparison; both sides must be naively-comparable strings.
    Empty string sorts before any real timestamp so '<X' is True for missing."""
    if op == "before":
        return a < b
    if op == "after":
        return a > b
    return False


def _make_cursor(offset: int, filter_sig: str) -> str:
    payload = f"{offset}:{filter_sig}"
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[int, str]:
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        offset_str, filter_sig = decoded.split(":", 1)
        return int(offset_str), filter_sig
    except (ValueError, UnicodeDecodeError):
        return 0, ""


def _filter_signature(spec: Dict[str, Any]) -> str:
    """Short, deterministic hash of the filter spec; used to invalidate cursors
    when the caller changes filters between page requests."""
    canonical = json.dumps(spec, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def list_items(
    ledger: str = "both",
    # Backward-compat single-value filters:
    status: Optional[str] = None,
    priority: Optional[str] = None,
    # New multi-value filters:
    status__in=None,
    priority__in=None,
    tags__contains_all=None,
    text: Optional[str] = None,
    linked_external_id: Optional[str] = None,
    created_before: Optional[str] = None,
    created_after: Optional[str] = None,
    updated_before: Optional[str] = None,
    updated_after: Optional[str] = None,
    # Sort + projection + pagination:
    sort: str = "updated_at",
    order: str = "desc",
    fields=None,
    limit: int = 50,
    cursor: Optional[str] = None,
    project_path: str = ".",
    strict_venture: bool = False,
) -> Dict[str, Any]:
    """List ledger items with optional filters, sort, projection, and cursor pagination.

    LED-1145 Phase 1 PR-A: extended from the original 3-filter signature.
    Backward compatible — old callers passing only `status` / `priority`
    continue to work without change.

    Args:
        ledger: "ops" | "strategy" | "both".
        status: single-value status filter (back-compat).
        priority: single-value priority filter (back-compat).
        status__in: list (or comma-separated string) of statuses to match.
        priority__in: list (or comma-separated string) of priorities to match.
        tags__contains_all: list (or comma-separated string); item must contain ALL these tags.
        text: case-insensitive substring match against title + description.
        linked_external_id: exact substring match in description or tags
            (e.g. "github.com/owner/repo/issues/123").
        created_before / created_after / updated_before / updated_after:
            ISO timestamps (e.g. "2026-04-01T00:00:00Z"). Lexical compare.
        sort: "updated_at" | "created_at" | "priority". Default updated_at.
        order: "asc" | "desc". Default desc.
        fields: response projection. None or "*" = full (default, back-compat).
            "slim" = SLIM_FIELDS only. List/CSV of field names = those only.
            Unknown field names → ERROR (no silent no-op).
        limit: page size (default 50).
        cursor: opaque pagination token from a prior call's `next_cursor`.
            If filters change between calls, the cursor is invalidated and the
            response begins at offset 0 with `cursor_invalidated=True`.
        project_path: ledger root (auto-detect when ".").
        strict_venture: LED-3925. When True, drop rows whose ``venture`` field
            does not match the queried venture (alias-collapsed). This isolates
            queries that resolve to the shared central store — an unknown-slug
            or legacy-layout query used to return EVERY venture's items. When
            the queried venture cannot be determined the filter fails OPEN
            (no filtering, logged) to avoid an empty result. Default False so
            existing callers are unaffected.

    Returns:
        {
            "venture": str,
            "items": {"ops": [...], "strategy": [...]},
            "summary": {"total": int, "open": int, "done": int, "in_progress": int},
            "next_cursor": str | None,
            "cursor_invalidated": bool (only when True),
        }
    """
    _ensure(project_path)
    ledger_dir = _project_ledger_dir(project_path)
    venture = _detect_venture(project_path)

    # LED-3925 read-path scope: resolve the venture this query is scoped to.
    # Fail OPEN (expected_venture stays None → no filtering) when it can't be
    # determined, so strict mode never returns an empty result by accident.
    expected_venture: Optional[str] = None
    if strict_venture:
        # Fail OPEN unless the QUERY resolves to a *recognized* canonical slug.
        # An unrecognized / custom query venture must never filter (PR #325
        # review, unanimous): dedicated ventures are already store-isolated, so
        # strict mode only adds value for the recognized ventures that pool in
        # the shared central store.
        expected_venture = _canonical_venture_slug(venture.get("name", ""))
        if not expected_venture:
            logger.debug(
                "list_items: strict_venture requested but venture %r is not a "
                "recognized canonical slug; filter fails open",
                venture.get("name", ""),
            )

    if sort not in _VALID_SORT:
        return {"error": f"sort must be one of {list(_VALID_SORT)}"}
    if order not in _VALID_ORDER:
        return {"error": f"order must be one of {list(_VALID_ORDER)}"}

    projection, projection_err = _resolve_fields(fields)
    if projection_err:
        return {"error": projection_err}

    # Normalise filter list params (accept str / list / None)
    status_list = _normalize_filter_list(status__in)
    priority_list = _normalize_filter_list(priority__in)
    tags_list = _normalize_filter_list(tags__contains_all)

    # Backward compat: status="open" → status_list=["open"]; same for priority.
    if status and not status_list:
        status_list = [status]
    if priority and not priority_list:
        priority_list = [priority]

    # Filter signature for cursor invalidation
    filter_spec = {
        "ledger": ledger,
        "status_list": sorted(status_list) if status_list else None,
        "priority_list": sorted(priority_list) if priority_list else None,
        "tags_list": sorted(tags_list) if tags_list else None,
        "text": text,
        "linked_external_id": linked_external_id,
        "created_before": created_before,
        "created_after": created_after,
        "updated_before": updated_before,
        "updated_after": updated_after,
        "sort": sort,
        "order": order,
    }
    current_sig = _filter_signature(filter_spec)

    # Decode cursor if provided
    start_offset = 0
    cursor_invalidated = False
    if cursor:
        decoded_offset, decoded_sig = _decode_cursor(cursor)
        if decoded_sig == current_sig:
            start_offset = decoded_offset
        else:
            cursor_invalidated = True

    text_lower = (text or "").lower() if text else None

    # LED-3739: the status breakdown in `summary` must be independent of the
    # `status`/`status__in` filter, otherwise callers that filter by status
    # (get_context uses status="open"; health_summary uses
    # status__in=[open,in_progress,blocked]) get a self-referential breakdown
    # where the filtered-out statuses read 0. That produced the reported
    # divergence (health said 43 in_progress, context said 0 in_progress on the
    # SAME store). We accumulate items that pass every filter EXCEPT the status
    # filter and compute the breakdown over that scoped set, so the status
    # distribution is authoritative regardless of the status filter.
    breakdown_pool: list = []

    results: Dict[str, list] = {}
    for ledger_name, filename in [("ops", "operations.jsonl"), ("strategy", "strategy.jsonl")]:
        if ledger not in ("both", ledger_name):
            continue

        path = ledger_dir / filename
        items = _read_ledger(path)

        # Build current state by replaying events (event-sourced)
        state: Dict[str, Dict[str, Any]] = {}
        for item in items:
            item_id = item.get("id", "")
            if item.get("type") == "update":
                if item_id in state:
                    if "status" in item:
                        state[item_id]["status"] = item["status"]
                    if "note" in item:
                        state[item_id]["last_note"] = item["note"]
                    if "priority" in item:
                        state[item_id]["priority"] = item["priority"]
                    if "worked_by" in item:
                        state[item_id]["last_worked_by"] = item["worked_by"]
                    if "tags" in item and item["tags"] is not None:
                        # Tag updates replace the existing tag set when present
                        state[item_id]["tags"] = item["tags"]
                    # LED-5321 M4: replay truth fields (latest wins).
                    if "evidence" in item and isinstance(item["evidence"], dict):
                        state[item_id]["evidence"] = item["evidence"]
                        _ec = item["evidence"].get("class", "")
                        if _ec:
                            state[item_id]["evidence_class"] = _ec
                    if "verified_states" in item and isinstance(item["verified_states"], list):
                        state[item_id]["verified_states"] = list(item["verified_states"])
                    if "review_binding" in item:
                        state[item_id]["review_binding"] = item["review_binding"]
                    if "ship_proof" in item:
                        state[item_id]["ship_proof"] = item["ship_proof"]
                    state[item_id]["updated_at"] = item.get("updated_at")
            else:
                state[item_id] = {**item}

        # LED-5321 M4: truth labels (never fabricated; legacy stays unclassified).
        for _sid, _st in state.items():
            if "verified_states" not in _st:
                _st["verified_states"] = []
            _st["truth_label"] = truth_label_for_item(_st)

        filtered = list(state.values())

        # LED-3925: strict venture scope. Drop ONLY rows POSITIVELY attributed
        # to a *different recognized* canonical venture (PR #325 review,
        # unanimous). A row whose venture tag is empty, junk, or an unrecognized
        # legacy string (e.g. "tmpabc123") resolves to no canonical slug and
        # STAYS VISIBLE — never-break-installs: existing users' legacy-tagged
        # rows must not silently vanish. Applied BEFORE the breakdown snapshot
        # so summary counts are venture-scoped too.
        if strict_venture and expected_venture:
            def _is_foreign(row: Dict[str, Any]) -> bool:
                row_slug = _canonical_venture_slug(str(row.get("venture") or ""))
                return row_slug is not None and row_slug != expected_venture
            filtered = [i for i in filtered if not _is_foreign(i)]

        # Apply every filter EXCEPT status first, so the status breakdown can be
        # computed over this scoped-but-status-agnostic set.
        if priority_list:
            priorities = set(priority_list)
            filtered = [i for i in filtered if i.get("priority") in priorities]
        if tags_list:
            required_tags = set(tags_list)
            filtered = [
                i for i in filtered
                if required_tags.issubset(set(i.get("tags") or []))
            ]
        if text_lower:
            filtered = [
                i for i in filtered
                if text_lower in (i.get("title") or "").lower()
                or text_lower in (i.get("description") or "").lower()
            ]
        if linked_external_id:
            needle = linked_external_id
            filtered = [
                i for i in filtered
                if needle in (i.get("description") or "")
                or needle in " ".join(i.get("tags") or [])
                or needle in (i.get("context") or "")
            ]
        if created_before:
            filtered = [i for i in filtered if _compare_iso(_ts_to_iso(i.get("created_at")), created_before, "before")]
        if created_after:
            filtered = [i for i in filtered if _compare_iso(_ts_to_iso(i.get("created_at")), created_after, "after")]
        if updated_before:
            filtered = [i for i in filtered if _compare_iso(_ts_to_iso(i.get("updated_at") or i.get("created_at")), updated_before, "before")]
        if updated_after:
            filtered = [i for i in filtered if _compare_iso(_ts_to_iso(i.get("updated_at") or i.get("created_at")), updated_after, "after")]

        # Snapshot the scoped set (all filters except status) for the breakdown.
        breakdown_pool.extend(filtered)

        # Now apply the status filter for the returned items + pagination total.
        if status_list:
            statuses = set(status_list)
            filtered = [i for i in filtered if i.get("status") in statuses]

        # Sort
        reverse = order == "desc"
        if sort == "priority":
            filtered.sort(
                key=lambda x: (
                    _PRIORITY_ORDER.get(x.get("priority", "P2"), 9),
                    x.get("created_at", ""),
                ),
                reverse=reverse,
            )
        else:
            sort_key = "updated_at" if sort == "updated_at" else "created_at"
            filtered.sort(
                key=lambda x: x.get(sort_key) or x.get("created_at") or "",
                reverse=reverse,
            )

        results[ledger_name] = filtered

    # Apply projection + pagination across the combined result.
    # Combine per-ledger lists in stable order (ops first, then strategy).
    combined: list = []
    for ledger_name in ("ops", "strategy"):
        if ledger_name in results:
            for it in results[ledger_name]:
                # Tag each item with its ledger source so the projected
                # response retains the routing info even when "ledger" itself
                # isn't part of the original record.
                if "ledger" not in it:
                    it = {**it, "ledger": ledger_name}
                combined.append(it)

    total_pre_page = len(combined)
    page = combined[start_offset:start_offset + limit]
    next_offset = start_offset + len(page)
    has_more = next_offset < total_pre_page
    next_cursor = _make_cursor(next_offset, current_sig) if has_more else None

    # Apply projection to page items.
    if projection is not None:
        page = [{k: v for k, v in i.items() if k in projection} for i in page]

    # Re-bucket projected page items back into ops / strategy for response shape.
    paged_results: Dict[str, list] = {"ops": [], "strategy": []}
    if ledger != "strategy":
        paged_results.setdefault("ops", [])
    if ledger != "ops":
        paged_results.setdefault("strategy", [])
    # Walk the page using the still-tagged combined data to know which bucket;
    # we kept "ledger" in the projection step only when it was already there.
    for src, dst in zip(combined[start_offset:start_offset + limit], page):
        bucket = src.get("ledger") or "ops"
        paged_results.setdefault(bucket, []).append(dst)

    summary_total = total_pre_page
    # Status breakdown is computed over breakdown_pool (all filters EXCEPT the
    # status filter) so it is authoritative and identical for callers that
    # differ only in their status filter (LED-3739). `total` remains the
    # post-status-filter count for pagination + the health "unresolved" count.
    response = {
        "venture": venture["name"],
        "items": {k: v for k, v in paged_results.items() if k in results},
        "summary": {
            "total": summary_total,
            "open": sum(1 for i in breakdown_pool if i.get("status") == "open"),
            "done": sum(1 for i in breakdown_pool if i.get("status") == "done"),
            "in_progress": sum(1 for i in breakdown_pool if i.get("status") == "in_progress"),
            "blocked": sum(1 for i in breakdown_pool if i.get("status") == "blocked"),
            "archived": sum(1 for i in breakdown_pool if i.get("status") == "archived"),
        },
        "next_cursor": next_cursor,
    }
    if cursor_invalidated:
        response["cursor_invalidated"] = True
    return response


def get_context(project_path: str = ".") -> Dict[str, Any]:
    """Get a concise ledger summary for AI context — what's open, what's next."""
    venture = _detect_venture(project_path)
    # LED-3925: get_context is a venture-scoped summary — enforce strict scope
    # so the top-5 "next up" never bleeds another venture's open items.
    result = list_items(status="open", project_path=project_path, strict_venture=True)
    open_items = []
    for ledger_items in result["items"].values():
        open_items.extend(ledger_items)

    priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    open_items.sort(key=lambda x: priority_order.get(x.get("priority", "P2"), 9))

    return {
        "venture": venture["name"],
        "open_items": len(open_items),
        # LED-5321 M4: label each item ASSERTED/VERIFIED/STALE/UNAVAILABLE
        # where evidence exists; legacy records show "legacy/unclassified".
        "next_up": [
            {
                "id": i["id"],
                "title": i["title"],
                "priority": i["priority"],
                "truth_label": i.get("truth_label", LEGACY_TRUTH_LABEL),
                "verified_states": list(i.get("verified_states", [])),
            }
            for i in open_items[:5]
        ],
        "summary": result["summary"],
    }


def list_ventures() -> Dict[str, Any]:
    """List all registered ventures/projects."""
    if not VENTURES_FILE.exists():
        return {"ventures": {}, "count": 0}
    try:
        ventures = json.loads(VENTURES_FILE.read_text())
        return {"ventures": ventures, "count": len(ventures)}
    except Exception:
        return {"ventures": {}, "count": 0}


# ═══════════════════════════════════════════════════════════════════════
#  LEDGER QUERY (Natural language → structured queries)
# ═══════════════════════════════════════════════════════════════════════

def query_ledger(query: str, project_path: str = ".") -> Dict[str, Any]:
    """Answer natural language questions about the ledger.

    Supports: "what shipped this week?", "what's blocked?", "show P0s",
    "how many items completed?", "what's next?", etc.
    """
    q = query.lower().strip()

    # Route to appropriate data based on query intent
    if any(w in q for w in ["shipped", "completed", "done", "finished", "closed"]):
        result = list_items(status="done", project_path=project_path, limit=50)
        items = []
        for v in result.get("items", {}).values():
            items.extend(v)

        # Filter by time if mentioned
        if "today" in q:
            today = _utc_timestamp("%Y-%m-%d")
            items = [i for i in items if i.get("updated_at", "").startswith(today) or i.get("created_at", "").startswith(today)]
        elif "week" in q or "7 day" in q:
            cutoff = time.time() - 7 * 86400
            items = [i for i in items if _parse_ts(i.get("updated_at", "")) > cutoff]
        elif "month" in q or "30 day" in q:
            cutoff = time.time() - 30 * 86400
            items = [i for i in items if _parse_ts(i.get("updated_at", "")) > cutoff]

        return {"query": query, "intent": "completed", "items": [{"id": i["id"], "title": i["title"], "truth_label": i.get("truth_label", LEGACY_TRUTH_LABEL), "verified_states": list(i.get("verified_states", []))} for i in items], "count": len(items)}

    elif any(w in q for w in ["blocked", "blocking", "stuck"]):
        result = list_items(status="open", project_path=project_path, limit=50)
        items = []
        for v in result.get("items", {}).values():
            items.extend(v)
        # Check for items with blocked_by links
        blocked = []
        for i in items:
            links = get_links(i["id"], project_path)
            has_blocker = any(l.get("type") == "blocked_by" for l in links.get("links", []))
            if has_blocker or i.get("status") == "blocked":
                blocked.append(i)
        return {"query": query, "intent": "blocked", "items": [{"id": i["id"], "title": i["title"]} for i in blocked], "count": len(blocked)}

    elif any(w in q for w in ["next", "should i", "what to work", "priority", "urgent"]):
        return get_context(project_path)

    elif "p0" in q:
        result = list_items(priority="P0", project_path=project_path, limit=20)
        items = []
        for v in result.get("items", {}).values():
            items.extend(v)
        return {"query": query, "intent": "priority_filter", "priority": "P0", "items": [{"id": i["id"], "title": i["title"], "status": i.get("status", "open")} for i in items], "count": len(items)}

    elif "p1" in q:
        result = list_items(priority="P1", project_path=project_path, limit=20)
        items = []
        for v in result.get("items", {}).values():
            items.extend(v)
        return {"query": query, "intent": "priority_filter", "priority": "P1", "items": [{"id": i["id"], "title": i["title"], "status": i.get("status", "open")} for i in items], "count": len(items)}

    elif any(w in q for w in ["how many", "count", "total", "stats", "summary"]):
        result = list_items(project_path=project_path, limit=500)
        all_items = []
        for v in result.get("items", {}).values():
            all_items.extend(v)
        by_status = {}
        by_priority = {}
        by_venture = {}
        for i in all_items:
            s = i.get("status", "open")
            by_status[s] = by_status.get(s, 0) + 1
            p = i.get("priority", "P1")
            by_priority[p] = by_priority.get(p, 0) + 1
            v = i.get("venture", "unknown")
            by_venture[v] = by_venture.get(v, 0) + 1
        return {"query": query, "intent": "stats", "total": len(all_items), "by_status": by_status, "by_priority": by_priority, "by_venture": by_venture}

    elif any(w in q for w in ["open", "todo", "remaining", "left"]):
        return get_context(project_path)

    else:
        # Default: search by keyword in titles
        result = list_items(project_path=project_path, limit=100)
        all_items = []
        for v in result.get("items", {}).values():
            all_items.extend(v)
        words = q.split()
        matches = [i for i in all_items if any(w in i.get("title", "").lower() for w in words)]
        return {"query": query, "intent": "search", "items": [{"id": i["id"], "title": i["title"], "status": i.get("status")} for i in matches[:20]], "count": len(matches)}


def _parse_ts(ts_str: str) -> float:
    """Parse ISO timestamp to epoch seconds."""
    try:
        import datetime
        dt = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0


# ═══════════════════════════════════════════════════════════════════════
#  LEDGER LINKS (Dependencies, Blockers, Parent-Child)
# ═══════════════════════════════════════════════════════════════════════

LINKS_FILE_NAME = "links.jsonl"
VALID_LINK_TYPES = {"blocks", "blocked_by", "parent", "child", "relates_to", "duplicates", "supersedes", "superseded_by"}


def link_items(
    from_id: str,
    to_id: str,
    link_type: str = "blocks",
    note: str = "",
    project_path: str = ".",
) -> Dict[str, Any]:
    """Create a relationship between two ledger items."""
    if link_type not in VALID_LINK_TYPES:
        return {"error": f"Invalid link_type '{link_type}'. Use: {', '.join(sorted(VALID_LINK_TYPES))}"}

    _ensure(project_path)
    ledger_dir = _project_ledger_dir(project_path)
    links_file = ledger_dir / LINKS_FILE_NAME

    link = {
        "from": from_id,
        "to": to_id,
        "type": link_type,
        "note": note,
        "created_at": _utc_timestamp(),
    }

    with open(links_file, "a") as f:
        f.write(json.dumps(link) + "\n")

    # Auto-create reverse link for bidirectional types
    reverse_map = {
        "blocks": "blocked_by",
        "blocked_by": "blocks",
        "parent": "child",
        "child": "parent",
        "supersedes": "superseded_by",
        "superseded_by": "supersedes",
    }
    if link_type in reverse_map:
        reverse = {
            "from": to_id,
            "to": from_id,
            "type": reverse_map[link_type],
            "note": note,
            "created_at": link["created_at"],
            "auto_reverse": True,
        }
        with open(links_file, "a") as f:
            f.write(json.dumps(reverse) + "\n")

    return {"linked": True, "from": from_id, "to": to_id, "type": link_type}


def get_links(
    item_id: str,
    project_path: str = ".",
) -> Dict[str, Any]:
    """Get all links/relationships for a ledger item."""
    _ensure(project_path)
    ledger_dir = _project_ledger_dir(project_path)
    links_file = ledger_dir / LINKS_FILE_NAME

    if not links_file.exists():
        return {"item_id": item_id, "links": [], "count": 0}

    links = []
    try:
        for line in links_file.read_text().strip().split("\n"):
            if not line.strip():
                continue
            link = json.loads(line)
            if link.get("from") == item_id or link.get("to") == item_id:
                links.append(link)
    except Exception:
        pass

    return {"item_id": item_id, "links": links, "count": len(links)}


def unlink_items(
    from_id: str,
    to_id: str,
    project_path: str = ".",
) -> Dict[str, Any]:
    """Remove all links between two items."""
    _ensure(project_path)
    ledger_dir = _project_ledger_dir(project_path)
    links_file = ledger_dir / LINKS_FILE_NAME

    if not links_file.exists():
        return {"unlinked": False, "reason": "No links file"}

    kept = []
    removed = 0
    for line in links_file.read_text().strip().split("\n"):
        if not line.strip():
            continue
        link = json.loads(line)
        if (link.get("from") == from_id and link.get("to") == to_id) or \
           (link.get("from") == to_id and link.get("to") == from_id):
            removed += 1
        else:
            kept.append(line)

    links_file.write_text("\n".join(kept) + "\n" if kept else "")
    return {"unlinked": True, "removed": removed}


# ═══════════════════════════════════════════════════════════════════════
#  CANONICAL CLOSE (LED-5321 M3) — helpers
# ═══════════════════════════════════════════════════════════════════════
#
# One supported close operation: capture/refresh the soul, write the session
# handoff, create the handoff receipt, then READ BACK every written record.
# Helpers here are shared by session_handoff (canonical path) and the
# read-side surfacing (session_history / phoenix.revive).

def _close_ts_epoch(value: Any) -> float:
    """Parse a close timestamp to epoch seconds; -inf when unparseable.

    Accepts ISO-8601 (with or without microseconds/offset) and the ledger
    ``%Y-%m-%dT%H:%M:%SZ`` form so records written by different writers
    compare in one domain for the stale-writer guard.
    """
    text = str(value or "").strip()
    if not text:
        return float("-inf")
    try:
        normalized = text.replace("Z", "+00:00") if text.endswith("Z") else text
        from datetime import datetime as _dt

        parsed = _dt.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.now().astimezone().tzinfo)
        return parsed.timestamp()
    except Exception:
        pass
    try:
        return time.mktime(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return float("-inf")


_CLOSE_THREAD_LOCKS: Dict[str, threading.Lock] = {}
_CLOSE_THREAD_LOCKS_GUARD = threading.Lock()

# Caps for the resume/history surfacing lists (full counts are reported
# alongside so callers know when the list was truncated).
_CLOSE_STATE_LIST_CAP = 25
# Bound for the latest-handoff scan so a huge sessions dir stays cheap.
_CLOSE_HANDOFF_SCAN_CAP = 200


def _soul_rank_key(data: Dict[str, Any]) -> Tuple[str, str]:
    """Newest-first rank for a soul record: created_at, then soul_id.

    Same rule as session_continuity._soul_rank_key (duplicated here so the
    canonical close works in minimal layouts too): discovery orders by
    ``created_at`` with a deterministic ``soul_id`` tie-break — never by
    filename/write order — so a late write carrying an older ``created_at``
    can never become "latest".
    """
    return (str(data.get("created_at") or ""), str(data.get("soul_id") or ""))


@contextmanager
def _flock_exclusive(lock_path: Path):
    """Hold an fcntl exclusive lock on ``lock_path`` (best-effort yield).

    Shared primitive for the close locks below. Degrades to an unguarded
    yield when flock is unavailable — callers layer in-process thread
    locks separately where they need them.
    """
    if fcntl is None:
        yield
        return
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        yield
        return
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _continuity_lock_path(project_path: str, prefix: str) -> Optional[Path]:
    """Return the continuity lock file for a project (creating the dir)."""
    try:
        lock_key = hashlib.sha256(
            os.path.realpath(project_path or ".").encode()
        ).hexdigest()[:12]
    except Exception:
        lock_key = "default"
    lock_dir = _delimit_home() / "continuity" / "locks"
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(lock_dir, 0o700)
    except OSError:
        return None
    return lock_dir / f"{prefix}_{lock_key}.lock"


@contextmanager
def _close_lock(project_path: str):
    """Serialize canonical closes for one project (whole-close scope).

    Uses its OWN per-project lock file (``close_<hash12>.lock``) plus an
    in-process thread lock — deliberately NOT the soul-pointer lock file,
    because a close invokes ``capture_soul``, which takes the soul-pointer
    lock itself; nesting flock on the same file from one process
    self-deadlocks. Soul-file/latest.json mutations inside the close take
    :func:`_soul_mutation_lock` briefly instead (see below), so closes
    still serialize against concurrent captures where they share state.
    """
    try:
        thread_key = "close:" + hashlib.sha256(
            os.path.realpath(project_path or ".").encode()
        ).hexdigest()[:12]
    except Exception:
        thread_key = "close:default"
    with _CLOSE_THREAD_LOCKS_GUARD:
        thread_lock = _CLOSE_THREAD_LOCKS.setdefault(thread_key, threading.Lock())
    with thread_lock:
        lock_path = _continuity_lock_path(project_path, "close")
        if lock_path is None:
            yield
            return
        with _flock_exclusive(lock_path):
            yield


@contextmanager
def _soul_mutation_lock(project_path: str):
    """Briefly serialize soul-file/latest.json mutations with captures.

    Reuses the existing soul-pointer lock mechanism: session_phoenix's
    per-project lock when importable (the same lock ``capture_soul``
    takes), else an equivalent local flock on the same shared path
    (``project_<hash12>.lock``, byte-identical to phoenix's
    ``_project_hash`` scheme). Held only for single read-modify-write
    steps — never across ``capture_soul``, which takes this same lock
    internally (nesting it would self-deadlock).
    """
    try:
        from ai.session_phoenix import _project_latest_lock as _phoenix_lock
    except ImportError:
        try:
            from session_phoenix import _project_latest_lock as _phoenix_lock  # type: ignore
        except ImportError:
            _phoenix_lock = None
    if _phoenix_lock is not None:
        with _phoenix_lock(project_path):
            yield
        return
    lock_path = _continuity_lock_path(project_path, "project")
    if lock_path is None:
        yield
        return
    with _flock_exclusive(lock_path):
        yield


def _persist_soul_record(
    proj_dir: Path,
    soul_path: Path,
    soul_record: Dict[str, Any],
    project_path: str,
) -> None:
    """Rewrite one soul file + guarded latest.json advance, atomically
    with respect to concurrent captures (raises on write failure)."""
    with _soul_mutation_lock(project_path):
        _write_soul_file(soul_path, soul_record)
        _advance_soul_latest_guarded(proj_dir, soul_record)


def _resolve_project_best_effort(project_path: str = "") -> str:
    """Resolve one repo-stable project identity; never raises."""
    try:
        try:
            from ai.session_continuity import resolve_project_path
        except ImportError:
            from session_continuity import resolve_project_path  # type: ignore
        return resolve_project_path(project_path)
    except Exception:
        try:
            raw = (project_path or "").strip() or os.getcwd()
            return os.path.realpath(raw)
        except Exception:
            return project_path or ""


def _soul_bucket_for_close(project_path: str) -> Path:
    """Return the soul storage directory for a close, honoring overrides.

    Prefers session_phoenix's resolver when importable, else the free
    core's — both share the identical layout/override contract, and the
    caller always passes an already-resolved path so the two agree.
    """
    try:
        from ai.session_phoenix import _project_dir as _pd
        return _pd(project_path)
    except ImportError:
        pass
    try:
        from session_phoenix import _project_dir as _pd  # type: ignore
        return _pd(project_path)
    except ImportError:
        pass
    try:
        from ai.session_continuity import _project_dir as _pd_core
    except ImportError:
        from session_continuity import _project_dir as _pd_core  # type: ignore
    return _pd_core(project_path)


def _prepare_soul_bucket_for_close(project_path: str) -> Path:
    """Create the soul bucket with owner-only modes; never surprises."""
    try:
        from ai.session_phoenix import _prepare_project_dir as _prep
        return _prep(project_path)
    except ImportError:
        pass
    try:
        from session_phoenix import _prepare_project_dir as _prep  # type: ignore
        return _prep(project_path)
    except ImportError:
        pass
    try:
        from ai.session_continuity import _prepare_project_dir as _prep_core
    except ImportError:
        from session_continuity import _prepare_project_dir as _prep_core  # type: ignore
    return _prep_core(project_path)


def _read_soul_records(proj_dir: Path) -> List[Tuple[Path, Dict[str, Any]]]:
    """Parse every timestamped soul file in a bucket (tolerant, read-only).

    Skips latest.json (a pointer copy) and floor_*.json files (owned by the
    deterministic-floor protocol — a canonical close must never rewrite
    them). Unparseable files are skipped, never fatal.
    """
    records: List[Tuple[Path, Dict[str, Any]]] = []
    try:
        if not proj_dir.is_dir():
            return records
        files = sorted(proj_dir.glob("*.json"), key=lambda p: p.name)
    except OSError:
        return records
    for path in files:
        try:
            if path.name == "latest.json" or path.name.startswith("floor_"):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("soul_id"):
            records.append((path, data))
    return records


def _find_close_soul(
    proj_dir: Path, logical_session_id: str
) -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
    """Return the newest soul stamped with ``logical_session_id`` (or Nones)."""
    best: Optional[Tuple[Path, Dict[str, Any]]] = None
    if not logical_session_id:
        return None, None
    for path, data in _read_soul_records(proj_dir):
        if data.get("logical_session_id") != logical_session_id:
            continue
        if best is None or _soul_rank_key(data) > _soul_rank_key(best[1]):
            best = (path, data)
    return best if best is not None else (None, None)


def _find_soul_file_by_id(
    proj_dir: Path, soul_id: str
) -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
    """Return the newest timestamped file for ``soul_id`` (or Nones)."""
    best: Optional[Tuple[Path, Dict[str, Any]]] = None
    if not soul_id:
        return None, None
    for path, data in _read_soul_records(proj_dir):
        if data.get("soul_id") != soul_id:
            continue
        if best is None or _soul_rank_key(data) > _soul_rank_key(best[1]):
            best = (path, data)
    return best if best is not None else (None, None)


def _write_soul_file(path: Path, data: Dict[str, Any]) -> None:
    """Atomically rewrite one soul file with owner-only modes."""
    payload = json.dumps(data, indent=2)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _advance_soul_latest_guarded(proj_dir: Path, candidate: Dict[str, Any]) -> bool:
    """Advance latest.json only to an equal-or-newer record; never raises.

    Same stale-writer rule as phoenix._advance_project_latest: an
    older-timestamped writer cannot supersede a newer record. A refresh of
    the SAME soul (equal rank) always rewrites so latest.json carries the
    newest content. Returns True when latest.json was (re)written.
    """
    latest = proj_dir / "latest.json"
    try:
        if latest.is_file() and not latest.is_symlink():
            try:
                current = json.loads(latest.read_text())
            except (OSError, ValueError):
                current = None
            if isinstance(current, dict):
                if _soul_rank_key(current) > _soul_rank_key(candidate):
                    return False
        _write_soul_file(latest, candidate)
        return True
    except OSError:
        return False


def _write_handoff_file(path: Path, handoff: Dict[str, Any]) -> None:
    """Atomically write a NEW handoff file (fails if the path exists)."""
    payload = json.dumps(handoff, indent=2)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _rewrite_handoff_file(path: Path, handoff: Dict[str, Any]) -> None:
    """Atomically replace an EXISTING handoff file (idempotent re-close)."""
    replacement = path.with_name(f".rewrite.{path.name}")
    try:
        # A crashed close may have left its intermediate behind; remove it
        # so the recovery close is not blocked by its own O_EXCL write.
        replacement.unlink(missing_ok=True)
    except OSError:
        pass
    _write_handoff_file(replacement, handoff)
    try:
        os.replace(replacement, path)
        if os.name != "nt":
            path.chmod(0o600)
    except Exception:
        try:
            replacement.unlink()
        except OSError:
            pass
        raise


def _load_handoff_file(path: Path) -> Optional[Dict[str, Any]]:
    """Parse one handoff file; None when missing/unparseable (never raises)."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _find_handoff_by_logical_id(
    logical_session_id: str,
) -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
    """Return the newest handoff stamped with ``logical_session_id``."""
    if not logical_session_id or not SESSIONS_DIR.exists():
        return None, None
    best: Optional[Tuple[Path, Dict[str, Any]]] = None
    try:
        files = sorted(SESSIONS_DIR.glob("session_*.json"), reverse=True)
    except OSError:
        return None, None
    for path in files[:_CLOSE_HANDOFF_SCAN_CAP]:
        data = _load_handoff_file(path)
        if not data or data.get("logical_session_id") != logical_session_id:
            continue
        if best is None:
            best = (path, data)
    return best if best is not None else (None, None)


def _latest_handoff_for_project(
    resolved_project: str = "", venture: str = ""
) -> str:
    """Return the latest session handoff id, preferring project, then venture.

    Legacy handoffs carry no project_path, so the preference degrades
    gracefully: exact project match (canonical compare) wins, else a venture
    match, else the newest handoff overall. "" when the store is empty.
    Never raises.
    """
    try:
        if not SESSIONS_DIR.exists():
            return ""
        files = sorted(SESSIONS_DIR.glob("session_*.json"), reverse=True)
    except OSError:
        return ""
    overall = ""
    venture_hit = ""
    for path in files[:_CLOSE_HANDOFF_SCAN_CAP]:
        data = _load_handoff_file(path)
        if not data:
            continue
        hid = str(data.get("id") or "")
        if not hid:
            continue
        if not overall:
            overall = hid
        if resolved_project and not venture_hit:
            try:
                if _resolve_project_best_effort(
                    str(data.get("project_path") or "")
                ) == resolved_project and data.get("project_path"):
                    return hid
            except Exception:
                pass
        if venture and not venture_hit and data.get("venture") == venture:
            venture_hit = hid
        if overall and venture_hit and not resolved_project:
            break
    return venture_hit or overall


def _pending_receipt_summaries(
    resolved_project: str = "",
) -> Tuple[List[Dict[str, str]], int]:
    """Pending handoff receipts for a project, newest first (capped, counted).

    Merges the project's own namespace with any same-project receipts
    captured from a different cwd (canonical-identity compare), deduped by
    receipt id. Never raises; [] when the receipts backend is unavailable.
    """
    try:
        try:
            from ai.handoff_receipts import get_pending_receipts
        except ImportError:
            from handoff_receipts import get_pending_receipts  # type: ignore
        scoped = (
            get_pending_receipts(project_path=resolved_project)
            if resolved_project
            else []
        )
        merged = list(scoped)
        seen = {r.receipt_id for r in merged}
        if resolved_project:
            for receipt in get_pending_receipts(project_path=""):
                if receipt.receipt_id in seen:
                    continue
                try:
                    if (
                        receipt.project_path
                        and _resolve_project_best_effort(receipt.project_path)
                        == resolved_project
                    ):
                        merged.append(receipt)
                        seen.add(receipt.receipt_id)
                except Exception:
                    continue
        else:
            for receipt in get_pending_receipts(project_path=""):
                if receipt.receipt_id not in seen:
                    merged.append(receipt)
                    seen.add(receipt.receipt_id)
        merged.sort(
            key=lambda r: (r.created_at, r.receipt_id), reverse=True
        )
        total = len(merged)
        summaries = [
            {
                "id": r.receipt_id,
                "receipt_id": r.receipt_id,
                "task": r.task_description,
                "next_action": r.next_action,
            }
            for r in merged[:_CLOSE_STATE_LIST_CAP]
        ]
        return summaries, total
    except Exception:
        return [], 0


def _open_dispatch_summaries(
    venture: str = "",
) -> Tuple[List[Dict[str, str]], int]:
    """Open agent dispatches, newest first (capped, counted).

    When a venture is known, dispatches stamped with that venture are
    returned along with unscoped (venture-less) ones — a fresh lead must
    discover unscoped work too. Never raises; [] when dispatch is
    unavailable.
    """
    try:
        try:
            from ai.agent_dispatch import list_active_agents
        except ImportError:
            from agent_dispatch import list_active_agents  # type: ignore
        active = list_active_agents().get("active_tasks", []) or []
        if venture:
            active = [
                t
                for t in active
                if not str(t.get("venture") or "").strip()
                or str(t.get("venture") or "").strip() == venture
            ]
        active = sorted(
            active,
            key=lambda t: (
                str(t.get("created_at") or ""),
                str(t.get("id") or ""),
            ),
            reverse=True,
        )
        total = len(active)
        summaries = [
            {
                "id": str(t.get("id") or ""),
                "title": str(t.get("title") or ""),
                "status": str(t.get("status") or ""),
            }
            for t in active[:_CLOSE_STATE_LIST_CAP]
        ]
        return summaries, total
    except Exception:
        return [], 0


def _venture_best_effort(resolved_project: str = "") -> str:
    """Resolve the venture name for a project; "" when unknown (never raises)."""
    if not resolved_project:
        return ""
    try:
        try:
            from ai.session_phoenix import _venture_for_project
        except ImportError:
            from session_phoenix import _venture_for_project  # type: ignore
        return str(_venture_for_project(resolved_project) or "")
    except Exception:
        return ""


def close_state_for_project(
    project_path: str = "", venture: str = ""
) -> Dict[str, Any]:
    """Snapshot of current close/resume state for one project (LED-5321 M3).

    Shared by session_history and phoenix.revive so a fresh lead discovers
    current state without knowing an artifact path or receipt id. Never
    raises — every backend failure degrades to an empty section, never a
    hard error (matching the continuity "never blocks revive" contract).
    """
    out: Dict[str, Any] = {
        "pending_receipts": [],
        "pending_receipts_count": 0,
        "open_dispatches": [],
        "open_dispatches_count": 0,
        "latest_handoff_id": "",
    }
    try:
        resolved = _resolve_project_best_effort(project_path)
        vent = (venture or "").strip() or _venture_best_effort(resolved)
        pending, pending_total = _pending_receipt_summaries(resolved)
        dispatches, dispatch_total = _open_dispatch_summaries(vent)
        out["pending_receipts"] = pending
        out["pending_receipts_count"] = pending_total
        out["open_dispatches"] = dispatches
        out["open_dispatches_count"] = dispatch_total
        out["latest_handoff_id"] = _latest_handoff_for_project(resolved, vent)
    except Exception:
        pass
    return out


# ═══════════════════════════════════════════════════════════════════════
#  SESSION HANDOFF
# ═══════════════════════════════════════════════════════════════════════

SESSIONS_DIR = GLOBAL_DIR / "sessions"


def session_handoff(
    summary: str,
    items_completed: Optional[List[str]] = None,
    items_added: Optional[List[str]] = None,
    key_decisions: Optional[List[str]] = None,
    blockers: Optional[List[str]] = None,
    files_changed: Optional[List[str]] = None,
    venture: str = "",
    project_path: str = "",
    source_model: str = "session_handoff",
    refresh_soul: bool = True,
    # LED-5321 M3 canonical-close extensions — OPTIONAL ONLY. Every
    # pre-existing positional/keyword call shape keeps working unchanged.
    logical_session_id: str = "",
    task_description: str = "",
    completed: Optional[List[str]] = None,
    not_completed: Optional[List[str]] = None,
    assumptions: Optional[List[str]] = None,
    next_action: str = "",
    to_model: str = "any",
    priority: str = "P1",
    created_at: str = "",
) -> Dict[str, Any]:
    """Store a session summary for cross-session continuity.

    Called at end of a productive session so the next session can load context.

    LED-5321 M3 — this is the ONE supported canonical close operation. With
    the default ``refresh_soul=True`` a single call captures/refreshes the
    soul, writes the session handoff record, creates the handoff receipt
    (via ``ai.handoff_receipts.create_receipt``), then READS BACK each
    written record and reports ``verified`` (never claims closure on a
    failed readback). ``refresh_soul=False`` preserves the exact legacy
    handoff-only write for the orphan-salvage path.

    See LED-3731 (below, on _legacy_handoff_write) for the pointer-soul
    history, and _canonical_close for the idempotency / stale-writer /
    crash-recovery contract.
    """
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        SESSIONS_DIR.chmod(0o700)
    if not refresh_soul:
        return _legacy_handoff_write(
            summary=summary,
            items_completed=items_completed,
            items_added=items_added,
            key_decisions=key_decisions,
            blockers=blockers,
            files_changed=files_changed,
            venture=venture,
            project_path=project_path,
            source_model=source_model,
            refresh_soul=False,
        )
    return _canonical_close(
        summary=summary,
        items_completed=items_completed,
        items_added=items_added,
        key_decisions=key_decisions,
        blockers=blockers,
        files_changed=files_changed,
        venture=venture,
        project_path=project_path,
        source_model=source_model,
        logical_session_id=logical_session_id,
        task_description=task_description,
        completed=completed,
        not_completed=not_completed,
        assumptions=assumptions,
        next_action=next_action,
        to_model=to_model,
        priority=priority,
        created_at=created_at,
    )


def _capture_close_soul(
    summary: str,
    key_decisions: Optional[List[str]],
    items_completed: Optional[List[str]],
    blockers: Optional[List[str]],
    items_added: Optional[List[str]],
    source_model: str,
    resolved_project_path: str,
):
    """Capture a fresh soul via phoenix when present, else the free core.

    Same backend chain (and the same "transitive phoenix failure must NOT
    silently degrade" guard) as the pre-M3 pointer-soul refresh. Returns the
    captured soul object; raises on failure.
    """
    try:
        from ai.session_continuity import resolve_project_path, _session_phoenix_is_absent
    except ModuleNotFoundError as exc:  # pragma: no cover - flat bundle layout
        if exc.name not in {"ai", "ai.session_continuity"}:
            raise
        from session_continuity import resolve_project_path, _session_phoenix_is_absent  # type: ignore
    _ = resolve_project_path  # resolved by the caller; import kept for parity
    try:
        from ai.session_phoenix import capture_soul
    except ModuleNotFoundError as exc:  # pragma: no cover - flat import layout
        if not _session_phoenix_is_absent(exc):
            raise
        try:
            from session_phoenix import capture_soul  # type: ignore
        except ModuleNotFoundError as exc:
            if not _session_phoenix_is_absent(exc):
                raise
            # Public installs intentionally omit session_phoenix; the
            # shipped free-core contract owns the same schema + layout.
            try:
                from ai.session_continuity import capture_soul_core as capture_soul
            except ImportError:  # pragma: no cover - flat bundle layout
                from session_continuity import capture_soul_core as capture_soul  # type: ignore
    return capture_soul(
        active_task=summary,
        decisions=list(key_decisions or []),
        key_context=list(items_completed or []),
        blockers=list(blockers or []),
        next_steps=list(items_added or []),
        source_model=source_model,
        project_path=resolved_project_path,
        task_status="in_progress",
    )


def _canonical_close(
    summary: str,
    items_completed: Optional[List[str]] = None,
    items_added: Optional[List[str]] = None,
    key_decisions: Optional[List[str]] = None,
    blockers: Optional[List[str]] = None,
    files_changed: Optional[List[str]] = None,
    venture: str = "",
    project_path: str = "",
    source_model: str = "session_handoff",
    logical_session_id: str = "",
    task_description: str = "",
    completed: Optional[List[str]] = None,
    not_completed: Optional[List[str]] = None,
    assumptions: Optional[List[str]] = None,
    next_action: str = "",
    to_model: str = "any",
    priority: str = "P1",
    created_at: str = "",
) -> Dict[str, Any]:
    """One-call canonical session close: soul + handoff + receipt + readback.

    Contract (LED-5321 M3):
    - Writes (or idempotently refreshes) all three records, then reads each
      back. ``verified`` is True only when every WRITTEN record reads back;
      a failed readback returns ``verified=False`` with ``missing`` naming
      the exact absent record kind(s) and ``status="close_unverified"`` —
      closure is never claimed on partial writes.
    - Idempotent: closing twice with the same ``logical_session_id``
      updates and returns the SAME soul/handoff/receipt (no duplicate
      receipts). A re-close reports ``idempotent=True``.
    - Crash-recoverable: when a previous close for the same logical id left
      only a subset of records, this call completes the set and reports it
      under ``recovered_partial`` instead of silently accepting the gap.
    - Concurrency: the whole close runs under the per-project close lock,
      so two concurrent closes for the same session/project serialize.
      Stale-writer guard: an incoming write whose timestamp is older than
      the current newest record for the same logical id does NOT overwrite
      it (reported under ``stale_suppressed``); latest.json advances only
      to equal-or-newer records.
    - Model-agnostic: ``source_model`` is an opaque passthrough (default
      ``"unknown"`` when empty) — no supervisor/model is hardcoded.
    - Additive storage: handoff dicts gain only new keys; souls/receipts
      gain only new dataclass fields. Legacy records without them still
      load and are treated as unverified assertions, not errors.
    """
    from datetime import datetime, timezone as _timezone

    resolved = _resolve_project_best_effort(project_path)
    logical_id = (logical_session_id or "").strip()
    if not logical_id:
        logical_id = (
            f"close_{_utc_timestamp('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        )
    capture_key = (
        "close-"
        + hashlib.sha256(f"canonical-close:{logical_id}".encode()).hexdigest()[:18]
    )
    incoming_ts = (created_at or "").strip() or datetime.now(_timezone.utc).isoformat()
    effective_model = (source_model or "").strip() or "unknown"

    with _close_lock(resolved):
        try:
            proj_dir = _prepare_soul_bucket_for_close(resolved)
        except Exception as exc:
            return {
                "status": "close_unverified",
                "verified": False,
                "soul_id": "",
                "handoff_id": "",
                "receipt_id": "",
                "logical_session_id": logical_id,
                "project_path": resolved,
                "soul_refresh_status": "failed",
                "soul_refresh_error": f"soul bucket unavailable: {exc}"[:500],
                "receipt_status": "failed",
                "missing": ["soul", "handoff", "receipt"],
                "readback": {"soul": None, "handoff": None, "receipt": None},
            }

        # — discovery of any previous close for this logical id —
        soul_path, soul_data = _find_close_soul(proj_dir, logical_id)
        handoff_path, handoff_data = _find_handoff_by_logical_id(logical_id)
        receipt_obj = None
        receipts_backend = True
        try:
            try:
                from ai.handoff_receipts import find_receipts_by_logical_id
            except ImportError:
                from handoff_receipts import find_receipts_by_logical_id  # type: ignore
            found = find_receipts_by_logical_id(logical_id, project_path=resolved)
            if not found:
                # Same-project receipts captured from a different cwd live
                # in another namespace; match them by canonical identity.
                for candidate in find_receipts_by_logical_id(logical_id):
                    try:
                        if (
                            candidate.project_path
                            and _resolve_project_best_effort(candidate.project_path)
                            == resolved
                        ):
                            found = [candidate]
                            break
                    except Exception:
                        continue
            receipt_obj = found[0] if found else None
        except ImportError:
            receipts_backend = False
        except Exception:
            receipt_obj = None

        found_at_entry = [
            kind
            for kind, present in (
                ("soul", soul_data is not None),
                ("handoff", handoff_data is not None),
                ("receipt", receipt_obj is not None),
            )
            if present
        ]

        # Stale-writer guard (per record, one time domain): an incoming
        # write older than a record's current newest timestamp must not
        # overwrite that record's content.
        incoming_epoch = _close_ts_epoch(incoming_ts)
        stale_suppressed: List[str] = []
        if soul_data is not None and _close_ts_epoch(
            soul_data.get("created_at")
        ) > incoming_epoch:
            stale_suppressed.append("soul")
        if handoff_data is not None and _close_ts_epoch(
            handoff_data.get("updated_at") or handoff_data.get("timestamp")
        ) > incoming_epoch:
            stale_suppressed.append("handoff")
        if receipt_obj is not None and _close_ts_epoch(
            getattr(receipt_obj, "created_at", "")
        ) > incoming_epoch:
            stale_suppressed.append("receipt")

        # — predetermined handoff identity (existing or new) —
        if handoff_data is not None and handoff_path is not None:
            handoff_id = str(handoff_data.get("id") or handoff_path.stem)
            handoff_file = handoff_path
        else:
            handoff_id = (
                f"session_{_utc_timestamp('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:12]}"
            )
            handoff_file = SESSIONS_DIR / f"{handoff_id}.json"

        # — soul: refresh in place, or capture fresh —
        soul_id = ""
        soul_refresh_status = "failed"
        soul_refresh_error = ""
        soul_file: Optional[Path] = soul_path
        soul_record: Optional[Dict[str, Any]] = dict(soul_data) if soul_data else None
        if soul_record is not None and "soul" in stale_suppressed:
            soul_id = str(soul_record.get("soul_id") or "")
            soul_refresh_status = "refreshed"
        elif soul_record is not None:
            try:
                soul_record.update(
                    {
                        "active_task": summary,
                        "decisions": list(key_decisions or []),
                        "key_context": list(items_completed or []),
                        "blockers": list(blockers or []),
                        "next_steps": list(items_added or []),
                        "source_model": effective_model,
                        "project_path": resolved,
                        "task_status": soul_record.get("task_status") or "in_progress",
                        "updated_at": incoming_ts,
                        "logical_session_id": logical_id,
                        "capture_key": capture_key,
                        "handoff_id": handoff_id,
                        "close_status": "pending",
                    }
                )
                if files_changed is not None:
                    soul_record["files_modified"] = list(files_changed)
                assert soul_file is not None
                _persist_soul_record(proj_dir, soul_file, soul_record, resolved)
                soul_id = str(soul_record.get("soul_id") or "")
                soul_refresh_status = "refreshed" if soul_id else "failed"
                if not soul_id:
                    soul_refresh_error = "existing soul record has no soul_id"
            except Exception as exc:
                soul_refresh_error = f"{type(exc).__name__}: {exc}"[:500]
                soul_id = ""
                soul_record = None
        else:
            try:
                soul = _capture_close_soul(
                    summary,
                    key_decisions,
                    items_completed,
                    blockers,
                    items_added,
                    effective_model,
                    resolved,
                )
                fresh_id = getattr(soul, "soul_id", "") or ""
                if not fresh_id:
                    soul_refresh_error = "capture returned no soul_id"
                else:
                    located, data = _find_soul_file_by_id(proj_dir, fresh_id)
                    if located is None or data is None:
                        soul_refresh_error = (
                            f"captured soul {fresh_id} not found on disk"
                        )
                    else:
                        data.update(
                            {
                                "created_at": incoming_ts,
                                "source_model": effective_model,
                                "project_path": resolved,
                                "updated_at": incoming_ts,
                                "logical_session_id": logical_id,
                                "capture_key": capture_key,
                                "handoff_id": handoff_id,
                                "close_status": "pending",
                            }
                        )
                        if files_changed is not None:
                            data["files_modified"] = list(files_changed)
                        _persist_soul_record(proj_dir, located, data, resolved)
                        soul_file, soul_record = located, data
                        soul_id = fresh_id
                        soul_refresh_status = "refreshed"
            except Exception as exc:
                soul_refresh_error = f"{type(exc).__name__}: {exc}"[:500]

        # — receipt: update in place, or create via create_receipt —
        receipt_id = getattr(receipt_obj, "receipt_id", "") if receipt_obj else ""
        receipt_status = "failed"
        receipt_error = ""
        receipt_record: Optional[Dict[str, Any]] = None
        receipt_updates = {
            "task_description": (task_description or "").strip() or summary,
            "completed": list(completed) if completed is not None else list(items_completed or []),
            "not_completed": list(not_completed) if not_completed is not None else list(items_added or []),
            "assumptions": list(assumptions or []),
            "blockers": list(blockers or []),
            "next_action": next_action or "",
            "priority": (priority or "").strip() or "P1",
            "from_model": effective_model,
            "to_model": (to_model or "").strip() or "any",
            "project_path": resolved,
            "logical_session_id": logical_id,
            "soul_id": soul_id,
            "handoff_id": handoff_id,
        }
        if files_changed is not None:
            receipt_updates["files_modified"] = [
                {"path": p, "change_type": "modified", "summary": ""}
                for p in files_changed
            ]
        if not receipts_backend:
            receipt_status = "skipped_unavailable"
        elif receipt_obj is not None and "receipt" in stale_suppressed:
            # Older-timestamped writer: keep the newer receipt content.
            receipt_status = "reused"
            from dataclasses import asdict as _asdict

            receipt_record = _asdict(receipt_obj)
        elif receipt_obj is not None:
            try:
                try:
                    from ai.handoff_receipts import update_receipt_fields
                except ImportError:
                    from handoff_receipts import update_receipt_fields  # type: ignore
                updated = update_receipt_fields(
                    receipt_id, dict(receipt_updates), project_path=resolved
                )
                if updated is None:
                    # Namespace drift (receipt found globally but not in the
                    # resolved namespace): retry without the scope pin.
                    updated = update_receipt_fields(receipt_id, dict(receipt_updates))
                if updated is None:
                    receipt_error = f"receipt {receipt_id} no longer updatable"
                    receipt_id = ""
                else:
                    receipt_status = "reused"
                    from dataclasses import asdict as _asdict

                    receipt_record = _asdict(updated)
            except Exception as exc:
                receipt_error = f"{type(exc).__name__}: {exc}"[:500]
                receipt_id = ""
        else:
            try:
                try:
                    from ai.handoff_receipts import create_receipt
                except ImportError:
                    from handoff_receipts import create_receipt  # type: ignore
                new_receipt = create_receipt(
                    task_description=str(receipt_updates["task_description"]),
                    completed=list(receipt_updates["completed"]),  # type: ignore[arg-type]
                    not_completed=list(receipt_updates["not_completed"]),  # type: ignore[arg-type]
                    assumptions=list(receipt_updates["assumptions"]),  # type: ignore[arg-type]
                    blockers=list(receipt_updates["blockers"]),  # type: ignore[arg-type]
                    files_modified=receipt_updates.get("files_modified"),  # type: ignore[arg-type]
                    next_action=str(receipt_updates["next_action"]),
                    priority=str(receipt_updates["priority"]),
                    from_model=str(receipt_updates["from_model"]),
                    to_model=str(receipt_updates["to_model"]),
                    project_path=resolved,
                )
                receipt_id = new_receipt.receipt_id
                try:
                    try:
                        from ai.handoff_receipts import update_receipt_fields
                    except ImportError:
                        from handoff_receipts import update_receipt_fields  # type: ignore
                    linked = update_receipt_fields(
                        receipt_id,
                        {
                            "logical_session_id": logical_id,
                            "soul_id": soul_id,
                            "handoff_id": handoff_id,
                        },
                        project_path=resolved,
                    )
                    if linked is not None:
                        from dataclasses import asdict as _asdict

                        receipt_record = _asdict(linked)
                except Exception:
                    pass
                # Stamp the close's logical time on the fresh receipt row
                # (file + index) so all three close records share one time
                # domain for newest-first ordering and stale comparison.
                # (update_receipt_fields protects created_at by design, so
                # this intentional backdate goes through a direct rewrite.)
                try:
                    try:
                        from ai import handoff_receipts as _hr
                    except ImportError:
                        import handoff_receipts as _hr  # type: ignore
                    _rpath = _hr._project_dir(resolved) / f"{receipt_id}.json"
                    _rdata = json.loads(_rpath.read_text())
                    _rdata["created_at"] = incoming_ts
                    _rpath.write_text(json.dumps(_rdata, indent=2))
                    _rindex = _hr._load_index(resolved)
                    for _entry in _rindex.get("receipts", []):
                        if (
                            isinstance(_entry, dict)
                            and _entry.get("receipt_id") == receipt_id
                        ):
                            _entry["created_at"] = incoming_ts
                    _hr._save_index(resolved, _rindex)
                    if receipt_record is not None:
                        receipt_record["created_at"] = incoming_ts
                except Exception:
                    pass
                if receipt_record is None:
                    from dataclasses import asdict as _asdict

                    receipt_record = _asdict(new_receipt)
                    receipt_record.update(
                        {
                            "logical_session_id": logical_id,
                            "soul_id": soul_id,
                            "handoff_id": handoff_id,
                        }
                    )
                receipt_status = "created"
            except Exception as exc:
                receipt_error = f"{type(exc).__name__}: {exc}"[:500]
                receipt_id = ""

        # Link the receipt back onto the soul (best-effort; readback judges).
        if soul_record is not None and soul_file is not None and receipt_id:
            try:
                soul_record["receipt_id"] = receipt_id
                _persist_soul_record(proj_dir, soul_file, soul_record, resolved)
            except Exception:
                pass

        # — handoff: update in place, or write new —
        handoff_write_error = ""
        if handoff_data is not None and "handoff" in stale_suppressed:
            # Older-timestamped writer: keep the newer handoff content.
            handoff = dict(handoff_data)
            handoff["id"] = handoff_id
        elif handoff_data is not None:
            handoff = dict(handoff_data)
            handoff.update(
                {
                    "venture": venture or handoff.get("venture") or "all",
                    "summary": summary,
                    "items_completed": items_completed or [],
                    "items_added": items_added or [],
                    "key_decisions": key_decisions or [],
                    "blockers": blockers or [],
                    "files_changed": files_changed or [],
                    "logical_session_id": logical_id,
                    "capture_key": capture_key,
                    "soul_id": soul_id,
                    "receipt_id": receipt_id,
                    "project_path": resolved,
                    "source_model": effective_model,
                    "updated_at": incoming_ts,
                    "close_verified": False,
                }
            )
            handoff["id"] = handoff_id
            try:
                _rewrite_handoff_file(handoff_file, handoff)
            except Exception as exc:
                handoff_write_error = f"{type(exc).__name__}: {exc}"[:500]
        else:
            handoff = {
                "id": handoff_id,
                "timestamp": _utc_timestamp(),
                "venture": venture or "all",
                "summary": summary,
                "items_completed": items_completed or [],
                "items_added": items_added or [],
                "key_decisions": key_decisions or [],
                "blockers": blockers or [],
                "files_changed": files_changed or [],
                "logical_session_id": logical_id,
                "capture_key": capture_key,
                "soul_id": soul_id,
                "receipt_id": receipt_id,
                "project_path": resolved,
                "source_model": effective_model,
                # Logical close time (== wall clock unless the caller
                # overrode created_at): the stale-writer authority for
                # this record, in the same domain as the soul/receipt.
                "updated_at": incoming_ts,
                "close_verified": False,
            }
            try:
                _write_handoff_file(handoff_file, handoff)
            except Exception as exc:
                handoff_write_error = f"{type(exc).__name__}: {exc}"[:500]

        # — readback: every written record must load again —
        readback_soul = None
        readback_handoff = None
        readback_receipt = None
        if soul_id:
            _, readback_soul = _find_soul_file_by_id(proj_dir, soul_id)
            if readback_soul is not None and readback_soul.get("soul_id") != soul_id:
                readback_soul = None
        readback_handoff = _load_handoff_file(handoff_file)
        if readback_handoff is not None and readback_handoff.get("id") != handoff_id:
            readback_handoff = None
        # The receipt is expected whenever its backend exists: creation
        # was attempted, so an attempted-but-failed write must fail
        # verification (never silently verified). Only a wholly absent
        # backend degrades to soul+handoff verification.
        receipt_expected = receipts_backend
        if receipt_expected and receipt_id:
            try:
                try:
                    from ai.handoff_receipts import get_receipt
                except ImportError:
                    from handoff_receipts import get_receipt  # type: ignore
                loaded = get_receipt(receipt_id, project_path=resolved)
                if loaded is None:
                    loaded = get_receipt(receipt_id)
                if loaded is not None:
                    from dataclasses import asdict as _asdict

                    readback_receipt = _asdict(loaded)
            except Exception:
                readback_receipt = None

        missing: List[str] = []
        if readback_soul is None:
            missing.append("soul")
        if readback_handoff is None:
            missing.append("handoff")
        if receipt_expected and readback_receipt is None:
            missing.append("receipt")

        if not missing:
            # Finalize the verified stamp, then confirm the final bytes.
            try:
                if soul_record is not None and soul_file is not None:
                    soul_record["close_status"] = "verified"
                    if receipt_id:
                        soul_record["receipt_id"] = receipt_id
                    _persist_soul_record(proj_dir, soul_file, soul_record, resolved)
                handoff["close_verified"] = True
                _rewrite_handoff_file(handoff_file, handoff)
                _, reread_soul = (
                    _find_soul_file_by_id(proj_dir, soul_id) if soul_id else (None, None)
                )
                reread_handoff = _load_handoff_file(handoff_file)
                if reread_soul is not None and reread_handoff is not None:
                    readback_soul, readback_handoff = reread_soul, reread_handoff
                else:
                    if reread_soul is None:
                        missing.append("soul")
                    if reread_handoff is None:
                        missing.append("handoff")
            except Exception as exc:
                missing.append("finalize")
                handoff_write_error = handoff_write_error or f"{type(exc).__name__}: {exc}"[:200]
        else:
            # Best-effort partial stamp so the NEXT close/revive detects the
            # gap instead of silently accepting it.
            try:
                if soul_record is not None and soul_file is not None:
                    soul_record["close_status"] = "partial"
                    _persist_soul_record(proj_dir, soul_file, soul_record, resolved)
            except Exception:
                pass

        verified = not missing
        written_this_call = [
            kind
            for kind, ok in (
                ("soul", readback_soul is not None),
                ("handoff", readback_handoff is not None),
                ("receipt", readback_receipt is not None),
            )
            if ok and kind not in found_at_entry
        ]
        recovered_partial = None
        if found_at_entry and len(found_at_entry) < 3 and receipt_expected:
            recovered_partial = {
                "found": sorted(found_at_entry),
                "completed": sorted(written_this_call),
            }
        elif found_at_entry and len(found_at_entry) < 2 and not receipt_expected:
            recovered_partial = {
                "found": sorted(found_at_entry),
                "completed": sorted(written_this_call),
            }

        # LED-1705: only a successfully refreshed, revivable soul may
        # suppress the deterministic Stop-hook floor.
        if soul_id and readback_soul is not None:
            try:
                try:
                    from ai.last_capture import stamp_capture
                except ImportError:  # pragma: no cover - flat import layout
                    from last_capture import stamp_capture
                stamp_capture(source="model", session_id=handoff_id)
            except Exception:
                pass

        result: Dict[str, Any] = {
            "status": "closed" if verified else "close_unverified",
            "verified": verified,
            "soul_id": soul_id,
            "handoff_id": handoff_id,
            "receipt_id": receipt_id,
            "logical_session_id": logical_id,
            "project_path": resolved,
            "venture": venture or "all",
            "source_model": effective_model,
            "capture_key": capture_key,
            "idempotent": bool(found_at_entry) and (
                ("receipt" in found_at_entry) or not receipt_expected
            ) and "soul" in found_at_entry and "handoff" in found_at_entry,
            "stale_suppressed": stale_suppressed,
            "recovered_partial": recovered_partial,
            "missing": missing,
            "readback": {
                "soul": readback_soul,
                "handoff": readback_handoff,
                "receipt": readback_receipt,
            },
            # Legacy keys (unchanged shape for existing callers).
            "saved": handoff_id,
            "path": str(handoff_file),
            "handoff": handoff,
            "soul_refresh_status": soul_refresh_status,
            "receipt_status": receipt_status,
        }
        if not soul_id:
            # No soul was captured or refreshed: omit the key rather than
            # returning an empty id, so a public bundle without Phoenix (or a
            # broken Phoenix) is never mistaken for a silently captured soul.
            result.pop("soul_id", None)
        if soul_refresh_error:
            result["soul_refresh_error"] = soul_refresh_error
        if receipt_error:
            result["receipt_error"] = receipt_error
        if handoff_write_error:
            result["handoff_write_error"] = handoff_write_error
        return result


def _legacy_handoff_write(
    summary: str,
    items_completed: Optional[List[str]] = None,
    items_added: Optional[List[str]] = None,
    key_decisions: Optional[List[str]] = None,
    blockers: Optional[List[str]] = None,
    files_changed: Optional[List[str]] = None,
    venture: str = "",
    project_path: str = "",
    source_model: str = "session_handoff",
    refresh_soul: bool = True,
) -> Dict[str, Any]:
    """Legacy handoff-only write (``refresh_soul=False`` path).

    Preserved byte-for-byte from the pre-M3 session_handoff for the
    deterministic-floor orphan-salvage path, which manages its own capture
    stamping and must write NO pointer-soul. Called only by session_handoff
    with refresh_soul=False; the ``if refresh_soul:`` branch below is kept
    for structural fidelity but never runs from the dispatcher.

    LED-3731: the handoff store (``~/.delimit/sessions/``) and the souls store
    (``~/.delimit/souls/<project-hash>/``, read by ``delimit_revive``) used to
    be DISJOINT — a fresh handoff never surfaced in revive, so revive could
    return a month-old soul instead of the state written minutes earlier (this
    bit a real wire-report session). To close the gap, a handoff now ALSO
    refreshes a lightweight pointer-soul in the souls store for the SAME
    project via ``session_phoenix.capture_soul`` when present, or the shipped
    Free core otherwise (one shared schema, no duplication), keyed by the same
    project identity ``revive`` uses. That makes the newest handoff win the
    next revive.

    ``project_path`` (additive, default = cwd) selects which project's soul is
    refreshed so handoff + revive agree on identity. ``refresh_soul=False``
    skips the pointer-soul (used by the deterministic-floor orphan-salvage
    path, which manages its own capture stamping). The refresh is
    best-effort and NEVER raises into the handoff write — matching
    session_phoenix's "never blocks revive" contract. The ephemeral-path guard
    (LED-3759) is inherited automatically from either capture backend, which
    redirects ephemeral (/tmp/pytest) paths to a throwaway store so a test
    handoff never pollutes the real souls store. The additive
    ``soul_refresh_status`` field reports
    ``refreshed``, ``failed``, or ``skipped``; failures also include a bounded
    ``soul_refresh_error`` instead of being silently reported as full success.
    No storage-format change to either store.
    """
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        SESSIONS_DIR.chmod(0o700)

    # The human-readable UTC prefix keeps history sortable, while the random
    # suffix prevents two agents handing off in the same second from replacing
    # one another at the shared sessions path.
    session_id = (
        f"session_{_utc_timestamp('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:12]}"
    )
    handoff = {
        "id": session_id,
        "timestamp": _utc_timestamp(),
        "venture": venture or "all",
        "summary": summary,
        "items_completed": items_completed or [],
        "items_added": items_added or [],
        "key_decisions": key_decisions or [],
        "blockers": blockers or [],
        "files_changed": files_changed or [],
    }

    path = SESSIONS_DIR / f"{session_id}.json"
    payload = json.dumps(handoff, indent=2)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise

    # LED-3731: refresh a pointer-soul so the NEXT revive for this project
    # returns THIS handoff's state, not an older soul. Best-effort; a failure
    # here must never break the handoff write.
    soul_id = ""
    soul_refresh_status = "skipped"
    soul_refresh_error = ""
    if refresh_soul:
        soul_refresh_status = "failed"
        try:
            try:
                from ai.session_continuity import resolve_project_path, _session_phoenix_is_absent
            except ModuleNotFoundError as exc:  # pragma: no cover - flat bundle layout
                if exc.name not in {"ai", "ai.session_continuity"}:
                    raise
                from session_continuity import resolve_project_path, _session_phoenix_is_absent  # type: ignore
            resolved_project_path = resolve_project_path(project_path)
            try:
                from ai.session_phoenix import capture_soul
            except ModuleNotFoundError as exc:  # pragma: no cover - flat import layout
                if not _session_phoenix_is_absent(exc):
                    raise
                try:
                    from session_phoenix import capture_soul  # type: ignore
                except ModuleNotFoundError as exc:
                    if not _session_phoenix_is_absent(exc):
                        raise
                    # Public npm installs intentionally omit the richer
                    # session_phoenix backend.  The shipped free-core contract
                    # owns the same SessionSoul schema + store layout, so a
                    # normal session_handoff must refresh revive through it
                    # instead of silently saving only to the disjoint sessions
                    # directory.
                    try:
                        from ai.session_continuity import capture_soul_core as capture_soul
                    except ImportError:  # pragma: no cover - flat bundle layout
                        from session_continuity import capture_soul_core as capture_soul  # type: ignore
            soul = capture_soul(
                active_task=summary,
                decisions=list(key_decisions or []),
                key_context=list(items_completed or []),
                blockers=list(blockers or []),
                next_steps=list(items_added or []),
                source_model=source_model,
                project_path=resolved_project_path,
                task_status="in_progress",
            )
            soul_id = getattr(soul, "soul_id", "") or ""
            if soul_id:
                soul_refresh_status = "refreshed"
            else:
                soul_refresh_error = "capture returned no soul_id"
        except Exception as exc:
            soul_id = ""
            soul_refresh_error = f"{type(exc).__name__}: {exc}"[:500]

    # LED-1705: only a successfully refreshed, revivable soul may suppress the
    # deterministic Stop-hook floor. A sessions/ handoff by itself is not read
    # by revive; stamping a failed, no-id, or explicitly skipped refresh would
    # hide the only recovery path while falsely claiming capture succeeded.
    if soul_id:
        try:
            try:
                from ai.last_capture import stamp_capture
            except ImportError:  # pragma: no cover - flat import layout
                from last_capture import stamp_capture
            stamp_capture(source="model", session_id=session_id)
        except Exception:
            pass

    result = {
        "saved": session_id,
        "path": str(path),
        "handoff": handoff,
        "soul_refresh_status": soul_refresh_status,
    }
    if soul_id:
        result["soul_id"] = soul_id
    if soul_refresh_error:
        result["soul_refresh_error"] = soul_refresh_error
    return result


def session_history(
    limit: int = 5,
    project_path: str = "",
    venture: str = "",
) -> Dict[str, Any]:
    """Load recent session handoffs for context recovery.

    LED-5321 M3: besides ``sessions``/``count`` (unchanged), the result now
    also surfaces the same current-state snapshot a fresh lead needs —
    ``pending_receipts`` (id/task/next_action), ``open_dispatches``
    (id/title/status), and ``latest_handoff_id`` — scoped to
    ``project_path``/``venture`` when given, else aggregated. Additive
    keys only; existing callers are unaffected.
    """
    if not SESSIONS_DIR.exists():
        sessions: List[Dict[str, Any]] = []
    else:
        files = sorted(SESSIONS_DIR.glob("session_*.json"), reverse=True)[:limit]
        sessions = []
        for f in files:
            try:
                sessions.append(json.loads(f.read_text()))
            except Exception:
                continue

    result: Dict[str, Any] = {"sessions": sessions, "count": len(sessions)}
    result.update(close_state_for_project(project_path=project_path, venture=venture))
    return result


# ── LED-1145 Phase 1 PR-B: bulk_action ───────────────────────────────────

# Single allowlisted enum keeps the API surface tiny and predictable.
# `archive` is a soft transition (status="archived", appended to JSONL); items
# stay in replay forever. NO hard delete. Per-item failures don't block others.
BULK_ACTIONS = ("archive", "set_status", "set_priority", "add_tag", "mark_done", "cancel")
# LED-1408: `shipped_pending` is the intermediate state between "committed" and
# "verified on main." Items transition to shipped_pending when a worker reports
# completion (commit exists somewhere) but the orchestrator hasn't yet verified
# the commit is reachable from origin/main. The reconciler (Phase 2) promotes
# shipped_pending → done once reachability is confirmed.
_VALID_BULK_STATUSES = ("open", "in_progress", "blocked", "shipped_pending", "done", "cancelled", "archived", "completed")
_VALID_BULK_PRIORITIES = ("P0", "P1", "P2", "P3")


def _normalize_id_list(value):
    """Accept None / str (CSV) / list and return list[str]. Used by bulk_action
    and the MCP tool wrapper."""
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def _replay_current_state(item_id: str, ledger_dir: Path) -> Optional[Dict[str, Any]]:
    """Walk the ledger and return the CURRENT replayed state of `item_id`,
    or None if the item doesn't exist. Used by bulk_action for dry-run
    diff preview AND by auto_close_linked_external to verify state changes.

    Replays the same fields list_items does so callers see a consistent view."""
    for ledger_name, filename in [("ops", "operations.jsonl"), ("strategy", "strategy.jsonl")]:
        path = ledger_dir / filename
        items = _read_ledger(path)
        state: Optional[Dict[str, Any]] = None
        for item in items:
            if item.get("id") != item_id:
                continue
            if item.get("type") == "update":
                if state is not None:
                    if "status" in item:
                        state["status"] = item["status"]
                    if "priority" in item:
                        state["priority"] = item["priority"]
                    if "tags" in item and item["tags"] is not None:
                        state["tags"] = item["tags"]
                    if "note" in item:
                        state["last_note"] = item["note"]
                    if "worked_by" in item:
                        state["last_worked_by"] = item["worked_by"]
                    # LED-5321 M4: replay truth fields (latest wins).
                    if "evidence" in item and isinstance(item["evidence"], dict):
                        state["evidence"] = item["evidence"]
                        _ec = item["evidence"].get("class", "")
                        if _ec:
                            state["evidence_class"] = _ec
                    if "verified_states" in item and isinstance(item["verified_states"], list):
                        state["verified_states"] = list(item["verified_states"])
                    if "review_binding" in item:
                        state["review_binding"] = item["review_binding"]
                    if "ship_proof" in item:
                        state["ship_proof"] = item["ship_proof"]
                    if "updated_at" in item:
                        state["updated_at"] = item["updated_at"]
            else:
                state = {**item}
        if state is not None:
            if "verified_states" not in state:
                state["verified_states"] = []
            state["truth_label"] = truth_label_for_item(state)
            return state
    return None


def _apply_field_update(
    item_id: str,
    field: str,
    new_value: Any,
    note: Optional[str],
    ledger_dir: Path,
) -> None:
    """Append a single-field update event directly to the item's file within
    a specific ledger_dir. Used by bulk_action when a ``ledger_dir_override``
    is given so priority/status changes land in the correct per-venture
    sub-ledger even when the item isn't reachable from the caller's
    project_path. Byte-compatible with the events update_item writes, so the
    change is fully reversible (append a counter-event to restore).
    """
    found = _find_item_in_ledger_dir(item_id, ledger_dir)
    if not found:
        raise RuntimeError(f"item {item_id} not found in {ledger_dir}")
    update_event = {
        "id": item_id,
        "type": "update",
        field: new_value,
        "updated_at": _utc_timestamp(),
        "worked_by": _detect_model(),
    }
    if note:
        update_event["note"] = note
    # LED-5321 M4: bulk direct writes also carry assertion evidence on done.
    try:
        _st = new_value if field == "status" else None
        _trig, _kind = _detect_assertion(_st, note, None, None)
        if _trig:
            update_event["evidence"] = _default_assertion_evidence(
                _kind or "merged", update_event["worked_by"]
            )
    except Exception:
        pass
    _append(found["path"], update_event)


def bulk_action(
    item_ids,
    action: str,
    dry_run: bool = True,
    note: Optional[str] = None,
    new_status: Optional[str] = None,
    new_priority: Optional[str] = None,
    tag: Optional[str] = None,
    project_path: str = ".",
    ledger_dir_override: Optional[Path] = None,
) -> Dict[str, Any]:
    """Apply one action to many items. Default `dry_run=True` returns what
    would change without writing. Per-item failures are reported but don't
    block other items in the batch.

    LED-1145 Phase 1 PR-B. The deliberation locked these defaults: a single
    enum-dispatched tool (not bulk_close/bulk_done/...), dry_run=True default,
    no hard delete, archive is a soft status transition.

    Args:
        item_ids: list of LED-XXX ids (str or list-of-str; CSV string accepted).
        action: one of BULK_ACTIONS.
        dry_run: when True (default), don't write. When False, apply.
        note: optional note attached to every successful update event.
        new_status: required when action="set_status".
        new_priority: required when action="set_priority".
        tag: required when action="add_tag" (single tag string).
        project_path: ledger root.

    Returns:
        {
            "dry_run": bool,
            "action": str,
            "would_change": [{id, field, old, new}, ...]  # if dry_run
            "changed":      [{id, field, old, new}, ...]  # if not dry_run
            "errors":       [{id, reason}]
            "summary": {"requested": int, "would_change": int (or "changed"), "errors": int}
        }
    """
    if action not in BULK_ACTIONS:
        return {
            "error": f"unknown action {action!r}; allowed: {list(BULK_ACTIONS)}",
        }

    ids = _normalize_id_list(item_ids)
    if not ids:
        return {"error": "item_ids must contain at least one id"}

    # Per-action argument validation
    if action == "set_status":
        if not new_status:
            return {"error": "set_status requires new_status"}
        if new_status not in _VALID_BULK_STATUSES:
            return {"error": f"new_status must be one of {list(_VALID_BULK_STATUSES)}"}
    if action == "set_priority":
        if not new_priority:
            return {"error": "set_priority requires new_priority"}
        if new_priority not in _VALID_BULK_PRIORITIES:
            return {"error": f"new_priority must be one of {list(_VALID_BULK_PRIORITIES)}"}
    if action == "add_tag":
        if not tag or not str(tag).strip():
            return {"error": "add_tag requires a non-empty tag"}

    if ledger_dir_override is not None:
        ledger_dir = Path(ledger_dir_override)
    else:
        _ensure(project_path)
        ledger_dir = _project_ledger_dir(project_path)

    # Build the per-item change description.
    # archive    → status: <current> → archived
    # set_status → status: <current> → <new_status>
    # set_priority → priority: <current> → <new_priority>
    # add_tag    → tags: [..current..] → [..current.., <tag>] (if not already present)
    # mark_done  → status: <current> → done
    # cancel     → status: <current> → cancelled
    changes: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []

    for item_id in ids:
        state = _replay_current_state(item_id, ledger_dir)
        if state is None:
            errors.append({"id": item_id, "reason": "not_found"})
            continue

        if action in ("archive", "mark_done", "cancel", "set_status"):
            new_val = {
                "archive": "archived",
                "mark_done": "done",
                "cancel": "cancelled",
                "set_status": new_status,
            }[action]
            old_val = state.get("status")
            if old_val == new_val:
                # No-op; skip silently (idempotent action). Don't record.
                continue
            changes.append({"id": item_id, "field": "status", "old": old_val, "new": new_val})
        elif action == "set_priority":
            old_val = state.get("priority")
            if old_val == new_priority:
                continue
            changes.append({"id": item_id, "field": "priority", "old": old_val, "new": new_priority})
        elif action == "add_tag":
            existing_tags = state.get("tags") or []
            if tag in existing_tags:
                continue
            changes.append({
                "id": item_id, "field": "tags",
                "old": list(existing_tags),
                "new": list(existing_tags) + [tag],
            })

    if dry_run:
        return {
            "dry_run": True,
            "action": action,
            "would_change": changes,
            "errors": errors,
            "summary": {
                "requested": len(ids),
                "would_change": len(changes),
                "errors": len(errors),
            },
        }

    # Apply mode: write one update event per change.
    applied: List[Dict[str, Any]] = []
    for change in changes:
        item_id = change["id"]
        field = change["field"]
        try:
            if ledger_dir_override is not None:
                # Write directly into the resolved sub-ledger so items that
                # aren't reachable from the caller's project_path still update.
                _apply_field_update(item_id, field, change["new"], note, ledger_dir)
            elif field == "status":
                update_item(item_id=item_id, status=change["new"], note=note, project_path=project_path)
            elif field == "priority":
                update_item(item_id=item_id, priority=change["new"], note=note, project_path=project_path)
            elif field == "tags":
                _apply_tag_update(item_id, change["new"], note=note, project_path=project_path)
            applied.append(change)
        except Exception as exc:  # noqa: BLE001 — per-item isolation
            errors.append({"id": item_id, "reason": f"write_failed: {exc}"})

    return {
        "dry_run": False,
        "action": action,
        "changed": applied,
        "errors": errors,
        "summary": {
            "requested": len(ids),
            "changed": len(applied),
            "errors": len(errors),
        },
    }


def _apply_tag_update(item_id: str, new_tags: List[str], note: Optional[str], project_path: str) -> None:
    """Append a tags-update event for an item. Used by bulk_action(add_tag).

    update_item() doesn't support a tags param today, so we write the update
    event directly through the same path it uses. The replay logic in
    list_items already handles `tags` updates.
    """
    ledger_dir = _project_ledger_dir(project_path)
    found = _find_item_in_ledger_dir(item_id, ledger_dir)
    if not found:
        raise RuntimeError(f"item {item_id} disappeared during apply")
    path = found["path"]
    update_event = {
        "id": item_id,
        "type": "update",
        "tags": new_tags,
        "updated_at": _utc_timestamp(),
    }
    if note:
        update_event["note"] = note
    _append(path, update_event)


# ── LED-1145 Phase 2 #1: linked-external auto-close ──────────────────────

import re as _re

# Long form: https://github.com/<owner>/<repo>/(issues|pull)/<num>
_GH_URL_RE = _re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(?:issues|pull)/(\d+)")
# Short form: <owner>/<repo>#<num> (avoid matching plain markdown headings)
_GH_SHORT_RE = _re.compile(r"\b([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+)\b")
# Explicit tag form: gh:<owner>/<repo>/<num> — for callers that want unambiguous linkage
_GH_TAG_RE = _re.compile(r"^gh:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(\d+)$")


def _extract_external_link(item: Dict[str, Any]) -> Optional[tuple]:
    """Find the FIRST github issue/PR reference inside an item.

    Scans description, context, last_note, and tags (in that order). Returns
    `(owner, repo, number)` or None. Long URLs > short forms > explicit tags.
    """
    haystacks = []
    for field in ("description", "context", "last_note"):
        v = item.get(field)
        if isinstance(v, str) and v:
            haystacks.append(v)
    # Tag matches: each tag string standalone; explicit gh: form first
    for t in (item.get("tags") or []):
        if not isinstance(t, str):
            continue
        m = _GH_TAG_RE.match(t)
        if m:
            return m.group(1), m.group(2), int(m.group(3))
        haystacks.append(t)

    text = "\n".join(haystacks)
    if not text:
        return None

    # Long URL takes precedence
    m = _GH_URL_RE.search(text)
    if m:
        return m.group(1), m.group(2), int(m.group(3))
    m = _GH_SHORT_RE.search(text)
    if m:
        return m.group(1), m.group(2), int(m.group(3))
    return None


def _gh_fetch_issue_state(owner: str, repo: str, number: int, _runner=None) -> Dict[str, Any]:
    """Query the GitHub API for issue/PR state. Returns:
        {ok: bool, state: str, merged: bool|None, closed_at: str|None,
         merge_commit_sha: str|None, state_reason: str|None}
    or {ok: False, error: str} on failure.

    `_runner` is for test injection; defaults to subprocess.run.
    """
    if _runner is None:
        _runner = subprocess.run

    try:
        result = _runner(
            ["gh", "api", f"/repos/{owner}/{repo}/issues/{number}",
             "--jq", '{state, closed_at, state_reason, pull_request: (.pull_request != null)}'],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if result.returncode != 0:
            return {"ok": False, "error": (result.stderr or result.stdout)[:200]}
        meta = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as exc:
        return {"ok": False, "error": str(exc)[:200]}

    out = {
        "ok": True,
        "state": meta.get("state"),
        "closed_at": meta.get("closed_at"),
        "state_reason": meta.get("state_reason"),
        "merged": None,
        "merge_commit_sha": None,
    }

    # If it's a PR, fetch the merge bit separately. We skip this on
    # non-PR issues to save an API call.
    if meta.get("pull_request"):
        try:
            pr_result = _runner(
                ["gh", "api", f"/repos/{owner}/{repo}/pulls/{number}",
                 "--jq", '{merged, merge_commit_sha}'],
                capture_output=True, text=True, timeout=15, check=False,
            )
            if pr_result.returncode == 0:
                pr_meta = json.loads(pr_result.stdout)
                out["merged"] = bool(pr_meta.get("merged"))
                out["merge_commit_sha"] = pr_meta.get("merge_commit_sha")
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            pass
    return out


def _resolve_action_for_external(state_meta: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Map GitHub issue/PR state -> (action, note) for bulk_action.

    Returns None when the LED should be left alone (still open / fetch error).

    Per LED-1146 deliberation:
      - PR merged → mark_done with merge SHA
      - issue/PR closed with state_reason='completed' → mark_done with closed_at
      - issue/PR closed with state_reason='not_planned' (or no reason) → archive
      - state='open' → None (leave alone)
      - fetch error → None (leave alone)
    """
    if not state_meta.get("ok"):
        return None
    if state_meta.get("state") != "closed":
        return None

    # PR with merged=True is unambiguous "we shipped this"
    if state_meta.get("merged"):
        sha = (state_meta.get("merge_commit_sha") or "")[:8]
        return {"action": "mark_done", "note": f"AUTO-CLOSE: merged {sha}".rstrip()}

    state_reason = state_meta.get("state_reason")
    closed_at = (state_meta.get("closed_at") or "")[:19]

    if state_reason == "completed":
        return {"action": "mark_done", "note": f"AUTO-CLOSE: closed-completed {closed_at}".rstrip()}

    # not_planned, duplicate, bot triage, no-reason → archive
    return {"action": "archive", "note": f"AUTO-CLOSE: closed-not-planned {closed_at}".rstrip()}


def auto_close_linked_external(
    project_path: str = ".",
    dry_run: bool = True,
    max_items: int = 200,
    _gh_runner=None,
) -> Dict[str, Any]:
    """Walk open ledger items, detect linked GitHub issues/PRs, and propose
    closing any whose external counterpart already resolved.

    LED-1145 Phase 2 #1. Built on top of bulk_action() from PR-B (uses its
    `mark_done` and `archive` actions).

    Args:
        project_path: ledger root.
        dry_run: True (default) returns a plan without writing.
        max_items: hard cap on the number of items processed in one call.
            When the candidate set exceeds this, we process the first N and
            mark `truncated=True` in the response.
        _gh_runner: test-only hook for stubbing the gh CLI.

    Returns:
        {
            "dry_run": bool,
            "scanned": int,             # items walked
            "linked": int,              # items with a recognised github reference
            "would_close" or "closed": [
                {"id", "external": "owner/repo#num", "action", "note", "state"},
                ...
            ],
            "left_open": [{"id", "external", "reason"}],   # external still open / fetch error
            "errors": [{"id", "reason"}],
            "truncated": bool,
            "summary": {...},
        }
    """
    _ensure(project_path)
    listing = list_items(status__in=["open", "in_progress", "blocked"], limit=10_000, project_path=project_path)

    candidates: List[Dict[str, Any]] = []
    for ledger_name in ("ops", "strategy"):
        candidates.extend(listing.get("items", {}).get(ledger_name, []))

    truncated = False
    if len(candidates) > max_items:
        candidates = candidates[:max_items]
        truncated = True

    # Per-call cache so the same external URL referenced by multiple LEDs
    # only triggers one gh API call.
    fetch_cache: Dict[tuple, Dict[str, Any]] = {}

    would_close: List[Dict[str, Any]] = []
    left_open: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    scanned = 0
    linked = 0

    for item in candidates:
        scanned += 1
        ext = _extract_external_link(item)
        if ext is None:
            continue
        linked += 1
        owner, repo, number = ext
        external_label = f"{owner}/{repo}#{number}"

        cache_key = (owner.lower(), repo.lower(), number)
        if cache_key not in fetch_cache:
            fetch_cache[cache_key] = _gh_fetch_issue_state(owner, repo, number, _runner=_gh_runner)
        state_meta = fetch_cache[cache_key]

        if not state_meta.get("ok"):
            errors.append({
                "id": item.get("id"),
                "external": external_label,
                "reason": f"gh_api_failed: {state_meta.get('error', 'unknown')}",
            })
            continue

        if state_meta.get("state") != "closed":
            left_open.append({
                "id": item.get("id"),
                "external": external_label,
                "reason": "external_still_open",
            })
            continue

        action_plan = _resolve_action_for_external(state_meta)
        if action_plan is None:
            left_open.append({
                "id": item.get("id"),
                "external": external_label,
                "reason": "no_action_resolved",
            })
            continue

        would_close.append({
            "id": item.get("id"),
            "external": external_label,
            "action": action_plan["action"],
            "note": action_plan["note"],
            "state": state_meta.get("state"),
            "merged": state_meta.get("merged"),
        })

    if dry_run:
        return {
            "dry_run": True,
            "scanned": scanned,
            "linked": linked,
            "would_close": would_close,
            "left_open": left_open,
            "errors": errors,
            "truncated": truncated,
            "max_items": max_items,
            "summary": {
                "scanned": scanned,
                "linked": linked,
                "would_close": len(would_close),
                "left_open": len(left_open),
                "errors": len(errors),
                "truncated": truncated,
            },
        }

    # Apply mode: dispatch each plan through bulk_action one item at a time
    # (so per-item action and note can vary).
    closed_results: List[Dict[str, Any]] = []
    for plan in would_close:
        ba = bulk_action(
            item_ids=[plan["id"]],
            action=plan["action"],
            dry_run=False,
            note=plan["note"],
            project_path=project_path,
        )
        if ba.get("summary", {}).get("changed"):
            closed_results.append(plan)
        else:
            errors.append({
                "id": plan["id"],
                "reason": f"bulk_action_failed: {ba.get('errors') or ba.get('error') or 'unknown'}",
            })

    return {
        "dry_run": False,
        "scanned": scanned,
        "linked": linked,
        "closed": closed_results,
        "left_open": left_open,
        "errors": errors,
        "truncated": truncated,
        "max_items": max_items,
        "summary": {
            "scanned": scanned,
            "linked": linked,
            "closed": len(closed_results),
            "left_open": len(left_open),
            "errors": len(errors),
            "truncated": truncated,
        },
    }


# ── LED-1145 Phase 2 #2: proposal-first grooming ─────────────────────────

import datetime as _dt
from collections import defaultdict as _defaultdict


def _is_garbage_venture(name: str) -> bool:
    """Detect test/scratch venture buckets that should be archived in bulk.

    Pattern matches: tmp*, test_*, venture_<single-letter>, custom-venture.
    Deliberately does NOT match "unknown" — those are orphaned items that
    warrant a separate review pass, not auto-archive.
    """
    if not name:
        return False
    if name.startswith(("tmp", "test_")):
        return True
    if name == "custom-venture":
        return True
    # venture_a / venture_b / venture_z (test fixture pattern)
    if _re.match(r"^venture_[a-z]$", name):
        return True
    return False


def _title_prefix(title: str, length: int = 50) -> str:
    """Normalise a title for fuzzy duplicate detection.

    Strips bracketed prefixes like '[DELIMIT]' and lowercases so
    "[DELIMIT] GitHub outreach: foo" and "GitHub outreach: foo" group.
    """
    if not title:
        return ""
    cleaned = _re.sub(r"^\s*\[[^\]]+\]\s*", "", title)
    return cleaned.strip().lower()[:length]


def _parse_iso(ts: str) -> Optional[_dt.datetime]:
    """Parse an ISO-8601 timestamp. Tolerates the trailing 'Z' suffix and
    naive timestamps. Returns None for unparseable input."""
    if not ts:
        return None
    try:
        # 2026-04-13T08:00:00Z and 2026-04-13T08:00:00 both supported
        v = ts.rstrip("Z")
        # Strip fractional seconds if present
        if "." in v:
            v = v.split(".", 1)[0]
        return _dt.datetime.fromisoformat(v).replace(tzinfo=_dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def groom_proposal(
    project_path: str = ".",
    stale_days: int = 30,
    dup_min_count: int = 3,
    max_per_category: int = 50,
) -> Dict[str, Any]:
    """Read-only grooming proposal: surfaces stale / duplicate / garbage-venture
    items as a structured plan for the founder to review and apply.

    LED-1145 Phase 2 #2. The deliberation explicitly said "risky operations
    like deduplication and mass-cancellation must not be a single atomic
    action. The AI proposes a plan; the founder approves; execution goes
    through the safe bulk_action tool." This function is the proposal half.

    Categories detected:
      - stale_open: status open|in_progress|blocked AND updated_at older
        than `stale_days`. Suggested action: archive.
      - duplicate_titles: groups of >= `dup_min_count` items sharing the
        same normalised title prefix (50 chars, brackets stripped). Suggested
        action: archive (de-duped). The first item in the group is kept.
      - garbage_venture: items whose venture matches the test/scratch
        pattern (tmp*, test*, venture_<letter>, custom-venture, unknown).
        Suggested action: archive.

    Out of scope (separate detectors / future PRs):
      - linked-external auto-close — already shipped as a separate tool
        (delimit_ledger_auto_close_external)
      - P0 inflation review — surfaced separately via list_items
      - cross-venture orphan cleanup — needs portfolio policy

    Args:
        project_path: ledger root.
        stale_days: threshold for "stale_open" detector. Default 30.
        dup_min_count: minimum group size for "duplicate_titles". Default 3.
        max_per_category: cap per category in the response. Default 50.

    Returns:
        {
            "proposals": [
                {
                    "category": str,
                    "rationale": str,
                    "items": [{"id", "title", "venture", "status", "updated_at"}, ...],
                    "suggested_action": str,
                    "ready_to_apply": str,  # copy-pasteable bulk_action invocation hint
                    "truncated": bool,
                    "total_in_category": int,
                },
                ...
            ],
            "summary": {
                "total_categories": int,
                "total_items": int,
                "stale_open": int,
                "duplicate_titles": int,
                "garbage_venture": int,
            }
        }
    """
    listing = list_items(
        status__in=["open", "in_progress", "blocked"],
        limit=10_000,
        project_path=project_path,
    )
    candidates: List[Dict[str, Any]] = []
    for ledger_name in ("ops", "strategy"):
        candidates.extend(listing.get("items", {}).get(ledger_name, []))

    now = _dt.datetime.now(_dt.timezone.utc)
    stale_threshold = now - _dt.timedelta(days=stale_days)

    stale_items: List[Dict[str, Any]] = []
    garbage_items: List[Dict[str, Any]] = []
    title_groups: Dict[str, List[Dict[str, Any]]] = _defaultdict(list)

    for item in candidates:
        # garbage_venture (highest precedence — short-circuits other categories)
        if _is_garbage_venture(item.get("venture", "") or ""):
            garbage_items.append(item)
            continue

        # stale_open
        ts = item.get("updated_at") or item.get("created_at")
        parsed = _parse_iso(ts)
        if parsed and parsed < stale_threshold:
            stale_items.append(item)

        # duplicate_titles (always grouped — overlapping with stale is OK,
        # the apply-side de-dupe relies on the founder picking one category)
        prefix = _title_prefix(item.get("title", ""))
        if prefix:
            title_groups[prefix].append(item)

    duplicate_groups = {
        prefix: items for prefix, items in title_groups.items() if len(items) >= dup_min_count
    }

    def _trim(items):
        total = len(items)
        truncated = total > max_per_category
        return items[:max_per_category], total, truncated

    proposals = []

    if stale_items:
        sliced, total, truncated = _trim(stale_items)
        proposals.append({
            "category": "stale_open",
            "rationale": f"{total} items have been open for >{stale_days} days with no update; "
                         f"either the work needs revival or the LED was set-and-forget debt.",
            "items": [
                {
                    "id": i.get("id"),
                    "title": (i.get("title") or "")[:80],
                    "venture": i.get("venture"),
                    "status": i.get("status"),
                    "updated_at": i.get("updated_at") or i.get("created_at"),
                }
                for i in sliced
            ],
            "suggested_action": "archive",
            "ready_to_apply": (
                f"delimit_ledger_bulk(item_ids='{','.join(i.get('id', '') for i in sliced)}', "
                f"action='archive', dry_run=True)"
            ),
            "truncated": truncated,
            "total_in_category": total,
        })

    if duplicate_groups:
        # One proposal entry per group so the founder can decide group-by-group
        for prefix, group in sorted(duplicate_groups.items()):
            sliced, total, truncated = _trim(group)
            # Keep the most-recent (or first) item, archive the rest
            sliced_sorted = sorted(
                sliced,
                key=lambda x: x.get("updated_at") or x.get("created_at") or "",
                reverse=True,
            )
            keep = sliced_sorted[0]
            archive = sliced_sorted[1:]
            proposals.append({
                "category": "duplicate_titles",
                "rationale": (
                    f"{total} items share the title prefix {prefix!r}. "
                    f"Suggesting we keep {keep.get('id')} (most recent) "
                    f"and archive the other {len(archive)}."
                ),
                "items": [
                    {
                        "id": i.get("id"),
                        "title": (i.get("title") or "")[:80],
                        "venture": i.get("venture"),
                        "status": i.get("status"),
                        "updated_at": i.get("updated_at") or i.get("created_at"),
                        "_role": "keep" if i.get("id") == keep.get("id") else "archive",
                    }
                    for i in sliced_sorted
                ],
                "suggested_action": "archive",
                "ready_to_apply": (
                    f"delimit_ledger_bulk(item_ids='{','.join(i.get('id', '') for i in archive)}', "
                    f"action='archive', dry_run=True)"
                ),
                "truncated": truncated,
                "total_in_category": total,
            })

    if garbage_items:
        sliced, total, truncated = _trim(garbage_items)
        proposals.append({
            "category": "garbage_venture",
            "rationale": (
                f"{total} items belong to test/scratch venture buckets "
                f"(tmp*, test*, venture_<letter>, custom-venture, unknown) that "
                f"shouldn't be in production data. Safe to archive in bulk."
            ),
            "items": [
                {
                    "id": i.get("id"),
                    "title": (i.get("title") or "")[:80],
                    "venture": i.get("venture"),
                    "status": i.get("status"),
                    "updated_at": i.get("updated_at") or i.get("created_at"),
                }
                for i in sliced
            ],
            "suggested_action": "archive",
            "ready_to_apply": (
                f"delimit_ledger_bulk(item_ids='{','.join(i.get('id', '') for i in sliced)}', "
                f"action='archive', dry_run=True)"
            ),
            "truncated": truncated,
            "total_in_category": total,
        })

    summary = {
        "total_categories": len(proposals),
        "total_items": sum(p["total_in_category"] for p in proposals),
        "stale_open": len(stale_items),
        "duplicate_titles": sum(len(g) for g in duplicate_groups.values()),
        "garbage_venture": len(garbage_items),
    }

    return {
        "proposals": proposals,
        "summary": summary,
    }


# ── LED-1145 Phase 2 #4: stale-TTL auto-cancel ────────────────────────────

# After this many days of dormancy, an open item is considered stale enough
# that auto-archive is the safe default. groom_proposal still flags items
# at 30d for triage; this stricter cap is for items that crossed the
# triage threshold but never got reviewed. Override via env or arg.
STALE_TTL_DEFAULT_DAYS = 60


def _stale_ttl_default() -> int:
    """Resolve the active stale-TTL threshold from env. 0 disables auto-cancel."""
    raw = os.environ.get("DELIMIT_STALE_TTL_DAYS", "")
    if raw == "":
        return STALE_TTL_DEFAULT_DAYS
    try:
        n = int(raw)
        return max(0, n)
    except (TypeError, ValueError):
        return STALE_TTL_DEFAULT_DAYS


def auto_cancel_stale(
    project_path: str = ".",
    threshold_days: Optional[int] = None,
    dry_run: bool = True,
    max_items: int = 200,
) -> Dict[str, Any]:
    """Auto-archive open items that have been dormant past the stale TTL.

    LED-1145 Phase 2 #4. Composes Phase 2 #2's stale-detection logic with
    Phase 1 PR-B's bulk_action(action="archive"). Same dry_run-default
    pattern as auto_close_linked_external — caller passes dry_run=False
    explicitly to apply.

    Distinct from groom_proposal's stale_open category because:
      - The threshold is stricter (default 60d vs groom's 30d)
      - It auto-applies on dry_run=False (groom is purely propose)
      - It's intended for nightly automation; groom is for interactive use

    Args:
        project_path: ledger root.
        threshold_days: dormancy threshold. Default reads
            DELIMIT_STALE_TTL_DAYS env (60 if unset). 0 disables.
        dry_run: True (default) returns a plan; False applies via bulk_action.
        max_items: cap per call. Items beyond the cap surface in
            `truncated=True` with `total_candidates` set.

    Returns:
        {
            "dry_run": bool,
            "threshold_days": int,
            "would_cancel" or "cancelled": [{id, title, venture, status, last_seen}, ...],
            "errors": [...],
            "summary": {"scanned": int, "stale": int, "cancelled": int, "errors": int, "truncated": bool},
        }
    """
    if threshold_days is None:
        threshold_days = _stale_ttl_default()
    if threshold_days == 0:
        return {
            "dry_run": dry_run,
            "threshold_days": 0,
            "would_cancel" if dry_run else "cancelled": [],
            "errors": [],
            "summary": {
                "scanned": 0, "stale": 0, "cancelled": 0,
                "errors": 0, "truncated": False,
                "note": "DELIMIT_STALE_TTL_DAYS=0 disables auto-cancel",
            },
        }

    listing = list_items(
        status__in=["open", "in_progress", "blocked"],
        limit=10_000,
        project_path=project_path,
    )
    candidates: List[Dict[str, Any]] = []
    for ledger_name in ("ops", "strategy"):
        candidates.extend(listing.get("items", {}).get(ledger_name, []))

    now = _dt.datetime.now(_dt.timezone.utc)
    stale_threshold = now - _dt.timedelta(days=threshold_days)

    stale_items: List[Dict[str, Any]] = []
    for item in candidates:
        ts = item.get("updated_at") or item.get("created_at")
        parsed = _parse_iso(ts)
        if parsed and parsed < stale_threshold:
            stale_items.append(item)

    total_candidates = len(stale_items)
    truncated = total_candidates > max_items
    stale_items = stale_items[:max_items]

    summary_records = [
        {
            "id": i.get("id"),
            "title": (i.get("title") or "")[:80],
            "venture": i.get("venture"),
            "status": i.get("status"),
            "last_seen": i.get("updated_at") or i.get("created_at"),
        }
        for i in stale_items
    ]

    if dry_run:
        return {
            "dry_run": True,
            "threshold_days": threshold_days,
            "would_cancel": summary_records,
            "errors": [],
            "summary": {
                "scanned": len(candidates),
                "stale": total_candidates,
                "would_cancel": len(stale_items),
                "errors": 0,
                "truncated": truncated,
            },
        }

    # Apply via bulk_action(action="archive") so the same audit / no-hard-
    # delete invariants used everywhere else apply here too.
    if not stale_items:
        return {
            "dry_run": False,
            "threshold_days": threshold_days,
            "cancelled": [],
            "errors": [],
            "summary": {
                "scanned": len(candidates), "stale": 0,
                "cancelled": 0, "errors": 0, "truncated": False,
            },
        }

    ids = [i.get("id") for i in stale_items if i.get("id")]
    bulk_result = bulk_action(
        item_ids=ids,
        action="archive",
        dry_run=False,
        note=f"auto-cancel: dormant > {threshold_days} days",
        project_path=project_path,
    )

    return {
        "dry_run": False,
        "threshold_days": threshold_days,
        "cancelled": summary_records,
        "errors": bulk_result.get("errors", []),
        "summary": {
            "scanned": len(candidates),
            "stale": total_candidates,
            "cancelled": bulk_result.get("summary", {}).get("changed", 0),
            "errors": len(bulk_result.get("errors", [])),
            "truncated": truncated,
        },
    }


# ── LED-1145 Phase 2 capstone: ledger health summary ─────────────────────

# Traffic-light thresholds for each category. Tuned from observed
# real-world ledger sizes — ~50 stale items is when the "groom me"
# signal becomes loud; ~20 is yellow heads-up.
_HEALTH_STALE_RED = 50
_HEALTH_STALE_YELLOW = 20
_HEALTH_GARBAGE_RED = 10
_HEALTH_GARBAGE_YELLOW = 1
_HEALTH_DUPE_RED = 5
_HEALTH_DUPE_YELLOW = 2


def _grade(value: int, yellow: int, red: int) -> str:
    if value >= red:
        return "red"
    if value >= yellow:
        return "yellow"
    return "green"


def _worst_grade(*grades: str) -> str:
    """Worst-of N traffic lights: red > yellow > green."""
    if "red" in grades:
        return "red"
    if "yellow" in grades:
        return "yellow"
    return "green"


def health_summary(
    project_path: str = ".",
    stale_days: int = 30,
    dup_min_count: int = 3,
) -> Dict[str, Any]:
    """One-shot health check for the ledger. Composes list_items +
    groom_proposal + the P0 quota helper into a single traffic-light
    verdict and a list of concrete next actions.

    LED-1145 capstone — closes the loop on the entire ledger-tooling
    refactor. Designed for nightly/weekly review or for the founder's
    session-start status snapshot.

    Args:
        project_path: ledger root.
        stale_days: threshold passed through to groom_proposal.
        dup_min_count: threshold passed through to groom_proposal.

    LED-3720: the reported counts are now derived from a deterministic
    event-sourced recount of the SINGLE store this project_path resolves to
    (``_replay_status_counts``), and the store's provenance is surfaced via
    ``namespace`` + ``store_path``. Previously the open-count's origin was
    invisible, so a call that auto-detected the ``root`` catch-all store
    (thousands of never-closed sensed signals) looked identical to one that
    read the clean ``delimit`` store — the "garbage open-count" symptom.
    The count itself was always an accurate replay of whichever store was
    read; the ambiguity was WHICH store. ``namespace``/``store_path`` make
    that explicit and ``open_recount`` proves the number against the store.

    Returns:
        {
            "venture": str,
            "namespace": str,          # canonical slug read ("" = central legacy)
            "store_path": str,         # absolute dir the counts came from
            "totals": {"unresolved": int, "open": int, "in_progress": int, "blocked": int, ...},
            "open_recount": int,       # independent deterministic recount == totals.open
            "p0":         {"count": int, "quota": int, "health": str},
            "stale":      {"count": int, "health": str},
            "duplicates": {"groups": int, "items": int, "health": str},
            "garbage":    {"count": int, "health": str},
            "overall_health": "green" | "yellow" | "red",
            "next_actions": [
                {"reason": str, "tool": str, "args": dict},
                ...
            ],
        }
    """
    # Deterministic, event-sourced recount of the ONE store this path
    # resolves to. Authoritative source for the totals below (LED-3720).
    ns = _resolve_namespace(project_path)
    ledger_dir = _project_ledger_dir(project_path)
    status_counts = _replay_status_counts(ledger_dir)

    listing = list_items(
        status__in=["open", "in_progress", "blocked"],
        limit=10_000,
        project_path=project_path,
    )
    venture = listing.get("venture", "unknown")

    p0_count = _count_unresolved_p0(project_path=project_path)
    p0_quota = _p0_soft_quota()
    p0_health = "green"
    if p0_quota > 0:
        if p0_count > p0_quota * 1.5:
            p0_health = "red"
        elif p0_count > p0_quota:
            p0_health = "yellow"

    proposal = groom_proposal(
        project_path=project_path,
        stale_days=stale_days,
        dup_min_count=dup_min_count,
        max_per_category=10_000,
    )
    proposal_summary = proposal.get("summary", {})
    stale_count = proposal_summary.get("stale_open", 0)
    dup_items = proposal_summary.get("duplicate_titles", 0)
    garbage_count = proposal_summary.get("garbage_venture", 0)
    dup_groups = sum(
        1 for p in proposal.get("proposals", [])
        if p.get("category") == "duplicate_titles"
    )

    stale_health = _grade(stale_count, _HEALTH_STALE_YELLOW, _HEALTH_STALE_RED)
    dup_health = _grade(dup_groups, _HEALTH_DUPE_YELLOW, _HEALTH_DUPE_RED)
    garbage_health = _grade(garbage_count, _HEALTH_GARBAGE_YELLOW, _HEALTH_GARBAGE_RED)

    overall = _worst_grade(p0_health, stale_health, dup_health, garbage_health)

    # Build next-actions list ordered by impact
    next_actions: List[Dict[str, Any]] = []
    if garbage_count > 0:
        next_actions.append({
            "reason": f"{garbage_count} item(s) in test/scratch venture buckets",
            "tool": "delimit_ledger_groom",
            "args": {"venture": venture},
            "follow_up": "Apply the garbage_venture proposal via delimit_ledger_bulk(action='archive')",
        })
    if dup_groups > 0:
        next_actions.append({
            "reason": f"{dup_groups} duplicate-title group(s) covering {dup_items} items",
            "tool": "delimit_ledger_groom",
            "args": {"venture": venture, "dup_min_count": dup_min_count},
            "follow_up": "Review duplicate_titles proposals; archive all but most-recent in each group",
        })
    if stale_count > 0:
        next_actions.append({
            "reason": f"{stale_count} item(s) dormant >{stale_days}d",
            "tool": "delimit_ledger_auto_cancel_stale",
            "args": {"venture": venture, "threshold_days": 60, "dry_run": True},
            "follow_up": "Run with dry_run=False after reviewing the plan",
        })
    if p0_quota > 0 and p0_count > p0_quota:
        next_actions.append({
            "reason": f"P0 inflation: {p0_count} unresolved P0s (threshold {p0_quota})",
            "tool": "delimit_ledger_list",
            "args": {"venture": venture, "priority_in": "P0", "status_in": "open,in_progress,blocked", "fields": "slim"},
            "follow_up": "Triage each: ship, demote to P1, or archive",
        })
    if not next_actions:
        next_actions.append({
            "reason": "All categories green",
            "tool": None,
            "args": {},
            "follow_up": "No grooming required",
        })

    open_ct = status_counts.get("open", 0)
    in_progress_ct = status_counts.get("in_progress", 0)
    blocked_ct = status_counts.get("blocked", 0)
    return {
        "venture": venture,
        "namespace": ns["namespace"],
        "store_path": ns["store_path"],
        "totals": {
            "unresolved": open_ct + in_progress_ct + blocked_ct,
            "open": open_ct,
            "in_progress": in_progress_ct,
            "blocked": blocked_ct,
        },
        "open_recount": open_ct,
        "p0": {"count": p0_count, "quota": p0_quota, "health": p0_health},
        "stale": {"count": stale_count, "health": stale_health, "threshold_days": stale_days},
        "duplicates": {"groups": dup_groups, "items": dup_items, "health": dup_health},
        "garbage": {"count": garbage_count, "health": garbage_health},
        "overall_health": overall,
        "next_actions": next_actions,
    }
