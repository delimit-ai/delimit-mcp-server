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


def _is_color_value(v: str) -> bool:
    v = v.lower().strip()
    if v.startswith("#") and len(v) in (4, 7, 9):
        return True
    if v.startswith(("rgb", "hsl", "oklch", "lab(", "lch(")):
        return True
    return False


def _extract_css_variables(text: str) -> Dict[str, List[Dict[str, str]]]:
    """Extract CSS custom properties grouped by category."""
    colors: List[Dict[str, str]] = []
    spacing: List[Dict[str, str]] = []
    typography: List[Dict[str, str]] = []
    other: List[Dict[str, str]] = []

    for name, value in _CSS_VAR_RE.findall(text):
        value = value.strip()
        entry = {"name": f"--{name}", "value": value}
        lower = name.lower()
        if any(k in lower for k in ("color", "bg", "text", "border", "fill", "stroke", "accent", "primary", "secondary")):
            colors.append(entry)
        elif any(k in lower for k in ("space", "gap", "margin", "padding", "size", "width", "height", "radius")):
            spacing.append(entry)
        elif any(k in lower for k in ("font", "line", "letter", "text", "heading")):
            typography.append(entry)
        elif _is_color_value(value):
            colors.append(entry)
        else:
            other.append(entry)

    return {"colors": colors, "spacing": spacing, "typography": typography, "other": other}


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

    all_tokens: Dict[str, List] = {"colors": [], "spacing": [], "typography": [], "breakpoints": [], "other": []}
    source_files: List[str] = []

    # 1. Tailwind config
    for tw_name in ("tailwind.config.js", "tailwind.config.ts", "tailwind.config.mjs", "tailwind.config.cjs"):
        tw_path = root / tw_name
        if tw_path.exists():
            text = _read_text(tw_path)
            parsed = _parse_tailwind_config(text)
            source_files.append(str(tw_path))
            if parsed["breakpoints"]:
                all_tokens["breakpoints"].extend(
                    [{"name": bp, "source": str(tw_path)} for bp in parsed["breakpoints"]]
                )
            break

    # 2. CSS / SCSS files
    css_files = _find_files(root, [".css", ".scss", ".sass"])
    for cf in css_files:
        text = _read_text(cf)
        if "--" not in text and "@media" not in text:
            continue
        source_files.append(str(cf))
        vars_found = _extract_css_variables(text)
        for cat in ("colors", "spacing", "typography", "other"):
            for entry in vars_found[cat]:
                entry["source"] = str(cf)
                all_tokens[cat].append(entry)

        # breakpoints from media queries
        for bp_val in _MEDIA_QUERY_RE.findall(text):
            all_tokens["breakpoints"].append({"value": bp_val.strip(), "source": str(cf)})

    # 3. Filter by token_types if specified
    if token_types:
        all_tokens = {k: v for k, v in all_tokens.items() if k in token_types}

    # Deduplicate breakpoints
    seen_bp = set()
    unique_bp = []
    for bp in all_tokens.get("breakpoints", []):
        key = bp.get("name", bp.get("value", ""))
        if key not in seen_bp:
            seen_bp.add(key)
            unique_bp.append(bp)
    if "breakpoints" in all_tokens:
        all_tokens["breakpoints"] = unique_bp

    total = sum(len(v) for v in all_tokens.values())
    result = {
        "tool": "design.extract_tokens",
        "status": "ok",
        "tokens": all_tokens,
        "total_tokens": total,
        "source_files": sorted(set(source_files)),
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
) -> Dict[str, Any]:
    """Validate responsive design patterns via static analysis (Playwright optional)."""
    root = Path(project_path)
    if not root.is_dir():
        return {"tool": "design.validate_responsive", "error": f"Directory not found: {root}"}

    issues: List[Dict[str, str]] = []
    breakpoints_found: List[str] = []
    viewport_meta = False
    responsive_units_count = 0

    # Scan HTML files for viewport meta
    html_files = _find_files(root, [".html", ".htm"])
    for hf in html_files:
        text = _read_text(hf)
        if _VIEWPORT_META_RE.search(text):
            viewport_meta = True
            break

    # Also check Next.js layout files
    if not viewport_meta:
        for layout_name in ("layout.tsx", "layout.jsx", "layout.js", "_document.tsx", "_document.jsx", "_app.tsx"):
            candidates = list(root.rglob(layout_name))
            for c in candidates[:5]:
                text = _read_text(c)
                if "viewport" in text.lower():
                    viewport_meta = True
                    break
            if viewport_meta:
                break

    if not viewport_meta:
        issues.append({"severity": "warning", "message": "No viewport meta tag detected", "fix": "Add <meta name='viewport' content='width=device-width, initial-scale=1'>"})

    # Scan CSS for media queries and responsive patterns
    css_files = _find_files(root, [".css", ".scss", ".sass"])
    for cf in css_files:
        text = _read_text(cf)
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

    if max_width_count > min_width_count * 2 and max_width_count > 3:
        issues.append({
            "severity": "info",
            "message": f"Desktop-first pattern detected ({max_width_count} max-width vs {min_width_count} min-width)",
            "fix": "Consider mobile-first approach using min-width media queries",
        })

    if not breakpoints_found and not any(
        (root / n).exists() for n in ("tailwind.config.js", "tailwind.config.ts", "tailwind.config.mjs")
    ):
        issues.append({
            "severity": "warning",
            "message": "No CSS breakpoints or Tailwind config detected",
            "fix": "Add responsive breakpoints via media queries or a CSS framework",
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

    return {
        "tool": "design.validate_responsive",
        "status": "ok",
        "breakpoints_found": breakpoints_found,
        "responsive_issues": issues,
        "viewport_meta": viewport_meta,
        "responsive_units_count": responsive_units_count,
        "mobile_first": min_width_count >= max_width_count,
    }


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
            return {
                "tool": "story.visual_test",
                "status": "puppeteer_error",
                "error": result.stderr.decode(errors="replace")[:500],
                "hint": "Puppeteer fallback failed. Install Playwright for better support: pip install playwright && python -m playwright install chromium",
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
        return {"tool": "story.visual_test", "status": "error", "error": str(e)}


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
            "status": "no_screenshot_tool",
            "message": (
                "No screenshot tool available. Install one of the following:\n"
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

_IMG_NO_ALT_RE = re.compile(r"<img(?![^>]*alt=)[^>]*>", re.IGNORECASE)
_INPUT_NO_LABEL_RE = re.compile(r"<input(?![^>]*(?:aria-label|aria-labelledby|id=)[^>]*>)[^>]*>", re.IGNORECASE)
_BUTTON_EMPTY_RE = re.compile(r"<button[^>]*>\s*</button>", re.IGNORECASE)
_A_NO_HREF_RE = re.compile(r"<a(?![^>]*href=)[^>]*>", re.IGNORECASE)
_HEADING_SKIP_RE = re.compile(r"<h([1-6])")
_ARIA_HIDDEN_FOCUSABLE_RE = re.compile(r'aria-hidden=["\']true["\'][^>]*(?:tabindex=["\']0["\']|<button|<a\s)', re.IGNORECASE)


def story_accessibility(
    project_path: str,
    standards: str = "WCAG2AA",
) -> Dict[str, Any]:
    """Run accessibility checks by scanning HTML/JSX/TSX for common issues."""
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

        # Missing alt on images
        for m in _IMG_NO_ALT_RE.finditer(text):
            issues.append({
                "rule": "img-alt",
                "severity": "error",
                "message": "Image missing alt attribute",
                "file": rel,
                "standard": "WCAG2A",
                "snippet": m.group()[:120],
            })

        # Inputs without labels
        for m in _INPUT_NO_LABEL_RE.finditer(text):
            snippet = m.group()
            # Skip hidden inputs
            if 'type="hidden"' in snippet or "type='hidden'" in snippet:
                continue
            issues.append({
                "rule": "input-label",
                "severity": "error",
                "message": "Input missing associated label or aria-label",
                "file": rel,
                "standard": "WCAG2A",
                "snippet": snippet[:120],
            })

        # Empty buttons
        for m in _BUTTON_EMPTY_RE.finditer(text):
            issues.append({
                "rule": "button-content",
                "severity": "error",
                "message": "Button has no text content or aria-label",
                "file": rel,
                "standard": "WCAG2A",
                "snippet": m.group()[:120],
            })

        # Links without href
        for m in _A_NO_HREF_RE.finditer(text):
            issues.append({
                "rule": "link-href",
                "severity": "warning",
                "message": "Anchor element missing href attribute",
                "file": rel,
                "standard": "WCAG2A",
                "snippet": m.group()[:120],
            })

        # Heading level skips (e.g., h1 -> h3 without h2)
        headings = [int(h) for h in _HEADING_SKIP_RE.findall(text)]
        for i in range(1, len(headings)):
            if headings[i] > headings[i - 1] + 1:
                issues.append({
                    "rule": "heading-order",
                    "severity": "warning",
                    "message": f"Heading level skipped: h{headings[i-1]} to h{headings[i]}",
                    "file": rel,
                    "standard": "WCAG2A",
                })

        # aria-hidden on focusable elements
        for m in _ARIA_HIDDEN_FOCUSABLE_RE.finditer(text):
            issues.append({
                "rule": "aria-hidden-focusable",
                "severity": "error",
                "message": "Focusable element has aria-hidden='true'",
                "file": rel,
                "standard": "WCAG2AA",
                "snippet": m.group()[:120],
            })

    # Filter by standard level if needed
    standard_levels = {"WCAG2A": 1, "WCAG2AA": 2, "WCAG2AAA": 3}
    requested_level = standard_levels.get(standards, 2)
    filtered = [i for i in issues if standard_levels.get(i.get("standard", "WCAG2A"), 1) <= requested_level]

    errors = [i for i in filtered if i["severity"] == "error"]
    warnings = [i for i in filtered if i["severity"] == "warning"]

    return {
        "tool": "story.accessibility",
        "status": "ok",
        "standard": standards,
        "issues": filtered,
        "passed_count": files_checked - len(set(i["file"] for i in errors)),
        "failed_count": len(set(i["file"] for i in errors)),
        "error_count": len(errors),
        "warning_count": len(warnings),
        "files_checked": files_checked,
    }
