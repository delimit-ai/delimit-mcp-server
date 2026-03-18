"""
FastAPI OpenAPI extractor — generates an OpenAPI spec from FastAPI source code
without running the server. Uses subprocess + app.openapi() for full fidelity.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from .detector import AppLocation, FrameworkInfo


# Script template injected into the user's project context.
# It imports the module, finds the FastAPI app, and prints the OpenAPI JSON.
_EXTRACTOR_SCRIPT = '''\
import sys, json, importlib, importlib.util, os

# Prevent the app from actually starting a server
os.environ["DELIMIT_EXTRACT"] = "1"

project_root = sys.argv[1]
module_path = sys.argv[2]
app_var = sys.argv[3]

# Add project root to sys.path so imports resolve
sys.path.insert(0, project_root)

# Convert file path to module name
# e.g. "app/main.py" -> "app.main"
module_name = module_path.replace(os.sep, ".").removesuffix(".py")

try:
    module = importlib.import_module(module_name)
except Exception as e:
    print(json.dumps({"error": f"Import failed: {e}", "type": "import"}))
    sys.exit(1)

app = getattr(module, app_var, None)
if app is None:
    print(json.dumps({"error": f"Variable '{app_var}' not found in {module_name}", "type": "app_not_found"}))
    sys.exit(1)

if not hasattr(app, "openapi"):
    print(json.dumps({"error": f"'{app_var}' is not a FastAPI app (no openapi method)", "type": "not_fastapi"}))
    sys.exit(1)

try:
    spec = app.openapi()
    print(json.dumps(spec, default=str))
except Exception as e:
    print(json.dumps({"error": f"OpenAPI generation failed: {e}", "type": "openapi_error"}))
    sys.exit(1)
'''


def extract_fastapi_spec(
    info: FrameworkInfo,
    project_dir: str = ".",
    timeout: int = 15,
    python_bin: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Extract OpenAPI spec from a FastAPI project.

    Args:
        info: FrameworkInfo from detect_framework()
        project_dir: Root of the FastAPI project
        timeout: Max seconds for extraction subprocess
        python_bin: Python binary to use (auto-detected if None)

    Returns:
        Dict with keys:
            - success: bool
            - spec: OpenAPI dict (if success)
            - spec_path: Path to temp YAML file (if success)
            - error: Error message (if not success)
            - error_type: Error category (if not success)
    """
    root = Path(project_dir).resolve()

    if not info.app_locations:
        return {
            "success": False,
            "error": "No FastAPI app instance found. Looked for `app = FastAPI()` in project files.",
            "error_type": "no_app",
        }

    app_loc = info.app_locations[0]
    python = python_bin or _find_python(root)

    if not python:
        return {
            "success": False,
            "error": "Python not found. Install Python 3.8+ or set python_bin.",
            "error_type": "no_python",
        }

    # Check if fastapi is importable
    dep_check = _check_fastapi_installed(python, root)
    if not dep_check["installed"]:
        return {
            "success": False,
            "error": "FastAPI not installed. Run: pip install fastapi",
            "error_type": "missing_deps",
        }

    # Write extractor script to temp file
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", prefix="_delimit_extract_", delete=False
    ) as f:
        f.write(_EXTRACTOR_SCRIPT)
        script_path = f.name

    try:
        result = subprocess.run(
            [python, script_path, str(root), app_loc.file, app_loc.variable],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(root),
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )

        if result.returncode != 0:
            # Try to parse structured error from stdout
            try:
                err = json.loads(result.stdout)
                return {
                    "success": False,
                    "error": err.get("error", "Extraction failed"),
                    "error_type": err.get("type", "unknown"),
                }
            except json.JSONDecodeError:
                stderr = result.stderr.strip()
                return {
                    "success": False,
                    "error": stderr or "Extraction subprocess failed",
                    "error_type": "subprocess",
                }

        # Parse the OpenAPI spec
        try:
            spec = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {
                "success": False,
                "error": "Extractor produced invalid JSON",
                "error_type": "parse",
            }

        if "error" in spec:
            return {
                "success": False,
                "error": spec["error"],
                "error_type": spec.get("type", "unknown"),
            }

        # Validate it looks like an OpenAPI spec
        if "openapi" not in spec and "swagger" not in spec:
            return {
                "success": False,
                "error": "Output is not a valid OpenAPI spec (missing 'openapi' key)",
                "error_type": "invalid_spec",
            }

        # Write to temp YAML file for downstream consumption
        spec_path = _write_temp_spec(spec, root)

        return {
            "success": True,
            "spec": spec,
            "spec_path": spec_path,
            "openapi_version": spec.get("openapi", spec.get("swagger", "unknown")),
            "paths_count": len(spec.get("paths", {})),
            "schemas_count": len(spec.get("components", {}).get("schemas", {})),
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": f"Extraction timed out after {timeout}s. Check for blocking I/O in app startup.",
            "error_type": "timeout",
        }
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass


def _find_python(root: Path) -> Optional[str]:
    """Find the best Python binary for the project."""
    # Check for project venv
    for venv_dir in [root / "venv", root / ".venv", root / "env"]:
        venv_python = venv_dir / "bin" / "python"
        if venv_python.exists():
            return str(venv_python)

    # Fall back to system python
    for name in ["python3", "python"]:
        try:
            result = subprocess.run(
                [name, "--version"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return name
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    return None


def _check_fastapi_installed(python: str, root: Path) -> Dict[str, Any]:
    """Check if FastAPI is importable with the given Python."""
    try:
        result = subprocess.run(
            [python, "-c", "import fastapi; print(fastapi.__version__)"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(root),
        )
        if result.returncode == 0:
            return {"installed": True, "version": result.stdout.strip()}
        return {"installed": False}
    except Exception:
        return {"installed": False}


def _write_temp_spec(spec: Dict[str, Any], root: Path) -> str:
    """Write extracted spec to a temp YAML file."""
    import hashlib

    try:
        import yaml
        formatter = yaml.dump
        ext = ".yaml"
    except ImportError:
        formatter = lambda d: json.dumps(d, indent=2)
        ext = ".json"

    # Deterministic filename based on project path
    hash_input = str(root).encode()
    short_hash = hashlib.sha256(hash_input).hexdigest()[:8]
    spec_path = os.path.join(tempfile.gettempdir(), f"delimit-inferred-{short_hash}{ext}")

    with open(spec_path, "w") as f:
        f.write(formatter(spec))

    return spec_path
