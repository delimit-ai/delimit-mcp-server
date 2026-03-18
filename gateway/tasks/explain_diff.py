import yaml
import json
from typing import Dict, List, Any
from core.registry import task_registry
from schemas.base import TaskRequest

register_task = task_registry.register

@register_task("explain-diff", version="v1", description="Explain differences between API versions")
def explain_diff_handler(request: TaskRequest) -> Dict[str, Any]:
    """Generate human-readable explanation of API changes"""
    
    files = request.files
    if len(files) != 2:
        raise ValueError("explain-diff requires exactly 2 files: old and new API spec")
    
    old_spec = load_spec(files[0])
    new_spec = load_spec(files[1])
    
    changes = analyze_changes(old_spec, new_spec)
    explanation = generate_explanation(changes)
    migration_guide = generate_migration_guide(changes)
    
    return {
        "summary": explanation["summary"],
        "changes": changes,
        "explanation": explanation["details"],
        "migration_guide": migration_guide,
        "impact_assessment": assess_impact(changes)
    }

def load_spec(file_path: str) -> Dict:
    """Load API specification"""
    with open(file_path, 'r') as f:
        if file_path.endswith('.yaml') or file_path.endswith('.yml'):
            return yaml.safe_load(f)
        else:
            return json.load(f)

def analyze_changes(old_spec: Dict, new_spec: Dict) -> Dict[str, Any]:
    """Analyze all changes between specs"""
    changes = {
        "endpoints": {
            "added": [],
            "removed": [],
            "modified": []
        },
        "models": {
            "added": [],
            "removed": [],
            "modified": []
        },
        "security": {
            "added": [],
            "removed": [],
            "modified": []
        },
        "metadata": {}
    }
    
    # Analyze endpoint changes
    old_paths = set(old_spec.get("paths", {}).keys())
    new_paths = set(new_spec.get("paths", {}).keys())
    
    changes["endpoints"]["added"] = list(new_paths - old_paths)
    changes["endpoints"]["removed"] = list(old_paths - new_paths)
    
    # Check for modified endpoints
    for path in old_paths & new_paths:
        old_methods = set(old_spec["paths"][path].keys())
        new_methods = set(new_spec["paths"][path].keys())
        
        if old_methods != new_methods or has_parameter_changes(
            old_spec["paths"][path], new_spec["paths"][path]
        ):
            changes["endpoints"]["modified"].append({
                "path": path,
                "methods_added": list(new_methods - old_methods),
                "methods_removed": list(old_methods - new_methods),
                "parameter_changes": get_parameter_changes(
                    old_spec["paths"][path], new_spec["paths"][path]
                )
            })
    
    # Analyze model/schema changes
    old_schemas = get_schemas(old_spec)
    new_schemas = get_schemas(new_spec)
    
    changes["models"]["added"] = list(set(new_schemas.keys()) - set(old_schemas.keys()))
    changes["models"]["removed"] = list(set(old_schemas.keys()) - set(new_schemas.keys()))
    
    # Check for modified models
    for model_name in set(old_schemas.keys()) & set(new_schemas.keys()):
        if old_schemas[model_name] != new_schemas[model_name]:
            changes["models"]["modified"].append({
                "name": model_name,
                "changes": compare_schemas(old_schemas[model_name], new_schemas[model_name])
            })
    
    # Analyze metadata changes
    if old_spec.get("info") != new_spec.get("info"):
        changes["metadata"]["version_change"] = {
            "old": old_spec.get("info", {}).get("version"),
            "new": new_spec.get("info", {}).get("version")
        }
    
    return changes

def has_parameter_changes(old_endpoint: Dict, new_endpoint: Dict) -> bool:
    """Check if endpoint has parameter changes"""
    for method in set(old_endpoint.keys()) & set(new_endpoint.keys()):
        old_params = old_endpoint[method].get("parameters", [])
        new_params = new_endpoint[method].get("parameters", [])
        
        if len(old_params) != len(new_params):
            return True
        
        old_param_names = {p.get("name") for p in old_params}
        new_param_names = {p.get("name") for p in new_params}
        
        if old_param_names != new_param_names:
            return True
    
    return False

def get_parameter_changes(old_endpoint: Dict, new_endpoint: Dict) -> List[Dict]:
    """Get detailed parameter changes"""
    changes = []
    
    for method in set(old_endpoint.keys()) & set(new_endpoint.keys()):
        old_params = {p.get("name"): p for p in old_endpoint[method].get("parameters", [])}
        new_params = {p.get("name"): p for p in new_endpoint[method].get("parameters", [])}
        
        added = set(new_params.keys()) - set(old_params.keys())
        removed = set(old_params.keys()) - set(new_params.keys())
        
        if added or removed:
            changes.append({
                "method": method.upper(),
                "added_params": list(added),
                "removed_params": list(removed)
            })
    
    return changes

def get_schemas(spec: Dict) -> Dict:
    """Extract schemas/models from spec"""
    if "components" in spec and "schemas" in spec["components"]:
        return spec["components"]["schemas"]
    elif "definitions" in spec:
        return spec["definitions"]
    return {}

def compare_schemas(old_schema: Dict, new_schema: Dict) -> List[str]:
    """Compare two schemas and return changes"""
    changes = []
    
    old_props = set(old_schema.get("properties", {}).keys())
    new_props = set(new_schema.get("properties", {}).keys())
    
    added = new_props - old_props
    removed = old_props - new_props
    
    if added:
        changes.append(f"Added fields: {', '.join(added)}")
    if removed:
        changes.append(f"Removed fields: {', '.join(removed)}")
    
    # Check for type changes
    for prop in old_props & new_props:
        old_type = old_schema["properties"][prop].get("type")
        new_type = new_schema["properties"][prop].get("type")
        if old_type != new_type:
            changes.append(f"Field '{prop}' type changed from {old_type} to {new_type}")
    
    return changes

def generate_explanation(changes: Dict) -> Dict[str, Any]:
    """Generate human-readable explanation"""
    explanation = {
        "summary": "",
        "details": []
    }
    
    # Calculate summary statistics
    total_changes = (
        len(changes["endpoints"]["added"]) +
        len(changes["endpoints"]["removed"]) +
        len(changes["endpoints"]["modified"]) +
        len(changes["models"]["added"]) +
        len(changes["models"]["removed"]) +
        len(changes["models"]["modified"])
    )
    
    if total_changes == 0:
        explanation["summary"] = "No significant changes detected between API versions"
    else:
        breaking = len(changes["endpoints"]["removed"]) + len(changes["models"]["removed"])
        if breaking > 0:
            explanation["summary"] = f"⚠️ {breaking} breaking changes detected among {total_changes} total changes"
        else:
            explanation["summary"] = f"✅ {total_changes} non-breaking changes detected"
    
    # Generate detailed explanations
    if changes["endpoints"]["added"]:
        explanation["details"].append(
            f"New endpoints added: {', '.join(changes['endpoints']['added'])}"
        )
    
    if changes["endpoints"]["removed"]:
        explanation["details"].append(
            f"⚠️ Endpoints removed (BREAKING): {', '.join(changes['endpoints']['removed'])}"
        )
    
    if changes["endpoints"]["modified"]:
        explanation["details"].append(
            f"Endpoints modified: {len(changes['endpoints']['modified'])} endpoints have changes"
        )
    
    if changes["models"]["added"]:
        explanation["details"].append(
            f"New models added: {', '.join(changes['models']['added'])}"
        )
    
    if changes["models"]["removed"]:
        explanation["details"].append(
            f"⚠️ Models removed (BREAKING): {', '.join(changes['models']['removed'])}"
        )
    
    return explanation

def generate_migration_guide(changes: Dict) -> List[str]:
    """Generate migration guide for developers"""
    guide = []
    
    if changes["endpoints"]["removed"]:
        guide.append("## Breaking Changes - Action Required")
        guide.append("The following endpoints have been removed:")
        for endpoint in changes["endpoints"]["removed"]:
            guide.append(f"  - {endpoint}: Find alternative endpoint or update implementation")
    
    if changes["endpoints"]["modified"]:
        guide.append("## Modified Endpoints")
        for mod in changes["endpoints"]["modified"]:
            if mod["methods_removed"]:
                guide.append(f"  - {mod['path']}: Methods {', '.join(mod['methods_removed'])} removed")
            if mod["parameter_changes"]:
                for change in mod["parameter_changes"]:
                    if change["removed_params"]:
                        guide.append(f"    - {change['method']}: Parameters {', '.join(change['removed_params'])} removed")
                    if change["added_params"]:
                        guide.append(f"    - {change['method']}: New parameters {', '.join(change['added_params'])} added")
    
    if changes["models"]["removed"]:
        guide.append("## Removed Models")
        for model in changes["models"]["removed"]:
            guide.append(f"  - {model}: Update code to use alternative model")
    
    if not guide:
        guide.append("No migration required - all changes are backwards compatible")
    
    return guide

def assess_impact(changes: Dict) -> Dict[str, Any]:
    """Assess the impact of changes"""
    impact = {
        "level": "none",
        "breaking_changes": 0,
        "affected_endpoints": 0,
        "affected_models": 0,
        "recommendation": ""
    }
    
    impact["breaking_changes"] = (
        len(changes["endpoints"]["removed"]) +
        len(changes["models"]["removed"]) +
        sum(len(m["methods_removed"]) for m in changes["endpoints"]["modified"])
    )
    
    impact["affected_endpoints"] = (
        len(changes["endpoints"]["added"]) +
        len(changes["endpoints"]["removed"]) +
        len(changes["endpoints"]["modified"])
    )
    
    impact["affected_models"] = (
        len(changes["models"]["added"]) +
        len(changes["models"]["removed"]) +
        len(changes["models"]["modified"])
    )
    
    if impact["breaking_changes"] > 0:
        impact["level"] = "high"
        impact["recommendation"] = "Major version bump required. Notify all API consumers."
    elif impact["affected_endpoints"] > 5:
        impact["level"] = "medium"
        impact["recommendation"] = "Minor version bump recommended. Review with team."
    elif impact["affected_endpoints"] > 0:
        impact["level"] = "low"
        impact["recommendation"] = "Patch version bump. Standard deployment process."
    else:
        impact["level"] = "none"
        impact["recommendation"] = "No action required."
    
    return impact