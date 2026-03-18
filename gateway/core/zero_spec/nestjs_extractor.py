"""
NestJS OpenAPI extractor — generates an OpenAPI spec from NestJS source code
without running the server. Uses subprocess + SwaggerModule.createDocument()
for full fidelity.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from .detector import AppLocation, FrameworkInfo


# Extraction script that imports the NestJS app, creates a document via
# SwaggerModule, and prints the OpenAPI JSON to stdout.
_EXTRACTOR_SCRIPT_TS = '''\
import {{ NestFactory }} from "@nestjs/core";
import {{ SwaggerModule, DocumentBuilder }} from "@nestjs/swagger";

async function extract() {{
  // Dynamically import the app module
  const mod = await import("{module_path}");
  const ModuleClass = mod.{module_export} || (mod as any).default;

  if (!ModuleClass) {{
    console.error(JSON.stringify({{ error: "AppModule not found in {module_path}", type: "module_not_found" }}));
    process.exit(1);
  }}

  try {{
    const app = await NestFactory.create(ModuleClass, {{ logger: false }});

    const config = new DocumentBuilder()
      .setTitle("{title}")
      .setVersion("{version}")
      .build();

    const document = SwaggerModule.createDocument(app, config);
    console.log(JSON.stringify(document));

    await app.close();
  }} catch (e) {{
    console.error(JSON.stringify({{ error: `NestFactory failed: ${{e.message}}`, type: "nest_create_error" }}));
    process.exit(1);
  }}
}}

extract().catch(e => {{
  console.error(JSON.stringify({{ error: `Extraction failed: ${{e.message}}`, type: "extract_error" }}));
  process.exit(1);
}});
'''

_EXTRACTOR_SCRIPT_JS = '''\
const {{ NestFactory }} = require("@nestjs/core");
const {{ SwaggerModule, DocumentBuilder }} = require("@nestjs/swagger");

async function extract() {{
  const mod = require("{module_path}");
  const ModuleClass = mod.{module_export} || mod.default;

  if (!ModuleClass) {{
    console.error(JSON.stringify({{ error: "AppModule not found in {module_path}", type: "module_not_found" }}));
    process.exit(1);
  }}

  try {{
    const app = await NestFactory.create(ModuleClass, {{ logger: false }});

    const config = new DocumentBuilder()
      .setTitle("{title}")
      .setVersion("{version}")
      .build();

    const document = SwaggerModule.createDocument(app, config);
    console.log(JSON.stringify(document));

    await app.close();
  }} catch (e) {{
    console.error(JSON.stringify({{ error: `NestFactory failed: ${{e.message}}`, type: "nest_create_error" }}));
    process.exit(1);
  }}
}}

extract().catch(e => {{
  console.error(JSON.stringify({{ error: `Extraction failed: ${{e.message}}`, type: "extract_error" }}));
  process.exit(1);
}});
'''


def extract_nestjs_spec(
    info: FrameworkInfo,
    project_dir: str = ".",
    timeout: int = 30,
    node_bin: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Extract OpenAPI spec from a NestJS project.

    Args:
        info: FrameworkInfo from detect_framework()
        project_dir: Root of the NestJS project
        timeout: Max seconds for extraction subprocess
        node_bin: Node binary to use (auto-detected if None)

    Returns:
        Dict with success/spec/error keys matching FastAPI extractor format.
    """
    root = Path(project_dir).resolve()

    # Check @nestjs/swagger is installed
    if not _has_swagger_package(root):
        return {
            "success": False,
            "error": "@nestjs/swagger not found. Run: npm install @nestjs/swagger",
            "error_type": "missing_deps",
        }

    # Check node_modules exists
    if not (root / "node_modules").exists():
        return {
            "success": False,
            "error": "node_modules not found. Run: npm install",
            "error_type": "missing_deps",
        }

    # Detect project structure
    is_typescript = (root / "tsconfig.json").exists()
    app_module = _find_app_module(root)
    entry_info = _parse_nest_cli(root)

    module_path = app_module or "./src/app.module"
    module_export = "AppModule"
    title = _get_package_name(root) or "API"
    version = _get_package_version(root) or "1.0.0"

    # Generate extraction script
    if is_typescript:
        script_content = _EXTRACTOR_SCRIPT_TS.format(
            module_path=module_path,
            module_export=module_export,
            title=title,
            version=version,
        )
        ext = ".ts"
    else:
        script_content = _EXTRACTOR_SCRIPT_JS.format(
            module_path=module_path,
            module_export=module_export,
            title=title,
            version=version,
        )
        ext = ".js"

    # Write temp script
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=ext, prefix="_delimit_extract_",
        dir=str(root), delete=False,
    ) as f:
        f.write(script_content)
        script_path = f.name

    try:
        # Build command
        cmd = _build_command(root, script_path, is_typescript, node_bin)
        if cmd is None:
            return {
                "success": False,
                "error": "No suitable Node.js runner found. Install ts-node or tsx for TypeScript projects.",
                "error_type": "no_runner",
            }

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(root),
            env={**os.environ, "NODE_ENV": "development"},
        )

        # Parse stdout for the JSON spec
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode != 0:
            # Try parsing structured error from stderr
            try:
                err = json.loads(stderr)
                return {
                    "success": False,
                    "error": err.get("error", "Extraction failed"),
                    "error_type": err.get("type", "unknown"),
                }
            except json.JSONDecodeError:
                return {
                    "success": False,
                    "error": stderr[:500] or "NestJS extraction subprocess failed",
                    "error_type": "subprocess",
                }

        # Parse spec from stdout
        try:
            spec = json.loads(stdout)
        except json.JSONDecodeError:
            return {
                "success": False,
                "error": "Extractor produced invalid JSON",
                "error_type": "parse",
            }

        if "error" in spec and "paths" not in spec:
            return {
                "success": False,
                "error": spec["error"],
                "error_type": spec.get("type", "unknown"),
            }

        # Validate it's an OpenAPI spec
        if "openapi" not in spec and "swagger" not in spec:
            return {
                "success": False,
                "error": "Output is not a valid OpenAPI spec (missing 'openapi' key)",
                "error_type": "invalid_spec",
            }

        # Write to temp YAML file
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


def _has_swagger_package(root: Path) -> bool:
    """Check if @nestjs/swagger is in package.json."""
    pkg = root / "package.json"
    if not pkg.exists():
        return False
    try:
        data = json.loads(pkg.read_text())
        all_deps = {}
        all_deps.update(data.get("dependencies", {}))
        all_deps.update(data.get("devDependencies", {}))
        return "@nestjs/swagger" in all_deps
    except Exception:
        return False


def _find_app_module(root: Path) -> Optional[str]:
    """Find the AppModule file in a NestJS project."""
    candidates = [
        "src/app.module.ts",
        "src/app.module.js",
        "app/app.module.ts",
        "app/app.module.js",
    ]
    for candidate in candidates:
        if (root / candidate).exists():
            return "./" + candidate.rsplit(".", 1)[0]  # Remove extension
    return None


def _parse_nest_cli(root: Path) -> Dict[str, Any]:
    """Parse nest-cli.json for project configuration."""
    cli_path = root / "nest-cli.json"
    if not cli_path.exists():
        return {}
    try:
        return json.loads(cli_path.read_text())
    except Exception:
        return {}


def _get_package_name(root: Path) -> Optional[str]:
    """Get package name from package.json."""
    try:
        data = json.loads((root / "package.json").read_text())
        return data.get("name")
    except Exception:
        return None


def _get_package_version(root: Path) -> Optional[str]:
    """Get package version from package.json."""
    try:
        data = json.loads((root / "package.json").read_text())
        return data.get("version")
    except Exception:
        return None


def _build_command(root: Path, script_path: str, is_typescript: bool, node_bin: Optional[str] = None):
    """Build the subprocess command to run the extraction script."""
    if is_typescript:
        # Try ts-node first, then tsx, then npx ts-node
        for runner in ["ts-node", "tsx"]:
            local = root / "node_modules" / ".bin" / runner
            if local.exists():
                return [str(local), script_path]

        # Try npx
        try:
            result = subprocess.run(
                ["npx", "--yes", "ts-node", "--version"],
                capture_output=True, timeout=10, cwd=str(root),
            )
            if result.returncode == 0:
                return ["npx", "--yes", "ts-node", script_path]
        except Exception:
            pass

        try:
            result = subprocess.run(
                ["npx", "--yes", "tsx", "--version"],
                capture_output=True, timeout=10, cwd=str(root),
            )
            if result.returncode == 0:
                return ["npx", "--yes", "tsx", script_path]
        except Exception:
            pass

        return None
    else:
        node = node_bin or "node"
        return [node, script_path]


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

    hash_input = str(root).encode()
    short_hash = hashlib.sha256(hash_input).hexdigest()[:8]
    spec_path = os.path.join(tempfile.gettempdir(), f"delimit-inferred-nestjs-{short_hash}{ext}")

    with open(spec_path, "w") as f:
        f.write(formatter(spec))

    return spec_path
