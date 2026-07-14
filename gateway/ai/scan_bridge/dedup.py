"""LED-1264: scan-bridge dedup — fingerprint a signal and check the ledger.

Two-stage dedup:

1. Extract a topic fingerprint from the signal — domain/orbit signal
   terms (reuse ``social_capability.fit_floor._extract_topic_fingerprint``
   if available), plus the canonical_url host + first significant path
   segment, plus the leading bracket-prefixed tag (e.g. ``[COMPETITOR
   RELEASE]``) which is a strong topic signal in our scan corpus.

2. Look the fingerprint up against the strategy ledger inside a
   60-day window (any status — open, done, cancelled, blocked,
   archived). If ANY active or recently-closed item matches, skip
   promotion. Per the directive: 60% recall is fine; cost of missing
   a duplicate is one founder-reviewed P2 item.

Skipped duplicates are logged to ``~/.delimit/scan_bridge_dedup.jsonl``
so the founder can audit what the bridge filtered out.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set
from urllib.parse import urlparse

DEDUP_LOG = Path.home() / ".delimit" / "scan_bridge_dedup.jsonl"

# Bracket-prefix tags carried by the scanner (e.g. "[COMPETITOR RELEASE]
# oasdiff …" or "[VENDOR NEWS] …"). These are strong topic signals — when
# present we lift them into the fingerprint as a single canonical token
# so two scans of "oasdiff v1.15.1" + "oasdiff v1.15.2" both share the
# "competitor_release:oasdiff" key.
_BRACKET_PREFIX_RE = re.compile(r"^\s*\[([^\]]{1,40})\]\s*([^\s:.]{1,80})", re.IGNORECASE)

# A trivial path-segment splitter; we just want the first non-empty
# significant chunk (e.g. "oasdiff" from /oasdiff/oasdiff/releases/tag/...).
_SIGNIFICANT_PATH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-.]{1,}")


def _domain_orbit_terms(text: str) -> Set[str]:
    """Best-effort import of fit_floor's topic extractor.

    fit_floor extracts the union of matched Delimit-domain + orbit
    signal terms. If the import fails for any reason (test isolation,
    refactor) we fall back to an empty set — the URL/bracket terms
    below are still load-bearing on their own.
    """
    try:
        from ai.social_capability.fit_floor import _extract_topic_fingerprint
    except Exception:  # pragma: no cover — tolerant fallback
        return set()
    try:
        return set(_extract_topic_fingerprint(text or ""))
    except Exception:  # pragma: no cover
        return set()


def _bracket_prefix_token(snippet: str) -> Optional[str]:
    """Extract a "<tag>:<head_word>" canonical token from a bracketed
    snippet header. Returns None when the snippet doesn't start with
    a recognisable bracket tag.
    """
    if not snippet:
        return None
    m = _BRACKET_PREFIX_RE.match(snippet)
    if not m:
        return None
    tag = re.sub(r"\s+", "_", m.group(1).strip().lower())
    head = m.group(2).strip().lower()
    if not tag or not head:
        return None
    return f"{tag}:{head}"


def _url_terms(canonical_url: str) -> Set[str]:
    """Return host + first significant path segment as canonical tokens."""
    if not canonical_url:
        return set()
    try:
        p = urlparse(canonical_url)
    except Exception:
        return set()
    out: Set[str] = set()
    host = (p.netloc or "").lower().lstrip("www.")
    if host:
        out.add(f"host:{host}")
    # Pull first 1-2 significant path segments. For github.com the first
    # is the org and the second is the repo — both useful as dedup keys.
    segments = [s for s in (p.path or "").split("/") if s]
    for seg in segments[:2]:
        m = _SIGNIFICANT_PATH_RE.search(seg)
        if m:
            out.add(f"seg:{m.group(0).lower()}")
    return out


# ── Idempotency key (repo full-name + release-version-or-none) ────────
#
# The token-overlap dedup below is fuzzy (recall ~60%). For competitor /
# release signals we want a HARD, reliable key so "oasdiff v1.22" never
# creates a second item when *any* oasdiff item is already tracked. The
# canonical key is the source repo's full name (host/org/repo) — release
# version is captured separately in metadata but the dedup key is
# repo-scoped, so we keep ONE canonical "watch" item per competitor repo
# rather than one per release.

# github.com/<org>/<repo>/releases/tag/<ver>  ->  ver
_RELEASE_TAG_RE = re.compile(r"/releases/(?:tag/)?([^/?#]+)")

# Known code-hosting hosts whose first two path segments are ``org/repo``.
# We scope the idempotency key to these so a plain content URL
# (``example.com/blog/post``) does NOT masquerade as a repo — those fall
# back to the fuzzy token dedup instead.
_CODE_HOST_SUFFIXES = (
    "github.com",
    "gitlab.com",
    "bitbucket.org",
    "codeberg.org",
    "sourceforge.net",
    "gitea.com",
)


def _repo_full_name_from_url(canonical_url: str) -> Optional[str]:
    """Return ``host/org/repo`` (lowercased) from a code-host URL, or None.

    Only returns a value when the URL is on a known code-hosting host and
    has an ``org/repo`` path head. Non-repo URLs (a blog post, a bare host)
    return None so the caller falls back to the fuzzy token dedup.
    """
    if not canonical_url:
        return None
    try:
        p = urlparse(canonical_url)
    except Exception:
        return None
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or not any(host == h or host.endswith("." + h) for h in _CODE_HOST_SUFFIXES):
        return None
    segments = [s for s in (p.path or "").split("/") if s]
    if len(segments) < 2:
        return None
    org, repo = segments[0].lower(), segments[1].lower()
    # Strip a trailing ".git" if present.
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not org or not repo:
        return None
    return f"{host}/{org}/{repo}"


def release_version_from_signal(signal: Dict[str, Any]) -> Optional[str]:
    """Best-effort release version from the signal URL, or None.

    Recognises ``/releases/tag/<ver>`` and ``/releases/<ver>``. Version is
    metadata only — it is NOT part of the dedup key (which is repo-scoped).
    """
    url = signal.get("canonical_url") or ""
    m = _RELEASE_TAG_RE.search(url)
    if m:
        ver = m.group(1).strip()
        if ver and ver.lower() != "latest":
            return ver
    return None


def idempotency_key(signal: Dict[str, Any]) -> Optional[str]:
    """Canonical, repo-scoped idempotency key for a scanned signal.

    ``idem:repo:<host/org/repo>``. Returns None when the signal has no
    repo-identifying URL (the caller then relies on token-overlap dedup).
    Deliberately version-AGNOSTIC so a new release of an already-tracked
    competitor does not create a second ledger item.
    """
    repo = _repo_full_name_from_url(signal.get("canonical_url") or "")
    if not repo:
        return None
    return f"idem:repo:{repo}"


def _item_idempotency_keys(item: Dict[str, Any]) -> Set[str]:
    """Recover idempotency keys stored on a ledger item.

    Auto-promoted items carry the key both as a ``idem:repo:*`` tag and in
    ``metadata.signal_ref.idempotency_key``. Older / hand-added items carry
    neither, so we also synthesise one from any URL in the description.
    """
    keys: Set[str] = set()
    for t in item.get("tags") or []:
        ts = str(t)
        if ts.startswith("idem:repo:"):
            keys.add(ts.lower())
    metadata = item.get("metadata") or {}
    signal_ref = metadata.get("signal_ref") or {}
    stored = signal_ref.get("idempotency_key")
    if isinstance(stored, str) and stored:
        keys.add(stored.lower())
    stored_url = signal_ref.get("canonical_url")
    if isinstance(stored_url, str) and stored_url:
        repo = _repo_full_name_from_url(stored_url)
        if repo:
            keys.add(f"idem:repo:{repo}")
    if not keys:
        # Fallback: recover a repo key from any github-style URL in text.
        for field in ("description", "context", "title"):
            val = item.get(field) or ""
            m = re.search(r"https?://[^\s)\"']+", val)
            if m:
                repo = _repo_full_name_from_url(m.group(0))
                if repo:
                    keys.add(f"idem:repo:{repo}")
    return keys


def extract_topic_fingerprint(signal: Dict[str, Any]) -> Set[str]:
    """Return the dedup fingerprint set for a single scanned signal.

    The fingerprint is a SET of canonical tokens. Two signals are
    considered overlapping when their fingerprint sets share at least
    one token. Per the directive: don't be too clever; 60% recall is
    fine.
    """
    snippet = signal.get("content_snippet") or ""
    canonical_url = signal.get("canonical_url") or ""
    rationale = signal.get("rationale") or ""

    tokens: Set[str] = set()
    tokens.update(_domain_orbit_terms(f"{snippet}\n{rationale}"))
    tokens.update(_url_terms(canonical_url))
    bracket = _bracket_prefix_token(snippet)
    if bracket:
        tokens.add(bracket)
    return tokens


# ── Ledger lookup ─────────────────────────────────────────────────────


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _item_fingerprint_tokens(item: Dict[str, Any]) -> Set[str]:
    """Recover a fingerprint token set from a stored ledger item.

    Auto-promoted items carry their fingerprint in
    ``metadata.signal_ref.fingerprint`` as a serialised list. Older /
    hand-added items don't, so we fall back to extracting on-the-fly
    from title + description + tags + context — the same fields a
    reasonable founder would have written about the same topic.
    """
    metadata = item.get("metadata") or {}
    signal_ref = metadata.get("signal_ref") or {}
    stored = signal_ref.get("fingerprint")
    if isinstance(stored, list) and stored:
        return {str(t).lower() for t in stored if t}
    if isinstance(stored, str) and stored:
        # Comma-separated fallback shape.
        return {p.strip().lower() for p in stored.split(",") if p.strip()}

    # Fallback: synthesise a fingerprint from the human text in the item.
    parts = [
        item.get("title") or "",
        item.get("description") or "",
        item.get("context") or "",
    ]
    tags = item.get("tags") or []
    if isinstance(tags, list):
        parts.append(" ".join(str(t) for t in tags))
    text = "\n".join(p for p in parts if p)
    fake_signal = {"content_snippet": text, "canonical_url": "", "rationale": ""}
    return extract_topic_fingerprint(fake_signal)


def _within_window(item: Dict[str, Any], window_days: int, now: datetime) -> bool:
    """Item is in-window if either created_at OR updated_at is within
    ``window_days`` of ``now``.
    """
    cutoff = now - timedelta(days=window_days)
    for field in ("updated_at", "created_at"):
        ts = _parse_iso(item.get(field))
        if ts and ts >= cutoff:
            return True
    return False


def _candidate_strategy_items(window_days: int = 60) -> Iterable[Dict[str, Any]]:
    """Yield strategy items in the dedup window.

    Imports ``ai.ledger_manager.list_items`` lazily so test patches
    targeting that symbol take effect at call time.
    """
    try:
        from ai.ledger_manager import list_items
    except Exception:  # pragma: no cover
        return iter(())
    now = datetime.now(timezone.utc)
    out: list = []
    cursor: Optional[str] = None
    seen_ids: Set[str] = set()
    # Walk pages defensively — most ledgers have <500 strategy items, but
    # paginate if needed.
    for _ in range(20):  # hard cap on pages, prevents accidental infinite loop
        resp = list_items(
            ledger="strategy",
            limit=500,
            cursor=cursor,
            sort="updated_at",
            order="desc",
        )
        items = (resp.get("items") or {}).get("strategy") or []
        if not items:
            break
        for item in items:
            iid = item.get("id") or ""
            if iid and iid in seen_ids:
                continue
            if iid:
                seen_ids.add(iid)
            if _within_window(item, window_days, now):
                out.append(item)
        cursor = resp.get("next_cursor")
        if not cursor:
            break
    return out


def _log_dedup(signal: Dict[str, Any], match: Dict[str, Any], reason: str) -> None:
    try:
        DEDUP_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEDUP_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "signal_fingerprint_id": signal.get("fingerprint"),
                "platform": signal.get("platform"),
                "canonical_url": signal.get("canonical_url"),
                "snippet_head": (signal.get("content_snippet") or "")[:160],
                "matched_item_id": match.get("id"),
                "matched_item_title": (match.get("title") or "")[:160],
                "matched_item_status": match.get("status"),
                "reason": reason,
            }) + "\n")
    except OSError:  # pragma: no cover — best-effort
        pass


def _is_strong_match(shared: Set[str], sig_tokens: Set[str]) -> bool:
    """Return True when the shared-token set is specific enough to
    claim two signals are about the same topic.

    Strict rule (chosen after empirical scan-corpus tuning, see
    LED-1264 memo): a true dedup match requires a SPECIFIC token —
    either a bracket-prefix token (``competitor_release:oasdiff``,
    ``vendor_news:cursor``, ``outreach_state_change:logto-io``) or a
    ``seg:<repo>`` URL path segment. Generic orbit terms ("mcp",
    "claude code", "cursor"), tech-context words, and bare host tokens
    are NOT enough on their own. A signal where two of those overlap
    but neither has a specific identifier is two different things
    that happen to live in the same ecosystem; we want them as
    separate ledger items.

    Per the directive: "don't be too clever — 60% recall on duplicates
    is fine; the cost of missing a duplicate is one founder-reviewed
    P2 ledger item, not a catastrophe." This rule errs toward
    promoting (more recall on the no-dedup decision).
    """
    if not shared:
        return False

    # Bracket-prefix tokens win — they're tightly scoped (vendor name
    # baked in). Excludes host: and seg: which use the same `:` syntax
    # but live in their own buckets below.
    if any(":" in t and not t.startswith("host:") and not t.startswith("seg:") for t in shared):
        return True

    # Specific repo segments win — same repo across two signals is a
    # real dedup. seg: tokens carry the repo name post-host (e.g. for
    # github.com/oasdiff/oasdiff we extract seg:oasdiff). When two
    # signals share that, they're about the same project.
    if any(t.startswith("seg:") for t in shared):
        return True

    return False


def is_duplicate(
    signal: Dict[str, Any],
    *,
    window_days: int = 60,
    candidates: Optional[Iterable[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Return the matching ledger item dict if ``signal`` collides with
    an existing strategy item inside the window; ``None`` otherwise.

    The match rule is intentionally specific — sharing only "mcp" or
    "host:github.com" between two signals isn't enough overlap to call
    them duplicates (that's most of the scan corpus). See
    :func:`_is_strong_match` for the exact rule.

    Parameters
    ----------
    signal:
        Raw scan target dict (the JSONL line shape from
        ``social_targets.jsonl``).
    window_days:
        Age window for "recently closed" items. Default 60 — per the
        directive, avoid re-raising things we explicitly chose not to act
        on within the last 60 days.
    candidates:
        Optional iterable of strategy items to check against. Tests pass
        an explicit list. Production callers omit it and we fetch from
        the live ledger.
    """
    sig_tokens = extract_topic_fingerprint(signal)
    sig_idem = idempotency_key(signal)
    if not sig_tokens and not sig_idem:
        # No tokens and no repo key means we can't make a useful dedup
        # judgement. Treat as non-duplicate; the tight confidence floor is
        # the main quality gate.
        return None

    items = list(candidates) if candidates is not None else list(
        _candidate_strategy_items(window_days=window_days)
    )

    now = datetime.now(timezone.utc)
    for item in items:
        # When candidates were supplied explicitly we still respect the
        # window so unit tests can assert window behaviour without
        # re-implementing the date filter.
        if candidates is not None and not _within_window(item, window_days, now):
            continue

        # 1. Hard idempotency-key match (repo-scoped). This is the reliable
        #    layer: oasdiff v1.22 dedups against ANY existing oasdiff item
        #    regardless of release version or fuzzy token overlap.
        if sig_idem:
            item_keys = _item_idempotency_keys(item)
            if sig_idem in item_keys:
                reason = "idem_open_match" if (item.get("status") == "open") else "idem_recent_match"
                _log_dedup(signal, item, reason)
                return item

        # 2. Fuzzy token-overlap match (fallback for non-repo signals and
        #    older items with no idempotency key).
        item_tokens = _item_fingerprint_tokens(item)
        if not item_tokens:
            continue
        shared = sig_tokens & item_tokens
        if not _is_strong_match(shared, sig_tokens):
            continue
        reason = "open_match" if (item.get("status") == "open") else "recent_match"
        _log_dedup(signal, item, reason)
        return item
    return None
