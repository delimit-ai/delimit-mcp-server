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
    """Build Storybook static site.  Works if Storybook is installed; helpful guidance otherwise."""
    import shutil as _shutil
    import subprocess as _subprocess

    root = Path(project_path)
    # Quick check: does the project have a Storybook config?
    has_storybook_config = any(
        (root / d).is_dir() for d in (".storybook",)
    )
    has_storybook_dep = False
    pkg_json = root / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            has_storybook_dep = any("storybook" in k for k in all_deps)
        except Exception:
            pass

    # Check if npx storybook is available
    npx = _shutil.which("npx")
    if not npx:
        return {
            "tool": "story.build",
            "project_path": project_path,
            "status": "no_npx",
            "message": (
                "npx is not available. Install Node.js and npm first, then:\n"
                "  1. cd {project_path}\n"
                "  2. npx storybook@latest init\n"
                "  3. npx storybook build"
            ).format(project_path=project_path),
        }

    if not has_storybook_config and not has_storybook_dep:
        return {
            "tool": "story.build",
            "project_path": project_path,
            "status": "not_configured",
            "message": (
                "Storybook is not configured in this project. To set it up:\n"
                "  1. cd {project_path}\n"
                "  2. npx storybook@latest init\n"
                "  3. npx storybook build\n\n"
                "Alternatively, use `delimit_story_generate` to create story files "
                "without installing Storybook."
            ).format(project_path=project_path),
        }

    # Storybook is present -- attempt to build
    cmd = ["npx", "storybook", "build"]
    if output_dir:
        cmd += ["-o", output_dir]
    try:
        result = _subprocess.run(
            cmd, cwd=str(root), capture_output=True, timeout=120,
        )
        if result.returncode == 0:
            out_dir = output_dir or str(root / "storybook-static")
            return {
                "tool": "story.build",
                "status": "ok",
                "project_path": project_path,
                "output_dir": out_dir,
                "message": f"Storybook built successfully to {out_dir}",
            }
        else:
            stderr = result.stderr.decode(errors="replace")[:800]
            return {
                "tool": "story.build",
                "status": "build_error",
                "project_path": project_path,
                "error": stderr,
                "hint": "Ensure dependencies are installed (npm install) and Storybook config is valid.",
            }
    except _subprocess.TimeoutExpired:
        return {
            "tool": "story.build",
            "status": "timeout",
            "project_path": project_path,
            "message": "Storybook build timed out after 120 seconds.",
        }
    except Exception as e:
        return {"tool": "story.build", "status": "error", "error": str(e)}


def story_accessibility_test(project_path: str, standards: str = "WCAG2AA") -> Dict[str, Any]:
    """Delegate to story_accessibility (renamed for backward compat)."""
    return story_accessibility(project_path=project_path, standards=standards)


# ─── TestSmith (Real implementations — tools_real.py) ─────────────────

def test_generate(project_path: str, source_files: Optional[List[str]] = None, framework: str = "jest") -> Dict[str, Any]:
    """Generate test skeletons for source files using the specified framework."""
    from .tools_real import test_generate as _real_test_generate
    return _real_test_generate(project_path=project_path, source_files=source_files, framework=framework)


def test_coverage(project_path: str, threshold: int = 80) -> Dict[str, Any]:
    """Estimate test coverage by counting test vs source files and checking config."""
    root = Path(project_path).resolve()
    skip = {"node_modules", "dist", ".next", ".git", "__pycache__", "build", ".cache", "venv", ".venv"}
    src_files, test_files = [], []
    src_exts = {".py", ".js", ".ts", ".jsx", ".tsx"}
    test_patterns = {"test_", "_test.", ".test.", ".spec.", "tests/", "test/", "__tests__/"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for f in filenames:
            fp = os.path.join(dirpath, f)
            ext = os.path.splitext(f)[1]
            if ext not in src_exts:
                continue
            if any(p in fp for p in test_patterns):
                test_files.append(fp)
            else:
                src_files.append(fp)
    ratio = (len(test_files) / max(len(src_files), 1)) * 100
    # Check for coverage config
    cov_configs = [c for c in ["jest.config.js", "jest.config.ts", ".nycrc", "pytest.ini",
                                "pyproject.toml", "setup.cfg", ".coveragerc"] if (root / c).exists()]
    meets_threshold = ratio >= threshold
    return {"tool": "test.coverage", "status": "ok", "project_path": str(root),
            "source_files": len(src_files), "test_files": len(test_files),
            "estimated_coverage_ratio": round(ratio, 1), "threshold": threshold,
            "meets_threshold": meets_threshold, "coverage_configs": cov_configs,
            "note": "File-count estimate. Run test runner with --coverage for precise line coverage."}


def test_smoke(project_path: str, test_suite: Optional[str] = None, timeout_seconds: Optional[int] = 120, extra_args: Optional[List[str]] = None, fail_fast: Optional[bool] = False) -> Dict[str, Any]:
    """Run smoke tests for the project using the detected test framework."""
    from .tools_real import test_smoke as _real_test_smoke
    return _real_test_smoke(project_path=project_path, test_suite=test_suite, timeout_seconds=timeout_seconds, extra_args=extra_args, fail_fast=fail_fast)


# ─── DocsWeaver (Real implementations — tools_real.py) ────────────────

def docs_generate(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    """Generate API documentation from source code docstrings and comments."""
    from .tools_real import docs_generate as _real_docs_generate
    return _real_docs_generate(target=target, options=options)


def docs_validate(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    """Validate documentation quality, coverage, and link integrity."""
    from .tools_real import docs_validate as _real_docs_validate
    return _real_docs_validate(target=target, options=options)
