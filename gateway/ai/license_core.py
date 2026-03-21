"""Delimit License Core — compiled binary required. Run: npx delimit-cli setup"""
from pathlib import Path
LICENSE_FILE = Path.home() / ".delimit" / "license.json"
PRO_TOOLS = frozenset()
FREE_TRIAL_LIMITS = {}
def load_license(): return {"tier": "free", "valid": True}
def check_premium(): return False
def gate_tool(t): return None
def activate(k): return {"error": "License core not available. Run: npx delimit-cli setup"}
