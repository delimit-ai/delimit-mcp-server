"""
Backend bridge to delimit-gateway core engine.

Adapter Boundary Contract v1.0:
- Pure translation layer: no governance logic here
- Deterministic error on failure (never swallow)
- Zero state (stateless between calls)
- No schema forking (gateway types are canonical)
"""

import sys
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.ai.gateway_core")

# Add gateway root to path so we can import core modules
GATEWAY_ROOT = Path(__file__).resolve().parent.parent.parent
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))


def _load_specs(spec_path: str) -> Dict[str, Any]:
    """Load an OpenAPI spec from a file path."""
    import json
    import yaml

    p = Path(spec_path)
    if not p.exists():
        raise FileNotFoundError(f"Spec file not found: {spec_path}")

    content = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        return yaml.safe_load(content)
    return json.loads(content)


def run_lint(old_spec: str, new_spec: str, policy_file: Optional[str] = None) -> Dict[str, Any]:
    """Run the full lint pipeline: diff + policy evaluation.

    This is the Tier 1 primary tool — combines diff detection with
    policy enforcement into a single pass/fail decision.
    """
    from core.policy_engine import evaluate_with_policy

    old = _load_specs(old_spec)
    new = _load_specs(new_spec)

    return evaluate_with_policy(old, new, policy_file)


def run_diff(old_spec: str, new_spec: str) -> Dict[str, Any]:
    """Run diff engine only — no policy evaluation."""
    from core.diff_engine_v2 import OpenAPIDiffEngine

    old = _load_specs(old_spec)
    new = _load_specs(new_spec)

    engine = OpenAPIDiffEngine()
    changes = engine.compare(old, new)

    breaking = [c for c in changes if c.is_breaking]

    return {
        "total_changes": len(changes),
        "breaking_changes": len(breaking),
        "changes": [
            {
                "type": c.type.value,
                "path": c.path,
                "message": c.message,
                "is_breaking": c.is_breaking,
                "details": c.details,
            }
            for c in changes
        ],
    }


def run_policy(spec_files: List[str], policy_file: Optional[str] = None) -> Dict[str, Any]:
    """Evaluate specs against governance policy without diffing."""
    from core.policy_engine import PolicyEngine

    engine = PolicyEngine(policy_file)

    return {
        "rules_loaded": len(engine.rules),
        "custom_rules": len(engine.custom_rules),
        "policy_file": policy_file,
        "template": engine.create_policy_template() if not policy_file else None,
    }


def query_ledger(
    ledger_path: str,
    api_name: Optional[str] = None,
    repository: Optional[str] = None,
    validate_chain: bool = False,
) -> Dict[str, Any]:
    """Query the contract ledger."""
    from core.contract_ledger import ContractLedger

    ledger = ContractLedger(ledger_path)

    if not ledger.exists():
        return {"error": "Ledger not found", "path": ledger_path}

    result: Dict[str, Any] = {"path": ledger_path, "event_count": ledger.get_event_count()}

    if validate_chain:
        try:
            ledger.validate_chain()
            result["chain_valid"] = True
        except Exception as e:
            result["chain_valid"] = False
            result["chain_error"] = str(e)

    if api_name:
        result["events"] = ledger.get_api_timeline(api_name)
    elif repository:
        result["events"] = ledger.get_events_by_repository(repository)
    else:
        latest = ledger.get_latest_event()
        result["latest_event"] = latest

    return result


def run_impact(api_name: str, dependency_file: Optional[str] = None) -> Dict[str, Any]:
    """Analyze downstream impact of an API change."""
    from core.dependency_graph import DependencyGraph
    from core.impact_analyzer import ImpactAnalyzer

    graph = DependencyGraph()
    if dependency_file:
        graph.load_from_file(dependency_file)

    analyzer = ImpactAnalyzer(graph)
    return analyzer.analyze(api_name)


def run_semver(
    old_spec: str,
    new_spec: str,
    current_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Classify the semver bump for a spec change.

    Returns detailed breakdown: bump level, per-category counts,
    and optionally the bumped version string.
    """
    from core.diff_engine_v2 import OpenAPIDiffEngine
    from core.semver_classifier import classify_detailed, bump_version, classify

    old = _load_specs(old_spec)
    new = _load_specs(new_spec)

    engine = OpenAPIDiffEngine()
    changes = engine.compare(old, new)
    result = classify_detailed(changes)

    if current_version:
        bump = classify(changes)
        result["current_version"] = current_version
        result["next_version"] = bump_version(current_version, bump)

    return result


def run_explain(
    old_spec: str,
    new_spec: str,
    template: str = "developer",
    old_version: Optional[str] = None,
    new_version: Optional[str] = None,
    api_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate a human-readable explanation of API changes.

    Supports 7 templates: developer, team_lead, product, migration,
    changelog, pr_comment, slack.
    """
    from core.diff_engine_v2 import OpenAPIDiffEngine
    from core.explainer import explain, TEMPLATES

    old = _load_specs(old_spec)
    new = _load_specs(new_spec)

    engine = OpenAPIDiffEngine()
    changes = engine.compare(old, new)

    output = explain(
        changes,
        template=template,
        old_version=old_version,
        new_version=new_version,
        api_name=api_name,
    )

    return {
        "template": template,
        "available_templates": TEMPLATES,
        "output": output,
    }


def run_zero_spec(
    project_dir: str = ".",
    python_bin: Optional[str] = None,
) -> Dict[str, Any]:
    """Detect framework and extract OpenAPI spec from source code.

    Currently supports FastAPI. Returns the extracted spec or an error
    with guidance on how to fix it.
    """
    from core.zero_spec.detector import detect_framework, Framework
    from core.zero_spec.express_extractor import extract_express_spec
    from core.zero_spec.fastapi_extractor import extract_fastapi_spec
    from core.zero_spec.nestjs_extractor import extract_nestjs_spec

    info = detect_framework(project_dir)

    result: Dict[str, Any] = {
        "framework": info.framework.value,
        "confidence": info.confidence,
        "message": info.message,
    }

    if info.framework == Framework.FASTAPI:
        extraction = extract_fastapi_spec(
            info, project_dir, python_bin=python_bin
        )
        result.update(extraction)
        if extraction["success"] and info.app_locations:
            loc = info.app_locations[0]
            result["app_file"] = loc.file
            result["app_variable"] = loc.variable
            result["app_line"] = loc.line
    elif info.framework == Framework.NESTJS:
        extraction = extract_nestjs_spec(info, project_dir)
        result.update(extraction)
        if extraction["success"] and info.app_locations:
            loc = info.app_locations[0]
            result["app_file"] = loc.file
            result["app_variable"] = loc.variable
            result["app_line"] = loc.line
    elif info.framework == Framework.EXPRESS:
        extraction = extract_express_spec(info, project_dir)
        result.update(extraction)
        if extraction["success"] and info.app_locations:
            loc = info.app_locations[0]
            result["app_file"] = loc.file
            result["app_variable"] = loc.variable
            result["app_line"] = loc.line
    else:
        result["success"] = False
        result["error"] = "No supported API framework found. Provide an OpenAPI spec file."
        result["error_type"] = "no_framework"

    return result
