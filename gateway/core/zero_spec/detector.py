"""
Framework detector — identifies API frameworks in a project directory.
Scans dependency files and source code to determine which framework is used.
"""

import ast
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional


class Framework(Enum):
    FASTAPI = "fastapi"
    EXPRESS = "express"
    NESTJS = "nestjs"
    UNKNOWN = "unknown"


@dataclass
class AppLocation:
    """Where the framework app instance was found."""
    file: str
    variable: str
    line: int


@dataclass
class FrameworkInfo:
    """Result of framework detection."""
    framework: Framework
    confidence: float  # 0.0 to 1.0
    app_locations: List[AppLocation] = field(default_factory=list)
    entry_point: Optional[str] = None
    message: str = ""


def detect_framework(project_dir: str = ".") -> FrameworkInfo:
    """
    Detect which API framework a project uses.

    Checks dependency files first (high confidence), then scans source
    files for framework imports (medium confidence).
    """
    root = Path(project_dir)

    # Check Python dependency files for FastAPI
    fastapi_confidence = _check_python_deps(root, "fastapi")
    if fastapi_confidence > 0:
        apps = _find_fastapi_apps(root)
        entry = apps[0].file if apps else None
        return FrameworkInfo(
            framework=Framework.FASTAPI,
            confidence=fastapi_confidence,
            app_locations=apps,
            entry_point=entry,
            message=f"FastAPI detected{f' in {entry}' if entry else ''}",
        )

    # Check Node dependency files for NestJS (before Express since NestJS uses Express internally)
    nestjs_confidence = _check_node_deps(root, "@nestjs/core")
    if nestjs_confidence > 0:
        apps = _find_nestjs_apps(root)
        entry = apps[0].file if apps else None
        return FrameworkInfo(
            framework=Framework.NESTJS,
            confidence=nestjs_confidence,
            app_locations=apps,
            entry_point=entry,
            message=f"NestJS detected{f' in {entry}' if entry else ''}",
        )

    # Check Node dependency files for Express
    express_confidence = _check_node_deps(root, "express")
    if express_confidence > 0:
        apps = _find_express_apps(root)
        entry = apps[0].file if apps else None
        return FrameworkInfo(
            framework=Framework.EXPRESS,
            confidence=express_confidence,
            app_locations=apps,
            entry_point=entry,
            message=f"Express detected{f' in {entry}' if entry else ''}",
        )

    return FrameworkInfo(
        framework=Framework.UNKNOWN,
        confidence=0.0,
        message="No supported API framework detected",
    )


def _check_python_deps(root: Path, package: str) -> float:
    """Check Python dependency files for a package. Returns confidence 0-1."""
    # pyproject.toml — highest confidence
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text()
        if re.search(rf'["\']?{package}["\']?\s*[>=<~!]', text) or f'"{package}"' in text or f"'{package}'" in text:
            return 0.95

    # requirements.txt
    for req_file in ["requirements.txt", "requirements/base.txt", "requirements/prod.txt"]:
        req_path = root / req_file
        if req_path.exists():
            text = req_path.read_text()
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("#") or not line:
                    continue
                if re.match(rf'^{package}\b', line):
                    return 0.9

    # setup.py / setup.cfg
    for setup_file in ["setup.py", "setup.cfg"]:
        path = root / setup_file
        if path.exists():
            text = path.read_text()
            if package in text:
                return 0.8

    # Fallback: scan .py files for import
    for py_file in _iter_python_files(root, max_files=50):
        try:
            text = py_file.read_text()
            if f"import {package}" in text or f"from {package}" in text:
                return 0.7
        except Exception:
            continue

    return 0.0


def _check_node_deps(root: Path, package: str) -> float:
    """Check Node.js dependency files for a package. Returns confidence 0-1."""
    pkg_json = root / "package.json"
    if not pkg_json.exists():
        return 0.0

    try:
        import json
        data = json.loads(pkg_json.read_text())
        all_deps = {}
        all_deps.update(data.get("dependencies", {}))
        all_deps.update(data.get("devDependencies", {}))
        if package in all_deps:
            return 0.9
    except Exception:
        pass

    return 0.0


def _find_express_apps(root: Path) -> List[AppLocation]:
    """Find Express app instances via regex scanning of JS/TS files."""
    apps = []

    # Check common entry points first
    priority_files = [
        "app.js", "src/app.js", "server.js", "src/server.js",
        "index.js", "src/index.js", "app.ts", "src/app.ts",
        "server.ts", "src/server.ts",
    ]
    checked = set()

    for rel in priority_files:
        path = root / rel
        if path.exists():
            checked.add(path)
            found = _scan_file_for_express(path, root)
            apps.extend(found)

    # If not found in priority files, scan .js files
    if not apps:
        skip_dirs = {
            "node_modules", ".git", "dist", "build", "coverage",
            ".nyc_output", ".next", ".nuxt",
        }
        count = 0
        for js_file in root.rglob("*.js"):
            if any(part in skip_dirs for part in js_file.parts):
                continue
            if js_file in checked:
                continue
            found = _scan_file_for_express(js_file, root)
            apps.extend(found)
            if apps:
                break
            count += 1
            if count >= 50:
                break

    return apps


def _scan_file_for_express(path: Path, root: Path) -> List[AppLocation]:
    """Scan a single JS/TS file for Express app instantiation."""
    results = []
    try:
        source = path.read_text()
    except Exception:
        return results

    # Must import express
    if not re.search(r"require\s*\(\s*['\"]express['\"]\s*\)", source) and \
       not re.search(r"from\s+['\"]express['\"]", source):
        return results

    # Find the express variable name
    express_var = None
    m_req = re.search(r"(?:const|let|var)\s+(\w+)\s*=\s*require\s*\(\s*['\"]express['\"]\s*\)", source)
    if m_req:
        express_var = m_req.group(1)

    if not express_var:
        return results

    # Find app = express()
    m_app = re.search(
        rf"(?:const|let|var)\s+(\w+)\s*=\s*{re.escape(express_var)}\s*\(\s*\)",
        source,
    )
    if m_app:
        var_name = m_app.group(1)
        # Determine the line number
        line_num = source[:m_app.start()].count("\n") + 1
        rel_path = str(path.relative_to(root))
        results.append(AppLocation(file=rel_path, variable=var_name, line=line_num))

    return results


def _find_nestjs_apps(root: Path) -> List[AppLocation]:
    """Find NestJS AppModule files."""
    apps = []
    candidates = [
        "src/app.module.ts",
        "src/app.module.js",
        "app/app.module.ts",
        "app/app.module.js",
    ]
    for rel in candidates:
        path = root / rel
        if path.exists():
            apps.append(AppLocation(file=rel, variable="AppModule", line=1))
            break
    return apps


def _find_fastapi_apps(root: Path) -> List[AppLocation]:
    """Find FastAPI app instances via AST analysis."""
    apps = []

    # Check common entry points first
    priority_files = ["main.py", "app.py", "app/main.py", "src/main.py", "src/app.py", "server.py"]
    checked = set()

    for rel in priority_files:
        path = root / rel
        if path.exists():
            checked.add(path)
            found = _scan_file_for_fastapi(path, root)
            apps.extend(found)

    # If not found in priority files, scan all .py files
    if not apps:
        for py_file in _iter_python_files(root, max_files=100):
            if py_file in checked:
                continue
            found = _scan_file_for_fastapi(py_file, root)
            apps.extend(found)
            if apps:
                break

    return apps


def _scan_file_for_fastapi(path: Path, root: Path) -> List[AppLocation]:
    """Scan a single Python file for FastAPI() instantiation via AST."""
    results = []
    try:
        source = path.read_text()
        tree = ast.parse(source)
    except Exception:
        return results

    # Check if FastAPI is imported
    has_fastapi_import = False
    fastapi_names = set()

    for node in ast.walk(tree):
        # from fastapi import FastAPI
        if isinstance(node, ast.ImportFrom) and node.module and "fastapi" in node.module:
            has_fastapi_import = True
            for alias in node.names:
                fastapi_names.add(alias.asname or alias.name)

        # import fastapi
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "fastapi" in alias.name:
                    has_fastapi_import = True
                    fastapi_names.add(alias.asname or alias.name)

    if not has_fastapi_import:
        return results

    # Find assignments like: app = FastAPI(...)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if isinstance(node.value, ast.Call):
                call = node.value
                func_name = _get_call_name(call)
                if func_name in fastapi_names or func_name == "FastAPI":
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            rel_path = str(path.relative_to(root))
                            results.append(AppLocation(
                                file=rel_path,
                                variable=target.id,
                                line=node.lineno,
                            ))

    return results


def _get_call_name(call: ast.Call) -> str:
    """Extract function name from an ast.Call node."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _iter_python_files(root: Path, max_files: int = 100) -> List[Path]:
    """Iterate Python files, skipping venvs and hidden dirs."""
    skip_dirs = {
        "venv", ".venv", "env", ".env", "node_modules",
        "__pycache__", ".git", ".tox", ".mypy_cache", ".pytest_cache",
        "dist", "build", "egg-info",
    }
    count = 0
    files = []
    for path in root.rglob("*.py"):
        if any(part in skip_dirs for part in path.parts):
            continue
        files.append(path)
        count += 1
        if count >= max_files:
            break
    return files
