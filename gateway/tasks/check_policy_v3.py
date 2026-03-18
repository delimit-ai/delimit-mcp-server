"""
Check Policy task with Evidence Contract - V12 Final
ONLY enforces implemented rules with Pydantic v2
"""

import yaml
import json
from typing import Dict, List
from pathlib import Path

from core.registry_v3 import task_registry
from schemas.requests_v2 import CheckPolicyRequest
from schemas.evidence import (
    PolicyComplianceEvidence, Decision, Violation, ViolationSeverity,
    Evidence, Remediation
)


# ONLY rules we actually implement
IMPLEMENTED_RULES = {
    "require_openapi_version",
    "require_info_description", 
    "require_security_definition",
    "require_https_only",
    "max_path_depth"
}


@task_registry.register("check-policy", task_version="1.0", description="Check API against implemented policy rules")
def check_policy_handler(request: CheckPolicyRequest) -> PolicyComplianceEvidence:
    """Validate API spec against ACTUALLY IMPLEMENTED policy rules"""
    
    # Get policy - either from file, inline, or defaults
    if request.policy_file:
        policy = load_policy(request.policy_file)
    elif request.policy_inline:
        policy = request.policy_inline
    else:
        policy = get_default_policy()
    
    violations = []
    evidence_list = []
    checks_performed = 0
    checks_passed = 0
    
    # Process each spec file
    for spec_file in request.spec_files:
        spec = load_spec(spec_file)
        
        # ONLY check rules we actually implement
        for rule_name in IMPLEMENTED_RULES:
            if not policy.get("rules", {}).get(rule_name, False):
                continue  # Rule not enabled in policy
            
            checks_performed += 1
            rule_passed, violation = check_rule(rule_name, spec, policy)
            
            if rule_passed:
                checks_passed += 1
                evidence_list.append(Evidence(
                    rule=rule_name,
                    passed=True,
                    details={"file": spec_file, "status": "passed"}
                ))
            else:
                violations.append(violation)
                evidence_list.append(Evidence(
                    rule=rule_name,
                    passed=False,
                    details={"file": spec_file, "reason": violation.message}
                ))
    
    # Calculate compliance score
    compliance_score = int((checks_passed / checks_performed * 100)) if checks_performed > 0 else 100
    
    # Determine decision
    if violations:
        high_severity = any(v.severity == ViolationSeverity.HIGH for v in violations)
        if high_severity:
            decision = Decision.FAIL
            exit_code = 1
        else:
            decision = Decision.WARN
            exit_code = 0
    else:
        decision = Decision.PASS
        exit_code = 0
    
    # Build summary
    if decision == Decision.PASS:
        summary = f"Policy check passed: All {checks_performed} checks passed"
    elif decision == Decision.WARN:
        summary = f"Policy check passed with warnings: {len(violations)} low-severity issues"
    else:
        summary = f"Policy check failed: {len(violations)} violations found"
    
    # Build remediation
    remediation = None
    if violations:
        steps = []
        for v in violations[:3]:  # Show top 3 violations
            if v.rule == "require_openapi_version":
                steps.append("Add 'openapi' field with version (e.g., openapi: 3.0.0)")
            elif v.rule == "require_info_description":
                steps.append("Add description field under info section")
            elif v.rule == "require_security_definition":
                steps.append("Define security schemes in components.securitySchemes")
            elif v.rule == "require_https_only":
                steps.append("Update all server URLs to use HTTPS")
            elif v.rule == "max_path_depth":
                steps.append("Simplify API paths to reduce nesting depth")
        
        remediation = Remediation(
            summary="Fix policy violations to ensure API compliance",
            steps=steps,
            documentation="https://docs.delimit.ai/policy-rules"
        )
    
    return PolicyComplianceEvidence(
        task="check-policy",
        task_version="1.0",
        decision=decision,
        exit_code=exit_code,
        violations=violations,
        evidence=evidence_list,
        remediation=remediation,
        summary=summary,
        correlation_id=request.correlation_id,
        metrics={
            "files_checked": len(request.spec_files),
            "rules_checked": checks_performed,
            "rules_passed": checks_passed,
            "violations": len(violations)
        },
        compliance_score=compliance_score,
        policy_version="1.0",
        checks_performed=checks_performed,
        checks_passed=checks_passed
    )


def check_rule(rule_name: str, spec: Dict, policy: Dict) -> tuple[bool, Violation]:
    """Check a specific rule - ONLY for implemented rules"""
    
    severity_map = policy.get("severity", {})
    
    if rule_name == "require_openapi_version":
        if "openapi" in spec or "swagger" in spec:
            return True, None
        return False, Violation(
            rule=rule_name,
            severity=ViolationSeverity(severity_map.get(rule_name, "high")),
            message="API specification must include OpenAPI version",
            details={"missing": "openapi field"}
        )
    
    elif rule_name == "require_info_description":
        if spec.get("info", {}).get("description"):
            return True, None
        return False, Violation(
            rule=rule_name,
            severity=ViolationSeverity(severity_map.get(rule_name, "low")),
            message="API must have a description in info section",
            details={"missing": "info.description"}
        )
    
    elif rule_name == "require_security_definition":
        has_security = (
            spec.get("security") or 
            spec.get("securityDefinitions") or 
            spec.get("components", {}).get("securitySchemes")
        )
        if has_security:
            return True, None
        return False, Violation(
            rule=rule_name,
            severity=ViolationSeverity(severity_map.get(rule_name, "high")),
            message="API must define security schemes",
            details={"missing": "security definitions"}
        )
    
    elif rule_name == "require_https_only":
        servers = spec.get("servers", [])
        if not servers:
            return True, None  # No servers defined, pass by default
        
        non_https = [s for s in servers if not s.get("url", "").startswith("https://")]
        if non_https:
            return False, Violation(
                rule=rule_name,
                severity=ViolationSeverity(severity_map.get(rule_name, "high")),
                message=f"All servers must use HTTPS ({len(non_https)} use HTTP)",
                details={"non_https_count": str(len(non_https))}
            )
        return True, None
    
    elif rule_name == "max_path_depth":
        max_depth = policy.get("rules", {}).get("max_path_depth", 5)
        violations_found = []
        
        if "paths" in spec:
            for path in spec["paths"]:
                depth = len([p for p in path.split("/") if p])
                if depth > max_depth:
                    violations_found.append(path)
        
        if violations_found:
            return False, Violation(
                rule=rule_name,
                severity=ViolationSeverity(severity_map.get(rule_name, "medium")),
                message=f"Paths exceed max depth of {max_depth}",
                path=violations_found[0],
                details={"paths_over_limit": str(len(violations_found))}
            )
        return True, None
    
    # Unknown rule - should never happen with IMPLEMENTED_RULES filter
    return True, None


def get_default_policy() -> Dict:
    """Get default policy with ONLY implemented rules"""
    return {
        "rules": {
            "require_openapi_version": True,
            "require_info_description": True,
            "require_security_definition": True,
            "require_https_only": True,
            "max_path_depth": 5
        },
        "severity": {
            "require_openapi_version": "high",
            "require_info_description": "low",
            "require_security_definition": "high",
            "require_https_only": "high",
            "max_path_depth": "medium"
        }
    }


def load_spec(file_path: str) -> Dict:
    """Load API specification"""
    path = Path(file_path)
    with path.open('r') as f:
        if path.suffix in ['.yaml', '.yml']:
            return yaml.safe_load(f)
        else:
            return json.load(f)


def load_policy(policy_path: str) -> Dict:
    """Load policy rules from file"""
    path = Path(policy_path)
    with path.open('r') as f:
        return yaml.safe_load(f)