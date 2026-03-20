"""
Bridge to delimit-generator MCP server.
Tier 3 Extended — code generation and project scaffolding.
"""

import os
import sys
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.ai.generate_bridge")

GEN_PACKAGE = Path(os.environ.get("DELIMIT_HOME", str(Path.home() / ".delimit"))) / "server" / "packages" / "delimit-generator"


def _ensure_gen_path():
    if str(GEN_PACKAGE) not in sys.path:
        sys.path.insert(0, str(GEN_PACKAGE))


def template(template_type: str, name: str, framework: str = "nextjs", features: Optional[List[str]] = None) -> Dict[str, Any]:
    """Generate code template."""
    _ensure_gen_path()
    try:
        from run_mcp import generate_template
        return generate_template(template_type=template_type, name=name, framework=framework, features=features or [])
    except (ImportError, AttributeError) as e:
        return {"tool": "gen.template", "template_type": template_type, "name": name, "framework": framework, "features": features or [], "note": str(e)}


def scaffold(project_type: str, name: str, packages: Optional[List[str]] = None) -> Dict[str, Any]:
    """Scaffold new project structure."""
    _ensure_gen_path()
    try:
        from run_mcp import scaffold_project
        return scaffold_project(project_type=project_type, name=name, packages=packages or [])
    except (ImportError, AttributeError) as e:
        return {"tool": "gen.scaffold", "project_type": project_type, "name": name, "packages": packages or [], "note": str(e)}
