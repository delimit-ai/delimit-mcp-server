import yaml
from typing import Dict, List, Any
from core.registry import task_registry
from schemas.base import TaskRequest

register_task = task_registry.register

@register_task("check-policy", version="v1", description="Check API against policy rules")
def check_policy_handler(request: TaskRequest) -> Dict[str, Any]:
    """Validate API specification against organizational policies"""
    
    files = request.files
    if not files:
        raise ValueError("check-policy requires at least one API spec file")
    
    # Load policy from config or use defaults
    policy = request.config.get("policy", get_default_policy())
    if isinstance(policy, str):
        # If policy is a file path, load it
        policy = load_policy(policy)
    
    violations = []
    warnings = []
    passed_checks = []
    
    for file_path in files:
        spec = load_spec(file_path)
        
        # Check various policy rules
        violations_found, warnings_found, passed = check_spec_against_policy(spec, policy)
        violations.extend(violations_found)
        warnings.extend(warnings_found)
        passed_checks.extend(passed)
    
    compliance_score = calculate_compliance_score(violations, warnings, passed_checks)
    
    return {
        "compliant": len(violations) == 0,
        "violations": violations,
        "warnings": warnings,
        "passed_checks": passed_checks,
        "compliance_score": compliance_score,
        "summary": {
            "total_violations": len(violations),
            "total_warnings": len(warnings),
            "total_passed": len(passed_checks)
        }
    }

def load_spec(file_path: str) -> Dict:
    """Load API specification"""
    with open(file_path, 'r') as f:
        if file_path.endswith('.yaml') or file_path.endswith('.yml'):
            return yaml.safe_load(f)
        else:
            import json
            return json.load(f)

def load_policy(policy_path: str) -> Dict:
    """Load policy rules from file"""
    with open(policy_path, 'r') as f:
        return yaml.safe_load(f)

def get_default_policy() -> Dict:
    """Get default policy rules"""
    return {
        "rules": {
            "require_version": True,
            "require_description": True,
            "require_auth": True,
            "require_https": True,
            "max_path_depth": 5,
            "naming_convention": "kebab-case",
            "require_response_codes": ["200", "400", "500"],
            "require_request_validation": True
        },
        "severity": {
            "require_version": "high",
            "require_auth": "high",
            "require_https": "high",
            "require_description": "low",
            "max_path_depth": "medium",
            "naming_convention": "low"
        }
    }

def check_spec_against_policy(spec: Dict, policy: Dict) -> tuple:
    """Check specification against policy rules"""
    violations = []
    warnings = []
    passed = []
    
    rules = policy.get("rules", {})
    severity = policy.get("severity", {})
    
    # Check version requirement
    if rules.get("require_version"):
        if "openapi" in spec or "swagger" in spec:
            passed.append("API version specified")
        else:
            violation = {
                "rule": "require_version",
                "message": "API specification must include version",
                "severity": severity.get("require_version", "medium")
            }
            if violation["severity"] == "high":
                violations.append(violation)
            else:
                warnings.append(violation["message"])
    
    # Check description
    if rules.get("require_description"):
        if spec.get("info", {}).get("description"):
            passed.append("API description present")
        else:
            violation = {
                "rule": "require_description",
                "message": "API must have a description",
                "severity": severity.get("require_description", "low")
            }
            warnings.append(violation["message"])
    
    # Check security/auth
    if rules.get("require_auth"):
        if spec.get("security") or spec.get("securityDefinitions") or spec.get("components", {}).get("securitySchemes"):
            passed.append("Security definitions present")
        else:
            violations.append({
                "rule": "require_auth",
                "message": "API must define security schemes",
                "severity": severity.get("require_auth", "high")
            })
    
    # Check HTTPS requirement
    if rules.get("require_https"):
        servers = spec.get("servers", [])
        if servers:
            non_https = [s for s in servers if not s.get("url", "").startswith("https://")]
            if non_https:
                violations.append({
                    "rule": "require_https",
                    "message": f"All servers must use HTTPS. Found non-HTTPS: {len(non_https)}",
                    "severity": severity.get("require_https", "high")
                })
            else:
                passed.append("All servers use HTTPS")
        else:
            warnings.append("No servers defined to check HTTPS requirement")
    
    # Check path depth
    max_depth = rules.get("max_path_depth", 5)
    if "paths" in spec:
        for path in spec["paths"]:
            depth = len([p for p in path.split("/") if p])
            if depth > max_depth:
                warnings.append(f"Path exceeds max depth ({max_depth}): {path}")
    
    return violations, warnings, passed

def calculate_compliance_score(violations: List, warnings: List, passed: List) -> int:
    """Calculate compliance score (0-100)"""
    total_checks = len(violations) + len(warnings) + len(passed)
    if total_checks == 0:
        return 100
    
    # High severity violations heavily impact score
    high_violations = len([v for v in violations if v.get("severity") == "high"])
    med_violations = len([v for v in violations if v.get("severity") == "medium"])
    low_violations = len([v for v in violations if v.get("severity") == "low"])
    
    score = 100
    score -= high_violations * 20
    score -= med_violations * 10
    score -= low_violations * 5
    score -= len(warnings) * 2
    
    return max(0, score)