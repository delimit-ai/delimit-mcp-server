"""SIEM Streaming — forward audit trail events to external security tools.

LED-280: Enterprise CISOs need audit logs in their existing SIEM, not just our dashboard.
Supports Splunk HEC, Datadog Logs API, and AWS EventBridge.

Config stored at ~/.delimit/siem.json. Each integration can be enabled independently.
Events are forwarded in real-time as they're written to the audit trail.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.ai.siem_streaming")

SIEM_CONFIG_PATH = Path.home() / ".delimit" / "siem.json"
SIEM_LOG_PATH = Path.home() / ".delimit" / "siem_delivery.jsonl"

DEFAULT_CONFIG = {
    "splunk": {
        "enabled": False,
        "hec_url": "",
        "hec_token": "",
        "index": "delimit",
        "source": "delimit-governance",
        "sourcetype": "_json",
    },
    "datadog": {
        "enabled": False,
        "api_key": "",
        "site": "datadoghq.com",
        "service": "delimit",
        "source": "delimit-governance",
        "tags": ["env:production"],
    },
    "eventbridge": {
        "enabled": False,
        "bus_name": "default",
        "region": "us-east-1",
        "source": "delimit.governance",
        "detail_type": "GovernanceEvent",
    },
    "webhook": {
        "enabled": False,
        "url": "",
        "headers": {},
        "method": "POST",
    },
}


def _load_config() -> Dict[str, Any]:
    if SIEM_CONFIG_PATH.exists():
        try:
            return json.loads(SIEM_CONFIG_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return dict(DEFAULT_CONFIG)


def _save_config(config: Dict[str, Any]) -> None:
    SIEM_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SIEM_CONFIG_PATH.write_text(json.dumps(config, indent=2))


def _log_delivery(integration: str, event_id: str, status: str, error: str = "") -> None:
    try:
        SIEM_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": time.time(),
            "integration": integration,
            "event_id": event_id,
            "status": status,
            "error": error,
        }
        with open(SIEM_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def configure(
    integration: str,
    settings: Optional[Dict[str, Any]] = None,
    enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    """Configure a SIEM integration.

    Args:
        integration: splunk, datadog, eventbridge, or webhook
        settings: Key-value pairs to update (e.g. {"hec_url": "...", "hec_token": "..."})
        enabled: Enable or disable the integration
    """
    if integration not in DEFAULT_CONFIG:
        return {"error": f"Unknown integration: {integration}. Choose: splunk, datadog, eventbridge, webhook"}

    config = _load_config()
    if integration not in config:
        config[integration] = dict(DEFAULT_CONFIG[integration])

    if settings:
        config[integration].update(settings)
    if enabled is not None:
        config[integration]["enabled"] = enabled

    _save_config(config)

    # Mask secrets in response
    safe = dict(config[integration])
    for key in ("hec_token", "api_key"):
        if key in safe and safe[key]:
            safe[key] = safe[key][:4] + "***" + safe[key][-4:]

    return {
        "status": "configured",
        "integration": integration,
        "config": safe,
    }


def get_status() -> Dict[str, Any]:
    """Get status of all SIEM integrations."""
    config = _load_config()
    integrations = {}

    for name, settings in config.items():
        if not isinstance(settings, dict):
            continue
        enabled = settings.get("enabled", False)
        healthy = False

        if enabled:
            if name == "splunk":
                healthy = bool(settings.get("hec_url") and settings.get("hec_token"))
            elif name == "datadog":
                healthy = bool(settings.get("api_key"))
            elif name == "eventbridge":
                healthy = bool(settings.get("bus_name"))
            elif name == "webhook":
                healthy = bool(settings.get("url"))

        integrations[name] = {
            "enabled": enabled,
            "healthy": healthy if enabled else None,
            "status": "active" if (enabled and healthy) else "misconfigured" if enabled else "disabled",
        }

    # Delivery stats
    delivery_count = 0
    delivery_errors = 0
    if SIEM_LOG_PATH.exists():
        for line in SIEM_LOG_PATH.read_text().strip().split("\n")[-100:]:
            try:
                entry = json.loads(line)
                delivery_count += 1
                if entry.get("status") == "error":
                    delivery_errors += 1
            except json.JSONDecodeError:
                continue

    return {
        "integrations": integrations,
        "active_count": sum(1 for i in integrations.values() if i["status"] == "active"),
        "total_deliveries": delivery_count,
        "delivery_errors": delivery_errors,
    }


def forward_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Forward a governance event to all enabled SIEM integrations.

    Called automatically by the audit trail when events are recorded.
    Returns delivery status per integration.
    """
    config = _load_config()
    results = {}
    event_id = event.get("id", str(time.time()))

    for name, settings in config.items():
        if not isinstance(settings, dict) or not settings.get("enabled"):
            continue

        try:
            if name == "splunk":
                results[name] = _forward_splunk(event, settings, event_id)
            elif name == "datadog":
                results[name] = _forward_datadog(event, settings, event_id)
            elif name == "eventbridge":
                results[name] = _forward_eventbridge(event, settings, event_id)
            elif name == "webhook":
                results[name] = _forward_webhook(event, settings, event_id)
        except Exception as e:
            results[name] = {"status": "error", "error": str(e)}
            _log_delivery(name, event_id, "error", str(e))

    return {
        "event_id": event_id,
        "forwarded_to": list(results.keys()),
        "results": results,
    }


def _forward_splunk(event: Dict, settings: Dict, event_id: str) -> Dict:
    """Forward to Splunk via HTTP Event Collector (HEC)."""
    import urllib.request

    payload = json.dumps({
        "event": event,
        "source": settings.get("source", "delimit-governance"),
        "sourcetype": settings.get("sourcetype", "_json"),
        "index": settings.get("index", "delimit"),
    }).encode()

    req = urllib.request.Request(
        settings["hec_url"],
        data=payload,
        headers={
            "Authorization": f"Splunk {settings['hec_token']}",
            "Content-Type": "application/json",
        },
    )
    resp = urllib.request.urlopen(req, timeout=10)
    _log_delivery("splunk", event_id, "ok")
    return {"status": "ok", "http_code": resp.status}


def _forward_datadog(event: Dict, settings: Dict, event_id: str) -> Dict:
    """Forward to Datadog Logs API."""
    import urllib.request

    site = settings.get("site", "datadoghq.com")
    payload = json.dumps([{
        "ddsource": settings.get("source", "delimit-governance"),
        "ddtags": ",".join(settings.get("tags", [])),
        "service": settings.get("service", "delimit"),
        "message": json.dumps(event),
    }]).encode()

    req = urllib.request.Request(
        f"https://http-intake.logs.{site}/api/v2/logs",
        data=payload,
        headers={
            "DD-API-KEY": settings["api_key"],
            "Content-Type": "application/json",
        },
    )
    resp = urllib.request.urlopen(req, timeout=10)
    _log_delivery("datadog", event_id, "ok")
    return {"status": "ok", "http_code": resp.status}


def _forward_eventbridge(event: Dict, settings: Dict, event_id: str) -> Dict:
    """Forward to AWS EventBridge."""
    try:
        import boto3
        client = boto3.client("events", region_name=settings.get("region", "us-east-1"))
        response = client.put_events(Entries=[{
            "Source": settings.get("source", "delimit.governance"),
            "DetailType": settings.get("detail_type", "GovernanceEvent"),
            "Detail": json.dumps(event),
            "EventBusName": settings.get("bus_name", "default"),
        }])
        failed = response.get("FailedEntryCount", 0)
        status = "ok" if failed == 0 else "partial"
        _log_delivery("eventbridge", event_id, status)
        return {"status": status, "failed_entries": failed}
    except ImportError:
        _log_delivery("eventbridge", event_id, "error", "boto3 not installed")
        return {"status": "error", "error": "boto3 not installed. Run: pip install boto3"}


def _forward_webhook(event: Dict, settings: Dict, event_id: str) -> Dict:
    """Forward to a generic webhook URL."""
    import urllib.request

    headers = {"Content-Type": "application/json"}
    headers.update(settings.get("headers", {}))

    req = urllib.request.Request(
        settings["url"],
        data=json.dumps(event).encode(),
        headers=headers,
        method=settings.get("method", "POST"),
    )
    resp = urllib.request.urlopen(req, timeout=10)
    _log_delivery("webhook", event_id, "ok")
    return {"status": "ok", "http_code": resp.status}
