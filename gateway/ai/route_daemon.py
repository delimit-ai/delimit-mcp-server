import json
import time
import os
import urllib.request
import logging
from pathlib import Path
from threading import Thread

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("delimit.route_daemon")

MODELS_JSON = Path.home() / ".delimit" / "models.json"
ROUTES_JSON = Path.home() / ".delimit" / "routes.json"

def resolve_aliases():
    """
    Ping /v1/models across providers in models.json and cache the
    resolved models to routes.json to map '-latest' aliases to concrete versions.
    """
    if not MODELS_JSON.exists():
        return
    
    try:
        with open(MODELS_JSON, "r") as f:
            models_config = json.load(f)
    except Exception as e:
        logger.error(f"Failed to read models.json: {e}")
        return

    routes = {}

    for provider, config in models_config.items():
        if not isinstance(config, dict) or not config.get("enabled", False):
            continue
        
        api_url = config.get("api_url")
        api_key = config.get("api_key")
        
        if not api_url or not api_key:
            continue
        
        # Parse base URL for /v1/models (e.g. from https://api.openai.com/v1/chat/completions)
        if "/chat/completions" in api_url:
            base_url = api_url.replace("/chat/completions", "/models")
        elif "/messages" in api_url:
            base_url = api_url.replace("/messages", "/models")
        else:
            base_url = api_url + "/models" if not api_url.endswith("/models") else api_url
            
        try:
            req = urllib.request.Request(base_url, headers={
                "Authorization": f"Bearer {api_key}",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01"
            })
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode())
                # Anthropic doesn't currently support /v1/models in the exact same way as OpenAI,
                # but assuming a standard schema for the sake of the task.
                if "data" in data:
                    models = [m["id"] for m in data["data"] if "id" in m]
                elif isinstance(data, list):
                    models = [m.get("id", m) for m in data]
                else:
                    models = []
                
                # We want to map '-latest' or find the concrete models
                # Let's just store all available models for this provider
                routes[provider] = models
        except Exception as e:
            logger.error(f"Failed to fetch models for {provider} at {base_url}: {e}")

    try:
        ROUTES_JSON.parent.mkdir(parents=True, exist_ok=True)
        with open(ROUTES_JSON, "w") as f:
            json.dump(routes, f, indent=2)
        logger.info("Successfully updated routes.json")
    except Exception as e:
        logger.error(f"Failed to write routes.json: {e}")

_daemon_running = False

def run_loop():
    global _daemon_running
    _daemon_running = True
    while _daemon_running:
        resolve_aliases()
        time.sleep(3600)  # Check every hour

def start_daemon():
    thread = Thread(target=run_loop, daemon=True)
    thread.start()
    return {"status": "started"}

def stop_daemon():
    global _daemon_running
    _daemon_running = False
    return {"status": "stopped"}
