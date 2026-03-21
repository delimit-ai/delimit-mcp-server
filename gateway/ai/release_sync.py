"""
Delimit Release Sync — single source of truth for all public surfaces.

Audit mode: scans all surfaces and reports inconsistencies.
Apply mode: fixes what it can automatically.

Central config: ~/.delimit/release.json
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

RELEASE_CONFIG = Path.home() / ".delimit" / "release.json"

DEFAULT_CONFIG = {
    "product_name": "Delimit",
    "tagline": "Governance toolkit for AI coding assistants",
    "description": "Governance toolkit for AI coding assistants — API checks, persistent memory, consensus, security.",
    "version": {
        "cli": "",  # filled dynamically
        "action": "",
        "gateway": "",
    },
    "urls": {
        "homepage": "https://delimit.ai",
        "docs": "https://delimit.ai/docs",
        "github": "https://github.com/delimit-ai/delimit",
        "action": "https://github.com/marketplace/actions/delimit-api-governance",
        "npm": "https://www.npmjs.com/package/delimit-cli",
        "quickstart": "https://github.com/delimit-ai/delimit-quickstart",
    },
}


def get_release_config() -> Dict[str, Any]:
    """Load or create the release config."""
    if RELEASE_CONFIG.exists():
        try:
            return json.loads(RELEASE_CONFIG.read_text())
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_release_config(config: Dict[str, Any]) -> None:
    """Save the release config."""
    RELEASE_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    RELEASE_CONFIG.write_text(json.dumps(config, indent=2))


def _read_file(path: str) -> Optional[str]:
    """Read a file, return None if missing."""
    try:
        return Path(path).read_text()
    except Exception:
        return None


def _check_contains(content: str, expected: str, surface: str) -> Dict:
    """Check if content contains expected string."""
    if content is None:
        return {"surface": surface, "status": "missing", "detail": "File not found"}
    if expected.lower() in content.lower():
        return {"surface": surface, "status": "ok"}
    return {
        "surface": surface,
        "status": "stale",
        "expected": expected,
        "detail": f"Does not contain: {expected[:80]}",
    }


def _get_npm_version(pkg_path: str) -> str:
    """Read version from package.json."""
    try:
        pkg = json.loads(Path(pkg_path).read_text())
        return pkg.get("version", "")
    except Exception:
        return ""


def _get_pyproject_version(path: str) -> str:
    """Read version from pyproject.toml."""
    try:
        content = Path(path).read_text()
        m = re.search(r'version\s*=\s*"([^"]+)"', content)
        return m.group(1) if m else ""
    except Exception:
        return ""


def audit(config: Optional[Dict] = None) -> Dict[str, Any]:
    """Audit all public surfaces for consistency with the release config."""
    cfg = config or get_release_config()
    tagline = cfg.get("tagline", "")
    description = cfg.get("description", "")
    results = []

    # 1. npm package.json
    npm_pkg = _read_file(os.path.expanduser("~/.delimit/server/../../../npm-delimit/package.json"))
    # Try common locations
    for candidate in [
        Path.home() / "npm-delimit" / "package.json",
    ]:
        if candidate.exists():
            npm_pkg = candidate.read_text()
            break

    if npm_pkg:
        try:
            pkg = json.loads(npm_pkg)
            pkg_desc = pkg.get("description", "")
            if tagline.lower() not in pkg_desc.lower():
                results.append({"surface": "npm package.json description", "status": "stale", "current": pkg_desc[:100], "expected": description})
            else:
                results.append({"surface": "npm package.json description", "status": "ok"})
            cfg.setdefault("version", {})["cli"] = pkg.get("version", "")
        except Exception:
            results.append({"surface": "npm package.json", "status": "error", "detail": "Could not parse"})

    # 2. CLAUDE.md
    claude_md = _read_file(str(Path.home() / "CLAUDE.md"))
    results.append(_check_contains(claude_md, tagline, "CLAUDE.md"))

    # 3. GitHub repo descriptions (requires gh CLI)
    for repo, surface in [
        ("delimit-ai/delimit", "GitHub: delimit repo"),
        ("delimit-ai/delimit-action", "GitHub: delimit-action repo"),
        ("delimit-ai/delimit-quickstart", "GitHub: quickstart repo"),
    ]:
        try:
            r = subprocess.run(
                ["gh", "api", f"repos/{repo}", "--jq", ".description"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                desc = r.stdout.strip()
                if tagline.lower() in desc.lower() or "governance" in desc.lower():
                    results.append({"surface": surface, "status": "ok", "current": desc[:100]})
                else:
                    results.append({"surface": surface, "status": "stale", "current": desc[:100], "expected": tagline})
            else:
                results.append({"surface": surface, "status": "error", "detail": "gh API failed"})
        except Exception:
            results.append({"surface": surface, "status": "skipped", "detail": "gh CLI not available"})

    # 4. GitHub org description
    try:
        r = subprocess.run(
            ["gh", "api", "orgs/delimit-ai", "--jq", ".description"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            org_desc = r.stdout.strip()
            results.append(_check_contains(org_desc, "governance" if "governance" in tagline.lower() else tagline[:30], "GitHub: org description"))
    except Exception:
        results.append({"surface": "GitHub: org description", "status": "skipped"})

    # 5. delimit.ai meta tags
    for layout_path in [
        Path.home() / "delimit-ui" / "app" / "layout.tsx",
    ]:
        if layout_path.exists():
            layout = layout_path.read_text()
            results.append(_check_contains(layout, tagline, "delimit.ai meta title"))
            break
    else:
        results.append({"surface": "delimit.ai meta title", "status": "skipped", "detail": "layout.tsx not found"})

    # 6. Gateway version
    for pyproject_path in [
        Path.home() / "delimit-gateway" / "pyproject.toml",
    ]:
        if pyproject_path.exists():
            gw_version = _get_pyproject_version(str(pyproject_path))
            cfg.setdefault("version", {})["gateway"] = gw_version
            results.append({"surface": "gateway pyproject.toml", "status": "ok", "version": gw_version})
            break

    # 7. GitHub releases
    try:
        r = subprocess.run(
            ["gh", "release", "list", "--repo", "delimit-ai/delimit", "--limit", "1", "--json", "tagName"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            releases = json.loads(r.stdout)
            if releases:
                release_ver = releases[0].get("tagName", "").lstrip("v")
                cli_ver = cfg.get("version", {}).get("cli", "")
                if release_ver == cli_ver:
                    results.append({"surface": "GitHub release", "status": "ok", "version": release_ver})
                else:
                    results.append({"surface": "GitHub release", "status": "stale", "current": release_ver, "expected": cli_ver})
    except Exception:
        results.append({"surface": "GitHub release", "status": "skipped"})

    # Summary
    ok = sum(1 for r in results if r["status"] == "ok")
    stale = sum(1 for r in results if r["status"] == "stale")
    errors = sum(1 for r in results if r["status"] in ("error", "missing"))

    return {
        "config": cfg,
        "surfaces": results,
        "summary": {
            "total": len(results),
            "ok": ok,
            "stale": stale,
            "errors": errors,
        },
        "all_synced": stale == 0 and errors == 0,
    }
