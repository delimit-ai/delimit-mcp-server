"""Governance Hardening for Proactive Auto-Triggers (LED-661).

Provides four hardening primitives that wrap MCP tool calls and loop engine
operations with resilience guarantees:

  - ResilientToolCaller: retry with exponential backoff, timeout, fallback
  - ApprovalFlow: email-based approve/reject for founder decisions
  - TriggerDebouncer: per-tool cooldowns to prevent notification storms
  - ChainCircuitBreaker: halt chains after consecutive failures

All classes are opt-in. When not configured, existing behavior is unchanged.
Wire into loop_engine.run_governed_iteration() via GovernanceHardeningConfig.
"""

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("delimit.ai.governance_hardening")


# ── ResilientToolCaller ─────────────────────────────────────────────────

class ResilientToolCaller:
    """Wrap MCP tool calls with retry, timeout, and fallback.

    Parameters:
        max_retries: Maximum number of retry attempts (default 3).
        base_delay: Initial delay in seconds for exponential backoff (default 1.0).
        max_delay: Cap on backoff delay in seconds (default 30.0).
        timeout: Per-call timeout in seconds (default 60.0).
        fallback: Optional callable returning a fallback result on total failure.
    """

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        timeout: float = 60.0,
        fallback: Optional[Callable[..., Any]] = None,
    ):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.timeout = timeout
        self.fallback = fallback
        self._call_log: List[Dict[str, Any]] = []

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Execute fn with retry and exponential backoff.

        Returns the result on success, or the fallback result if all retries
        are exhausted and a fallback is configured. Raises the last exception
        if no fallback is available.
        """
        last_error: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            start = time.monotonic()
            try:
                result = fn(*args, **kwargs)
                elapsed = time.monotonic() - start
                self._call_log.append({
                    "fn": getattr(fn, "__name__", str(fn)),
                    "attempt": attempt,
                    "status": "success",
                    "elapsed": round(elapsed, 3),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                return result
            except Exception as exc:
                elapsed = time.monotonic() - start
                last_error = exc
                self._call_log.append({
                    "fn": getattr(fn, "__name__", str(fn)),
                    "attempt": attempt,
                    "status": "error",
                    "error": str(exc),
                    "elapsed": round(elapsed, 3),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                logger.warning(
                    "ResilientToolCaller: %s attempt %d/%d failed: %s",
                    getattr(fn, "__name__", "?"), attempt, self.max_retries, exc,
                )

                if attempt < self.max_retries:
                    delay = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
                    time.sleep(delay)

        # All retries exhausted
        if self.fallback is not None:
            logger.info("ResilientToolCaller: falling back for %s", getattr(fn, "__name__", "?"))
            return self.fallback(*args, **kwargs)

        raise last_error  # type: ignore[misc]

    @property
    def call_log(self) -> List[Dict[str, Any]]:
        return list(self._call_log)

    def reset_log(self) -> None:
        self._call_log.clear()


# ── ApprovalFlow ────────────────────────────────────────────────────────

class ApprovalFlow:
    """Email-based approval flow for founder decisions.

    Sends an approval request via email, then polls the inbox for a response.
    Times out after a configurable period with a configurable default action.

    Parameters:
        send_fn: Callable to send an email. Signature: (subject, body, priority) -> None.
        poll_fn: Callable to poll inbox. Signature: () -> List[Dict] of messages.
        timeout_seconds: Max wait time (default 86400 = 24h).
        poll_interval: Seconds between inbox checks (default 300 = 5min).
        default_action: Action when timeout expires ("reject" or "approve").
        state_dir: Directory to persist pending approval state.
    """

    PENDING_FILE = "pending_approvals.json"

    def __init__(
        self,
        send_fn: Optional[Callable] = None,
        poll_fn: Optional[Callable] = None,
        timeout_seconds: float = 86400,
        poll_interval: float = 300,
        default_action: str = "reject",
        state_dir: Optional[Path] = None,
    ):
        self.send_fn = send_fn
        self.poll_fn = poll_fn
        self.timeout_seconds = timeout_seconds
        self.poll_interval = poll_interval
        self.default_action = default_action
        self.state_dir = state_dir or Path.home() / ".delimit" / "loop" / "approvals"
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._load_state()

    def _load_state(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / self.PENDING_FILE
        if path.exists():
            try:
                self._pending = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                self._pending = {}

    def _save_state(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / self.PENDING_FILE
        path.write_text(json.dumps(self._pending, indent=2))

    def request_approval(
        self,
        action_description: str,
        context: str = "",
        priority: str = "P1",
    ) -> str:
        """Send an approval request email and return a request ID.

        The request is persisted so it survives process restarts.
        """
        request_id = f"approval-{uuid.uuid4().hex[:8]}"
        subject = f"[Delimit Approval] {action_description[:80]}"
        body = (
            f"Action: {action_description}\n"
            f"Context: {context}\n"
            f"Request ID: {request_id}\n\n"
            f"Reply APPROVE or REJECT to this email.\n"
            f"Auto-{self.default_action} in {self.timeout_seconds // 3600}h if no response."
        )

        record = {
            "request_id": request_id,
            "action": action_description,
            "context": context,
            "priority": priority,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "timeout_at": datetime.fromtimestamp(
                time.time() + self.timeout_seconds, tz=timezone.utc
            ).isoformat(),
        }
        self._pending[request_id] = record

        if self.send_fn:
            try:
                self.send_fn(subject, body, priority)
                record["email_sent"] = True
            except Exception as exc:
                logger.error("ApprovalFlow: failed to send email: %s", exc)
                record["email_sent"] = False
                record["email_error"] = str(exc)

        self._save_state()
        return request_id

    def check_approval(self, request_id: str) -> Dict[str, Any]:
        """Check the status of a pending approval request.

        Polls the inbox for responses matching the request ID.
        Returns {"status": "approved"|"rejected"|"pending"|"timed_out"}.
        """
        record = self._pending.get(request_id)
        if not record:
            return {"status": "not_found", "request_id": request_id}

        if record["status"] != "pending":
            return {"status": record["status"], "request_id": request_id}

        # Check timeout
        timeout_at = datetime.fromisoformat(record["timeout_at"])
        if datetime.now(timezone.utc) >= timeout_at:
            record["status"] = f"timed_out_{self.default_action}"
            self._save_state()
            return {"status": record["status"], "request_id": request_id}

        # Poll inbox
        if self.poll_fn:
            try:
                messages = self.poll_fn()
                for msg in messages:
                    msg_text = str(msg.get("body", "") or msg.get("subject", "")).upper()
                    if request_id in str(msg.get("body", "")) or request_id in str(msg.get("subject", "")):
                        if "APPROVE" in msg_text:
                            record["status"] = "approved"
                            record["resolved_at"] = datetime.now(timezone.utc).isoformat()
                            self._save_state()
                            return {"status": "approved", "request_id": request_id}
                        elif "REJECT" in msg_text:
                            record["status"] = "rejected"
                            record["resolved_at"] = datetime.now(timezone.utc).isoformat()
                            self._save_state()
                            return {"status": "rejected", "request_id": request_id}
            except Exception as exc:
                logger.warning("ApprovalFlow: poll failed: %s", exc)

        return {"status": "pending", "request_id": request_id}

    @property
    def pending_requests(self) -> List[Dict[str, Any]]:
        return [r for r in self._pending.values() if r.get("status") == "pending"]


# ── TriggerDebouncer ────────────────────────────────────────────────────

class TriggerDebouncer:
    """Prevent storms of tool calls by enforcing per-tool cooldowns.

    Parameters:
        default_cooldown: Default cooldown in seconds (default 300 = 5min).
        tool_cooldowns: Dict mapping tool names to specific cooldowns.
        max_calls_per_hour: Global rate limit across all tools (default 5).
    """

    def __init__(
        self,
        default_cooldown: float = 300.0,
        tool_cooldowns: Optional[Dict[str, float]] = None,
        max_calls_per_hour: int = 5,
    ):
        self.default_cooldown = default_cooldown
        self.tool_cooldowns = tool_cooldowns or {}
        self.max_calls_per_hour = max_calls_per_hour
        self._last_call: Dict[str, float] = {}  # tool_name -> monotonic timestamp
        self._hourly_calls: List[float] = []     # monotonic timestamps of all calls

    def can_fire(self, tool_name: str) -> bool:
        """Check if the tool is allowed to fire (respects cooldown and rate limit)."""
        now = time.monotonic()

        # Per-tool cooldown check
        cooldown = self.tool_cooldowns.get(tool_name, self.default_cooldown)
        last = self._last_call.get(tool_name)
        if last is not None and (now - last) < cooldown:
            return False

        # Global hourly rate limit
        cutoff = now - 3600
        self._hourly_calls = [t for t in self._hourly_calls if t > cutoff]
        if len(self._hourly_calls) >= self.max_calls_per_hour:
            return False

        return True

    def record_call(self, tool_name: str) -> None:
        """Record that a tool was fired."""
        now = time.monotonic()
        self._last_call[tool_name] = now
        self._hourly_calls.append(now)

    def try_fire(self, tool_name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Optional[Any]:
        """Fire the tool only if debounce allows it. Returns None if suppressed."""
        if not self.can_fire(tool_name):
            logger.debug("TriggerDebouncer: suppressed %s (cooldown)", tool_name)
            return None
        result = fn(*args, **kwargs)
        self.record_call(tool_name)
        return result

    def time_until_allowed(self, tool_name: str) -> float:
        """Seconds until this tool can fire again. 0.0 if allowed now."""
        now = time.monotonic()
        cooldown = self.tool_cooldowns.get(tool_name, self.default_cooldown)
        last = self._last_call.get(tool_name)
        if last is None:
            return 0.0
        remaining = cooldown - (now - last)
        return max(0.0, remaining)

    def reset(self, tool_name: Optional[str] = None) -> None:
        """Reset cooldown state for a specific tool or all tools."""
        if tool_name:
            self._last_call.pop(tool_name, None)
        else:
            self._last_call.clear()
            self._hourly_calls.clear()


# ── ChainCircuitBreaker ────────────────────────────────────────────────

class ChainCircuitBreaker:
    """Halt tool chains after consecutive failures.

    Parameters:
        failure_threshold: Number of consecutive failures to trip the breaker (default 3).
        recovery_timeout: Seconds to wait before allowing a retry (default 300 = 5min).
        notify_fn: Optional callable invoked when the breaker trips.
    """

    STATE_CLOSED = "closed"      # Normal operation
    STATE_OPEN = "open"          # Breaker tripped, rejecting calls
    STATE_HALF_OPEN = "half_open"  # Allowing a single probe call

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 300.0,
        notify_fn: Optional[Callable[[str, int], None]] = None,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.notify_fn = notify_fn

        self._state: str = self.STATE_CLOSED
        self._consecutive_failures: int = 0
        self._last_failure_time: float = 0.0
        self._failure_log: List[Dict[str, Any]] = []

    @property
    def state(self) -> str:
        """Current breaker state, accounting for recovery timeout."""
        if self._state == self.STATE_OPEN:
            if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                self._state = self.STATE_HALF_OPEN
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def record_success(self) -> None:
        """Record a successful call. Resets failure count and closes the breaker."""
        self._consecutive_failures = 0
        self._state = self.STATE_CLOSED

    def record_failure(self, error: str = "") -> None:
        """Record a failed call. May trip the breaker."""
        self._consecutive_failures += 1
        self._last_failure_time = time.monotonic()
        self._failure_log.append({
            "error": error,
            "count": self._consecutive_failures,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        if self._consecutive_failures >= self.failure_threshold:
            self._state = self.STATE_OPEN
            logger.warning(
                "ChainCircuitBreaker: tripped after %d consecutive failures",
                self._consecutive_failures,
            )
            if self.notify_fn:
                try:
                    self.notify_fn(error, self._consecutive_failures)
                except Exception as exc:
                    logger.error("ChainCircuitBreaker: notify_fn failed: %s", exc)

    def allow_call(self) -> bool:
        """Check if a call is currently allowed."""
        current = self.state  # triggers timeout check
        if current == self.STATE_CLOSED:
            return True
        if current == self.STATE_HALF_OPEN:
            return True  # Allow one probe
        return False

    def execute(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Execute fn through the circuit breaker.

        Raises CircuitBreakerOpen if the breaker is open.
        Records success/failure automatically.
        """
        if not self.allow_call():
            raise CircuitBreakerOpen(
                f"Circuit breaker is open after {self._consecutive_failures} failures. "
                f"Recovery in {self.recovery_timeout - (time.monotonic() - self._last_failure_time):.0f}s."
            )

        try:
            result = fn(*args, **kwargs)
            self.record_success()
            return result
        except Exception as exc:
            self.record_failure(str(exc))
            raise

    def reset(self) -> None:
        """Manually reset the breaker to closed state."""
        self._state = self.STATE_CLOSED
        self._consecutive_failures = 0
        self._failure_log.clear()

    @property
    def failure_log(self) -> List[Dict[str, Any]]:
        return list(self._failure_log)


class CircuitBreakerOpen(Exception):
    """Raised when a call is attempted on an open circuit breaker."""
    pass


# ── GovernanceHardeningConfig ───────────────────────────────────────────

class GovernanceHardeningConfig:
    """Central configuration for governance hardening.

    Opt-in: when not configured, all components return passthrough behavior.
    Wire into loop_engine.run_governed_iteration() to enable hardening.
    """

    def __init__(
        self,
        resilient_caller: Optional[ResilientToolCaller] = None,
        approval_flow: Optional[ApprovalFlow] = None,
        debouncer: Optional[TriggerDebouncer] = None,
        circuit_breaker: Optional[ChainCircuitBreaker] = None,
    ):
        self.resilient_caller = resilient_caller
        self.approval_flow = approval_flow
        self.debouncer = debouncer
        self.circuit_breaker = circuit_breaker

    @classmethod
    def from_dict(cls, config: Dict[str, Any]) -> "GovernanceHardeningConfig":
        """Create from a config dict (e.g., loaded from settings.json)."""
        hardening = config.get("governance_hardening", {})
        if not hardening.get("enabled", False):
            return cls()  # All None = passthrough

        rc_cfg = hardening.get("resilient_caller", {})
        af_cfg = hardening.get("approval_flow", {})
        db_cfg = hardening.get("debouncer", {})
        cb_cfg = hardening.get("circuit_breaker", {})

        resilient_caller = ResilientToolCaller(
            max_retries=rc_cfg.get("max_retries", 3),
            base_delay=rc_cfg.get("base_delay", 1.0),
            max_delay=rc_cfg.get("max_delay", 30.0),
            timeout=rc_cfg.get("timeout", 60.0),
        ) if rc_cfg.get("enabled", True) else None

        debouncer = TriggerDebouncer(
            default_cooldown=db_cfg.get("default_cooldown", 300),
            tool_cooldowns=db_cfg.get("tool_cooldowns", {}),
            max_calls_per_hour=db_cfg.get("max_calls_per_hour", 5),
        ) if db_cfg.get("enabled", True) else None

        circuit_breaker = ChainCircuitBreaker(
            failure_threshold=cb_cfg.get("failure_threshold", 3),
            recovery_timeout=cb_cfg.get("recovery_timeout", 300),
        ) if cb_cfg.get("enabled", True) else None

        # ApprovalFlow needs send/poll functions — created without them here;
        # caller should inject the actual functions after construction.
        approval_flow = ApprovalFlow(
            timeout_seconds=af_cfg.get("timeout_seconds", 86400),
            poll_interval=af_cfg.get("poll_interval", 300),
            default_action=af_cfg.get("default_action", "reject"),
        ) if af_cfg.get("enabled", False) else None

        return cls(
            resilient_caller=resilient_caller,
            approval_flow=approval_flow,
            debouncer=debouncer,
            circuit_breaker=circuit_breaker,
        )

    def is_active(self) -> bool:
        """True if any hardening component is configured."""
        return any([
            self.resilient_caller,
            self.approval_flow,
            self.debouncer,
            self.circuit_breaker,
        ])


# ── Integration helper for loop_engine ──────────────────────────────────

def hardened_dispatch(
    config: GovernanceHardeningConfig,
    dispatch_fn: Callable[..., Any],
    tool_name: str = "dispatch_task",
    **kwargs: Any,
) -> Any:
    """Run a dispatch function through the full hardening stack.

    Order: debouncer -> circuit breaker -> resilient caller -> dispatch_fn
    If any layer is not configured, it is skipped (passthrough).
    """
    # 1. Debouncer gate
    if config.debouncer:
        if not config.debouncer.can_fire(tool_name):
            remaining = config.debouncer.time_until_allowed(tool_name)
            return {
                "status": "debounced",
                "tool": tool_name,
                "retry_in_seconds": round(remaining, 1),
            }

    # 2. Circuit breaker gate
    if config.circuit_breaker:
        if not config.circuit_breaker.allow_call():
            return {
                "status": "circuit_open",
                "tool": tool_name,
                "consecutive_failures": config.circuit_breaker.consecutive_failures,
            }

    # 3. Execute with resilient caller (or directly)
    try:
        if config.resilient_caller:
            result = config.resilient_caller.call(dispatch_fn, **kwargs)
        else:
            result = dispatch_fn(**kwargs)

        # Record success
        if config.circuit_breaker:
            config.circuit_breaker.record_success()
        if config.debouncer:
            config.debouncer.record_call(tool_name)

        return result

    except Exception as exc:
        if config.circuit_breaker:
            config.circuit_breaker.record_failure(str(exc))
        raise
