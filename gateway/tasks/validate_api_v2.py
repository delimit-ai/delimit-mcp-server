"""
Validate API task with Evidence Contract
V12 Core Hardening
"""

import yaml
import json
from typing import Dict, List, Set
from pathlib import Path

from core.registry_v2 import task_registry
from schemas.requests import ValidateAPIRequest
from schemas.evidence import (
    APIChangeEvidence, Decision, Violation, ViolationSeverity,
    Evidence, Remediation
)


@task_registry.register("validate-api", version="1.0", description="Check API for breaking changes")
def validate_api_handler(request: ValidateAPIRequest) -> APIChangeEvidence:
    """Check API specifications for breaking changes with evidence contract"""
    
    # Load specifications
    old_spec = load_spec(request.old_spec)
    new_spec = load_spec(request.new_spec)
    
    # Analyze changes
    violations = []
    evidence_list = []
    breaking_changes = []
    non_breaking_changes = []
    
    # Check removed endpoints
    old_paths = set(old_spec.get("paths", {}).keys())
    new_paths = set(new_spec.get("paths", {}).keys())
    
    removed_paths = old_paths - new_paths
    for path in removed_paths:
        violations.append(Violation(
            rule="no_removed_endpoint",
            severity=ViolationSeverity.HIGH,
            path=path,
            message=f"Endpoint removed: {path}",
            details={"type": "endpoint_removed"}
        ))
        breaking_changes.append({
            "type": "endpoint_removed",
            "path": path
        })
        evidence_list.append(Evidence(
            rule="no_removed_endpoint",
            passed=False,
            details={"path": path, "status": "removed"}
        ))
    
    # Check for removed methods
    for path in old_paths & new_paths:
        old_methods = set(old_spec["paths"][path].keys())
        new_methods = set(new_spec["paths"][path].keys())
        
        removed_methods = old_methods - new_methods
        for method in removed_methods:
            violations.append(Violation(
                rule="no_removed_method",
                severity=ViolationSeverity.HIGH,
                path=f"{path}:{method.upper()}",
                message=f"Method removed: {method.upper()} {path}",
                details={"type": "method_removed", "method": method}
            ))
            breaking_changes.append({
                "type": "method_removed",
                "path": path,
                "method": method.upper()
            })
            evidence_list.append(Evidence(
                rule="no_removed_method",
                passed=False,
                details={"path": path, "method": method, "status": "removed"}
            ))
        
        # Check for new required parameters (breaking)
        for method in old_methods & new_methods:
            old_params = extract_parameters(old_spec["paths"][path][method])
            new_params = extract_parameters(new_spec["paths"][path][method])
            
            old_required = {p["name"] for p in old_params if p.get("required", False)}
            new_required = {p["name"] for p in new_params if p.get("required", False)}
            
            newly_required = new_required - old_required
            for param in newly_required:
                violations.append(Violation(
                    rule="no_new_required_param",
                    severity=ViolationSeverity.HIGH,
                    path=f"{path}:{method.upper()}",
                    message=f"New required parameter: {param}",
                    details={"type": "required_param_added", "parameter": param}
                ))
                breaking_changes.append({
                    "type": "required_param_added",
                    "path": path,
                    "method": method.upper(),
                    "parameter": param
                })
                evidence_list.append(Evidence(
                    rule="no_new_required_param",
                    passed=False,
                    details={"path": path, "method": method, "parameter": param}
                ))
    
    # Check for added endpoints (non-breaking)
    added_paths = new_paths - old_paths
    for path in added_paths:
        non_breaking_changes.append({
            "type": "endpoint_added",
            "path": path
        })
        evidence_list.append(Evidence(
            rule="backward_compatible_additions",
            passed=True,
            details={"path": path, "status": "added"}
        ))
    
    # Determine decision and exit code
    if violations:
        decision = Decision.FAIL
        exit_code = 1
        summary = f"API validation failed: {len(violations)} breaking changes detected"
    else:
        decision = Decision.PASS
        exit_code = 0
        summary = "API validation passed: No breaking changes detected"
    
    # Calculate risk score
    risk_score = min(len(violations) * 10, 100)
    
    # Build remediation if needed
    remediation = None
    if violations:
        remediation = Remediation(
            summary="Breaking changes detected in API specification",
            steps=[
                "Option 1: Restore removed endpoints/methods to maintain compatibility",
                "Option 2: Create a new API version (e.g., v2) for breaking changes",
                "Option 3: Implement deprecation warnings before removal",
                "Option 4: Document migration path for API consumers"
            ],
            examples=[
                "Keep old endpoint with deprecation notice",
                "Add version prefix: /v2/api/..."
            ],
            documentation="https://docs.delimit.ai/api-versioning"
        )
    
    # Return evidence contract
    return APIChangeEvidence(
        task="validate-api",
        task_version="1.0",
        decision=decision,
        exit_code=exit_code,
        violations=violations,
        evidence=evidence_list,
        remediation=remediation,
        summary=summary,
        correlation_id=request.correlation_id,
        metrics={
            "endpoints_checked": len(old_paths | new_paths),
            "breaking_changes": len(breaking_changes),
            "non_breaking_changes": len(non_breaking_changes)
        },
        breaking_changes=breaking_changes,
        non_breaking_changes=non_breaking_changes,
        risk_score=risk_score
    )


def load_spec(file_path: str) -> Dict:
    """Load API specification from file"""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Spec file not found: {file_path}\n"
            f"If the spec was deleted, ensure both old and new spec paths exist before running validation."
        )
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


def extract_parameters(operation: Dict) -> List[Dict]:
    """Extract parameters from an operation"""
    params = operation.get("parameters", [])
    # Also check requestBody for required fields
    if "requestBody" in operation and operation["requestBody"].get("required", False):
        params.append({
            "name": "requestBody",
            "required": True,
            "in": "body"
        })
    return params