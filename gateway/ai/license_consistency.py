"""License-consistency lint — does the Pro tier LABEL match the actual GATE?

Motivation (2026-06-15 audit): the MCP surface gates Pro tools two ways, and they
use DIFFERENT name conventions, so they silently disagree:

  * ``require_premium(name)``  — prefixes ``name`` to ``delimit_<name>`` and checks
    membership in ``PRO_TOOLS``. This one WORKS.
  * ``_check_pro(name)`` (called inside ``_with_next_steps(name, ...)``) — checks
    ``name`` VERBATIM against ``PRO_TOOLS``. But ``PRO_TOOLS`` holds only
    ``delimit_``-prefixed names, and ``_with_next_steps`` is called with SHORT
    names (e.g. ``"agent_dispatch"``) — so this central gate NEVER fires.

Consequence classes this lint reports (all deterministic, ast + source scan; no
import of the tool bodies, no execution):

  UNENFORCED_PRO   — a ``PRO_TOOLS`` member with NO working ``require_premium``
                     gate anywhere in the source. Intended-paid, ships FREE
                     (revenue leak). The fix (gate it, or drop it from PRO_TOOLS)
                     is a PRICING decision — gating a currently-free tool breaks
                     existing free users (customer-protection), so this lint only
                     REPORTS; it never edits.
  DEAD_REQUIRE_PREMIUM — a ``require_premium("X")`` call whose ``delimit_X`` is NOT
                     in PRO_TOOLS, so the call is a no-op (looks gated, isn't).
  DECORATIVE_PRO_LABEL — a tool whose docstring claims Pro/Premium but whose name
                     is NOT in PRO_TOOLS (false public-tier claim).

Pure stdlib + ``ai.license.PRO_TOOLS``. Safe in CI / the publish path.
"""

from __future__ import annotations

import ast
import re
from typing import Any, Dict, List, Set

# Match a Pro/Premium TIER LABEL — not the bare word "premium" in prose (which
# false-positives on tools that merely describe a premium-related check).
_PRO_LABEL_RE = re.compile(
    r"\(pro\)|\(premium\)|\bpro only\b|\bpro/premium\b|"
    r"requires?\s+(?:a\s+)?(?:delimit\s+)?(?:pro|premium)\b",
    re.IGNORECASE,
)
_REQ_RE = re.compile(r'require_premium\(\s*["\']([^"\']+)["\']')
_WNS_RE = re.compile(r'_with_next_steps\(\s*["\']([^"\']+)["\']')
# Grace-gate wrapper (LED-1741): a working enforcement path that calls
# require_premium internally, with a grace+grandfather window for newly-Pro
# tools. Names prefix-normalize like require_premium.
_GRACED_RE = re.compile(r'_pro_gate_graced\(\s*["\']([^"\']+)["\']')


def _norm(name: str) -> str:
    """The PRO_TOOLS convention: ``delimit_``-prefixed."""
    return name if name.startswith("delimit_") else f"delimit_{name}"


def _registered_tool_names(source: str) -> Dict[str, str]:
    """Map registered MCP tool name -> its docstring. Covers @mcp.tool() decorated
    defs AND ``x = mcp.tool()(fn)`` assignment registration (named by the function,
    as the live server exposes it)."""
    out: Dict[str, str] = {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out
    func_by_name = {
        n.name: n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def _is_mcp_tool(deco: ast.expr) -> bool:
        target = deco.func if isinstance(deco, ast.Call) else deco
        return (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                and target.value.id == "mcp" and target.attr == "tool")

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(_is_mcp_tool(d) for d in node.decorator_list):
                out[node.name] = ast.get_docstring(node) or ""
        elif isinstance(node, ast.Call) and _is_mcp_tool(node.func):
            if node.args and isinstance(node.args[0], ast.Name):
                fn = func_by_name.get(node.args[0].id)
                if fn is not None:
                    out[fn.name] = ast.get_docstring(fn) or ""
    return out


def audit_license_consistency(source: str, pro_tools: Set[str]) -> Dict[str, Any]:
    """Return the label-vs-gate consistency matrix (deterministic)."""
    pro = set(pro_tools)
    req_names = set(_REQ_RE.findall(source))
    wns_names = set(_WNS_RE.findall(source))
    graced_names = set(_GRACED_RE.findall(source))

    # A PRO_TOOLS member is ENFORCED iff some require_premium(name) or
    # _pro_gate_graced(name) prefixes to it, OR some _with_next_steps(name)
    # passes the exact long name (so _check_pro fires).
    enforced = ({_norm(n) for n in req_names}
                | {_norm(n) for n in graced_names}
                | {_norm(n) for n in wns_names if _norm(n) in pro})

    unenforced_pro = sorted(t for t in pro if t not in enforced)
    dead_require_premium = sorted(n for n in req_names if _norm(n) not in pro)

    docs = _registered_tool_names(source)
    decorative = sorted(
        name for name, doc in docs.items()
        if _PRO_LABEL_RE.search(doc or "") and name not in pro
    )

    central_gate_live = sorted(_norm(n) for n in wns_names if _norm(n) in pro)
    return {
        "pro_tools_total": len(pro),
        "unenforced_pro": unenforced_pro,
        "dead_require_premium": dead_require_premium,
        "decorative_pro_label": decorative,
        "central_check_pro_fires_for": central_gate_live,  # empty => dead central gate
        "summary": {
            "unenforced_pro": len(unenforced_pro),
            "dead_require_premium": len(dead_require_premium),
            "decorative_pro_label": len(decorative),
            "central_gate_dead": not central_gate_live,
        },
    }


def audit_file(path: str = "ai/server.py") -> Dict[str, Any]:
    """Convenience: audit a server source file against the live PRO_TOOLS."""
    from ai.license import PRO_TOOLS
    with open(path, encoding="utf-8") as fh:
        return audit_license_consistency(fh.read(), set(PRO_TOOLS))
