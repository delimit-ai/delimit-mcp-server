"""
Bridge to UI tooling: designsystem, storybook, testsmith, docsweaver.
UI/DX tools for component development and testing.
"""

import sys
import json
import asyncio
import importlib
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from .async_utils import run_async

logger = logging.getLogger("delimit.ai.ui_bridge")

PACKAGES = Path("/home/delimit/.delimit_suite/packages")

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


# ─── DesignSystem (custom classes, no BaseMCPServer) ───────────────────
# designsystem uses DesignSystemGenerator, not the _tool_* pattern.
# Provide graceful pass-through until refactored.

def design_validate_responsive(project_path: str, check_types: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"tool": "design.validate_responsive", "project_path": project_path, "status": "pass-through"}


def design_extract_tokens(figma_file_key: str, token_types: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"tool": "design.extract_tokens", "figma_file_key": figma_file_key, "status": "pass-through"}


def design_generate_component(component_name: str, figma_node_id: Optional[str] = None, output_path: Optional[str] = None) -> Dict[str, Any]:
    return {"tool": "design.generate_component", "component_name": component_name, "status": "pass-through"}


def design_generate_tailwind(figma_file_key: str, output_path: Optional[str] = None) -> Dict[str, Any]:
    return {"tool": "design.generate_tailwind", "figma_file_key": figma_file_key, "status": "pass-through"}


def design_component_library(project_path: str, output_format: str = "json") -> Dict[str, Any]:
    return {"tool": "design.component_library", "project_path": project_path, "status": "pass-through"}


# ─── Storybook (custom classes, no BaseMCPServer) ─────────────────────

def story_generate(component_path: str, story_name: Optional[str] = None, variants: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"tool": "story.generate", "component_path": component_path, "status": "pass-through"}


def story_visual_test(url: str, project_path: Optional[str] = None, threshold: float = 0.05) -> Dict[str, Any]:
    return {"tool": "story.visual_test", "url": url, "status": "pass-through"}


def story_build(project_path: str, output_dir: Optional[str] = None) -> Dict[str, Any]:
    return {"tool": "story.build", "project_path": project_path, "status": "pass-through"}


def story_accessibility_test(project_path: str, standards: str = "WCAG2AA") -> Dict[str, Any]:
    return {"tool": "story.accessibility_test", "project_path": project_path, "status": "pass-through"}


# ─── TestSmith (BaseMCPServer pattern) ─────────────────────────────────

def test_generate(project_path: str, source_files: Optional[List[str]] = None, framework: str = "jest") -> Dict[str, Any]:
    return _call("testsmith", "create_testsmith_server", "_tool_generate",
                 {"project_path": project_path, "source_files": source_files, "framework": framework}, "test.generate")


def test_coverage(project_path: str, threshold: int = 80) -> Dict[str, Any]:
    return _call("testsmith", "create_testsmith_server", "_tool_coverage",
                 {"project_path": project_path, "threshold": threshold}, "test.coverage")


def test_smoke(project_path: str, test_suite: Optional[str] = None) -> Dict[str, Any]:
    result = _call("testsmith", "create_testsmith_server", "_tool_smoke",
                   {"project_path": project_path}, "test.smoke")
    # Guard against stub that says "passed" with 0 tests actually run
    if result.get("tests_run", -1) == 0 and result.get("passed") is True:
        return {"tool": "test.smoke", "status": "no_tests",
                "error": "No smoke tests configured. The test runner found 0 tests to execute."}
    return result


# ─── DocsWeaver (BaseMCPServer pattern) ────────────────────────────────

def docs_generate(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    return _call("docsweaver", "create_docsweaver_server", "_tool_generate",
                 {"project_path": target, "doc_types": ["api", "readme"], **(options or {})}, "docs.generate")


def docs_validate(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    return _call("docsweaver", "create_docsweaver_server", "_tool_validate",
                 {"docs_path": target, **(options or {})}, "docs.validate")
