"""LED-2268 P0 Phase 0.1 — gateway-side tenant API key validator.

The dashboard at app.delimit.ai (`/dashboard/api-keys`) issues per-user
keys with the `dlmt_<43-char-base64url>` shape. Only the sha256 of the
plaintext is stored — see supabase migration 034 + lib/user-api-keys.ts.

This module owns the gateway side of that contract:
  - parse `Authorization: ApiKey dlmt_xxx` from an HTTP header
  - sha256-hash the plaintext
  - look up the hash in `user_api_keys` via service-role Supabase REST
  - return `{user_id, scope, key_id}` for a live (non-revoked) match
  - return None for anything else (bad shape, no match, revoked, etc.)

Phase 0.1 stays minimal on purpose:
  - no `last_used_at` write (deferred — adds a write per call; Phase 0.2)
  - no cache (every call hits Supabase; fine at current volume)
  - no JWT, no rotation grace period — soft-delete is hard once set

Phase 0.2 will add tenant-scoped data routing (per-user data root under
~/.delimit/tenants/<user_id>/); this module only resolves identity.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional, TypedDict

logger = logging.getLogger("delimit.tenant_auth")

# Process-local counter for failed last_used_at PATCH writes. Lets
# operators (and future /heartbeats-style health surfaces) see whether
# the audit-write fire-and-forget is silently dropping a sustained
# burst — debug log on every error is too quiet to notice in journalctl
# during a Supabase outage. Reset only on process restart by design.
_last_used_dropped_count = 0
_last_used_dropped_lock = threading.Lock()
# Log at INFO every Nth drop so a sustained outage surfaces without
# flooding the journal on transient blips. First drop is also INFO so
# the first sign of trouble is visible.
_LAST_USED_DROP_LOG_EVERY = 10


def get_last_used_dropped_count() -> int:
    """How many last_used_at PATCH writes have been dropped since process start.

    Read-only; intended for /heartbeats, future metrics endpoints, and
    operational tooling. NOT a security signal — dropped writes don't
    affect auth correctness, only audit completeness.
    """
    with _last_used_dropped_lock:
        return _last_used_dropped_count


class TenantIdentity(TypedDict):
    """Resolved tenant identity for a presented API key."""
    user_id: str
    scope: str
    key_id: str


# The plaintext shape issued by lib/user-api-keys.ts is `dlmt_` + 43
# base64url chars (32 random bytes encoded). Reject anything that doesn't
# fit before hashing — saves a Supabase round-trip on malformed input.
_KEY_PREFIX = "dlmt_"
_KEY_PLAINTEXT_LEN_MIN = len(_KEY_PREFIX) + 32  # be lenient on lower bound
_KEY_PLAINTEXT_LEN_MAX = len(_KEY_PREFIX) + 128  # cap to defeat absurd inputs


def parse_auth_header(header: str) -> Optional[tuple[str, str]]:
    """Parse `Authorization` into (scheme, token).

    Recognizes two schemes:
      - `Bearer <token>` — existing shared-bearer pattern (founder/system)
      - `ApiKey <plaintext>` — per-user tenant key (this module's domain)

    Returns (scheme_lowercase, token) on match, None on anything else.
    Caller decides which scheme is acceptable for which endpoint.
    """
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2:
        return None
    scheme, token = parts[0].strip().lower(), parts[1].strip()
    if scheme in ("bearer", "apikey") and token:
        return (scheme, token)
    return None


def _hash_key(plaintext: str) -> str:
    """sha256(plaintext) as lowercase hex — matches lib/user-api-keys.ts."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _looks_like_tenant_key(plaintext: str) -> bool:
    """Cheap shape check before we bother Supabase."""
    if not plaintext.startswith(_KEY_PREFIX):
        return False
    n = len(plaintext)
    return _KEY_PLAINTEXT_LEN_MIN <= n <= _KEY_PLAINTEXT_LEN_MAX


def validate_api_key(plaintext: str) -> Optional[TenantIdentity]:
    """Resolve `dlmt_xxx` plaintext to a tenant identity, or None.

    Returns None for: malformed input, no Supabase config, network
    failure, no row matched, row marked revoked. Caller treats None as
    "unauthorized" — never leak why specifically.

    This function is intentionally synchronous + fire-and-forget on
    errors. Logs them at debug level. Production audit comes from the
    request-log layer (each endpoint logs the resolved user_id, not
    the validator).
    """
    if not _looks_like_tenant_key(plaintext):
        return None

    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not service_key:
        # If the gateway host hasn't been configured for Supabase, tenant
        # auth simply doesn't work — the shared-bearer path stays intact.
        logger.debug("validate_api_key: supabase env not configured")
        return None

    key_hash = _hash_key(plaintext)
    # Active-only lookup: the partial index `idx_user_api_keys_active_hash`
    # makes this O(log n) and gauarantees revoked keys never match.
    url = (
        f"{supabase_url}/rest/v1/user_api_keys"
        f"?select=id,user_id,scope"
        f"&key_hash=eq.{urllib.parse.quote(key_hash, safe='')}"
        f"&revoked_at=is.null"
        f"&limit=1"
    )
    req = urllib.request.Request(
        url,
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        logger.debug("validate_api_key supabase HTTP %s", getattr(e, "code", "?"))
        return None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        logger.debug("validate_api_key supabase net err: %s", e)
        return None

    try:
        rows = json.loads(body)
    except json.JSONDecodeError:
        logger.debug("validate_api_key non-json response")
        return None
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[0]
    if not isinstance(row, dict):
        return None
    user_id = row.get("user_id") or ""
    if not user_id:
        return None
    key_id = str(row.get("id") or "")
    # Phase 0.2: fire-and-forget last_used_at write. Lets operators see
    # "this key was actually used in the last N hours" in the dashboard
    # API-keys list, which is important for rotation hygiene (you can
    # tell which keys are dead before deciding what to revoke).
    # Backgrounded so the validate path stays as fast as it was in 0.1.
    if key_id:
        _fire_last_used_update(supabase_url, service_key, key_id)
    return TenantIdentity(
        user_id=str(user_id),
        scope=str(row.get("scope") or ""),
        key_id=key_id,
    )


def _fire_last_used_update(supabase_url: str, service_key: str, key_id: str) -> None:
    """Background-thread PATCH to bump last_used_at on a successful validate.

    Errors are swallowed; the validate path NEVER blocks on this and the
    foreground response is unaffected. The point is best-effort audit
    signal, not authorization.

    The thread is daemonised so a hung Supabase call can't keep the
    process alive past shutdown.
    """
    def _patch():
        try:
            url = (
                f"{supabase_url.rstrip('/')}/rest/v1/user_api_keys"
                f"?id=eq.{urllib.parse.quote(key_id, safe='')}"
            )
            body = json.dumps({
                "last_used_at": datetime.now(timezone.utc).isoformat(),
            }).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=body,
                method="PATCH",
                headers={
                    "apikey": service_key,
                    "Authorization": f"Bearer {service_key}",
                    "Content-Type": "application/json",
                    # Prefer: return=minimal — we don't need the row back.
                    "Prefer": "return=minimal",
                },
            )
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception as e:  # noqa: BLE001 — fire-and-forget; never raise
            # Bump the process-local dropped-write counter and log at
            # INFO every Nth drop (plus the first). Lets a sustained
            # outage surface in journalctl without spam on blips.
            global _last_used_dropped_count
            with _last_used_dropped_lock:
                _last_used_dropped_count += 1
                count = _last_used_dropped_count
            if count == 1 or count % _LAST_USED_DROP_LOG_EVERY == 0:
                logger.info(
                    "last_used_at update dropped (cum_dropped=%d): %s",
                    count, e,
                )
            else:
                logger.debug(
                    "last_used_at update dropped (cum_dropped=%d): %s",
                    count, e,
                )

    t = threading.Thread(target=_patch, daemon=True, name="delimit-last-used-update")
    t.start()


def authenticate(
    header: str,
    shared_bearer: str = "",
    impersonation_header: str = "",
) -> Optional[dict]:
    """End-to-end auth resolver for an HTTP request.

    Returns a dict describing the resolved identity, or None if the
    request should be rejected. Three accepted-request outcomes:

      - `{"auth_mode": "bearer", "is_tenant_scoped": False}` — shared-
        bearer match WITHOUT impersonation. Founder/system access to
        the shared `~/.delimit/` view. No user_id field present.
      - `{"auth_mode": "bearer", "is_tenant_scoped": True, "user_id":
        ..., "scope": "", "key_id": "bearer-impersonation"}` — shared
        bearer match WITH a valid impersonation header. The trusted
        BFF/system is acting on behalf of a specific tenant (LED-2268
        Phase 0.5a, lets the Vercel dashboard read/write tenant data
        on behalf of a NextAuth-authenticated user without the user
        ever exposing their plaintext API key to the BFF).
      - `{"auth_mode": "apikey", "is_tenant_scoped": True, "user_id":
        ..., "scope": ..., "key_id": ...}` — tenant key match.

    Trust model: the shared bearer is held only by a SMALL set of
    trusted clients (Vercel BFF + the gateway host). If it leaks, the
    blast radius is already total (founder-class access to everything
    the gateway serves). The impersonation header just lets that
    bearer be more granular per-request; it does NOT grant access the
    bearer didn't already have.

    Order: Bearer first (cheap string compare), then ApiKey (Supabase
    round-trip). A request can only present one Authorization header,
    so the order is which-scheme-wins-when-the-shape-fits.
    """
    parsed = parse_auth_header(header)
    if not parsed:
        return None
    scheme, token = parsed
    if scheme == "bearer":
        if not shared_bearer or token != shared_bearer:
            return None
        # Phase 0.5a — optional tenant impersonation. If the BFF/system
        # presented a tenant header AND it sanitises to a valid segment,
        # treat as tenant-scoped under that user_id. Validate via the
        # SAME sanitiser tenant_paths uses for filesystem routing so the
        # downstream code sees a consistent identity.
        if impersonation_header:
            # Lazy import to avoid circular: tenant_paths only needed when
            # impersonation is actually requested.
            from . import tenant_paths
            seg = tenant_paths.safe_user_segment(impersonation_header)
            if seg is None:
                # Header was present but garbage. Reject the request
                # entirely rather than silently falling back to shared
                # scope — a confused BFF surfacing here is exactly the
                # class of bug that header validation should catch.
                logger.info(
                    "authenticate: bearer + invalid impersonation header rejected: %r",
                    impersonation_header[:64],
                )
                return None
            # We pass the RAW header value (not the sanitised segment)
            # downstream so callers see the same user_id shape as the
            # ApiKey path. tenant_paths.safe_user_segment runs again
            # inside tenant_data_root for actual fs routing.
            return {
                "auth_mode": "bearer",
                "is_tenant_scoped": True,
                "user_id": impersonation_header,
                "scope": "",
                "key_id": "bearer-impersonation",
            }
        return {"auth_mode": "bearer", "is_tenant_scoped": False}
    if scheme == "apikey":
        identity = validate_api_key(token)
        if identity is None:
            return None
        return {
            "auth_mode": "apikey",
            "is_tenant_scoped": True,
            "user_id": identity["user_id"],
            "scope": identity["scope"],
            "key_id": identity["key_id"],
        }
    return None
