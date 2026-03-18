"""
Validate API task with Evidence Contract - V12 Final
Complete implementation with Pydantic v2
"""

import yaml
import json
from typing import Dict, List, Set
from pathlib import Path

from core.registry_v3 import task_registry
from core.diff_engine_v2 import OpenAPIDiffEngine, ChangeType
from schemas.requests_v2 import ValidateAPIRequest
from schemas.evidence import (
    APIChangeEvidence, Decision, Violation, ViolationSeverity,
    Evidence, Remediation
)


@task_registry.register("validate-api", task_version="1.0", description="Check API for breaking changes")
def validate_api_handler(request: ValidateAPIRequest) -> APIChangeEvidence:
    """Check API specifications for breaking changes with evidence contract"""
    
    # Load specifications
    old_spec = load_spec(request.old_spec)
    new_spec = load_spec(request.new_spec)
    
    # Use diff engine for comprehensive change detection
    diff_engine = OpenAPIDiffEngine()
    all_changes = diff_engine.compare(old_spec, new_spec)
    
    # Process changes from diff engine
    violations = []
    evidence_list = []
    breaking_changes = []
    non_breaking_changes = []
    
    for change in all_changes:
        if change.is_breaking:
            # Convert to violation
            violations.append(Violation(
                rule=f"no_{change.type.value}",
                severity=ViolationSeverity.HIGH if change.severity == "high" else ViolationSeverity.MEDIUM,
                path=change.path,
                message=change.message,
                details=change.details
            ))
            breaking_changes.append({
                "type": change.type.value,
                "path": change.path,
                **change.details
            })
            evidence_list.append(Evidence(
                rule=f"no_{change.type.value}",
                passed=False,
                details={"path": change.path, **change.details}
            ))
        else:
            # Non-breaking change
            non_breaking_changes.append({
                "type": change.type.value,
                "path": change.path,
                **change.details
            })
            evidence_list.append(Evidence(
                rule="backward_compatible_additions",
                passed=True,
                details={"path": change.path, **change.details}
            ))
    
    # Also keep legacy endpoint checks for backward compatibility
    # Legacy checks are now handled by diff engine above
    
    # Get path counts for metrics
    old_paths = set(old_spec.get("paths", {}).keys())
    new_paths = set(new_spec.get("paths", {}).keys())
    
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