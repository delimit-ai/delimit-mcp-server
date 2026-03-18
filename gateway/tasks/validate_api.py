import yaml
import json
from typing import Dict, List, Any
from core.registry import task_registry
from schemas.base import TaskRequest

register_task = task_registry.register

@register_task("validate-api", version="v1", description="Validate API for breaking changes")
def validate_api_handler(request: TaskRequest) -> Dict[str, Any]:
    """Check API specifications for breaking changes"""
    
    files = request.files
    if len(files) != 2:
        raise ValueError("validate-api requires exactly 2 files: old and new API spec")
    
    old_spec = load_spec(files[0])
    new_spec = load_spec(files[1])
    
    breaking_changes = []
    warnings = []
    
    # Check removed endpoints
    old_paths = set(old_spec.get("paths", {}).keys())
    new_paths = set(new_spec.get("paths", {}).keys())
    
    removed = old_paths - new_paths
    if removed:
        for path in removed:
            breaking_changes.append({
                "type": "endpoint_removed",
                "path": path,
                "severity": "high"
            })
    
    # Check modified endpoints
    for path in old_paths & new_paths:
        old_methods = set(old_spec["paths"][path].keys())
        new_methods = set(new_spec["paths"][path].keys())
        
        removed_methods = old_methods - new_methods
        if removed_methods:
            for method in removed_methods:
                breaking_changes.append({
                    "type": "method_removed",
                    "path": path,
                    "method": method.upper(),
                    "severity": "high"
                })
        
        # Check parameter changes
        for method in old_methods & new_methods:
            old_params = old_spec["paths"][path][method].get("parameters", [])
            new_params = new_spec["paths"][path][method].get("parameters", [])
            
            old_required = {p["name"] for p in old_params if p.get("required", False)}
            new_required = {p["name"] for p in new_params if p.get("required", False)}
            
            new_required_params = new_required - old_required
            if new_required_params:
                for param in new_required_params:
                    breaking_changes.append({
                        "type": "required_parameter_added",
                        "path": path,
                        "method": method.upper(),
                        "parameter": param,
                        "severity": "high"
                    })
    
    # Check for new optional endpoints (non-breaking)
    added = new_paths - old_paths
    if added:
        for path in added:
            warnings.append(f"New endpoint added: {path}")
    
    risk_score = calculate_risk_score(breaking_changes)
    
    return {
        "breaking_changes": breaking_changes,
        "warnings": warnings,
        "risk_score": risk_score,
        "risk_level": get_risk_level(risk_score),
        "summary": {
            "total_breaking_changes": len(breaking_changes),
            "endpoints_removed": len([c for c in breaking_changes if c["type"] == "endpoint_removed"]),
            "methods_removed": len([c for c in breaking_changes if c["type"] == "method_removed"]),
            "required_params_added": len([c for c in breaking_changes if c["type"] == "required_parameter_added"])
        }
    }

def load_spec(file_path: str) -> Dict:
    """Load API specification from YAML or JSON"""
    with open(file_path, 'r') as f:
        if file_path.endswith('.yaml') or file_path.endswith('.yml'):
            return yaml.safe_load(f)
        elif file_path.endswith('.json'):
            return json.load(f)
        else:
            # Try YAML first, then JSON
            content = f.read()
            try:
                return yaml.safe_load(content)
            except:
                return json.loads(content)

def calculate_risk_score(breaking_changes: List[Dict]) -> int:
    """Calculate risk score based on breaking changes"""
    if not breaking_changes:
        return 0
    
    score = 0
    for change in breaking_changes:
        if change["severity"] == "high":
            score += 10
        elif change["severity"] == "medium":
            score += 5
        else:
            score += 1
    
    return min(score, 100)

def get_risk_level(score: int) -> str:
    """Convert risk score to level"""
    if score == 0:
        return "none"
    elif score < 20:
        return "low"
    elif score < 50:
        return "medium"
    else:
        return "high"