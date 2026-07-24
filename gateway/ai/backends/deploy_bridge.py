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


def _list_plans(
    app: Optional[str] = None,
    env: Optional[str] = None,
    repo_path: Optional[str] = None,
) -> List[Dict]:
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
            if repo_path and data.get("repo_path") != str(Path(repo_path).expanduser().resolve()):
                continue
            plans.append(data)
        except Exception:
            continue
    return plans


def plan(
    app: str,
    env: str,
    git_ref: Optional[str] = None,
    repo_path: str = "",
    venture: str = "",
) -> Dict[str, Any]:
    """Create a deploy plan."""
    _ensure_dir()
    plan_id = f"PLAN-{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    data = {
        "plan_id": plan_id,
        "app": app,
        "env": env,
        "git_ref": git_ref or "HEAD",
        "repo_path": str(Path(repo_path).expanduser().resolve()) if repo_path else "",
        "venture": venture,
        "status": "planned",
        "created_at": now,
        "updated_at": now,
        "history": [{"status": "planned", "at": now}],
    }
    (DEPLOY_DIR / f"{plan_id}.json").write_text(json.dumps(data, indent=2))
    return data


def status(app: str, env: str, repo_path: str = "") -> Dict[str, Any]:
    """Get latest deploy status for an app+env."""
    plans = _list_plans(app=app, env=env, repo_path=repo_path or None)
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
        "repo_path": str(Path(repo_path).expanduser().resolve()) if repo_path else "",
        "latest_plan": latest["plan_id"],
        "status": latest["status"],
        "git_ref": latest.get("git_ref"),
        "updated_at": latest.get("updated_at"),
        "total_plans": len(plans),
    }


def build(app: str, git_ref: Optional[str] = None, repo_path: str = "") -> Dict[str, Any]:
    """Check if a Dockerfile exists and return build info."""
    repository = Path(repo_path).expanduser().resolve() if repo_path else Path.cwd()
    dockerfile = repository / "Dockerfile"
    if not dockerfile.exists():
        # Check app-specific paths
        for candidate in [
            repository / app / "Dockerfile",
            Path.home() / app / "Dockerfile",
        ]:
            if candidate.exists():
                dockerfile = candidate
                break

    if dockerfile.exists():
        return {
            "app": app,
            "git_ref": git_ref or "HEAD",
            "repo_path": str(repository),
            "dockerfile": str(dockerfile),
            "status": "ready",
            "message": f"Dockerfile found at {dockerfile}. Ready to build.",
        }
    return {
        "app": app,
        "git_ref": git_ref or "HEAD",
        "repo_path": str(repository),
        "status": "no_dockerfile",
        "message": f"No Dockerfile found for {app}. Create one to enable Docker builds.",
    }


def publish(app: str, git_ref: Optional[str] = None, repo_path: str = "") -> Dict[str, Any]:
    """Update latest plan status to published after basic readiness checks."""
    plans = _list_plans(app=app, repo_path=repo_path or None)
    if not plans:
        return {"error": f"No deploy plans found for {app}"}
    latest = plans[0]
    current_status = latest.get("status", "unknown")
    if current_status == "published":
        return {
            "app": app,
            "plan_id": latest["plan_id"],
            "status": "already_published",
            "message": f"Latest plan {latest['plan_id']} is already published.",
        }
    if current_status == "rolled_back":
        return {
            "app": app,
            "plan_id": latest["plan_id"],
            "status": "invalid_state",
            "message": f"Latest plan {latest['plan_id']} was rolled back and cannot be republished.",
            "current_status": current_status,
        }
    if current_status not in {"planned", "built", "verified"}:
        return {
            "app": app,
            "plan_id": latest["plan_id"],
            "status": "invalid_state",
            "message": f"Latest plan {latest['plan_id']} is not ready to publish from status '{current_status}'.",
            "current_status": current_status,
        }

    build_result = build(
        app=app,
        git_ref=git_ref or latest.get("git_ref"),
        repo_path=repo_path or latest.get("repo_path", ""),
    )
    if build_result.get("status") != "ready":
        return {
            "app": app,
            "plan_id": latest["plan_id"],
            "status": "not_ready",
            "message": build_result.get("message", "Build prerequisites are not satisfied."),
            "build_status": build_result.get("status"),
        }

    now = datetime.now(timezone.utc).isoformat()
    latest["status"] = "published"
    latest["updated_at"] = now
    latest["history"].append({"status": "published", "at": now})
    (DEPLOY_DIR / f"{latest['plan_id']}.json").write_text(json.dumps(latest, indent=2))
    return latest


DEPLOY_TARGETS = [
    {"name": "delimit.ai", "url": "https://delimit.ai", "kind": "vercel"},
    {"name": "electricgrill.com", "url": "https://electricgrill.com", "kind": "vercel"},
    {"name": "robotax.com", "url": "https://robotax.com", "kind": "vercel"},
    {"name": "npm:delimit-cli", "url": "https://www.npmjs.com/package/delimit-cli", "kind": "npm"},
    {
        "name": "github:delimit-mcp-server",
        "url": "https://github.com/delimit-ai/delimit-mcp-server",
        "kind": "github",
    },
]


def _check_http_health(url: str, timeout: int = 10) -> Dict[str, Any]:
    """Check HTTP health for a single URL. Returns status, response time, headers."""
    import http.client
    import socket
    import ssl
    import time
    import urllib.request

    result: Dict[str, Any] = {"url": url, "healthy": False}

    class _PinnedHTTPSConnection(http.client.HTTPSConnection):
        """Connect to a resolved+validated public IP rather than re-resolving the
        hostname, so the address we vetted is the address we reach. SNI and cert
        validation still use the original hostname (DNS-rebinding / TOCTOU
        defense)."""

        def connect(self):
            pinned_ip = _resolve_pinned_public_ip(self.host)
            sock = socket.create_connection((pinned_ip, self.port), self.timeout)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)

    class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(_PinnedHTTPSConnection, req, context=self._context)

    class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
        """Revalidate every redirect hop so a vetted public URL cannot bounce
        the request to an internal address (SSRF-via-redirect defense). The
        redirected fetch is itself pinned by _PinnedHTTPSConnection."""

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            _validate_verify_url(newurl, "redirect target")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    try:
        ctx = ssl.create_default_context()
        opener = urllib.request.build_opener(
            _PinnedHTTPSHandler(context=ctx), _ValidatingRedirectHandler()
        )
        req = urllib.request.Request(
            url, method="GET", headers={"User-Agent": "delimit-deploy-verify/1.0"}
        )
        start = time.monotonic()
        with opener.open(req, timeout=timeout) as resp:
            elapsed_ms = round((time.monotonic() - start) * 1000)
            result["status_code"] = resp.status
            result["response_time_ms"] = elapsed_ms
            result["healthy"] = 200 <= resp.status < 400
    except Exception as exc:
        result["error"] = str(exc)
        result["status_code"] = None
        result["response_time_ms"] = None
    return result


def _check_ssl_cert(hostname: str, port: int = 443, warn_days: int = 30) -> Dict[str, Any]:
    """Validate SSL certificate for a hostname. Checks expiry within warn_days."""
    import socket
    import ssl

    result: Dict[str, Any] = {"hostname": hostname, "ssl_valid": False}
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((hostname, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
                if not cert:
                    result["error"] = "No certificate returned"
                    return result
                not_after_str = cert.get("notAfter", "")
                # Python ssl cert dates: 'Mon DD HH:MM:SS YYYY GMT'
                not_after = datetime.strptime(not_after_str, "%b %d %H:%M:%S %Y %Z").replace(
                    tzinfo=timezone.utc
                )
                now = datetime.now(timezone.utc)
                days_remaining = (not_after - now).days
                result["ssl_valid"] = True
                result["expires"] = not_after.isoformat()
                result["days_remaining"] = days_remaining
                result["expiry_warning"] = days_remaining < warn_days
                if days_remaining < warn_days:
                    result["warning"] = (
                        f"SSL certificate expires in {days_remaining} days (threshold: {warn_days})"
                    )
                # Extract issuer for diagnostics
                issuer = dict(x[0] for x in cert.get("issuer", ()))
                result["issuer"] = issuer.get(
                    "organizationName", issuer.get("commonName", "unknown")
                )
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _check_npm_version(expected_version: Optional[str] = None) -> Dict[str, Any]:
    """Check the published npm version of delimit-cli."""
    import subprocess

    result: Dict[str, Any] = {"package": "delimit-cli", "healthy": False}
    try:
        proc = subprocess.run(
            ["npm", "view", "delimit-cli", "version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode == 0:
            published = proc.stdout.strip()
            result["published_version"] = published
            result["healthy"] = True
            if expected_version:
                result["expected_version"] = expected_version
                result["version_match"] = published == expected_version
                if published != expected_version:
                    result["warning"] = (
                        f"Version mismatch: published={published}, expected={expected_version}"
                    )
        else:
            result["error"] = proc.stderr.strip() or "npm view returned non-zero"
    except FileNotFoundError:
        result["error"] = "npm not found on PATH"
    except subprocess.TimeoutExpired:
        result["error"] = "npm view timed out after 15s"
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _extract_hostname(url: str) -> str:
    """Extract hostname from a URL."""
    from urllib.parse import urlparse

    return urlparse(url).hostname or ""


def _is_non_public_address(address) -> bool:
    """True when an ipaddress object is anything other than a routable public host."""
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    )


def _resolve_pinned_public_ip(hostname: str) -> str:
    """Resolve a hostname and return one public IP to connect to, failing closed.

    Used at connection time so the address we validated is the exact address we
    connect to — closing the re-resolution gap between validation and fetch
    (DNS-rebinding / TOCTOU defense). Raises if resolution fails or ANY resolved
    address is non-public.
    """
    import ipaddress
    import socket

    infos = socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)
    chosen = None
    for info in infos:
        raw = info[4][0]
        address = ipaddress.ip_address(raw)
        if _is_non_public_address(address):
            raise ValueError(f"{hostname} resolves to a non-public IP address ({raw})")
        if chosen is None:
            chosen = raw
    if chosen is None:
        raise ValueError(f"{hostname} did not resolve to a usable address")
    return chosen


def _validate_verify_url(url: str, label: str) -> str:
    """Reject verification targets that could turn the tool into an SSRF primitive.

    Beyond the URL-string checks (https, no credentials, no local hostname,
    no non-public IP literal) this resolves DNS hostnames and requires EVERY
    resolved address to be public. A hostname that resolves to a private,
    loopback, link-local (e.g. cloud-metadata 169.254.169.254), reserved, or
    multicast address is rejected, and an unresolvable hostname fails closed —
    a name we cannot vet is never fetched. (LED-3852 merge-review finding.)
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    if not isinstance(url, str):
        raise ValueError(f"{label} must be a URL string")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"{label} must use https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{label} must not contain credentials")
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    if not hostname:
        raise ValueError(f"{label} must contain a hostname")
    if (
        hostname == "localhost"
        or "." not in hostname
        or hostname.endswith((".localhost", ".local", ".internal"))
    ):
        raise ValueError(f"{label} targets a local hostname")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_non_public_address(literal):
            raise ValueError(f"{label} targets a non-public IP address")
        return url
    # DNS hostname: resolve and reject if ANY resolved address is non-public.
    # A hostname that fails to resolve is not an SSRF risk — the actual fetch
    # resolves it the same way and simply fails — so a resolution error is not
    # fatal here; only a successful resolution to an internal address is.
    try:
        infos = socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return url
    for info in infos:
        raw = info[4][0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if _is_non_public_address(address):
            raise ValueError(f"{label} resolves to a non-public IP address ({raw})")
    return url


def _configured_verify_targets(
    app: str,
    repo_path: str = "",
    target_urls: Optional[List[str]] = None,
) -> List[Dict[str, str]]:
    """Resolve only the requested app's verification targets.

    Explicit URLs win. Repositories may provide
    ``.delimit/deploy-targets.json`` with ``{"apps": {app: [...]}}``.
    The built-in registry is retained only as an exact-name compatibility
    fallback; a named app never expands to every global target.
    """
    if target_urls:
        targets: List[Dict[str, str]] = []
        for index, url in enumerate(target_urls):
            url = _validate_verify_url(url, f"target_urls[{index}]")
            targets.append({"name": app or _extract_hostname(url), "url": url, "kind": "vercel"})
        return targets

    if repo_path:
        config_path = Path(repo_path).expanduser().resolve() / ".delimit" / "deploy-targets.json"
        try:
            raw_config = json.loads(config_path.read_text())
        except FileNotFoundError:
            raw_config = {}
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid deploy target config at {config_path}: {exc}") from exc
        apps = raw_config.get("apps", raw_config) if isinstance(raw_config, dict) else {}
        configured = apps.get(app, []) if isinstance(apps, dict) and app else []
        if configured:
            if not isinstance(configured, list):
                raise ValueError(f"Deploy targets for {app!r} must be a list")
            targets = []
            for index, item in enumerate(configured):
                if isinstance(item, str):
                    item = {"url": item}
                if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                    raise ValueError(f"Deploy target {index} for {app!r} must contain a URL")
                url = item["url"]
                url = _validate_verify_url(url, f"Deploy target {index} for {app!r}")
                targets.append(
                    {
                        "name": str(item.get("name") or app),
                        "url": url,
                        "kind": str(item.get("kind") or "vercel"),
                    }
                )
            return targets

    if app:
        app_key = app.casefold()
        return [target for target in DEPLOY_TARGETS if target["name"].casefold() == app_key]
    return list(DEPLOY_TARGETS)


def verify(
    app: str,
    env: str,
    git_ref: Optional[str] = None,
    repo_path: str = "",
    target_urls: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Verify deployment health with real HTTP checks, SSL validation, and npm version.

    Checks only the requested app's configured deployment targets for:
    - HTTP 2xx reachability and response time
    - SSL certificate validity (warns if expiring within 30 days)
    - npm published version (for npm targets)

    Also cross-references local deploy plan status when available.
    """
    now = datetime.now(timezone.utc).isoformat()
    checks: List[Dict[str, Any]] = []
    warnings: List[str] = []

    try:
        targets = _configured_verify_targets(app, repo_path, target_urls)
    except ValueError as exc:
        return {
            "app": app,
            "env": env or "production",
            "repo_path": (str(Path(repo_path).expanduser().resolve()) if repo_path else ""),
            "status": "invalid_verification_config",
            "verdict": "unhealthy",
            "healthy": False,
            "error": str(exc),
        }
    if app and not targets:
        return {
            "app": app,
            "env": env or "production",
            "repo_path": (str(Path(repo_path).expanduser().resolve()) if repo_path else ""),
            "status": "verification_target_not_configured",
            "verdict": "unhealthy",
            "healthy": False,
            "error": f"No verification target configured for app {app!r}",
            "hint": "Pass target_urls or add .delimit/deploy-targets.json in repo_path.",
        }

    for target in targets:
        entry: Dict[str, Any] = {"name": target["name"], "kind": target["kind"]}
        component_results: List[bool] = []

        # HTTP health
        http = _check_http_health(target["url"])
        entry["http"] = http
        component_results.append(bool(http.get("healthy")))

        # SSL cert check
        hostname = _extract_hostname(target["url"])
        if hostname:
            ssl_result = _check_ssl_cert(hostname)
            entry["ssl"] = ssl_result
            if ssl_result.get("expiry_warning"):
                warnings.append(ssl_result.get("warning", f"SSL expiry warning for {hostname}"))
            component_results.append(bool(ssl_result.get("ssl_valid")))

        # npm version check (only for npm targets)
        if target["kind"] == "npm":
            npm_result = _check_npm_version()
            entry["npm"] = npm_result
            component_results.append(bool(npm_result.get("healthy")))

        entry["healthy"] = bool(component_results) and all(component_results)
        entry["partial"] = any(component_results) and not all(component_results)

        checks.append(entry)

    healthy_count = sum(1 for check in checks if check["healthy"])
    any_success = any(check["healthy"] or check["partial"] for check in checks)
    if healthy_count == len(checks):
        verdict = "healthy"
    elif any_success:
        verdict = "partial"
    else:
        verdict = "unhealthy"

    # Cross-reference deploy plan if one exists
    plan_info: Optional[Dict[str, Any]] = None
    plans = _list_plans(app=app or None, env=env or None, repo_path=repo_path or None)
    if plans:
        latest = plans[0]
        plan_info = {
            "plan_id": latest["plan_id"],
            "plan_status": latest["status"],
            "updated_at": latest.get("updated_at"),
        }

    result: Dict[str, Any] = {
        "app": app or "all",
        "env": env or "production",
        "repo_path": str(Path(repo_path).expanduser().resolve()) if repo_path else "",
        "git_ref": git_ref,
        "verified_at": now,
        "status": verdict,
        "verdict": verdict,
        "healthy": verdict == "healthy",
        "targets_checked": len(checks),
        "targets_healthy": healthy_count,
        "checks": checks,
    }
    if warnings:
        result["warnings"] = warnings
    if plan_info:
        result["deploy_plan"] = plan_info
    return result


def rollback(
    app: str, env: str, to_sha: Optional[str] = None, repo_path: str = ""
) -> Dict[str, Any]:
    """Mark latest published plan as rolled back."""
    plans = _list_plans(app=app, env=env, repo_path=repo_path or None)
    if not plans:
        return {"error": f"No deploy plans found for {app} in {env}"}
    latest = plans[0]
    current_status = latest.get("status", "unknown")
    if current_status == "rolled_back":
        return {
            "app": app,
            "env": env,
            "plan_id": latest["plan_id"],
            "status": "already_rolled_back",
            "rolled_back_to": latest.get("rolled_back_to"),
        }
    if current_status != "published":
        return {
            "app": app,
            "env": env,
            "plan_id": latest["plan_id"],
            "status": "not_ready",
            "message": f"Cannot roll back plan {latest['plan_id']} from status '{current_status}'. Publish it first.",
            "current_status": current_status,
        }

    now = datetime.now(timezone.utc).isoformat()
    latest["status"] = "rolled_back"
    latest["updated_at"] = now
    latest["rolled_back_to"] = to_sha
    latest["history"].append({"status": "rolled_back", "at": now, "to_sha": to_sha})
    (DEPLOY_DIR / f"{latest['plan_id']}.json").write_text(json.dumps(latest, indent=2))
    return latest
