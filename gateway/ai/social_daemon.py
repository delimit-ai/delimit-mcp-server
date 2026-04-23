"""
Social sensing daemon for Delimit.

Runs social discovery scans (X, Reddit, GitHub, Dev.to) on a regular interval.
Deduplicates findings via SQLite cache and emits HTML draft emails for human approval.
Also monitors for direct replies to owned posts (LED-300).

Consensus 123: Part of the continuous sensing loop.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.ai.social_daemon")

# ── Vertex AI credentials (prefer ADC from gcloud auth) ─────────────
_adc_path = str(Path.home() / ".config" / "gcloud" / "application_default_credentials.json")
if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") and os.path.exists(_adc_path):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _adc_path
if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
    os.environ["GOOGLE_CLOUD_PROJECT"] = "jamsons"

# ── Configuration ────────────────────────────────────────────────────
# Default to 15 minutes (900 seconds)
SCAN_INTERVAL = int(os.environ.get("DELIMIT_SOCIAL_SCAN_INTERVAL", "900"))
MAX_CONSECUTIVE_FAILURES = 3

# Retry config: exponential backoff (5s, 15s, 45s)
RETRY_DELAYS = [5, 15, 45]
MAX_RETRIES = len(RETRY_DELAYS)

ALERTS_DIR = Path.home() / ".delimit" / "alerts"
ALERT_FILE = ALERTS_DIR / "social_daemon.json"
DAEMON_STATE = Path.home() / ".delimit" / "social_daemon_state.json"
OWNER_ACTION_SUMMARY = Path.home() / ".delimit" / "owner_action_summary.json"

class SocialDaemonState:
    """Thread-safe state for the social sensing daemon."""

    def __init__(self):
        self.running = False
        self.last_scan: Optional[str] = None
        self.targets_found: int = 0
        self.consecutive_failures: int = 0
        self.total_scans: int = 0
        self.stopped_reason: Optional[str] = None
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # Scan stats for compact output
        self.last_scan_stats: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            result = {
                "running": self.running,
                "last_scan": self.last_scan,
                "targets_found": self.targets_found,
                "consecutive_failures": self.consecutive_failures,
                "total_scans": self.total_scans,
                "stopped_reason": self.stopped_reason,
                "scan_interval_seconds": SCAN_INTERVAL,
            }
            if self.last_scan_stats:
                result["last_scan_stats"] = self.last_scan_stats
            return result

    def record_success(self, found: int, stats: Optional[Dict[str, Any]] = None):
        with self._lock:
            self.consecutive_failures = 0
            self.targets_found += found
            self.total_scans += 1
            self.last_scan = datetime.now(timezone.utc).isoformat()
            if stats:
                self.last_scan_stats = stats

    def record_failure(self) -> int:
        with self._lock:
            self.consecutive_failures += 1
            self.total_scans += 1
            self.last_scan = datetime.now(timezone.utc).isoformat()
            return self.consecutive_failures

_daemon_state = SocialDaemonState()


def _scan_with_retry() -> Dict[str, Any]:
    """Execute scan_targets with exponential backoff retry on failure.

    Retries up to MAX_RETRIES times with delays of 5s, 15s, 45s.
    """
    from ai.social_target import scan_targets, process_targets

    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            targets = []
            # Use broad reddit_scanner (scans 25+ subreddits with relevance scoring)
            try:
                from ai.reddit_scanner import scan_all
                reddit_result = scan_all(sort="hot", limit_per_sub=10)
                reddit_targets = reddit_result.get("targets", [])
                # Sort by engagement (score + comments), take top 5 for drafts
                MAX_REDDIT_DRAFTS = 5
                eligible = [rt for rt in reddit_targets if rt.get("priority") in ("high", "medium")]
                eligible.sort(key=lambda t: (t.get("score", 0) or 0) + (t.get("num_comments", 0) or 0) * 2, reverse=True)
                top_ids = set(id(rt) for rt in eligible[:MAX_REDDIT_DRAFTS])
                for rt in reddit_targets:
                    if id(rt) in top_ids:
                        rt.setdefault("classification", "reply")
                    else:
                        rt.setdefault("classification", "skip")
                    rt.setdefault("platform", "reddit")
                    rt.setdefault("venture", "delimit")
                    rt.setdefault("fingerprint", f"reddit:{rt.get('id', '')}")
                targets.extend(reddit_targets)
                logger.info("Reddit broad scan: %d targets", len(reddit_targets))
            except Exception as reddit_err:
                logger.warning("Reddit broad scan failed: %s", reddit_err)

            # Also run venture-based scan for non-Reddit platforms
            try:
                other_targets = scan_targets(platforms=["x", "github", "hn", "devto"])
                targets.extend(other_targets)
            except Exception as other_err:
                logger.warning("Venture scan failed: %s", other_err)

            return {"targets": targets, "attempt": attempt + 1}
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES:
                delay = RETRY_DELAYS[attempt]
                logger.warning(
                    "Scan attempt %d/%d failed: %s. Retrying in %ds...",
                    attempt + 1, MAX_RETRIES + 1, e, delay,
                )
                # Use stop_event.wait so we can be interrupted during retry sleep
                if _daemon_state._stop_event.wait(timeout=delay):
                    # Daemon was stopped during retry
                    raise
            else:
                logger.error(
                    "All %d scan attempts failed. Last error: %s",
                    MAX_RETRIES + 1, e,
                )
                raise


def _build_compact_summary(targets: List[Dict], processed: Dict) -> Dict[str, Any]:
    """Build a compact scan summary instead of returning all 322 posts.

    Returns summary counts + only new high-priority posts.
    """
    # Separate high-priority from regular targets
    high_priority = [
        t for t in targets
        if not t.get("error")
        and t.get("relevance_score", 0) > 0.8
    ]
    medium_priority = [
        t for t in targets
        if not t.get("error")
        and 0.3 < t.get("relevance_score", 0) <= 0.8
    ]
    auto_ledger = [t for t in targets if t.get("auto_ledger")]

    # Platform breakdown
    platform_counts: Dict[str, int] = {}
    for t in targets:
        if not t.get("error"):
            p = t.get("platform", "unknown")
            platform_counts[p] = platform_counts.get(p, 0) + 1

    # Get cache stats if available
    cache_stats = {}
    try:
        from ai.social_cache import get_scan_stats
        cache_stats = get_scan_stats()
    except Exception:
        pass

    return {
        "summary": {
            "total_new_targets": len([t for t in targets if not t.get("error")]),
            "high_priority": len(high_priority),
            "medium_priority": len(medium_priority),
            "auto_ledger_flagged": len(auto_ledger),
            "platform_breakdown": platform_counts,
            "drafted": len(processed.get("drafted", [])),
            "ledger_items": len(processed.get("ledger_items", [])),
            "owner_actions": len(processed.get("owner_actions", [])),
        },
        "high_priority_targets": [
            {
                "fingerprint": t.get("fingerprint"),
                "subreddit": t.get("subreddit"),
                "post_title": t.get("post_title"),
                "relevance_score": t.get("relevance_score"),
                "canonical_url": t.get("canonical_url"),
                "venture": t.get("venture"),
                "auto_ledger": t.get("auto_ledger", False),
            }
            for t in high_priority[:10]  # Cap at 10 for compact output
        ],
        "cache_stats": cache_stats,
    }


_scan_digest_count_today: int = 0
_scan_digest_last_date: str = ""
_SCAN_DIGEST_MAX_PER_DAY = 4  # Max scan digest emails per day


def _send_scan_digest(compact: Dict, processed: Dict) -> None:
    """Send a digest email summarizing the scan results.

    Only sends if there are REAL actionable items (ready drafts, not placeholders).
    Suppresses digest if nothing actionable to avoid email fatigue.
    Capped at 4 per day to prevent inbox flooding.
    """
    global _scan_digest_count_today, _scan_digest_last_date
    try:
        from ai.notify import send_email

        s = compact.get("summary", {})
        high = s.get("high_priority", 0)
        ledger_items = s.get("ledger_items", 0)
        total = s.get("total_new_targets", 0)
        platforms = s.get("platform_breakdown", {})

        # Count only REAL owner actions (not placeholder drafts)
        owner_actions = [a for a in processed.get("owner_actions", []) if a.get("draft_id")]
        real_owner_actions = len(owner_actions)

        # Count ready drafts only (not placeholders that failed quality check)
        real_drafted = len([d for d in processed.get("drafted", [])
                          if not d.get("suppressed_reason") and not d.get("deduped")])

        # Only send if there's something genuinely actionable
        if high == 0 and real_drafted == 0 and ledger_items == 0 and real_owner_actions == 0:
            return

        # Daily cap — reset counter at midnight
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != _scan_digest_last_date:
            _scan_digest_count_today = 0
            _scan_digest_last_date = today
        if _scan_digest_count_today >= _SCAN_DIGEST_MAX_PER_DAY:
            logger.info("Scan digest daily cap reached (%d/%d). Suppressing.",
                       _scan_digest_count_today, _SCAN_DIGEST_MAX_PER_DAY)
            return
        _scan_digest_count_today += 1

        lines = []
        lines.append(f"Social scan found {total} new targets across {platforms}.")
        lines.append("")

        if high > 0:
            lines.append(f"HIGH PRIORITY: {high} targets need attention")
            for t in compact.get("high_priority_targets", [])[:5]:
                sub = t.get("subreddit", t.get("platform", ""))
                title = t.get("title", t.get("text", ""))[:80]
                url = t.get("url", t.get("canonical_url", ""))
                lines.append(f"  [{sub}] {title}")
                if url:
                    lines.append(f"  {url}")
                lines.append("")

        if real_drafted > 0:
            lines.append(f"DRAFTS: {real_drafted} ready drafts (quality-checked)")
            lines.append("")
            # Include actual draft text for ready drafts
            for action in processed.get("owner_actions", []):
                draft_id = action.get("draft_id", "")
                if not draft_id:
                    continue
                try:
                    from ai.social import list_drafts
                    all_drafts = list_drafts(status="pending")
                    for d in all_drafts:
                        if d.get("draft_id") == draft_id and d.get("quality") in ("ready", "review"):
                            link = action.get("link", "")
                            platform = action.get("platform", "X")
                            lines.append(f"--- {platform} DRAFT ---")
                            if link:
                                lines.append(f"REPLY TO: {link}")
                            lines.append(f"WHY: {action.get('summary', '')[:100]}")
                            lines.append("")
                            lines.append("COPY THIS:")
                            lines.append(d.get("text", ""))
                            lines.append("--- END ---")
                            lines.append("")
                            break
                except Exception:
                    pass

        if ledger_items > 0:
            lines.append(f"LEDGER: {ledger_items} items added to project ledger")
            lines.append("")

        if real_owner_actions > 0:
            lines.append(f"ACTIONS: {real_owner_actions} items need your review")
            lines.append("")

        cache = compact.get("cache_stats", {})
        lines.append(f"Cache: {cache.get('total_cached', 0)} posts tracked, "
                      f"{cache.get('high_relevance', 0)} high relevance")

        send_email(
            message="\n".join(lines),
            subject=f"[SOCIAL] {high} high-pri, {real_drafted} ready drafts, {real_owner_actions} actions",
            event_type="social_digest",
        )
    except Exception as e:
        logger.warning("Failed to send scan digest email: %s", e)


_scan_lock = threading.Lock()

def scan_once() -> Dict[str, Any]:
    """Execute a single social scan cycle and process results (LED-238).

    Uses retry with exponential backoff and returns compact summary.
    Thread-safe: only one scan runs at a time.
    """
    if not _scan_lock.acquire(blocking=False):
        return {"error": "Scan already in progress", "skipped": True}
    try:
        from ai.social_target import process_targets

        # 1. DISCOVER: Scan all platforms (with retry)
        scan_result = _scan_with_retry()
        targets = scan_result["targets"]
        attempt = scan_result["attempt"]
        found = len(targets)

        # 2. ORCHESTRATE: Process discovered targets (LED-238)
        processed = process_targets(targets, draft_replies=True, create_ledger=True)

        # 3. Build compact summary
        compact = _build_compact_summary(targets, processed)

        # 4. Write owner action summary
        OWNER_ACTION_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
        OWNER_ACTION_SUMMARY.write_text(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "targets_found": found,
            "scan_attempt": attempt,
            "owner_actions": len(processed.get("owner_actions", [])),
            "drafted": len(processed.get("drafted", [])),
            "ledger_items": len(processed.get("ledger_items", [])),
            "strategy_items": len(processed.get("strategy_items", [])),
            "compact_summary": compact["summary"],
        }, indent=2) + "\n")

        # 5. Log scan stats
        s = compact["summary"]
        logger.info(
            "Scan complete: %d new targets (%d high-pri, %d med-pri, %d auto-ledger) "
            "in %d attempt(s). Platforms: %s",
            s["total_new_targets"], s["high_priority"], s["medium_priority"],
            s["auto_ledger_flagged"], attempt, s["platform_breakdown"],
        )

        _daemon_state.record_success(found, stats=compact["summary"])

        # 6. Send digest email if there are actionable items
        _send_scan_digest(compact, processed)

        # Return compact output (not all 322 targets)
        return {
            "targets_found": found,
            "scan_attempt": attempt,
            "compact_summary": compact,
        }
    except Exception as e:
        failures = _daemon_state.record_failure()
        logger.error("Social scan failed after retries: %s", e)
        if failures >= MAX_CONSECUTIVE_FAILURES:
            reason = f"3 consecutive social scan failures. Last: {e}"
            _daemon_state.stopped_reason = reason
            _daemon_state.running = False
            _daemon_state._stop_event.set()
        return {"error": str(e), "consecutive_failures": failures}
    finally:
        _scan_lock.release()

def _daemon_loop() -> None:
    """Main scanning loop."""
    logger.info("Social daemon started. Scanning every %d seconds.", SCAN_INTERVAL)

    while not _daemon_state._stop_event.is_set():
        try:
            result = scan_once()
            if "error" in result:
                logger.warning("Scan cycle error: %s", result["error"])
            else:
                summary = result.get("compact_summary", {}).get("summary", {})
                logger.info(
                    "Scan cycle done: %d targets, %d high-pri, cache=%s",
                    result.get("targets_found", 0),
                    summary.get("high_priority", 0),
                    result.get("compact_summary", {}).get("cache_stats", {}).get("total_cached", "?"),
                )
        except Exception as e:
            logger.error("Unexpected error in social daemon loop: %s", e)
            failures = _daemon_state.record_failure()
            if failures >= MAX_CONSECUTIVE_FAILURES:
                break

        _daemon_state._stop_event.wait(timeout=SCAN_INTERVAL)

    _daemon_state.running = False
    logger.info("Social daemon stopped.")

def start_daemon() -> Dict[str, Any]:
    """Start the social daemon in a background thread."""
    if _daemon_state.running:
        return {"status": "already_running", **_daemon_state.to_dict()}

    _daemon_state.running = True
    _daemon_state.stopped_reason = None
    _daemon_state.consecutive_failures = 0
    _daemon_state._stop_event.clear()

    thread = threading.Thread(target=_daemon_loop, name="social-daemon", daemon=True)
    _daemon_state._thread = thread
    thread.start()

    return {"status": "started", **_daemon_state.to_dict()}

def stop_daemon() -> Dict[str, Any]:
    """Stop the social daemon."""
    if not _daemon_state.running:
        return {"status": "not_running", **_daemon_state.to_dict()}

    _daemon_state._stop_event.set()
    _daemon_state.stopped_reason = "manual_stop"
    if _daemon_state._thread:
        _daemon_state._thread.join(timeout=5)
    _daemon_state.running = False

    return {"status": "stopped", **_daemon_state.to_dict()}

def get_daemon_status() -> Dict[str, Any]:
    """Get current daemon status including cache stats."""
    status = _daemon_state.to_dict()
    try:
        from ai.social_cache import get_scan_stats
        status["cache_stats"] = get_scan_stats()
    except Exception:
        pass
    return status

def main():
    """Run as standalone process."""
    import argparse
    parser = argparse.ArgumentParser(description="Delimit social sensing daemon")
    parser.add_argument("--interval", type=int, help="Scan interval in seconds")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    if args.interval:
        global SCAN_INTERVAL
        SCAN_INTERVAL = args.interval

    if args.once:
        scan_once()
        return

    _daemon_state.running = True
    _daemon_loop()

if __name__ == "__main__":
    main()
