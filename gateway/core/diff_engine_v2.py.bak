"""
Enhanced OpenAPI diff engine with deep schema comparison.
Handles nested objects, response schemas, enums, and edge cases.
"""

from typing import Dict, List, Any, Optional, Set, Tuple
from dataclasses import dataclass
from enum import Enum

class ChangeType(Enum):
    # Breaking changes
    ENDPOINT_REMOVED = "endpoint_removed"
    METHOD_REMOVED = "method_removed"
    REQUIRED_PARAM_ADDED = "required_param_added"
    PARAM_REMOVED = "param_removed"
    RESPONSE_REMOVED = "response_removed"
    REQUIRED_FIELD_ADDED = "required_field_added"
    FIELD_REMOVED = "field_removed"
    TYPE_CHANGED = "type_changed"
    FORMAT_CHANGED = "format_changed"
    ENUM_VALUE_REMOVED = "enum_value_removed"
    
    # Non-breaking changes
    ENDPOINT_ADDED = "endpoint_added"
    METHOD_ADDED = "method_added"
    OPTIONAL_PARAM_ADDED = "optional_param_added"
    RESPONSE_ADDED = "response_added"
    OPTIONAL_FIELD_ADDED = "optional_field_added"
    ENUM_VALUE_ADDED = "enum_value_added"
    DESCRIPTION_CHANGED = "description_changed"

@dataclass
class Change:
    type: ChangeType
    path: str
    details: Dict[str, Any]
    severity: str  # high, medium, low
    message: str
    
    @property
    def is_breaking(self) -> bool:
        return self.type in [
            ChangeType.ENDPOINT_REMOVED,
            ChangeType.METHOD_REMOVED,
            ChangeType.REQUIRED_PARAM_ADDED,
            ChangeType.PARAM_REMOVED,
            ChangeType.RESPONSE_REMOVED,
            ChangeType.REQUIRED_FIELD_ADDED,
            ChangeType.FIELD_REMOVED,
            ChangeType.TYPE_CHANGED,
            ChangeType.FORMAT_CHANGED,
            ChangeType.ENUM_VALUE_REMOVED,
        ]

class OpenAPIDiffEngine:
    """Advanced diff engine for OpenAPI specifications."""
    
    def __init__(self):
        self.changes: List[Change] = []
    
    def compare(self, old_spec: Dict, new_spec: Dict) -> List[Change]:
        """Compare two OpenAPI specifications and return all changes."""
        self.changes = []
        
        # Compare paths
        self._compare_paths(old_spec.get("paths", {}), new_spec.get("paths", {}))
        
        # Compare components/schemas
        self._compare_schemas(
            old_spec.get("components", {}).get("schemas", {}),
            new_spec.get("components", {}).get("schemas", {})
        )
        
        # Compare security schemes
        self._compare_security(
            old_spec.get("components", {}).get("securitySchemes", {}),
            new_spec.get("components", {}).get("securitySchemes", {})
        )
        
        return self.changes
    
    def _compare_paths(self, old_paths: Dict, new_paths: Dict):
        """Compare API paths/endpoints."""
        old_set = set(old_paths.keys())
        new_set = set(new_paths.keys())
        
        # Check removed endpoints
        for path in old_set - new_set:
            self.changes.append(Change(
                type=ChangeType.ENDPOINT_REMOVED,
                path=path,
                details={"endpoint": path},
                severity="high",
                message=f"Endpoint removed: {path}"
            ))
        
        # Check added endpoints
        for path in new_set - old_set:
            self.changes.append(Change(
                type=ChangeType.ENDPOINT_ADDED,
                path=path,
                details={"endpoint": path},
                severity="low",
                message=f"New endpoint added: {path}"
            ))
        
        # Check modified endpoints
        for path in old_set & new_set:
            self._compare_methods(path, old_paths[path], new_paths[path])
    
    def _compare_methods(self, path: str, old_methods: Dict, new_methods: Dict):
        """Compare HTTP methods for an endpoint."""
        old_set = set(m for m in old_methods.keys() if m in ["get", "post", "put", "delete", "patch", "head", "options"])
        new_set = set(m for m in new_methods.keys() if m in ["get", "post", "put", "delete", "patch", "head", "options"])
        
        # Check removed methods
        for method in old_set - new_set:
            self.changes.append(Change(
                type=ChangeType.METHOD_REMOVED,
                path=f"{path}:{method.upper()}",
                details={"endpoint": path, "method": method.upper()},
                severity="high",
                message=f"Method removed: {method.upper()} {path}"
            ))
        
        # Check modified methods
        for method in old_set & new_set:
            self._compare_operation(
                f"{path}:{method.upper()}",
                old_methods[method],
                new_methods[method]
            )
    
    def _compare_operation(self, operation_id: str, old_op: Dict, new_op: Dict):
        """Compare operation details (parameters, responses, etc.)."""
        
        # Compare parameters
        old_params = {self._param_key(p): p for p in old_op.get("parameters", [])}
        new_params = {self._param_key(p): p for p in new_op.get("parameters", [])}
        
        # Check removed parameters
        for param_key in set(old_params.keys()) - set(new_params.keys()):
            param = old_params[param_key]
            self.changes.append(Change(
                type=ChangeType.PARAM_REMOVED,
                path=operation_id,
                details={"parameter": param["name"], "in": param["in"]},
                severity="high",
                message=f"Parameter removed: {param['name']} from {operation_id}"
            ))
        
        # Check added required parameters
        for param_key in set(new_params.keys()) - set(old_params.keys()):
            param = new_params[param_key]
            if param.get("required", False):
                self.changes.append(Change(
                    type=ChangeType.REQUIRED_PARAM_ADDED,
                    path=operation_id,
                    details={"parameter": param["name"], "in": param["in"]},
                    severity="high",
                    message=f"Required parameter added: {param['name']} to {operation_id}"
                ))
        
        # Check parameter schema changes
        for param_key in set(old_params.keys()) & set(new_params.keys()):
            self._compare_parameter_schemas(
                operation_id,
                old_params[param_key],
                new_params[param_key]
            )
        
        # Compare request body
        if "requestBody" in old_op or "requestBody" in new_op:
            self._compare_request_body(
                operation_id,
                old_op.get("requestBody"),
                new_op.get("requestBody")
            )
        
        # Compare responses
        self._compare_responses(
            operation_id,
            old_op.get("responses", {}),
            new_op.get("responses", {})
        )
    
    def _compare_parameter_schemas(self, operation_id: str, old_param: Dict, new_param: Dict):
        """Compare parameter schemas for type changes."""
        old_schema = old_param.get("schema", {})
        new_schema = new_param.get("schema", {})
        
        # Check type changes
        if old_schema.get("type") != new_schema.get("type"):
            self.changes.append(Change(
                type=ChangeType.TYPE_CHANGED,
                path=operation_id,
                details={
                    "parameter": old_param["name"],
                    "old_type": old_schema.get("type"),
                    "new_type": new_schema.get("type")
                },
                severity="high",
                message=f"Parameter type changed: {old_param['name']} from {old_schema.get('type')} to {new_schema.get('type')}"
            ))
        
        # Check enum changes
        if "enum" in old_schema or "enum" in new_schema:
            self._compare_enums(
                f"{operation_id}:{old_param['name']}",
                old_schema.get("enum", []),
                new_schema.get("enum", [])
            )
    
    def _compare_request_body(self, operation_id: str, old_body: Optional[Dict], new_body: Optional[Dict]):
        """Compare request body schemas."""
        if old_body and not new_body:
            self.changes.append(Change(
                type=ChangeType.FIELD_REMOVED,
                path=operation_id,
                details={"field": "request_body"},
                severity="high",
                message=f"Request body removed from {operation_id}"
            ))
        elif not old_body and new_body and new_body.get("required", False):
            self.changes.append(Change(
                type=ChangeType.REQUIRED_FIELD_ADDED,
                path=operation_id,
                details={"field": "request_body"},
                severity="high",
                message=f"Required request body added to {operation_id}"
            ))
        elif old_body and new_body:
            # Compare content types
            old_content = old_body.get("content", {})
            new_content = new_body.get("content", {})
            
            for content_type in old_content.keys() & new_content.keys():
                self._compare_schema_deep(
                    f"{operation_id}:request",
                    old_content[content_type].get("schema", {}),
                    new_content[content_type].get("schema", {})
                )
    
    def _compare_responses(self, operation_id: str, old_responses: Dict, new_responses: Dict):
        """Compare response definitions."""
        old_codes = set(old_responses.keys())
        new_codes = set(new_responses.keys())
        
        # Check removed responses
        for code in old_codes - new_codes:
            # Only flag 2xx responses as breaking
            if code.startswith("2"):
                self.changes.append(Change(
                    type=ChangeType.RESPONSE_REMOVED,
                    path=operation_id,
                    details={"response_code": code},
                    severity="high",
                    message=f"Success response {code} removed from {operation_id}"
                ))
        
        # Compare response schemas
        for code in old_codes & new_codes:
            old_resp = old_responses[code]
            new_resp = new_responses[code]
            
            if "content" in old_resp or "content" in new_resp:
                old_content = old_resp.get("content", {})
                new_content = new_resp.get("content", {})
                
                for content_type in old_content.keys() & new_content.keys():
                    self._compare_schema_deep(
                        f"{operation_id}:{code}",
                        old_content[content_type].get("schema", {}),
                        new_content[content_type].get("schema", {})
                    )
    
    def _compare_schema_deep(self, path: str, old_schema: Dict, new_schema: Dict, required_fields: Optional[Set[str]] = None):
        """Deep comparison of schemas including nested objects."""
        
        # Handle references
        if "$ref" in old_schema or "$ref" in new_schema:
            # TODO: Resolve references properly
            return
        
        # Compare types
        old_type = old_schema.get("type")
        new_type = new_schema.get("type")
        
        if old_type != new_type and old_type is not None:
            self.changes.append(Change(
                type=ChangeType.TYPE_CHANGED,
                path=path,
                details={"old_type": old_type, "new_type": new_type},
                severity="high",
                message=f"Type changed from {old_type} to {new_type} at {path}"
            ))
            return
        
        # Compare object properties
        if old_type == "object":
            old_props = old_schema.get("properties", {})
            new_props = new_schema.get("properties", {})
            old_required = set(old_schema.get("required", []))
            new_required = set(new_schema.get("required", []))
            
            # Check removed fields
            for prop in set(old_props.keys()) - set(new_props.keys()):
                if prop in old_required:
                    self.changes.append(Change(
                        type=ChangeType.FIELD_REMOVED,
                        path=f"{path}.{prop}",
                        details={"field": prop},
                        severity="high",
                        message=f"Required field '{prop}' removed at {path}"
                    ))
            
            # Check new required fields
            for prop in new_required - old_required:
                if prop not in old_props:
                    self.changes.append(Change(
                        type=ChangeType.REQUIRED_FIELD_ADDED,
                        path=f"{path}.{prop}",
                        details={"field": prop},
                        severity="high",
                        message=f"New required field '{prop}' added at {path}"
                    ))
            
            # Recursively compare nested properties
            for prop in set(old_props.keys()) & set(new_props.keys()):
                self._compare_schema_deep(
                    f"{path}.{prop}",
                    old_props[prop],
                    new_props[prop],
                    old_required if prop in old_required else None
                )
        
        # Compare arrays
        elif old_type == "array":
            if "items" in old_schema and "items" in new_schema:
                self._compare_schema_deep(
                    f"{path}[]",
                    old_schema["items"],
                    new_schema["items"]
                )
        
        # Compare enums
        if "enum" in old_schema or "enum" in new_schema:
            self._compare_enums(path, old_schema.get("enum", []), new_schema.get("enum", []))
    
    def _compare_enums(self, path: str, old_enum: List, new_enum: List):
        """Compare enum values."""
        old_set = set(old_enum)
        new_set = set(new_enum)
        
        # Removed enum values are breaking
        for value in old_set - new_set:
            self.changes.append(Change(
                type=ChangeType.ENUM_VALUE_REMOVED,
                path=path,
                details={"value": value},
                severity="high",
                message=f"Enum value '{value}' removed at {path}"
            ))
        
        # Added enum values are non-breaking
        for value in new_set - old_set:
            self.changes.append(Change(
                type=ChangeType.ENUM_VALUE_ADDED,
                path=path,
                details={"value": value},
                severity="low",
                message=f"Enum value '{value}' added at {path}"
            ))
    
    def _compare_schemas(self, old_schemas: Dict, new_schemas: Dict):
        """Compare component schemas."""
        # Schema removal is breaking if referenced
        for schema_name in set(old_schemas.keys()) - set(new_schemas.keys()):
            self.changes.append(Change(
                type=ChangeType.FIELD_REMOVED,
                path=f"#/components/schemas/{schema_name}",
                details={"schema": schema_name},
                severity="medium",
                message=f"Schema '{schema_name}' removed"
            ))
        
        # Compare existing schemas
        for schema_name in set(old_schemas.keys()) & set(new_schemas.keys()):
            self._compare_schema_deep(
                f"#/components/schemas/{schema_name}",
                old_schemas[schema_name],
                new_schemas[schema_name]
            )
    
    def _compare_security(self, old_security: Dict, new_security: Dict):
        """Compare security schemes."""
        # Security scheme changes are usually breaking
        for scheme in set(old_security.keys()) - set(new_security.keys()):
            self.changes.append(Change(
                type=ChangeType.FIELD_REMOVED,
                path=f"#/components/securitySchemes/{scheme}",
                details={"scheme": scheme},
                severity="high",
                message=f"Security scheme '{scheme}' removed"
            ))
    
    def _param_key(self, param: Dict) -> str:
        """Generate unique key for parameter."""
        return f"{param.get('in', 'query')}:{param.get('name', '')}"
    
    def get_breaking_changes(self) -> List[Change]:
        """Get only breaking changes."""
        return [c for c in self.changes if c.is_breaking]
    
    def get_summary(self) -> Dict[str, Any]:
        """Get summary of all changes."""
        breaking = self.get_breaking_changes()
        return {
            "total_changes": len(self.changes),
            "breaking_changes": len(breaking),
            "endpoints_removed": len([c for c in breaking if c.type == ChangeType.ENDPOINT_REMOVED]),
            "methods_removed": len([c for c in breaking if c.type == ChangeType.METHOD_REMOVED]),
            "parameters_changed": len([c for c in breaking if c.type in [ChangeType.PARAM_REMOVED, ChangeType.REQUIRED_PARAM_ADDED]]),
            "schemas_changed": len([c for c in breaking if c.type in [ChangeType.FIELD_REMOVED, ChangeType.REQUIRED_FIELD_ADDED, ChangeType.TYPE_CHANGED]]),
            "is_breaking": len(breaking) > 0
        }