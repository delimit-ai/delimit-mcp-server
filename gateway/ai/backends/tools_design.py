"""
Real implementations for design/* and story/* tools.
Works WITHOUT Figma or Storybook by default — scans local project files.
Optional Figma API integration when FIGMA_TOKEN env var is set.
"""

import ast
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.ai.tools_design")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_playwright() -> bool:
    """Check whether Playwright is importable and browsers are installed."""
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


_PLAYWRIGHT_PYTHON: Optional[str] = None


def _resolve_playwright_python() -> str:
    """Return a python interpreter that can ``import playwright``.

    (LED-2316) The MCP server's own venv (``sys.executable``) frequently lacks
    playwright — it is installed in a separate suite venv (e.g.
    ``~/.delimit_suite/venv``). Launching the responsive sandbox with a bare
    ``sys.executable`` therefore reported "Playwright is not installed in the
    subprocess environment" even though a sitewide install exists. Probe
    candidate interpreters, cache the first that can import playwright, and fall
    back to ``sys.executable`` so the sandbox still runs (and surfaces its own
    error) if none is found.
    """
    global _PLAYWRIGHT_PYTHON
    if _PLAYWRIGHT_PYTHON:
        return _PLAYWRIGHT_PYTHON
    import glob as _glob
    candidates = [
        sys.executable,
        "/home/delimit/.delimit_suite/venv/bin/python",
        os.path.expanduser("~/.delimit_suite/venv/bin/python"),
        shutil.which("python3"),
    ]
    candidates += _glob.glob("/home/*/.delimit_suite/venv/bin/python")
    seen = set()
    for c in candidates:
        if not c or c in seen:
            continue
        seen.add(c)
        try:
            if not Path(c).exists():
                continue
            probe = subprocess.run(
                [c, "-c", "import playwright"], capture_output=True, timeout=10
            )
            if probe.returncode == 0:
                _PLAYWRIGHT_PYTHON = c
                return c
        except Exception:
            continue
    return sys.executable


def _find_files(root: Path, extensions: List[str], max_depth: int = 6) -> List[Path]:
    """Recursively find files by extension, skipping node_modules/dist/.next."""
    skip = {"node_modules", "dist", ".next", ".git", "__pycache__", "build", ".cache"}
    results: List[Path] = []
    if not root.is_dir():
        return results

    def _walk(p: Path, depth: int):
        if depth > max_depth:
            return
        try:
            for child in sorted(p.iterdir()):
                if child.name in skip:
                    continue
                if child.is_dir():
                    _walk(child, depth + 1)
                elif child.suffix in extensions:
                    results.append(child)
        except PermissionError:
            pass

    _walk(root, 0)
    return results


def _read_text(path: Path, limit: int = 200_000) -> str:
    """Read file text, capped at *limit* chars."""
    try:
        return path.read_text(errors="replace")[:limit]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# CSS / Tailwind token extraction helpers
# ---------------------------------------------------------------------------

_CSS_VAR_RE = re.compile(r"--([a-zA-Z0-9_-]+)\s*:\s*([^;]+);")
_MEDIA_QUERY_RE = re.compile(r"@media[^{]*\(\s*(?:min|max)-width\s*:\s*([^)]+)\)")

# LED-1010: selector-aware extraction. Captures the selector (e.g. `:root`,
# `.dark`, `[data-theme="dark"]`) that owns each custom property block so
# light/dark variants of the same token name don't silently dedupe into one.
_CSS_BLOCK_RE = re.compile(
    r"(?P<selector>[^{}@\n][^{}]{0,200})\{(?P<body>[^{}]*)\}",
    re.DOTALL,
)


def _mode_from_selector(selector: str) -> str:
    """Best-effort mode guess from a CSS selector: 'dark' / 'light' / 'base'."""
    s = selector.strip().lower()
    if any(t in s for t in (".dark", "[data-theme=\"dark\"]", "[data-mode=\"dark\"]", "prefers-color-scheme: dark")):
        return "dark"
    if any(t in s for t in (".light", "[data-theme=\"light\"]", "[data-mode=\"light\"]", "prefers-color-scheme: light")):
        return "light"
    if s in (":root", "html", "body", "*"):
        return "base"
    return "scoped"


# LED-1010: common domain/semantic token prefixes. Anything starting with these
# reads as application-meaning rather than theme-primitive, and belongs in the
# `semantic` bucket, not `other`. Downstream generators need this split to know
# which tokens are safe to remap vs which carry app meaning.
_SEMANTIC_PREFIXES = (
    "score-", "status-", "blur-", "price-", "rank-", "tier-", "risk-",
    "badge-", "level-", "alert-",
)


def _token_taxonomy(name: str) -> str:
    """Classify a token name as primitive | semantic | other."""
    n = name.lower().lstrip("-")
    if any(n.startswith(p) for p in _SEMANTIC_PREFIXES):
        return "semantic"
    # Core theme primitives
    if any(n.startswith(p) for p in (
        "color-", "bg-", "text-", "border-", "fill-", "stroke-",
        "accent", "primary", "secondary", "muted", "foreground",
        "background", "surface", "ring-", "input-",
        "space-", "spacing-", "gap-", "size-", "radius-",
        "font-", "leading-", "tracking-",
    )):
        return "primitive"
    return "other"


def _extract_css_variables(text: str, source: str = "") -> Dict[str, List[Dict[str, str]]]:
    """Extract CSS custom properties grouped by category.

    LED-1010: now selector-aware. Each returned entry carries `selector`
    (the CSS rule it was declared in), `mode` (dark/light/base/scoped), and
    `taxonomy` (primitive/semantic/other) so consumers can tell
    `--bg-base` in `:root` from `--bg-base` in `.dark`.
    """
    colors: List[Dict[str, str]] = []
    spacing: List[Dict[str, str]] = []
    typography: List[Dict[str, str]] = []
    other: List[Dict[str, str]] = []
    semantic: List[Dict[str, str]] = []

    # Walk each CSS rule block so we know which selector owns each declaration.
    for m in _CSS_BLOCK_RE.finditer(text):
        selector = m.group("selector").strip()
        mode = _mode_from_selector(selector)
        body = m.group("body")
        for name, value in _CSS_VAR_RE.findall(body):
            value = value.strip()
            taxonomy = _token_taxonomy(name)
            entry = {
                "name": f"--{name}",
                "value": value,
                "selector": selector,
                "mode": mode,
                "taxonomy": taxonomy,
            }
            if source:
                entry["source"] = source
            lower = name.lower()

            # Semantic tokens bypass the keyword bucket: they carry app meaning
            # and should be routed to the semantic bucket regardless of value type.
            if taxonomy == "semantic":
                semantic.append(entry)
                continue

            if any(k in lower for k in ("color", "bg", "text", "border", "fill", "stroke", "accent", "primary", "secondary", "surface", "foreground", "background", "ring")):
                colors.append(entry)
            elif any(k in lower for k in ("space", "gap", "margin", "padding", "size", "width", "height", "radius")):
                spacing.append(entry)
            elif any(k in lower for k in ("font", "line", "letter", "text", "heading", "leading", "tracking")):
                typography.append(entry)
            elif _is_color_value(value):
                colors.append(entry)
            else:
                other.append(entry)

    return {
        "colors": colors,
        "spacing": spacing,
        "typography": typography,
        "other": other,
        "semantic": semantic,
    }


def _is_color_value(v: str) -> bool:
    v = v.lower().strip()
    if v.startswith("#") and len(v) in (4, 7, 9):
        return True
    if v.startswith(("rgb", "hsl", "oklch", "lab(", "lch(")):
        return True
    return False


def _parse_tailwind_config(text: str) -> Dict[str, Any]:
    """Best-effort parse of tailwind.config.{js,ts,mjs} into token counts."""
    colors_count = 0
    spacing_count = 0
    breakpoints: List[str] = []

    # Extract theme.extend or theme sections
    for match in re.finditer(r"colors\s*:\s*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}", text):
        block = match.group(1)
        colors_count += block.count(":")

    for match in re.finditer(r"spacing\s*:\s*\{([^}]*)\}", text):
        block = match.group(1)
        spacing_count += block.count(":")

    for match in re.finditer(r"screens\s*:\s*\{([^}]*)\}", text):
        block = match.group(1)
        for bp_match in re.finditer(r"['\"]?(\w+)['\"]?\s*:", block):
            breakpoints.append(bp_match.group(1))

    return {"colors_count": colors_count, "spacing_count": spacing_count, "breakpoints": breakpoints}


# ---------------------------------------------------------------------------
# LED-1010: Tailwind-awareness helpers
# ---------------------------------------------------------------------------

# Tailwind ships an opinionated default scale. When a repo has tailwind.config.js
# and doesn't override these, the framework defaults are the design tokens in
# use — `extract_tokens` previously reported 0 for each because it only scanned
# CSS `--*` variables.
TAILWIND_DEFAULT_BREAKPOINTS = ["sm=640px", "md=768px", "lg=1024px", "xl=1280px", "2xl=1536px"]
TAILWIND_DEFAULT_SPACING = [
    "0=0px", "px=1px", "0.5=0.125rem", "1=0.25rem", "1.5=0.375rem", "2=0.5rem",
    "2.5=0.625rem", "3=0.75rem", "3.5=0.875rem", "4=1rem", "5=1.25rem", "6=1.5rem",
    "7=1.75rem", "8=2rem", "9=2.25rem", "10=2.5rem", "11=2.75rem", "12=3rem",
    "14=3.5rem", "16=4rem", "20=5rem", "24=6rem", "28=7rem", "32=8rem", "36=9rem",
    "40=10rem", "44=11rem", "48=12rem", "52=13rem", "56=14rem", "60=15rem",
    "64=16rem", "72=18rem", "80=20rem", "96=24rem",
]
TAILWIND_DEFAULT_FONT_SIZES = [
    "text-xs=0.75rem", "text-sm=0.875rem", "text-base=1rem", "text-lg=1.125rem",
    "text-xl=1.25rem", "text-2xl=1.5rem", "text-3xl=1.875rem", "text-4xl=2.25rem",
    "text-5xl=3rem", "text-6xl=3.75rem", "text-7xl=4.5rem", "text-8xl=6rem",
    "text-9xl=8rem",
]

# Responsive prefix regex. Matches `sm:`, `md:`, `lg:`, `xl:`, `2xl:` in a
# JSX className/string context. Tailwind is mobile-first by default: these
# prefixes apply at and above the named breakpoint.
_TAILWIND_RESPONSIVE_PREFIX_RE = re.compile(r"(?<![\w-])(sm|md|lg|xl|2xl):[\w\[\]-]+", re.IGNORECASE)

# Dark-mode class usage in JSX: `dark:bg-black` etc.
_TAILWIND_DARK_PREFIX_RE = re.compile(r"(?<![\w-])dark:[\w\[\]-]+")

# Tailwind spacing/layout utility class usage — rough counter for the
# responsive_units_count signal.
_TAILWIND_UTILITY_RE = re.compile(
    r"(?<![\w-])(?:w|h|m|p|mx|my|mt|mb|ml|mr|px|py|pt|pb|pl|pr|gap|space|"
    r"max-w|min-w|max-h|min-h|inset|top|bottom|left|right|z|grid-cols|col-span)"
    r"-[\w./\[\]%-]+"
)


def _has_tailwind_config(root: Path) -> Optional[Path]:
    """Return the first found tailwind.config.{js,ts,mjs,cjs} or None."""
    for tw_name in ("tailwind.config.js", "tailwind.config.ts", "tailwind.config.mjs", "tailwind.config.cjs"):
        tw_path = root / tw_name
        if tw_path.exists():
            return tw_path
    # Tailwind v4 uses @import "tailwindcss" in CSS with no separate config
    return None


def _detect_tailwind_v4(root: Path) -> bool:
    """Detect Tailwind v4 (no config file, uses @import "tailwindcss" in CSS)."""
    for cf in list(root.rglob("*.css"))[:20]:
        try:
            text = cf.read_text(errors="replace")[:5000]
            if '@import "tailwindcss"' in text or "@import 'tailwindcss'" in text:
                return True
        except Exception:
            continue
    return False


def _scan_tailwind_utilities(root: Path) -> Dict[str, Any]:
    """Scan JSX/TSX/Vue/Svelte files for Tailwind utility class usage.

    Returns counts of responsive-prefixed classes, dark-prefix classes,
    and general utility classes — what the old validate_responsive missed
    entirely when it only looked at raw CSS.
    """
    component_files = _find_files(root, [".tsx", ".jsx", ".ts", ".js", ".vue", ".svelte", ".html"])
    responsive_hits = 0
    dark_hits = 0
    utility_hits = 0
    breakpoints_seen: set = set()
    files_with_utilities = 0
    # Cap to keep runtime bounded on huge monorepos
    for cf in component_files[:500]:
        text = _read_text(cf, limit=200_000)
        if not text:
            continue
        # Require a Tailwind signal before counting (otherwise every JS file
        # matches via collisions like `py-pi`)
        if "className" not in text and "class=" not in text and "tw`" not in text:
            continue
        file_had_utility = False
        for m in _TAILWIND_RESPONSIVE_PREFIX_RE.finditer(text):
            responsive_hits += 1
            breakpoints_seen.add(m.group(1).lower())
            file_had_utility = True
        dark_hits += len(_TAILWIND_DARK_PREFIX_RE.findall(text))
        for m in _TAILWIND_UTILITY_RE.finditer(text):
            utility_hits += 1
            file_had_utility = True
        if file_had_utility:
            files_with_utilities += 1
    return {
        "responsive_prefix_count": responsive_hits,
        "dark_prefix_count": dark_hits,
        "utility_count": utility_hits,
        "files_with_utilities": files_with_utilities,
        "files_scanned": len(component_files),
        "breakpoints_seen": sorted(breakpoints_seen, key=lambda b: ["sm", "md", "lg", "xl", "2xl"].index(b) if b in ["sm", "md", "lg", "xl", "2xl"] else 99),
    }


# ---------------------------------------------------------------------------
# LED-1010: status taxonomy
# ---------------------------------------------------------------------------
# Consumers branch on `status` to decide whether to gate CI. A tool that
# returns `ok` while producing partial/wrong results is a silent failure —
# the biggest gap identified by the 2026-04-24 pilot run. These constants
# define the explicit vocabulary.

STATUS_OK = "ok"                          # all checks ran, no gaps
STATUS_DEGRADED = "degraded"              # ran, but known gaps (e.g. only CSS scanned, not JSX)
STATUS_PARTIAL_COVERAGE = "partial_coverage"  # ran a subset of the requested standard
STATUS_TOOLCHAIN_MISSING = "toolchain_missing"  # required external tool absent
STATUS_ERROR = "error"                    # unrecoverable


# ---------------------------------------------------------------------------
# Component scanning helpers
# ---------------------------------------------------------------------------

_REACT_COMPONENT_RE = re.compile(
    r"(?:export\s+(?:default\s+)?)?(?:function|const)\s+([A-Z][A-Za-z0-9]*)"
)
_PROPS_INTERFACE_RE = re.compile(
    r"(?:interface|type)\s+(\w+Props)\s*(?:=\s*)?\{([^}]*)\}", re.DOTALL
)
_EXPORT_RE = re.compile(r"export\s+(?:default\s+)?(?:function|const|class)\s+(\w+)")
_VUE_NAME_RE = re.compile(r"name\s*:\s*['\"]([^'\"]+)['\"]")
_SVELTE_EXPORT_RE = re.compile(r"export\s+let\s+(\w+)")


def _scan_react_component(path: Path, text: str) -> Optional[Dict[str, Any]]:
    """Extract component metadata from a React/TSX/JSX file."""
    components = _REACT_COMPONENT_RE.findall(text)
    if not components:
        return None
    exports = _EXPORT_RE.findall(text)
    props_raw = _PROPS_INTERFACE_RE.findall(text)
    props: List[str] = []
    for _name, body in props_raw:
        for line in body.strip().split("\n"):
            line = line.strip().rstrip(";").rstrip(",")
            if line and not line.startswith("//"):
                props.append(line)
    return {
        "name": components[0],
        "path": str(path),
        "props": props,
        "exports": exports,
        "framework": "react",
    }


def _scan_vue_component(path: Path, text: str) -> Optional[Dict[str, Any]]:
    m = _VUE_NAME_RE.search(text)
    name = m.group(1) if m else path.stem
    props = re.findall(r"(?:defineProps|props)\s*(?:<[^>]+>)?\s*\(\s*\{([^}]*)\}", text, re.DOTALL)
    prop_list = []
    for block in props:
        for line in block.strip().split("\n"):
            line = line.strip().rstrip(",")
            if line and not line.startswith("//"):
                prop_list.append(line)
    return {"name": name, "path": str(path), "props": prop_list, "exports": [name], "framework": "vue"}


def _scan_svelte_component(path: Path, text: str) -> Optional[Dict[str, Any]]:
    props = _SVELTE_EXPORT_RE.findall(text)
    return {"name": path.stem, "path": str(path), "props": props, "exports": [path.stem], "framework": "svelte"}


# ---------------------------------------------------------------------------
# 19. design_extract_tokens
# ---------------------------------------------------------------------------

def design_extract_tokens(
    figma_file_key: Optional[str] = None,
    token_types: Optional[List[str]] = None,
    project_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Extract design tokens from project CSS/SCSS/Tailwind config.

    If a Figma token is available and figma_file_key provided, fetches from Figma API.
    Otherwise scans local project files for CSS variables, Tailwind config, etc.

    Token resolution order: FIGMA_TOKEN env var -> ~/.delimit/secrets/figma.json -> free fallback.
    """
    from ai.key_resolver import get_figma_token
    figma_token, _token_source = get_figma_token()
    if figma_token and figma_file_key:
        return _figma_extract_tokens(figma_file_key, figma_token, token_types)

    # Local extraction
    root = Path(project_path) if project_path else Path.cwd()
    if not root.is_dir():
        return {"tool": "design.extract_tokens", "error": f"Directory not found: {root}"}

    all_tokens: Dict[str, List] = {
        "colors": [], "spacing": [], "typography": [], "breakpoints": [],
        "other": [], "semantic": [],
    }
    source_files: List[str] = []
    coverage: Dict[str, Any] = {
        "css_scanned": False,
        "tailwind_config_found": False,
        "tailwind_v4_detected": False,
        "tailwind_defaults_emitted": False,
        "selector_aware": True,
        "semantic_taxonomy": True,
    }
    gaps: List[str] = []

    # 1. Tailwind config (LED-1010: emit framework defaults when present)
    tw_path = _has_tailwind_config(root)
    tw_v4 = False
    tw_config_text = ""
    if tw_path:
        coverage["tailwind_config_found"] = True
        tw_config_text = _read_text(tw_path)
        parsed = _parse_tailwind_config(tw_config_text)
        source_files.append(str(tw_path))
        # User-defined breakpoints override defaults
        if parsed["breakpoints"]:
            all_tokens["breakpoints"].extend(
                [{"name": bp, "source": str(tw_path), "origin": "tailwind_config"} for bp in parsed["breakpoints"]]
            )
    else:
        tw_v4 = _detect_tailwind_v4(root)
        coverage["tailwind_v4_detected"] = tw_v4

    # If Tailwind is in play at all, surface the framework's default token
    # scales so downstream consumers can see what utility classes reference.
    # Previously these returned zero and the caller couldn't tell if the
    # design system was truly empty or just invisible to us.
    if tw_path or tw_v4:
        coverage["tailwind_defaults_emitted"] = True
        framework_source = str(tw_path) if tw_path else "tailwind-v4 (@import)"

        # Emit defaults only when user config didn't already cover them
        if not all_tokens["breakpoints"]:
            for bp_def in TAILWIND_DEFAULT_BREAKPOINTS:
                name, value = bp_def.split("=", 1)
                all_tokens["breakpoints"].append({
                    "name": name, "value": value, "source": framework_source,
                    "origin": "tailwind_default",
                })

        for sp_def in TAILWIND_DEFAULT_SPACING:
            name, value = sp_def.split("=", 1)
            all_tokens["spacing"].append({
                "name": f"spacing.{name}", "value": value, "source": framework_source,
                "origin": "tailwind_default", "taxonomy": "primitive",
            })

        for fs_def in TAILWIND_DEFAULT_FONT_SIZES:
            name, value = fs_def.split("=", 1)
            all_tokens["typography"].append({
                "name": name, "value": value, "source": framework_source,
                "origin": "tailwind_default", "taxonomy": "primitive",
            })

    # 2. CSS / SCSS files (now selector-aware via _extract_css_variables)
    css_files = _find_files(root, [".css", ".scss", ".sass"])
    for cf in css_files:
        text = _read_text(cf)
        if "--" not in text and "@media" not in text:
            continue
        source_files.append(str(cf))
        coverage["css_scanned"] = True
        vars_found = _extract_css_variables(text, source=str(cf))
        for cat in ("colors", "spacing", "typography", "other", "semantic"):
            all_tokens[cat].extend(vars_found.get(cat, []))

        # breakpoints from media queries (user-authored, origin=css_media)
        for bp_val in _MEDIA_QUERY_RE.findall(text):
            all_tokens["breakpoints"].append({
                "value": bp_val.strip(), "source": str(cf),
                "origin": "css_media",
            })

    # 3. Filter by token_types if specified
    if token_types:
        all_tokens = {k: v for k, v in all_tokens.items() if k in token_types}

    # LED-1010 dark-mode dedup: collapse (name, mode) duplicates rather than
    # eliding mode altogether. A token with the same name in `:root` and
    # `.dark` is one logical token with two values — surface both.
    for cat in ("colors", "spacing", "typography", "other", "semantic"):
        if cat not in all_tokens:
            continue
        seen: Dict[tuple, Dict] = {}
        for entry in all_tokens[cat]:
            key = (entry.get("name", ""), entry.get("mode", ""))
            if key in seen:
                continue
            seen[key] = entry
        all_tokens[cat] = list(seen.values())

    # Breakpoint dedup by (name|value, origin)
    seen_bp: set = set()
    unique_bp: List[Dict[str, Any]] = []
    for bp in all_tokens.get("breakpoints", []):
        key = (bp.get("name", bp.get("value", "")), bp.get("origin", ""))
        if key in seen_bp:
            continue
        seen_bp.add(key)
        unique_bp.append(bp)
    if "breakpoints" in all_tokens:
        all_tokens["breakpoints"] = unique_bp

    # Coverage gaps → status taxonomy (LED-1010)
    if not coverage["css_scanned"] and not (tw_path or tw_v4):
        gaps.append("No CSS variables and no Tailwind config found — project may not use standard design tokens")
    if (tw_path or tw_v4) and not coverage["css_scanned"]:
        gaps.append("Tailwind detected but no user CSS variables found — only framework defaults emitted")

    if gaps:
        status = STATUS_PARTIAL_COVERAGE
    else:
        status = STATUS_OK

    total = sum(len(v) for v in all_tokens.values() if isinstance(v, list))
    result = {
        "tool": "design.extract_tokens",
        "status": status,
        "tokens": all_tokens,
        "total_tokens": total,
        "source_files": sorted(set(source_files)),
        "coverage": coverage,
        "gaps": gaps,
        "figma_used": False,
    }
    # If user passed a figma_file_key but no token is available, add a hint
    if figma_file_key and not figma_token:
        result["hint"] = (
            "Figma integration available -- set FIGMA_TOKEN env var "
            "or store your token with `delimit_secret_store key=figma value=<token>`. "
            "Local CSS/Tailwind tokens were extracted instead."
        )
    return result


def _figma_extract_tokens(file_key: str, token: str, token_types: Optional[List[str]]) -> Dict[str, Any]:
    """Fetch design tokens from Figma API."""
    try:
        import urllib.request
        url = f"https://api.figma.com/v1/files/{file_key}/styles"
        req = urllib.request.Request(url, headers={"X-Figma-Token": token})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        styles = data.get("meta", {}).get("styles", [])
        tokens: Dict[str, List] = {"colors": [], "typography": [], "spacing": [], "other": []}
        for s in styles:
            entry = {"name": s.get("name", ""), "key": s.get("key", ""), "style_type": s.get("style_type", "")}
            stype = s.get("style_type", "").upper()
            if stype == "FILL":
                tokens["colors"].append(entry)
            elif stype == "TEXT":
                tokens["typography"].append(entry)
            else:
                tokens["other"].append(entry)
        if token_types:
            tokens = {k: v for k, v in tokens.items() if k in token_types}
        return {"tool": "design.extract_tokens", "status": "ok", "tokens": tokens,
                "total_tokens": sum(len(v) for v in tokens.values()),
                "source_files": [f"figma:{file_key}"], "figma_used": True}
    except Exception as e:
        return {"tool": "design.extract_tokens", "error": f"Figma API error: {e}", "figma_used": True}


# ---------------------------------------------------------------------------
# 20. design_generate_component
# ---------------------------------------------------------------------------

def design_generate_component(
    component_name: str,
    figma_node_id: Optional[str] = None,
    output_path: Optional[str] = None,
    project_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate a React/Next.js component skeleton.

    Uses Tailwind classes if tailwind.config is detected in the project.
    """
    root = Path(project_path) if project_path else Path.cwd()
    use_tailwind = any((root / n).exists() for n in
                       ("tailwind.config.js", "tailwind.config.ts", "tailwind.config.mjs", "tailwind.config.cjs"))

    # Determine output path
    if output_path:
        out = Path(output_path)
    else:
        # Default: components/<Name>/<Name>.tsx
        comp_dir = root / "components" / component_name
        out = comp_dir / f"{component_name}.tsx"

    # Determine file extension for template
    is_tsx = out.suffix in (".tsx", ".ts")

    # Build component content
    props_type = f"{component_name}Props"
    if use_tailwind:
        style_attr = 'className="p-4"'
    else:
        style_attr = 'style={{ padding: "1rem" }}'

    if is_tsx:
        content = f"""import React from 'react';

export interface {props_type} {{
  /** Primary content */
  children?: React.ReactNode;
  /** Additional CSS class names */
  className?: string;
}}

export default function {component_name}({{ children, className }}: {props_type}) {{
  return (
    <div {style_attr} data-testid="{component_name.lower()}">
      {{children}}
    </div>
  );
}}
"""
    else:
        content = f"""import React from 'react';

/**
 * @param {{{{ children?: React.ReactNode, className?: string }}}} props
 */
export default function {component_name}({{ children, className }}) {{
  return (
    <div {style_attr} data-testid="{component_name.lower()}">
      {{children}}
    </div>
  );
}}
"""

    # Write file
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content)
    except Exception as e:
        return {"tool": "design.generate_component", "error": f"Failed to write: {e}"}

    return {
        "tool": "design.generate_component",
        "status": "ok",
        "component_path": str(out),
        "props": ["children?: React.ReactNode", "className?: string"],
        "template_used": "tailwind" if use_tailwind else "inline-style",
        "typescript": is_tsx,
    }


# ---------------------------------------------------------------------------
# 21. design_generate_tailwind
# ---------------------------------------------------------------------------

def design_generate_tailwind(
    figma_file_key: Optional[str] = None,
    output_path: Optional[str] = None,
    project_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Read existing tailwind.config or generate a default one from detected CSS tokens."""
    root = Path(project_path) if project_path else Path.cwd()
    out = Path(output_path) if output_path else root / "tailwind.config.js"

    # Check for existing config
    for tw_name in ("tailwind.config.js", "tailwind.config.ts", "tailwind.config.mjs", "tailwind.config.cjs"):
        existing = root / tw_name
        if existing.exists():
            text = _read_text(existing)
            parsed = _parse_tailwind_config(text)
            return {
                "tool": "design.generate_tailwind",
                "status": "ok",
                "config_path": str(existing),
                "colors_count": parsed["colors_count"],
                "spacing_values": parsed["spacing_count"],
                "breakpoints": parsed["breakpoints"],
                "generated": False,
            }

    # Generate from CSS tokens
    tokens_result = design_extract_tokens(project_path=str(root))
    tokens = tokens_result.get("tokens", {})
    colors = tokens.get("colors", [])
    spacing = tokens.get("spacing", [])

    # Build color entries
    color_entries = []
    for c in colors[:50]:
        name = c.get("name", "").lstrip("-").replace("-", "_")
        val = c.get("value", "")
        if name and val:
            safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
            color_entries.append(f"        '{safe_name}': '{val}',")

    spacing_entries = []
    for s in spacing[:30]:
        name = s.get("name", "").lstrip("-").replace("-", "_")
        val = s.get("value", "")
        if name and val:
            safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
            spacing_entries.append(f"        '{safe_name}': '{val}',")

    colors_block = "\n".join(color_entries) if color_entries else "        // No CSS color tokens detected"
    spacing_block = "\n".join(spacing_entries) if spacing_entries else "        // No CSS spacing tokens detected"

    config_content = f"""/** @type {{import('tailwindcss').Config}} */
module.exports = {{
  content: [
    './src/**/*.{{js,ts,jsx,tsx,mdx}}',
    './app/**/*.{{js,ts,jsx,tsx,mdx}}',
    './components/**/*.{{js,ts,jsx,tsx,mdx}}',
    './pages/**/*.{{js,ts,jsx,tsx,mdx}}',
  ],
  theme: {{
    extend: {{
      colors: {{
{colors_block}
      }},
      spacing: {{
{spacing_block}
      }},
    }},
  }},
  plugins: [],
}};
"""

    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(config_content)
    except Exception as e:
        return {"tool": "design.generate_tailwind", "error": f"Failed to write: {e}"}

    return {
        "tool": "design.generate_tailwind",
        "status": "ok",
        "config_path": str(out),
        "colors_count": len(color_entries),
        "spacing_values": len(spacing_entries),
        "breakpoints": [],
        "generated": True,
    }


# ---------------------------------------------------------------------------
# 22. design_validate_responsive
# ---------------------------------------------------------------------------

_VIEWPORT_META_RE = re.compile(r'<meta[^>]*name=["\']viewport["\'][^>]*>', re.IGNORECASE)
_RESPONSIVE_UNITS_RE = re.compile(r"(?:vw|vh|vmin|vmax|%|rem|em|clamp|min\(|max\()")


def design_validate_responsive(
    project_path: str,
    check_types: Optional[List[str]] = None,
    url: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate responsive design patterns via static analysis.

    LED-1010: now Tailwind-aware. Scans JSX/TSX/Vue/Svelte for utility-class
    responsive prefixes (sm:/md:/lg:/xl:/2xl:) in addition to @media CSS.
    Tailwind is mobile-first by default; previously the tool reported
    `mobile_first: false` on any Tailwind-only codebase, which was wrong.
    """
    root = Path(project_path)
    if not root.is_dir():
        return {"tool": "design.validate_responsive", "error": f"Directory not found: {root}"}

    issues: List[Dict[str, str]] = []
    breakpoints_found: List[str] = []
    viewport_meta = False
    viewport_blocks_zoom = False
    responsive_units_count = 0
    coverage: Dict[str, Any] = {
        "css_scanned": False,
        "jsx_scanned": False,
        "tailwind_detected": False,
    }

    # Scan HTML files for viewport meta
    html_files = _find_files(root, [".html", ".htm"])
    for hf in html_files:
        text = _read_text(hf)
        m = _VIEWPORT_META_RE.search(text)
        if m:
            viewport_meta = True
            if "maximum-scale" in m.group() or "user-scalable=no" in m.group():
                viewport_blocks_zoom = True
            break

    # Also check Next.js layout files
    if not viewport_meta:
        for layout_name in ("layout.tsx", "layout.jsx", "layout.js", "_document.tsx", "_document.jsx", "_app.tsx"):
            candidates = list(root.rglob(layout_name))
            for c in candidates[:5]:
                text = _read_text(c)
                if "viewport" in text.lower():
                    viewport_meta = True
                    if "maximum-scale" in text.lower() or "userScalable: false" in text:
                        viewport_blocks_zoom = True
                    break
            if viewport_meta:
                break

    if not viewport_meta:
        issues.append({"severity": "warning", "message": "No viewport meta tag detected", "fix": "Add <meta name='viewport' content='width=device-width, initial-scale=1'>"})

    # LED-1010: WCAG2AA (1.4.4) — zoom-blocking viewport
    if viewport_meta and viewport_blocks_zoom:
        issues.append({
            "severity": "error",
            "message": "Viewport blocks user zoom (maximum-scale=1 or user-scalable=no) — WCAG 1.4.4 violation",
            "fix": "Remove maximum-scale and user-scalable restrictions so users can zoom to 200%",
        })

    # Scan CSS for media queries and responsive patterns
    css_files = _find_files(root, [".css", ".scss", ".sass"])
    for cf in css_files:
        text = _read_text(cf)
        if text:
            coverage["css_scanned"] = True
        for bp_val in _MEDIA_QUERY_RE.findall(text):
            bp_val = bp_val.strip()
            if bp_val not in breakpoints_found:
                breakpoints_found.append(bp_val)
        responsive_units_count += len(_RESPONSIVE_UNITS_RE.findall(text))

    # Check for mobile-first patterns (min-width preferred over max-width)
    min_width_count = 0
    max_width_count = 0
    for cf in css_files:
        text = _read_text(cf)
        min_width_count += len(re.findall(r"min-width\s*:", text))
        max_width_count += len(re.findall(r"max-width\s*:", text))

    # LED-1010: Tailwind utility class scan. The old mobile_first heuristic
    # only considered raw CSS and returned `false` on any Tailwind codebase.
    # Tailwind prefixes (sm:/md:/...) are mobile-first by design: they apply
    # AT OR ABOVE the named breakpoint.
    tw_path = _has_tailwind_config(root)
    tw_v4 = False if tw_path else _detect_tailwind_v4(root)
    coverage["tailwind_detected"] = bool(tw_path or tw_v4)

    tw_stats: Dict[str, Any] = {}
    if coverage["tailwind_detected"]:
        tw_stats = _scan_tailwind_utilities(root)
        coverage["jsx_scanned"] = tw_stats.get("files_scanned", 0) > 0

        # Add Tailwind default breakpoints that were actually USED in the codebase
        for bp in tw_stats.get("breakpoints_seen", []):
            bp_label = f"tailwind:{bp}"
            if bp_label not in breakpoints_found:
                breakpoints_found.append(bp_label)

        # Utility classes count as responsive units (layout-responsive classes
        # like w-full/h-screen/max-w-*)
        responsive_units_count += tw_stats.get("utility_count", 0)

    # Determine mobile_first. Tailwind prefixes → mobile-first. Raw CSS: prefer
    # min-width over max-width.
    if coverage["tailwind_detected"] and tw_stats.get("responsive_prefix_count", 0) > 0:
        mobile_first = True
    elif min_width_count == 0 and max_width_count == 0:
        # No explicit breakpoints at all — don't claim mobile_first either way
        mobile_first = None
    else:
        mobile_first = min_width_count >= max_width_count

    if max_width_count > min_width_count * 2 and max_width_count > 3:
        issues.append({
            "severity": "info",
            "message": f"Desktop-first pattern detected ({max_width_count} max-width vs {min_width_count} min-width)",
            "fix": "Consider mobile-first approach using min-width media queries",
        })

    if not breakpoints_found and not coverage["tailwind_detected"]:
        issues.append({
            "severity": "warning",
            "message": "No CSS breakpoints or Tailwind config detected",
            "fix": "Use a responsive grid or standard breakpoints (sm, md, lg) for adaptable layout",
        })

    # Run Playwright sandbox if a URL or HTML file is provided
    sandbox_results = {}
    if url:
        try:
            sandbox_script = Path(__file__).parent / "playwright_sandbox.py"
            # (LED-2316) The MCP server's own venv (sys.executable) does NOT have
            # playwright installed — it lives in the separate suite venv. Launching
            # the sandbox with a bare sys.executable therefore reported "Playwright
            # is not installed in the subprocess environment" even though the
            # sitewide install exists. Resolve an interpreter that can actually
            # import playwright (probe candidates, cache the winner), fall back to
            # sys.executable so behavior degrades gracefully if none is found.
            _pw_py = _resolve_playwright_python()
            cmd = [_pw_py, str(sandbox_script), "--url-or-path", url]
            if check_types:
                cmd.extend(["--check-types"] + check_types)

            sandbox_proc = subprocess.run(cmd, capture_output=True, text=True)
            if sandbox_proc.returncode == 0:
                try:
                    sandbox_results = json.loads(sandbox_proc.stdout)
                    if sandbox_results.get("status") == "ok":
                        issues.extend(sandbox_results.get("issues", []))
                        coverage["playwright_tested"] = True
                        coverage["breakpoints_tested"] = sandbox_results.get("breakpoints_tested", [])
                    else:
                        issues.append({
                            "severity": "error", 
                            "message": f"Playwright sandbox error: {sandbox_results.get('error')}"
                        })
                except json.JSONDecodeError:
                    issues.append({
                        "severity": "error",
                        "message": f"Playwright sandbox returned invalid JSON: {sandbox_proc.stdout}"
                    })
            else:
                issues.append({
                    "severity": "error",
                    "message": f"Playwright sandbox failed: {sandbox_proc.stderr}"
                })
        except Exception as e:
            issues.append({
                "severity": "error",
                "message": f"Could not run Playwright sandbox: {e}"
            })

    # Check for fixed-width containers
    for cf in css_files:
        text = _read_text(cf)
        fixed_widths = re.findall(r"width\s*:\s*(\d{4,}px)", text)
        for fw in fixed_widths:
            issues.append({
                "severity": "warning",
                "message": f"Fixed width {fw} in {cf.name} may cause horizontal scroll on mobile",
                "fix": "Use max-width or responsive units instead",
            })

    # Status taxonomy (LED-1010)
    gaps: List[str] = []
    if coverage["tailwind_detected"] and not coverage["jsx_scanned"]:
        gaps.append("Tailwind detected but no JSX/TSX/Vue/Svelte files found — responsive coverage may be underreported")
    if not coverage["css_scanned"] and not coverage["tailwind_detected"]:
        gaps.append("No CSS and no Tailwind detected — nothing to validate against")

    status = STATUS_PARTIAL_COVERAGE if gaps else STATUS_OK

    result = {
        "tool": "design.validate_responsive",
        "status": status,
        "breakpoints_found": breakpoints_found,
        "responsive_issues": issues,
        "viewport_meta": viewport_meta,
        "viewport_blocks_zoom": viewport_blocks_zoom,
        "responsive_units_count": responsive_units_count,
        "mobile_first": mobile_first,
        "coverage": coverage,
        "gaps": gaps,
    }
    if tw_stats:
        result["tailwind_stats"] = tw_stats
    return result


# ---------------------------------------------------------------------------
# 23. design_component_library
# ---------------------------------------------------------------------------

def design_component_library(
    project_path: str,
    output_format: str = "json",
) -> Dict[str, Any]:
    """Scan for React/Vue/Svelte components and build a catalog."""
    root = Path(project_path)
    if not root.is_dir():
        return {"tool": "design.component_library", "error": f"Directory not found: {root}"}

    components: List[Dict[str, Any]] = []

    # React / TSX / JSX
    for f in _find_files(root, [".tsx", ".jsx"]):
        text = _read_text(f)
        info = _scan_react_component(f, text)
        if info:
            components.append(info)

    # Vue
    for f in _find_files(root, [".vue"]):
        text = _read_text(f)
        info = _scan_vue_component(f, text)
        if info:
            components.append(info)

    # Svelte
    for f in _find_files(root, [".svelte"]):
        text = _read_text(f)
        info = _scan_svelte_component(f, text)
        if info:
            components.append(info)

    # Sort by name
    components.sort(key=lambda c: c["name"])

    result: Dict[str, Any] = {
        "tool": "design.component_library",
        "status": "ok",
        "components": components,
        "total_count": len(components),
    }

    if output_format == "markdown":
        lines = [f"# Component Library ({len(components)} components)\n"]
        for c in components:
            lines.append(f"## {c['name']}")
            lines.append(f"- **Path**: `{c['path']}`")
            lines.append(f"- **Framework**: {c['framework']}")
            if c.get("props"):
                lines.append(f"- **Props**: {', '.join(c['props'][:10])}")
            if c.get("exports"):
                lines.append(f"- **Exports**: {', '.join(c['exports'][:10])}")
            lines.append("")
        result["markdown"] = "\n".join(lines)

    return result


# ---------------------------------------------------------------------------
# 24. story_generate
# ---------------------------------------------------------------------------

def story_generate(
    component_path: str,
    story_name: Optional[str] = None,
    variants: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Generate a .stories.tsx file for a component (no Storybook required)."""
    comp = Path(component_path)
    if not comp.exists() and "/" not in component_path and "\\" not in component_path:
        # Bare name like "Button" — search current directory for matching files
        search_root = Path.cwd()
        name = comp.stem
        for pattern in [f"**/{name}.tsx", f"**/{name}.jsx", f"**/{name}.ts", f"**/{name}.js"]:
            matches = [p for p in search_root.glob(pattern)
                       if "node_modules" not in str(p) and ".next" not in str(p)]
            if matches:
                comp = matches[0]
                break
    if not comp.exists():
        return {"tool": "story.generate", "error": f"Component file not found: {comp}"}

    text = _read_text(comp)
    info = _scan_react_component(comp, text)
    if not info:
        # Try to use filename as component name
        info = {"name": comp.stem, "props": [], "exports": [comp.stem]}

    comp_name = info["name"]
    name = story_name or comp_name
    variant_list = variants or ["Default", "WithChildren"]

    # Determine import path (relative from story file location)
    story_path = comp.with_suffix(".stories.tsx")
    import_name = f"./{comp.stem}"

    # Build story content
    stories = []
    for v in variant_list:
        variant_fn = v.replace(" ", "")
        if v.lower() == "default":
            stories.append(f"""
export const {variant_fn}: Story = {{
  args: {{}},
}};""")
        elif "children" in v.lower() or v.lower() == "withchildren":
            stories.append(f"""
export const {variant_fn}: Story = {{
  args: {{
    children: 'Sample content',
  }},
}};""")
        else:
            stories.append(f"""
export const {variant_fn}: Story = {{
  args: {{}},
}};""")

    content = f"""import type {{ Meta, StoryObj }} from '@storybook/react';
import {comp_name} from '{import_name}';

const meta: Meta<typeof {comp_name}> = {{
  title: '{name}',
  component: {comp_name},
  tags: ['autodocs'],
}};

export default meta;
type Story = StoryObj<typeof {comp_name}>;
{"".join(stories)}
"""

    try:
        story_path.write_text(content)
    except Exception as e:
        return {"tool": "story.generate", "error": f"Failed to write: {e}"}

    return {
        "tool": "story.generate",
        "status": "ok",
        "story_path": str(story_path),
        "component_name": comp_name,
        "variants_generated": variant_list,
    }


# ---------------------------------------------------------------------------
# 25a. Puppeteer fallback for screenshots
# ---------------------------------------------------------------------------

def _puppeteer_screenshot_fallback(url: str, baselines_dir: Path) -> Dict[str, Any]:
    """Take a screenshot via puppeteer (npx) when Playwright is not available."""
    try:
        baselines_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^a-zA-Z0-9]", "_", url)[:100]
        screenshot_path = baselines_dir / f"{safe_name}.png"

        # Inline JS script for puppeteer
        script = (
            "const puppeteer = require('puppeteer');"
            "(async () => {"
            "  const browser = await puppeteer.launch({headless: 'new', args: ['--no-sandbox']});"
            "  const page = await browser.newPage();"
            "  await page.setViewport({width: 1280, height: 720});"
            f"  await page.goto('{url}', {{waitUntil: 'networkidle2', timeout: 15000}});"
            f"  await page.screenshot({{path: '{screenshot_path}'}});"
            "  await browser.close();"
            "})();"
        )
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")[:500]
            # LED-1010: distinguish "module not found" (toolchain missing) from
            # runtime errors (toolchain present but failed). Consumers need
            # this distinction to know whether to install or debug.
            if "Cannot find module 'puppeteer'" in stderr or "Error: Cannot find module" in stderr:
                return {
                    "tool": "story.visual_test",
                    "status": STATUS_TOOLCHAIN_MISSING,
                    "missing": ["puppeteer", "playwright"],
                    "error": stderr,
                    "install_commands": [
                        "pip install playwright && python -m playwright install chromium",
                        "npm install -g puppeteer",
                    ],
                    "hint": (
                        "No screenshot engine available. Install Playwright (preferred) "
                        "or Puppeteer. After install, re-run — the tool auto-detects."
                    ),
                }
            return {
                "tool": "story.visual_test",
                "status": "error",
                "engine": "puppeteer",
                "error": stderr,
                "screenshot_path": None,
                "hint": "Puppeteer is installed but the screenshot call failed at runtime. Check the URL reachability, sandbox permissions, or memory limits.",
            }

        return {
            "tool": "story.visual_test",
            "status": "ok",
            "screenshot_path": str(screenshot_path),
            "baseline_exists": False,
            "diff_percent": None,
            "engine": "puppeteer_fallback",
            "hint": "Screenshot taken with puppeteer (fallback). Install Playwright for full visual regression with baseline comparison.",
        }
    except Exception as e:
        return {"tool": "story.visual_test", "status": "error", "error": str(e), "screenshot_path": None}


# ---------------------------------------------------------------------------
# 25. story_visual_test
# ---------------------------------------------------------------------------

def story_visual_test(
    url: str,
    project_path: Optional[str] = None,
    threshold: float = 0.05,
) -> Dict[str, Any]:
    """Take a screenshot with Playwright and compare against baseline.

    Falls back to puppeteer (npx) if Playwright is not installed,
    and returns install guidance if neither is available.
    """
    from ai.key_resolver import get_playwright, get_puppeteer

    root = Path(project_path) if project_path else Path.cwd()
    baselines_dir = root / ".delimit" / "visual-baselines"

    pw_available, _ = get_playwright()

    if not pw_available:
        # Try puppeteer fallback via npx
        pup_available, _ = get_puppeteer()
        if pup_available:
            return _puppeteer_screenshot_fallback(url, baselines_dir)

        return {
            "tool": "story.visual_test",
            "status": STATUS_TOOLCHAIN_MISSING,
            "missing": ["playwright", "puppeteer"],
            "install_commands": [
                "pip install playwright && python -m playwright install chromium",
                "npm install -g puppeteer",
            ],
            "message": (
                "No screenshot engine available. Install one:\n"
                "  - Playwright (recommended): pip install playwright && python -m playwright install chromium\n"
                "  - Puppeteer (fallback): npm install -g puppeteer"
            ),
            "screenshot_path": None,
            "baseline_exists": False,
            "diff_percent": None,
            "next_steps_hint": (
                "Install Playwright for full visual regression testing, "
                "or use `delimit_story_accessibility` for static checks that require no browser."
            ),
        }

    try:
        from playwright.sync_api import sync_playwright

        baselines_dir.mkdir(parents=True, exist_ok=True)
        # Sanitize URL to filename
        safe_name = re.sub(r"[^a-zA-Z0-9]", "_", url)[:100]
        screenshot_path = baselines_dir / f"{safe_name}.png"
        baseline_path = baselines_dir / f"{safe_name}.baseline.png"

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(url, wait_until="networkidle", timeout=15000)
            page.screenshot(path=str(screenshot_path))
            browser.close()

        baseline_exists = baseline_path.exists()
        diff_percent = None

        if baseline_exists:
            # Simple pixel comparison
            try:
                import hashlib
                current_hash = hashlib.md5(screenshot_path.read_bytes()).hexdigest()
                baseline_hash = hashlib.md5(baseline_path.read_bytes()).hexdigest()
                diff_percent = 0.0 if current_hash == baseline_hash else None
                if diff_percent is None:
                    # Rough byte-level diff
                    cur = screenshot_path.read_bytes()
                    base = baseline_path.read_bytes()
                    min_len = min(len(cur), len(base))
                    if min_len > 0:
                        diffs = sum(1 for i in range(0, min_len, 4) if cur[i:i+4] != base[i:i+4])
                        diff_percent = round(diffs / (min_len / 4) * 100, 2)
                    else:
                        diff_percent = 100.0
            except Exception:
                diff_percent = None
        else:
            # Save as baseline
            import shutil as _shutil
            _shutil.copy2(str(screenshot_path), str(baseline_path))

        passed = diff_percent is not None and diff_percent <= (threshold * 100)

        return {
            "tool": "story.visual_test",
            "status": "ok",
            "screenshot_path": str(screenshot_path),
            "baseline_exists": baseline_exists,
            "baseline_path": str(baseline_path),
            "diff_percent": diff_percent,
            "threshold_percent": threshold * 100,
            "passed": passed if baseline_exists else None,
        }

    except Exception as e:
        pup_available, _ = get_puppeteer()
        if pup_available:
            return _puppeteer_screenshot_fallback(url, baselines_dir)

        return {
            "tool": "story.visual_test",
            "status": "playwright_error",
            "error": str(e),
            "screenshot_path": None,
            "baseline_exists": False,
            "diff_percent": None,
            "engine": "playwright",
            "hint": (
                "Playwright is installed but could not launch a browser in this environment. "
                "Install/configure a browser runtime that works in the current sandbox, or "
                "use Puppeteer as a fallback."
            ),
        }


# ---------------------------------------------------------------------------
# 26. story_accessibility
# ---------------------------------------------------------------------------

# LED-1010 FIX: the original patterns used `<tag` with re.IGNORECASE but no
# word boundary after the tag name. That meant `<ArrowLeft>` matched `<a` +
# `rrowLeft>`, producing 128 false-positive link-href "errors" on a single
# DomainVested scan. Require the character AFTER the tag name to be
# whitespace, `/`, or `>` so PascalCase React components can't collide with
# HTML anchors / images / inputs / buttons.
_IMG_NO_ALT_RE = re.compile(r"<img(?=[\s/>])(?![^>]*\salt=)[^>]*>", re.IGNORECASE)
_INPUT_NO_LABEL_RE = re.compile(r"<input(?=[\s/>])(?![^>]*(?:\saria-label|\saria-labelledby|\sid=|type=[\"']hidden[\"']))[^>]*>", re.IGNORECASE)
_BUTTON_EMPTY_RE = re.compile(r"<button(?=[\s>])[^>]*>\s*</button>", re.IGNORECASE)
_A_NO_HREF_RE = re.compile(r"<a(?=[\s/>])(?![^>]*\shref=)[^>]*>", re.IGNORECASE)
_HEADING_SKIP_RE = re.compile(r"<h([1-6])(?=[\s/>])")
_ARIA_HIDDEN_FOCUSABLE_RE = re.compile(r'aria-hidden=["\']true["\'][^>]*(?:tabindex=["\']0["\']|<button(?=[\s>])|<a(?=[\s>]))', re.IGNORECASE)


# LED-1010: WCAG coverage map. The old tool accepted standards="WCAG2AA"
# but only implemented 3 of ~50 AA rules, and stamped every issue "WCAG2A"
# regardless. This map declares exactly which rules the scanner covers so
# the response can surface `coverage` honestly and `standard_requested` vs
# `standard_implemented` are separate fields.
#
# Source references per rule: https://www.w3.org/WAI/WCAG21/quickref/
IMPLEMENTED_WCAG_RULES = {
    "img-alt":                {"criterion": "1.1.1", "level": "A",  "title": "Non-text Content"},
    "input-label":            {"criterion": "1.3.1", "level": "A",  "title": "Info and Relationships"},
    "button-content":         {"criterion": "4.1.2", "level": "A",  "title": "Name, Role, Value"},
    "link-href":              {"criterion": "2.4.4", "level": "A",  "title": "Link Purpose (In Context)"},
    "heading-order":          {"criterion": "1.3.1", "level": "A",  "title": "Info and Relationships"},
    "aria-hidden-focusable":  {"criterion": "4.1.2", "level": "A",  "title": "Name, Role, Value"},
    "viewport-zoom-blocked":  {"criterion": "1.4.4", "level": "AA", "title": "Resize Text"},
}

# Rules that exist in each level but we DON'T implement — surfaced as gaps
# so consumers know coverage is partial.
UNIMPLEMENTED_WCAG_RULES_AA = [
    {"criterion": "1.4.3", "level": "AA",  "title": "Contrast (Minimum)"},
    {"criterion": "2.4.7", "level": "AA",  "title": "Focus Visible"},
    {"criterion": "2.1.2", "level": "A",   "title": "No Keyboard Trap"},
    {"criterion": "2.3.3", "level": "AAA", "title": "Animation from Interactions"},
    {"criterion": "3.1.1", "level": "A",   "title": "Language of Page"},
    {"criterion": "1.4.1", "level": "A",   "title": "Use of Color"},
    {"criterion": "1.3.5", "level": "AA",  "title": "Identify Input Purpose"},
    {"criterion": "3.3.1", "level": "A",   "title": "Error Identification"},
    {"criterion": "3.3.2", "level": "A",   "title": "Labels or Instructions"},
    {"criterion": "2.5.3", "level": "A",   "title": "Label in Name"},
    {"criterion": "2.5.2", "level": "A",   "title": "Pointer Cancellation"},
    {"criterion": "4.1.3", "level": "AA",  "title": "Status Messages"},
]


def _stamp_rule(rule: str) -> str:
    """Return the actual WCAG level the rule enforces — not the caller's request."""
    info = IMPLEMENTED_WCAG_RULES.get(rule)
    if not info:
        return "WCAG2A"
    level = info["level"]
    return "WCAG2A" if level == "A" else ("WCAG2AA" if level == "AA" else "WCAG2AAA")


def story_accessibility(
    project_path: str,
    standards: str = "WCAG2AA",
) -> Dict[str, Any]:
    """Run accessibility checks by scanning HTML/JSX/TSX for common issues.

    LED-1010: issues are now stamped with the ACTUAL WCAG level the rule
    enforces (not the caller's requested level). `standard_requested`,
    `implemented_rules`, `unimplemented_rules`, and `coverage_percent`
    surface exactly what ran vs what's still in the standard, so a caller
    asking for WCAG2AA does not see a false-confident pass.
    """
    root = Path(project_path)
    if not root.is_dir():
        return {"tool": "story.accessibility", "error": f"Directory not found: {root}"}

    issues: List[Dict[str, Any]] = []
    files_checked = 0

    scan_files = _find_files(root, [".html", ".htm", ".tsx", ".jsx", ".vue", ".svelte"])

    for f in scan_files:
        text = _read_text(f)
        files_checked += 1
        rel = str(f.relative_to(root)) if f.is_relative_to(root) else str(f)

        # Missing alt on images (WCAG 1.1.1, Level A)
        for m in _IMG_NO_ALT_RE.finditer(text):
            issues.append({
                "rule": "img-alt",
                "severity": "error",
                "message": "Image missing alt attribute",
                "file": rel,
                "standard": _stamp_rule("img-alt"),
                "wcag": IMPLEMENTED_WCAG_RULES["img-alt"],
                "snippet": m.group()[:120],
            })

        # Inputs without labels (WCAG 1.3.1, Level A)
        for m in _INPUT_NO_LABEL_RE.finditer(text):
            snippet = m.group()
            if 'type="hidden"' in snippet or "type='hidden'" in snippet:
                continue
            issues.append({
                "rule": "input-label",
                "severity": "error",
                "message": "Input missing associated label or aria-label",
                "file": rel,
                "standard": _stamp_rule("input-label"),
                "wcag": IMPLEMENTED_WCAG_RULES["input-label"],
                "snippet": snippet[:120],
            })

        # Empty buttons (WCAG 4.1.2, Level A)
        for m in _BUTTON_EMPTY_RE.finditer(text):
            issues.append({
                "rule": "button-content",
                "severity": "error",
                "message": "Button has no text content or aria-label",
                "file": rel,
                "standard": _stamp_rule("button-content"),
                "wcag": IMPLEMENTED_WCAG_RULES["button-content"],
                "snippet": m.group()[:120],
            })

        # Links without href (WCAG 2.4.4, Level A) — regex fixed to not
        # false-match PascalCase JSX components (`<ArrowLeft />` etc).
        for m in _A_NO_HREF_RE.finditer(text):
            issues.append({
                "rule": "link-href",
                "severity": "warning",
                "message": "Anchor element missing href attribute",
                "file": rel,
                "standard": _stamp_rule("link-href"),
                "wcag": IMPLEMENTED_WCAG_RULES["link-href"],
                "snippet": m.group()[:120],
            })

        # Heading level skips (WCAG 1.3.1, Level A)
        headings = [int(h) for h in _HEADING_SKIP_RE.findall(text)]
        for i in range(1, len(headings)):
            if headings[i] > headings[i - 1] + 1:
                issues.append({
                    "rule": "heading-order",
                    "severity": "warning",
                    "message": f"Heading level skipped: h{headings[i-1]} to h{headings[i]}",
                    "file": rel,
                    "standard": _stamp_rule("heading-order"),
                    "wcag": IMPLEMENTED_WCAG_RULES["heading-order"],
                })

        # aria-hidden on focusable elements (WCAG 4.1.2, Level A)
        for m in _ARIA_HIDDEN_FOCUSABLE_RE.finditer(text):
            issues.append({
                "rule": "aria-hidden-focusable",
                "severity": "error",
                "message": "Focusable element has aria-hidden='true'",
                "file": rel,
                "standard": _stamp_rule("aria-hidden-focusable"),
                "wcag": IMPLEMENTED_WCAG_RULES["aria-hidden-focusable"],
                "snippet": m.group()[:120],
            })

    # Filter by requested standard level
    standard_levels = {"WCAG2A": 1, "WCAG2AA": 2, "WCAG2AAA": 3}
    requested_level = standard_levels.get(standards, 2)
    filtered = [i for i in issues if standard_levels.get(i.get("standard", "WCAG2A"), 1) <= requested_level]

    errors = [i for i in filtered if i["severity"] == "error"]
    warnings = [i for i in filtered if i["severity"] == "warning"]

    # LED-1010: group by (rule, file) so 77 input-label errors across 30+
    # files can be triaged as ~30 groups with counts rather than 77 individual
    # call-sites in the caller's inbox.
    groups: Dict[tuple, Dict[str, Any]] = {}
    for issue in filtered:
        key = (issue["rule"], issue.get("file", ""))
        if key not in groups:
            groups[key] = {
                "rule": issue["rule"],
                "file": issue.get("file", ""),
                "count": 0,
                "severity": issue["severity"],
                "standard": issue.get("standard", "WCAG2A"),
            }
        groups[key]["count"] += 1

    # LED-1010 coverage: we implement ~7 rules; WCAG2AA covers many more.
    # Count rules at/below the requested level to be honest about coverage.
    implemented_at_level = [
        r for r, info in IMPLEMENTED_WCAG_RULES.items()
        if standard_levels.get("WCAG2" + info["level"], 1) <= requested_level
    ]
    unimplemented_at_level = [
        r for r in UNIMPLEMENTED_WCAG_RULES_AA
        if standard_levels.get("WCAG2" + r["level"], 1) <= requested_level
    ]
    total_rules = max(1, len(implemented_at_level) + len(unimplemented_at_level))
    coverage_pct = len(implemented_at_level) * 100 // total_rules

    # Status: partial_coverage when coverage < 100%. A `status: ok` on a scan
    # that ran 7 of ~50 WCAG2AA rules is exactly the silent-false-confidence
    # failure mode LED-1010 flagged.
    status = STATUS_PARTIAL_COVERAGE if coverage_pct < 100 else STATUS_OK

    return {
        "tool": "story.accessibility",
        "status": status,
        "standard_requested": standards,
        "standard": standards,  # retained for back-compat
        "implemented_rules": implemented_at_level,
        "unimplemented_rules": unimplemented_at_level,
        "coverage_percent": coverage_pct,
        "issues": filtered,
        "groups": sorted(groups.values(), key=lambda g: (-g["count"], g["file"])),
        "passed_count": files_checked - len(set(i["file"] for i in errors)),
        "failed_count": len(set(i["file"] for i in errors)),
        "error_count": len(errors),
        "warning_count": len(warnings),
        "files_checked": files_checked,
    }
