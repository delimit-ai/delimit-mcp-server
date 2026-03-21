"""
Real implementations for Tier 4 tools: test_generate, test_smoke, docs_generate, docs_validate.

All tools work WITHOUT external integrations by default.
They use AST parsing, filesystem scanning, and subprocess invocation.
"""

import ast
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.ai.tools_real")


# ═══════════════════════════════════════════════════════════════════════
#  test_generate — Generate test skeletons via AST/regex extraction
# ═══════════════════════════════════════════════════════════════════════

def _extract_python_functions(file_path: Path) -> List[Dict[str, Any]]:
    """Parse a Python file with ast and extract public function/method signatures."""
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    functions = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            args = []
            for arg in node.args.args:
                if arg.arg == "self":
                    continue
                args.append(arg.arg)
            # Extract docstring if present
            docstring = ast.get_docstring(node) or ""
            # Get return annotation
            ret = ""
            if node.returns and isinstance(node.returns, ast.Constant):
                ret = str(node.returns.value)
            elif node.returns and isinstance(node.returns, ast.Name):
                ret = node.returns.id

            functions.append({
                "name": node.name,
                "args": args,
                "docstring": docstring[:200],
                "returns": ret,
                "lineno": node.lineno,
                "is_async": isinstance(node, ast.AsyncFunctionDef),
            })
    return functions


def _extract_js_functions(file_path: Path) -> List[Dict[str, Any]]:
    """Extract function names from JS/TS files using regex."""
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return []

    functions = []
    patterns = [
        # function declarations: function myFunc(...)
        r"(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(",
        # arrow / const declarations: const myFunc = (...) =>
        r"(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(?",
        # class methods: myMethod(...) {
        r"^\s+(?:async\s+)?(\w+)\s*\([^)]*\)\s*\{",
    ]
    seen = set()
    for pat in patterns:
        for m in re.finditer(pat, source, re.MULTILINE):
            name = m.group(1)
            if name and not name.startswith("_") and name not in seen:
                seen.add(name)
                functions.append({
                    "name": name,
                    "args": [],
                    "docstring": "",
                    "returns": "",
                    "lineno": source[:m.start()].count("\n") + 1,
                    "is_async": "async" in source[max(0, m.start()-20):m.start()],
                })
    return functions


def _find_existing_test_files(project: Path) -> set:
    """Return set of source file stems that already have test files."""
    tested = set()
    for pattern in ["**/test_*.py", "**/*.test.ts", "**/*.test.js", "**/*.spec.ts", "**/*.spec.js", "**/*_test.py"]:
        for tf in project.glob(pattern):
            stem = tf.stem.replace("test_", "").replace(".test", "").replace(".spec", "").replace("_test", "")
            tested.add(stem)
    return tested


def _generate_pytest_skeleton(source_file: Path, functions: List[Dict]) -> str:
    """Generate a pytest test file skeleton."""
    module_name = source_file.stem
    lines = [
        f'"""Auto-generated test skeleton for {source_file.name}."""',
        f"import pytest",
        "",
    ]
    # Try to build a reasonable import
    lines.append(f"# TODO: adjust import path as needed")
    lines.append(f"# from ... import {module_name}")
    lines.append("")

    for fn in functions:
        args_str = ", ".join(fn["args"])
        test_name = f"test_{fn['name']}"
        lines.append("")
        if fn["docstring"]:
            lines.append(f"# Source docstring: {fn['docstring'][:80]}")
        if fn["is_async"]:
            lines.append(f"@pytest.mark.asyncio")
            lines.append(f"async def {test_name}():")
        else:
            lines.append(f"def {test_name}():")
        lines.append(f'    """Test {fn["name"]}({args_str})."""')
        lines.append(f"    # TODO: implement test")
        if fn["returns"]:
            lines.append(f"    # Expected return type: {fn['returns']}")
        lines.append(f"    assert True  # placeholder")
        lines.append("")

    return "\n".join(lines)


def _generate_jest_skeleton(source_file: Path, functions: List[Dict]) -> str:
    """Generate a jest/vitest test file skeleton."""
    module_name = source_file.stem
    lines = [
        f"// Auto-generated test skeleton for {source_file.name}",
        f"// TODO: adjust import path as needed",
        f"// import {{ ... }} from './{module_name}';",
        "",
        f"describe('{module_name}', () => {{",
    ]
    for fn in functions:
        prefix = "  "
        lines.append(f"{prefix}test('{fn['name']} should work', {'async ' if fn['is_async'] else ''}() => {{")
        lines.append(f"{prefix}  // TODO: implement test")
        lines.append(f"{prefix}  expect(true).toBe(true); // placeholder")
        lines.append(f"{prefix}}});")
        lines.append("")
    lines.append("});")
    return "\n".join(lines)


def test_generate(project_path: str, source_files: Optional[List[str]] = None, framework: str = "jest") -> Dict[str, Any]:
    """Generate test skeletons for a project using AST parsing (Python) or regex (JS/TS).

    Works offline with no external dependencies. Parses source files, extracts
    public function signatures, and generates test file skeletons.
    """
    project = Path(project_path).resolve()
    if not project.is_dir():
        return {"error": "project_not_found", "message": f"Directory not found: {project_path}"}

    is_python = framework == "pytest"
    existing_tests = _find_existing_test_files(project)

    # Determine which source files to process
    if source_files:
        candidates = [project / f for f in source_files if (project / f).is_file()]
    else:
        if is_python:
            candidates = sorted(project.rglob("*.py"))
        else:
            candidates = sorted(
                f for ext in ("*.js", "*.ts", "*.jsx", "*.tsx")
                for f in project.rglob(ext)
            )
        # Exclude test files, node_modules, venv, __pycache__
        skip_dirs = {"node_modules", "__pycache__", "venv", ".venv", ".git", "dist", "build", "tests", "test", "__tests__"}
        candidates = [
            f for f in candidates
            if not any(d in f.parts for d in skip_dirs)
            and not f.name.startswith("test_")
            and ".test." not in f.name
            and ".spec." not in f.name
        ]

    generated = []
    total_functions = 0
    skipped_already_tested = []

    for src in candidates:
        if src.stem in existing_tests:
            skipped_already_tested.append(str(src.relative_to(project)))
            continue

        if is_python:
            funcs = _extract_python_functions(src)
        else:
            funcs = _extract_js_functions(src)

        if not funcs:
            continue

        total_functions += len(funcs)

        # Determine output path
        if is_python:
            test_dir = project / "tests"
            test_dir.mkdir(exist_ok=True)
            test_file = test_dir / f"test_{src.stem}.py"
            skeleton = _generate_pytest_skeleton(src, funcs)
        else:
            test_dir = src.parent / "__tests__"
            test_dir.mkdir(exist_ok=True)
            ext = src.suffix
            test_file = test_dir / f"{src.stem}.test{ext}"
            skeleton = _generate_jest_skeleton(src, funcs)

        test_file.write_text(skeleton, encoding="utf-8")
        generated.append({
            "source": str(src.relative_to(project)),
            "test_file": str(test_file.relative_to(project)),
            "function_count": len(funcs),
            "functions": [f["name"] for f in funcs],
        })

    return {
        "tool": "test.generate",
        "status": "ok",
        "framework": framework,
        "project_path": str(project),
        "files_generated": len(generated),
        "total_functions": total_functions,
        "generated": generated,
        "skipped_already_tested": skipped_already_tested[:20],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════
#  test_smoke — Detect framework and run tests
# ═══════════════════════════════════════════════════════════════════════

def _detect_test_framework(project: Path) -> Optional[Dict[str, str]]:
    """Detect the test framework and return the run command."""
    # Python: pytest
    if (project / "pytest.ini").exists() or (project / "pyproject.toml").exists() or (project / "setup.cfg").exists():
        # Check for pytest in pyproject.toml
        pyproject = project / "pyproject.toml"
        if pyproject.exists():
            content = pyproject.read_text(encoding="utf-8", errors="replace")
            if "pytest" in content or "[tool.pytest" in content:
                return {"framework": "pytest", "cmd": "python -m pytest -q --tb=short"}
        if (project / "pytest.ini").exists():
            return {"framework": "pytest", "cmd": "python -m pytest -q --tb=short"}
        # Check setup.cfg
        setup_cfg = project / "setup.cfg"
        if setup_cfg.exists():
            content = setup_cfg.read_text(encoding="utf-8", errors="replace")
            if "pytest" in content:
                return {"framework": "pytest", "cmd": "python -m pytest -q --tb=short"}

    # Also detect pytest if there's a tests/ dir with test_*.py files
    tests_dir = project / "tests"
    if tests_dir.is_dir() and any(tests_dir.glob("test_*.py")):
        return {"framework": "pytest", "cmd": "python -m pytest -q --tb=short"}

    # Node: check package.json
    pkg_json = project / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = pkg.get("scripts", {})
            test_script = scripts.get("test", "")
            deps = {**pkg.get("devDependencies", {}), **pkg.get("dependencies", {})}

            if "vitest" in deps or "vitest" in test_script:
                return {"framework": "vitest", "cmd": "npx vitest run --reporter=json"}
            if "jest" in deps or "jest" in test_script:
                return {"framework": "jest", "cmd": "npx jest --json --silent"}
            if "mocha" in deps or "mocha" in test_script:
                return {"framework": "mocha", "cmd": "npx mocha --reporter json"}
            if test_script and test_script != "echo \"Error: no test specified\" && exit 1":
                return {"framework": "npm_test", "cmd": "npm test"}
        except (json.JSONDecodeError, OSError):
            pass

    return None


def _parse_pytest_output(stdout: str, stderr: str) -> Dict[str, int]:
    """Parse pytest short summary output."""
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    # Pytest summary line: "5 passed, 2 failed, 1 error in 1.23s"
    combined = stdout + stderr
    summary_match = re.search(r"([\d]+ passed)?(.*?)([\d]+ failed)?(.*?)([\d]+ error)?(.*?)([\d]+ skipped)?", combined)
    for key in counts:
        m = re.search(rf"(\d+) {key}", combined)
        if m:
            counts[key] = int(m.group(1))
    return counts


def _parse_jest_output(stdout: str) -> Dict[str, int]:
    """Parse jest JSON output."""
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    try:
        data = json.loads(stdout)
        counts["passed"] = data.get("numPassedTests", 0)
        counts["failed"] = data.get("numFailedTests", 0)
        counts["skipped"] = data.get("numPendingTests", 0)
    except (json.JSONDecodeError, KeyError):
        # Fallback: regex parse
        m = re.search(r"Tests:\s+(\d+) passed", stdout)
        if m:
            counts["passed"] = int(m.group(1))
        m = re.search(r"Tests:\s+(\d+) failed", stdout)
        if m:
            counts["failed"] = int(m.group(1))
    return counts


def test_smoke(project_path: str, test_suite: Optional[str] = None) -> Dict[str, Any]:
    """Detect test framework and run tests. Returns pass/fail/error counts.

    Works by detecting the test framework from project config files,
    then running the appropriate test command and parsing the output.
    """
    project = Path(project_path).resolve()
    if not project.is_dir():
        return {"error": "project_not_found", "message": f"Directory not found: {project_path}"}

    detected = _detect_test_framework(project)
    if detected is None:
        return {
            "tool": "test.smoke",
            "status": "no_framework",
            "error": "No test framework detected. Looked for: pytest.ini, pyproject.toml, package.json scripts.test",
            "project_path": str(project),
        }

    framework = detected["framework"]
    cmd = detected["cmd"]

    # Build command as list (never shell=True with user input)
    import shlex
    cmd_list = shlex.split(cmd)

    # If a specific suite is requested, validate and append
    if test_suite:
        # Sanitize: only allow alphanumeric, slashes, dots, underscores, hyphens, colons
        import re
        if not re.match(r'^[\w/.\-:*\[\]]+$', test_suite):
            return {"tool": "test.smoke", "status": "error", "error": f"Invalid test_suite: {test_suite}"}
        cmd_list.append(test_suite)

    # Detect the right Python executable
    if framework == "pytest":
        python_found = False
        for venv_dir in ["venv", ".venv", "env"]:
            venv_python = project / venv_dir / "bin" / "python"
            if venv_python.exists():
                cmd_list[0] = str(venv_python)
                python_found = True
                break
        if not python_found:
            import sys as _sys
            cmd_list[0] = _sys.executable

    try:
        result = subprocess.run(
            cmd_list,
            shell=False,
            cwd=str(project),
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "CI": "1", "FORCE_COLOR": "0"},
        )
    except subprocess.TimeoutExpired:
        return {
            "tool": "test.smoke",
            "status": "timeout",
            "error": "Test execution timed out after 120 seconds",
            "framework_detected": framework,
            "project_path": str(project),
        }
    except OSError as e:
        return {
            "tool": "test.smoke",
            "status": "execution_error",
            "error": f"Failed to run test command: {e}",
            "framework_detected": framework,
            "command": cmd,
        }

    # Parse output based on framework
    if framework == "pytest":
        counts = _parse_pytest_output(result.stdout, result.stderr)
    elif framework in ("jest", "vitest"):
        counts = _parse_jest_output(result.stdout)
    else:
        counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
        # Try generic parsing
        for key in counts:
            m = re.search(rf"(\d+) {key}", result.stdout + result.stderr)
            if m:
                counts[key] = int(m.group(1))

    # Truncate output to keep response reasonable
    output = (result.stdout + result.stderr).strip()
    if len(output) > 3000:
        output = output[:1500] + "\n\n... [truncated] ...\n\n" + output[-1500:]

    return {
        "tool": "test.smoke",
        "status": "ok",
        "exit_code": result.returncode,
        "framework_detected": framework,
        "passed": counts["passed"],
        "failed": counts["failed"],
        "errors": counts["errors"],
        "skipped": counts.get("skipped", 0),
        "all_passed": result.returncode == 0,
        "output": output,
        "command": cmd,
        "project_path": str(project),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════
#  docs_generate — Extract docstrings/JSDoc and build markdown reference
# ═══════════════════════════════════════════════════════════════════════

def _extract_python_docs(file_path: Path) -> List[Dict[str, str]]:
    """Extract function signatures and docstrings from a Python file."""
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    docs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            args = []
            for arg in node.args.args:
                if arg.arg == "self":
                    continue
                annotation = ""
                if arg.annotation:
                    annotation = ast.unparse(arg.annotation) if hasattr(ast, "unparse") else ""
                args.append(f"{arg.arg}: {annotation}" if annotation else arg.arg)

            ret_annotation = ""
            if node.returns:
                ret_annotation = ast.unparse(node.returns) if hasattr(ast, "unparse") else ""

            sig = f"{'async ' if isinstance(node, ast.AsyncFunctionDef) else ''}def {node.name}({', '.join(args)})"
            if ret_annotation:
                sig += f" -> {ret_annotation}"

            docstring = ast.get_docstring(node) or ""
            docs.append({
                "name": node.name,
                "signature": sig,
                "docstring": docstring,
                "lineno": node.lineno,
            })
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            docstring = ast.get_docstring(node) or ""
            docs.append({
                "name": node.name,
                "signature": f"class {node.name}",
                "docstring": docstring,
                "lineno": node.lineno,
            })
    return docs


def _extract_jsdoc(file_path: Path) -> List[Dict[str, str]]:
    """Extract JSDoc comments and associated function signatures from JS/TS files."""
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return []

    docs = []
    # Match JSDoc blocks followed by function-like declarations
    pattern = r"/\*\*(.*?)\*/\s*(?:export\s+)?(?:async\s+)?(?:function\s+(\w+)|(?:const|let|var)\s+(\w+))"
    for m in re.finditer(pattern, source, re.DOTALL):
        jsdoc_body = m.group(1).strip()
        name = m.group(2) or m.group(3)
        if not name:
            continue
        # Clean up JSDoc
        cleaned = re.sub(r"^\s*\*\s?", "", jsdoc_body, flags=re.MULTILINE).strip()
        lineno = source[:m.start()].count("\n") + 1
        docs.append({
            "name": name,
            "signature": name,
            "docstring": cleaned,
            "lineno": lineno,
        })

    # Also find functions without JSDoc
    func_pattern = r"(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\("
    for m in re.finditer(func_pattern, source):
        name = m.group(1)
        if not any(d["name"] == name for d in docs) and not name.startswith("_"):
            lineno = source[:m.start()].count("\n") + 1
            docs.append({
                "name": name,
                "signature": name,
                "docstring": "",
                "lineno": lineno,
            })

    return docs


def docs_generate(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    """Generate API reference documentation by scanning source files for docstrings/JSDoc.

    Extracts function signatures and documentation strings, then produces
    a markdown file organized by source file.
    """
    project = Path(target).resolve()
    if not project.is_dir():
        return {"error": "project_not_found", "message": f"Directory not found: {target}"}

    skip_dirs = {"node_modules", "__pycache__", "venv", ".venv", ".git", "dist", "build"}

    all_docs = {}
    files_processed = 0
    functions_documented = 0
    functions_undocumented = 0

    # Scan Python files
    for py_file in sorted(project.rglob("*.py")):
        if any(d in py_file.parts for d in skip_dirs):
            continue
        if py_file.name.startswith("test_") or py_file.name == "conftest.py":
            continue
        docs = _extract_python_docs(py_file)
        if docs:
            files_processed += 1
            rel = str(py_file.relative_to(project))
            all_docs[rel] = docs
            for d in docs:
                if d["docstring"]:
                    functions_documented += 1
                else:
                    functions_undocumented += 1

    # Scan JS/TS files
    for ext in ("*.js", "*.ts", "*.jsx", "*.tsx"):
        for js_file in sorted(project.rglob(ext)):
            if any(d in js_file.parts for d in skip_dirs):
                continue
            if ".test." in js_file.name or ".spec." in js_file.name:
                continue
            docs = _extract_jsdoc(js_file)
            if docs:
                files_processed += 1
                rel = str(js_file.relative_to(project))
                all_docs[rel] = docs
                for d in docs:
                    if d["docstring"]:
                        functions_documented += 1
                    else:
                        functions_undocumented += 1

    # Generate markdown
    md_lines = [
        "# API Reference",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"Files: {files_processed} | Documented: {functions_documented} | Missing docs: {functions_undocumented}",
        "",
        "---",
        "",
    ]

    for file_path, docs in sorted(all_docs.items()):
        md_lines.append(f"## `{file_path}`")
        md_lines.append("")
        for d in docs:
            md_lines.append(f"### `{d['signature']}`")
            md_lines.append("")
            if d["docstring"]:
                md_lines.append(d["docstring"])
            else:
                md_lines.append("*No documentation.*")
            md_lines.append("")
            md_lines.append(f"*Line {d['lineno']}*")
            md_lines.append("")
        md_lines.append("---")
        md_lines.append("")

    output_path = project / "API_REFERENCE.md"
    output_path.write_text("\n".join(md_lines), encoding="utf-8")

    return {
        "tool": "docs.generate",
        "status": "ok",
        "files_processed": files_processed,
        "functions_documented": functions_documented,
        "functions_undocumented": functions_undocumented,
        "output_path": str(output_path),
        "project_path": str(project),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════
#  docs_validate — Check documentation quality and completeness
# ═══════════════════════════════════════════════════════════════════════

def _check_broken_links(md_file: Path, project: Path) -> List[str]:
    """Check for broken internal links in a markdown file."""
    issues = []
    try:
        content = md_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return issues

    # Find markdown links: [text](path)
    for m in re.finditer(r"\[([^\]]*)\]\(([^)]+)\)", content):
        link_text = m.group(1)
        link_target = m.group(2)

        # Skip external URLs and anchors
        if link_target.startswith(("http://", "https://", "mailto:", "#")):
            continue

        # Strip anchors from path
        path_part = link_target.split("#")[0]
        if not path_part:
            continue

        # Resolve relative to the markdown file's directory
        target_path = (md_file.parent / path_part).resolve()
        if not target_path.exists():
            rel_md = str(md_file.relative_to(project))
            issues.append(f"Broken link in {rel_md}: [{link_text}]({link_target})")

    return issues


def _check_python_docstring_coverage(file_path: Path) -> Dict[str, Any]:
    """Check docstring coverage for public functions in a Python file."""
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, UnicodeDecodeError):
        return {"total": 0, "documented": 0, "missing": []}

    total = 0
    documented = 0
    missing = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name.startswith("_"):
                continue
            total += 1
            ds = ast.get_docstring(node)
            if ds:
                documented += 1
            else:
                missing.append(f"{node.name} (line {node.lineno})")

    return {"total": total, "documented": documented, "missing": missing}


def docs_validate(target: str = ".", options: Optional[Dict] = None) -> Dict[str, Any]:
    """Validate documentation quality: README existence, docstring coverage, broken links.

    Pure filesystem analysis with no external dependencies.
    """
    project = Path(target).resolve()
    if not project.is_dir():
        return {"error": "project_not_found", "message": f"Directory not found: {target}"}

    issues = []
    skip_dirs = {"node_modules", "__pycache__", "venv", ".venv", ".git", "dist", "build"}

    # 1. Check README
    has_readme = False
    for name in ("README.md", "readme.md", "README.rst", "README.txt", "README"):
        if (project / name).exists():
            has_readme = True
            break
    if not has_readme:
        issues.append({"severity": "error", "message": "No README file found in project root"})

    # 2. Check docstring coverage on Python files
    total_public = 0
    total_documented = 0
    missing_docs = []

    for py_file in sorted(project.rglob("*.py")):
        if any(d in py_file.parts for d in skip_dirs):
            continue
        if py_file.name.startswith("test_") or py_file.name == "conftest.py":
            continue

        coverage = _check_python_docstring_coverage(py_file)
        total_public += coverage["total"]
        total_documented += coverage["documented"]
        if coverage["missing"]:
            rel = str(py_file.relative_to(project))
            for m in coverage["missing"]:
                missing_docs.append(f"{rel}: {m}")

    # 3. Check broken internal links in all markdown files
    broken_links = []
    for md_file in sorted(project.rglob("*.md")):
        if any(d in md_file.parts for d in skip_dirs):
            continue
        broken_links.extend(_check_broken_links(md_file, project))

    for bl in broken_links:
        issues.append({"severity": "warning", "message": bl})

    # 4. Check for changelog
    has_changelog = any(
        (project / name).exists()
        for name in ("CHANGELOG.md", "CHANGES.md", "HISTORY.md", "changelog.md")
    )
    if not has_changelog:
        issues.append({"severity": "info", "message": "No CHANGELOG file found"})

    # Calculate coverage percentage
    coverage_percent = round((total_documented / total_public * 100), 1) if total_public > 0 else 0.0

    # Add docstring coverage issues
    if coverage_percent < 50:
        issues.append({
            "severity": "warning",
            "message": f"Low docstring coverage: {coverage_percent}% ({total_documented}/{total_public} public functions)"
        })

    return {
        "tool": "docs.validate",
        "status": "ok",
        "project_path": str(project),
        "has_readme": has_readme,
        "has_changelog": has_changelog,
        "coverage_percent": coverage_percent,
        "total_public_functions": total_public,
        "documented_functions": total_documented,
        "issues": issues,
        "missing_docs": missing_docs[:50],  # Cap at 50 to keep response reasonable
        "broken_links": broken_links[:20],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
