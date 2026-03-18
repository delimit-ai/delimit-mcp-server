"""
Explain Diff task with Evidence Contract
V12 Core Hardening - Complete Implementation
"""

import yaml
import json
from typing import Dict, List, Set
from pathlib import Path

from core.registry_v3 import task_registry
from schemas.requests_v2 import ExplainDiffRequest
from schemas.evidence import (
    DiffExplanationEvidence, Decision, Violation, ViolationSeverity,
    Evidence, Remediation
)


@task_registry.register("explain-diff", task_version="1.0", description="Explain API differences")
def explain_diff_handler(request: ExplainDiffRequest) -> DiffExplanationEvidence:
    """Generate human-readable explanation of API changes with evidence contract"""
    
    # Load specifications
    old_spec = load_spec(request.old_spec)
    new_spec = load_spec(request.new_spec)
    
    # Analyze changes
    violations = []
    evidence_list = []
    changes_summary = {
        "endpoints_added": [],
        "endpoints_removed": [],
        "endpoints_modified": [],
        "models_added": [],
        "models_removed": [],
        "models_modified": []
    }
    
    # Check endpoint changes
    old_paths = set(old_spec.get("paths", {}).keys())
    new_paths = set(new_spec.get("paths", {}).keys())
    
    # Removed endpoints (breaking)
    removed_paths = old_paths - new_paths
    for path in removed_paths:
        changes_summary["endpoints_removed"].append(path)
        violations.append(Violation(
            rule="no_removed_endpoint",
            severity=ViolationSeverity.HIGH,
            path=path,
            message=f"Breaking: Endpoint removed - {path}",
            details={"change_type": "endpoint_removed"}
        ))
        evidence_list.append(Evidence(
            rule="endpoint_tracking",
            passed=False,
            details={"path": path, "change": "removed"}
        ))
    
    # Added endpoints (non-breaking)
    added_paths = new_paths - old_paths
    for path in added_paths:
        changes_summary["endpoints_added"].append(path)
        evidence_list.append(Evidence(
            rule="endpoint_tracking",
            passed=True,
            details={"path": path, "change": "added"}
        ))
    
    # Modified endpoints
    for path in old_paths & new_paths:
        old_methods = set(old_spec["paths"][path].keys())
        new_methods = set(new_spec["paths"][path].keys())
        
        if old_methods != new_methods:
            changes_summary["endpoints_modified"].append(path)
            
            # Removed methods (breaking)
            removed_methods = old_methods - new_methods
            for method in removed_methods:
                violations.append(Violation(
                    rule="no_removed_method",
                    severity=ViolationSeverity.HIGH,
                    path=f"{path}:{method.upper()}",
                    message=f"Breaking: Method removed - {method.upper()} {path}",
                    details={"change_type": "method_removed", "method": method}
                ))
                evidence_list.append(Evidence(
                    rule="method_tracking",
                    passed=False,
                    details={"path": path, "method": method, "change": "removed"}
                ))
    
    # Check model/schema changes
    old_schemas = extract_schemas(old_spec)
    new_schemas = extract_schemas(new_spec)
    
    # Removed models (breaking)
    removed_models = set(old_schemas.keys()) - set(new_schemas.keys())
    for model in removed_models:
        changes_summary["models_removed"].append(model)
        violations.append(Violation(
            rule="no_removed_model",
            severity=ViolationSeverity.HIGH,
            message=f"Breaking: Model removed - {model}",
            details={"change_type": "model_removed", "model": model}
        ))
        evidence_list.append(Evidence(
            rule="model_tracking",
            passed=False,
            details={"model": model, "change": "removed"}
        ))
    
    # Added models (non-breaking)
    added_models = set(new_schemas.keys()) - set(old_schemas.keys())
    for model in added_models:
        changes_summary["models_added"].append(model)
        evidence_list.append(Evidence(
            rule="model_tracking",
            passed=True,
            details={"model": model, "change": "added"}
        ))
    
    # Check for modified models
    for model in set(old_schemas.keys()) & set(new_schemas.keys()):
        if schemas_differ(old_schemas[model], new_schemas[model]):
            changes_summary["models_modified"].append(model)
            evidence_list.append(Evidence(
                rule="model_tracking",
                passed=True,  # Modifications are warnings, not failures
                details={"model": model, "change": "modified"}
            ))
    
    # Determine impact and migration requirements
    breaking_changes = len(violations)
    migration_required = breaking_changes > 0
    
    if breaking_changes == 0:
        impact_level = "none"
        decision = Decision.PASS
        exit_code = 0
        summary = "No breaking changes detected"
    elif breaking_changes <= 2:
        impact_level = "low"
        decision = Decision.WARN
        exit_code = 0
        summary = f"{breaking_changes} breaking changes detected (low impact)"
    elif breaking_changes <= 5:
        impact_level = "medium"
        decision = Decision.WARN
        exit_code = 0
        summary = f"{breaking_changes} breaking changes detected (medium impact)"
    else:
        impact_level = "high"
        decision = Decision.FAIL
        exit_code = 1
        summary = f"{breaking_changes} breaking changes detected (high impact)"
    
    # Build remediation guidance
    remediation = None
    if violations:
        steps = []
        if changes_summary["endpoints_removed"]:
            steps.append("Restore removed endpoints or provide migration path")
        if changes_summary["models_removed"]:
            steps.append("Restore removed models or update dependent code")
        if any(v.details.get("change_type") == "method_removed" for v in violations):
            steps.append("Restore removed methods or document alternatives")
        
        steps.extend([
            "Consider versioning the API (e.g., /v2/) for breaking changes",
            "Add deprecation notices before removing features",
            "Document migration guide for consumers"
        ])
        
        remediation = Remediation(
            summary="Breaking changes require migration planning",
            steps=steps,
            examples=[
                "Add version prefix: /v2/api/endpoints",
                "Keep old endpoints with deprecation headers",
                "Provide transformation utilities for model changes"
            ],
            documentation="https://docs.delimit.ai/api-migration"
        )
    
    # Add detail based on request level
    if request.detail_level == "summary":
        # Minimal details
        metrics = {
            "total_changes": sum(len(v) for v in changes_summary.values()),
            "breaking_changes": breaking_changes
        }
    else:
        # Full metrics
        metrics = {
            "endpoints_added": len(changes_summary["endpoints_added"]),
            "endpoints_removed": len(changes_summary["endpoints_removed"]),
            "endpoints_modified": len(changes_summary["endpoints_modified"]),
            "models_added": len(changes_summary["models_added"]),
            "models_removed": len(changes_summary["models_removed"]),
            "models_modified": len(changes_summary["models_modified"]),
            "breaking_changes": breaking_changes,
            "total_changes": sum(len(v) for v in changes_summary.values())
        }
    
    return DiffExplanationEvidence(
        task="explain-diff",
        task_version="1.0",
        decision=decision,
        exit_code=exit_code,
        violations=violations,
        evidence=evidence_list,
        remediation=remediation,
        summary=summary,
        correlation_id=request.correlation_id,
        metrics=metrics,
        changes_summary=changes_summary,
        migration_required=migration_required,
        impact_level=impact_level
    )


def load_spec(file_path: str) -> Dict:
    """Load API specification from file"""
    path = Path(file_path)
    with path.open('r') as f:
        if path.suffix in ['.yaml', '.yml']:
            return yaml.safe_load(f)
        elif path.suffix == '.json':
            return json.load(f)
        else:
            # Try YAML first, then JSON
            content = f.read()
            try:
                return yaml.safe_load(content)
            except:
                return json.loads(content)


def extract_schemas(spec: Dict) -> Dict:
    """Extract schemas/models from specification"""
    if "components" in spec and "schemas" in spec.get("components", {}):
        return spec["components"]["schemas"]
    elif "definitions" in spec:
        return spec["definitions"]
    return {}


def schemas_differ(old_schema: Dict, new_schema: Dict) -> bool:
    """Check if two schemas are different"""
    # Simple comparison - could be enhanced
    old_props = set(old_schema.get("properties", {}).keys())
    new_props = set(new_schema.get("properties", {}).keys())
    
    # Check for property changes
    if old_props != new_props:
        return True
    
    # Check for type changes
    for prop in old_props:
        old_type = old_schema.get("properties", {}).get(prop, {}).get("type")
        new_type = new_schema.get("properties", {}).get(prop, {}).get("type")
        if old_type != new_type:
            return True
    
    return False