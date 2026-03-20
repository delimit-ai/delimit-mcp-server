"""
Bridge to UI tooling: designsystem, storybook, testsmith, docsweaver.
UI/DX tools for component development and testing.
"""

import os
import sys
import json
import asyncio
import importlib
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from .async_utils import run_async

logger = logging.getLogger("delimit.ai.ui_bridge")

PACKAGES = Path(os.environ.get("DELIMIT_HOME", str(Path.home() / ".delimit"))) / "server" / "packages"

# Add PACKAGES dir so `from shared.base_server import BaseMCPServer` resolves
_packages = str(PACKAGES)
if _packages not in sys.path:
    sys.path.insert(0, _packages)

_servers = {}


def _call(pkg: str, factory_name: str, method: str, args: Dict, tool_label: str) -> Dict[str, Any]:
    """Call a _tool_* method on a BaseMCPServer-derived package."""
    try:
        srv = _servers.get(pkg)
        if srv is None:
            mod = importlib.import_module(f"{pkg}.server")
            factory = getattr(mod, factory_name)
            srv = factory()
            _servers[pkg] = srv
        fn = getattr(srv, method, None)
        if fn is None:
            return {"tool": tool_label, "status": "not_implemented", "error": f"Method {method} not found"}
        result = run_async(fn(args, None))
        return json.loads(result) if isinstance(result, str) else result
    except Exception as e:
        return {"tool": tool_label, "error": str(e)}


# ─── DesignSystem (real implementations in tools_design.py) ────────────
from .tools_design import (
    design_extract_tokens,
    design_generate_component,
    design_generate_tailwind,
    design_validate_responsive,
    design_component_library,
    story_generate,
    story_visual_test,
    story_accessibility,
)


def story_build(project_path: str, output_dir: Optional[str] = None) -> Dict[str, Any]:
    """Story build remains a stub — requires Storybook installed."""
    return {"tool": "story.build", "project_path": project_path, "status": "not_available",
            "message": "Storybook build requires Storybook installed. Run: npx storybook init"}


def story_accessibility_test(project_path: str, standards: str = "WCAG2AA") -> Dict[str, Any]:
    """Delegate to story_accessibility (renamed for backward compat)."""
    return story_accessibility(project_path=project_path, standards=standards)


# ─── TestSmith (Real implementations — tools_real.py) ─────────────────

def test_generate(project_path: str, source_files: Optional[List[str]] = None, framework: str = "jest") -> Dict[str, Any]:
    from .tools_real import test_generate as _real_test_generate
    return _real_test_generate(project_path=project_path, source_files=source_files, framework=framework)


def test_coverage(project_path: str, threshold: int = 80) -> Dict[str, Any]:
    return _call("testsmith", "create_testsmith_server", "_tool_coverage",
                 {"project_path": project_path, "threshold": threshold}, "test.coverage")


def test_smoke(project_path: str, test_suite: Optional[str] = None) -> Dict[str, Any]:
    from .tools_real import test_smoke as _real_test_smoke
    return _real_test_smoke(project_path=project_path, test_suite=test_suite)


# ─── DocsWeaver (Real implementations — tools_real.py) ────────────────

def docs_generate(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    from .tools_real import docs_generate as _real_docs_generate
    return _real_docs_generate(target=target, options=options)


def docs_validate(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    from .tools_real import docs_validate as _real_docs_validate
    return _real_docs_validate(target=target, options=options)
