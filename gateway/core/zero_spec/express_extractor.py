"""
Express OpenAPI extractor — generates an OpenAPI spec from Express source code
by introspecting the app's route stack at runtime. Since Express has no built-in
OpenAPI generator, we inject a script that requires the app, walks
app._router.stack, and builds a minimal spec from discovered routes.
"""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .detector import AppLocation, FrameworkInfo


# Node.js script that requires the Express app, walks the router stack,
# and emits a minimal OpenAPI 3.0.3 spec as JSON to stdout.
_EXTRACTOR_SCRIPT = r'''
"use strict";

const path = require("path");

const projectRoot = process.argv[2];
const appFile = process.argv[3];
const appVar = process.argv[4];

// Resolve the app module relative to the project root
const modulePath = path.resolve(projectRoot, appFile);

let appModule;
try {
  appModule = require(modulePath);
} catch (e) {
  console.log(JSON.stringify({ error: "Import failed: " + e.message, type: "import" }));
  process.exit(1);
}

const app = appModule[appVar] || appModule.default || appModule;

if (!app || typeof app !== "function" && typeof app !== "object") {
  console.log(JSON.stringify({ error: "Variable '" + appVar + "' not found or not an Express app", type: "app_not_found" }));
  process.exit(1);
}

// Check if this looks like an Express app (has _router or use/get methods)
if (!app._router && !app.get && !app.use) {
  console.log(JSON.stringify({ error: "'" + appVar + "' does not appear to be an Express app (no _router)", type: "not_express" }));
  process.exit(1);
}

// Force Express to initialise its router if lazy
if (!app._router && typeof app.lazyrouter === "function") {
  app.lazyrouter();
}

// Collect routes -------------------------------------------------------

function expressParamToOpenAPI(routePath) {
  // Convert Express :param to OpenAPI {param}
  return routePath.replace(/:([A-Za-z0-9_]+)/g, "{$1}");
}

function extractPathParams(routePath) {
  const params = [];
  const re = /:([A-Za-z0-9_]+)/g;
  let m;
  while ((m = re.exec(routePath)) !== null) {
    params.push(m[1]);
  }
  return params;
}

function collectRoutes(stack, prefix) {
  const routes = [];
  if (!stack) return routes;

  for (const layer of stack) {
    if (layer.route) {
      // Direct route on the app
      const routePath = prefix + layer.route.path;
      const methods = Object.keys(layer.route.methods).filter(m => m !== "_all");
      for (const method of methods) {
        routes.push({ path: routePath, method: method.toLowerCase() });
      }
    } else if (layer.name === "router" && layer.handle && layer.handle.stack) {
      // Mounted sub-router via app.use('/prefix', router)
      let mountPath = "";
      if (layer.regexp && layer.keys && layer.keys.length === 0) {
        // Try to extract the mount path from the regexp source
        mountPath = regexpToPath(layer.regexp);
      }
      if (layer.path) {
        mountPath = layer.path;
      }
      // Recurse into the sub-router
      const subRoutes = collectRoutes(layer.handle.stack, prefix + mountPath);
      routes.push(...subRoutes);
    }
  }

  return routes;
}

function regexpToPath(regexp) {
  // Express stores mount paths as regexps. Common pattern:
  // /^\/api\/v1\/?(?=\/|$)/i  =>  /api/v1
  if (!regexp || !regexp.source) return "";
  const src = regexp.source;
  // Strip anchors and optional trailing slash patterns
  let cleaned = src
    .replace(/^\^/, "")
    .replace(/\\\/\?\(\?=\\\/\|\$\)$/i, "")
    .replace(/\\\/\?\(\?:\\\/\)\?$/i, "")
    .replace(/\\\//g, "/")
    .replace(/\$$/,"");
  // Only return if it looks like a clean path
  if (/^[\/A-Za-z0-9_\-\.{}:]+$/.test(cleaned)) {
    return cleaned;
  }
  return "";
}

let routes;
try {
  routes = collectRoutes(app._router ? app._router.stack : [], "");
} catch (e) {
  console.log(JSON.stringify({ error: "Route extraction failed: " + e.message, type: "route_extraction" }));
  process.exit(1);
}

if (routes.length === 0) {
  console.log(JSON.stringify({ error: "No routes found in app._router.stack", type: "no_routes" }));
  process.exit(1);
}

// Build OpenAPI spec ---------------------------------------------------

// Read package.json for metadata
let pkgName = "Express API";
let pkgVersion = "1.0.0";
try {
  const pkg = require(path.resolve(projectRoot, "package.json"));
  pkgName = pkg.name || pkgName;
  pkgVersion = pkg.version || pkgVersion;
} catch (_) {}

const paths = {};
for (const route of routes) {
  const oapiPath = expressParamToOpenAPI(route.path);
  if (!paths[oapiPath]) {
    paths[oapiPath] = {};
  }

  const pathParams = extractPathParams(route.path);
  const parameters = pathParams.map(p => ({
    name: p,
    in: "path",
    required: true,
    schema: { type: "string" },
  }));

  const operation = {
    summary: route.method.toUpperCase() + " " + oapiPath,
    responses: {
      "200": { description: "Successful response" },
    },
  };

  if (parameters.length > 0) {
    operation.parameters = parameters;
  }

  // POST/PUT/PATCH get a requestBody placeholder
  if (["post", "put", "patch"].includes(route.method)) {
    operation.requestBody = {
      content: {
        "application/json": {
          schema: { type: "object" },
        },
      },
    };
  }

  paths[oapiPath][route.method] = operation;
}

const spec = {
  openapi: "3.0.3",
  info: {
    title: pkgName,
    version: pkgVersion,
  },
  paths: paths,
};

console.log(JSON.stringify(spec));
'''


def extract_express_spec(
    info: FrameworkInfo,
    project_dir: str = ".",
    timeout: int = 15,
    node_bin: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Extract OpenAPI spec from an Express project.

    Args:
        info: FrameworkInfo from detect_framework()
        project_dir: Root of the Express project
        timeout: Max seconds for extraction subprocess
        node_bin: Node binary to use (auto-detected if None)

    Returns:
        Dict with keys:
            - success: bool
            - spec: OpenAPI dict (if success)
            - spec_path: Path to temp YAML file (if success)
            - openapi_version: str (if success)
            - paths_count: int (if success)
            - schemas_count: int (if success)
            - error: Error message (if not success)
            - error_type: Error category (if not success)
    """
    root = Path(project_dir).resolve()

    if not info.app_locations:
        # Try to auto-detect the app file
        app_loc = _find_express_app_fallback(root)
        if not app_loc:
            return {
                "success": False,
                "error": "No Express app instance found. Looked for module.exports = app or exports patterns.",
                "error_type": "no_app",
            }
    else:
        app_loc = info.app_locations[0]

    node = node_bin or _find_node(root)
    if not node:
        return {
            "success": False,
            "error": "Node.js not found. Install Node.js 14+ or set node_bin.",
            "error_type": "no_node",
        }

    # Check node_modules exists (Express needs to be installed)
    if not (root / "node_modules").exists():
        return {
            "success": False,
            "error": "node_modules not found. Run: npm install",
            "error_type": "missing_deps",
        }

    # Check express is actually importable
    if not _check_express_installed(node, root):
        return {
            "success": False,
            "error": "Express not installed. Run: npm install express",
            "error_type": "missing_deps",
        }

    # Write extractor script to temp file inside the project (so require() resolves)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", prefix="_delimit_extract_",
        dir=str(root), delete=False,
    ) as f:
        f.write(_EXTRACTOR_SCRIPT)
        script_path = f.name

    try:
        result = subprocess.run(
            [node, script_path, str(root), app_loc.file, app_loc.variable],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(root),
            env={**os.environ, "NODE_ENV": "production"},
        )

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode != 0:
            # Try to parse structured error from stdout
            try:
                err = json.loads(stdout)
                return {
                    "success": False,
                    "error": err.get("error", "Extraction failed"),
                    "error_type": err.get("type", "unknown"),
                }
            except json.JSONDecodeError:
                return {
                    "success": False,
                    "error": stderr[:500] or stdout[:500] or "Express extraction subprocess failed",
                    "error_type": "subprocess",
                }

        # Parse the OpenAPI spec
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

        # Validate it looks like an OpenAPI spec
        if "openapi" not in spec and "swagger" not in spec:
            return {
                "success": False,
                "error": "Output is not a valid OpenAPI spec (missing 'openapi' key)",
                "error_type": "invalid_spec",
            }

        # Write to temp YAML/JSON file for downstream consumption
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


def _find_node(root: Path) -> Optional[str]:
    """Find the best Node.js binary."""
    # Check for nvm/local node
    for name in ["node", "nodejs"]:
        try:
            result = subprocess.run(
                [name, "--version"], capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return name
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def _check_express_installed(node: str, root: Path) -> bool:
    """Check if express is importable with the given Node."""
    try:
        result = subprocess.run(
            [node, "-e", "require('express'); console.log('ok')"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(root),
        )
        return result.returncode == 0
    except Exception:
        return False


def _find_express_app_fallback(root: Path) -> Optional[AppLocation]:
    """Try to find an Express app export in common entry point files."""
    candidates = [
        "app.js", "src/app.js", "server.js", "src/server.js",
        "index.js", "src/index.js", "app.ts", "src/app.ts",
    ]

    for rel_path in candidates:
        full_path = root / rel_path
        if not full_path.exists():
            continue

        try:
            content = full_path.read_text()
        except Exception:
            continue

        # Check for Express app creation patterns
        if not re.search(r"require\s*\(\s*['\"]express['\"]\s*\)", content) and \
           not re.search(r"from\s+['\"]express['\"]", content):
            continue

        # Find the variable name of the Express app
        var_name = _detect_app_variable(content)
        if var_name:
            return AppLocation(file=rel_path, variable=var_name, line=1)

    return None


def _detect_app_variable(content: str) -> Optional[str]:
    """Detect the variable name of an Express app instance in source code."""
    # Pattern: const app = express()
    m = re.search(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:express\s*\(\s*\)|require\s*\(\s*['\"]express['\"]\s*\)\s*\(\s*\))", content)
    if m:
        return m.group(1)

    # Pattern: const express = require('express'); ... const app = express();
    # First find what express is called
    express_var = None
    m_req = re.search(r"(?:const|let|var)\s+(\w+)\s*=\s*require\s*\(\s*['\"]express['\"]\s*\)", content)
    if m_req:
        express_var = m_req.group(1)

    if express_var:
        m_app = re.search(rf"(?:const|let|var)\s+(\w+)\s*=\s*{re.escape(express_var)}\s*\(\s*\)", content)
        if m_app:
            return m_app.group(1)

    # Fallback: look for module.exports = <varname> where varname is likely the app
    m_exp = re.search(r"module\.exports\s*=\s*(\w+)", content)
    if m_exp:
        return m_exp.group(1)

    # Fallback: exports.app = ...
    m_exp2 = re.search(r"exports\.(\w+)\s*=", content)
    if m_exp2:
        return m_exp2.group(1)

    return None


def _iter_js_files(root: Path, max_files: int = 50) -> List[Path]:
    """Iterate JS/TS files, skipping node_modules and hidden dirs."""
    skip_dirs = {
        "node_modules", ".git", "dist", "build", "coverage",
        ".nyc_output", ".next", ".nuxt",
    }
    count = 0
    files = []
    for path in root.rglob("*.js"):
        if any(part in skip_dirs for part in path.parts):
            continue
        files.append(path)
        count += 1
        if count >= max_files:
            break
    return files


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
    spec_path = os.path.join(tempfile.gettempdir(), f"delimit-inferred-express-{short_hash}{ext}")

    with open(spec_path, "w") as f:
        f.write(formatter(spec))

    return spec_path
