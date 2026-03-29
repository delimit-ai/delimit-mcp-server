"""Delimit Deliberation Engine — Pro feature.

Multi-model consensus requires Delimit Pro ($10/mo) or your own API keys
configured in ~/.delimit/models.json.

Free tier: 3 deliberations using hosted keys, no setup required.
BYOK: configure your own API keys for unlimited use.

Run: npx delimit-cli setup
"""

def get_deliberation_status():
    """Check deliberation usage and mode."""
    return {
        "mode": "free",
        "hosted_used": 0,
        "hosted_remaining": 3,
        "hosted_limit": 3,
        "total_deliberations": 0,
        "note": "Deliberation runs server-side. Configure ~/.delimit/models.json for BYOK mode.",
    }

def deliberate(**kwargs):
    """Run multi-model consensus — requires Pro or BYOK keys."""
    return {"error": "Deliberation requires server-side execution. Ensure your MCP server is running."}

def configure_models():
    """Configure API keys for deliberation models."""
    return {"error": "Configure models via ~/.delimit/models.json"}

def get_models_config(**kwargs):
    return {}
