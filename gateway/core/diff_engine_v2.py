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
    PARAM_TYPE_CHANGED = "param_type_changed"
    PARAM_REQUIRED_CHANGED = "param_required_changed"
    RESPONSE_TYPE_CHANGED = "response_type_changed"
    SECURITY_REMOVED = "security_removed"
    SECURITY_SCOPE_REMOVED = "security_scope_removed"
    MAX_LENGTH_DECREASED = "max_length_decreased"
    MIN_LENGTH_INCREASED = "min_length_increased"
    # LED-1600: a field that was REQUIRED becoming OPTIONAL. In a RESPONSE this
    # is BREAKING — consumers can no longer rely on the field always being
    # present. In a REQUEST it is non-breaking (the server relaxes what it
    # demands). Direction is resolved by Change.context, not by the type alone.
    FIELD_REQUIREMENT_RELAXED = "field_requirement_relaxed"

    # Non-breaking changes
    ENDPOINT_ADDED = "endpoint_added"
    METHOD_ADDED = "method_added"
    OPTIONAL_PARAM_ADDED = "optional_param_added"
    RESPONSE_ADDED = "response_added"
    OPTIONAL_FIELD_ADDED = "optional_field_added"
    ENUM_VALUE_ADDED = "enum_value_added"
    DESCRIPTION_CHANGED = "description_changed"
    SECURITY_ADDED = "security_added"
    DEPRECATED_ADDED = "deprecated_added"
    DEFAULT_CHANGED = "default_changed"

# Change types that are ALWAYS breaking, independent of request/response
# context. The context-sensitive types (field add/remove/requirement) are
# handled separately in Change.is_breaking.
_ALWAYS_BREAKING = frozenset({
    ChangeType.ENDPOINT_REMOVED,
    ChangeType.METHOD_REMOVED,
    ChangeType.REQUIRED_PARAM_ADDED,
    ChangeType.PARAM_REMOVED,
    ChangeType.RESPONSE_REMOVED,
    ChangeType.TYPE_CHANGED,
    ChangeType.FORMAT_CHANGED,
    ChangeType.ENUM_VALUE_REMOVED,
    ChangeType.PARAM_TYPE_CHANGED,
    ChangeType.PARAM_REQUIRED_CHANGED,
    ChangeType.RESPONSE_TYPE_CHANGED,
    ChangeType.SECURITY_REMOVED,
    ChangeType.SECURITY_SCOPE_REMOVED,
    ChangeType.MAX_LENGTH_DECREASED,
    ChangeType.MIN_LENGTH_INCREASED,
})


@dataclass
class Change:
    type: ChangeType
    path: str
    details: Dict[str, Any]
    severity: str  # high, medium, low
    message: str
    # LED-1600: request/response context.
    context: Optional[str] = None

    @property
    def is_breaking(self) -> bool:
        ct = self.type

        if ct in _ALWAYS_BREAKING:
            return True

        if ct == ChangeType.REQUIRED_FIELD_ADDED:
            return self.context != "response"

        if ct == ChangeType.FIELD_REMOVED:
            return self.context != "request"

        if ct == ChangeType.FIELD_REQUIREMENT_RELAXED:
            return self.context != "request"

        return False

class OpenAPIDiffEngine:
    """Advanced diff engine for OpenAPI specifications."""
    
    def __init__(self):
        self.changes: List[Change] = []
        # LED-1588: fail-open skips (unresolvable refs, malformed nodes)
        self.advisories: List[Dict[str, Any]] = []
        # Roots for resolving local $ref pointers; populated per compare().
        self._old_root: Dict = {}
        self._new_root: Dict = {}
        # (old_ref, new_ref) pairs on the current descent path — cycle guard.
        self._ref_stack: Set[tuple] = set()

    def _add_advisory(self, kind: str, path: str, detail: str) -> None:
        """Record a fail-open skip. Dedupes identical (kind, path, detail)."""
        entry = {"kind": kind, "path": path, "detail": detail}
        if entry not in self.advisories:
            self.advisories.append(entry)

    def compare(self, old_spec: Dict, new_spec: Dict) -> List[Change]:
        """Compare two OpenAPI specifications and return all changes."""
        self.changes = []
        self.advisories = []
        old_spec = old_spec or {}
        new_spec = new_spec or {}
        
        self._old_root = old_spec
        self._new_root = new_spec
        self._ref_stack = set()

        # Honesty advisory (LED-1588): Swagger 2.0 detection.
        if "swagger" in old_spec or "swagger" in new_spec:
            self._add_advisory(
                "partial_spec_support", "(spec)",
                "Swagger 2.0 detected: top-level definitions are compared, but "
                "v2-style inline path/response/body schemas (responses[].schema, "
                "in:body parameters) are not yet deep-compared",
            )

        # Compare paths
        self._compare_paths(old_spec.get("paths", {}), new_spec.get("paths", {}))
        
        # Compare components/schemas (OpenAPI 3.x)
        _old_components = old_spec.get("components", {})
        _new_components = new_spec.get("components", {})
        self._compare_schemas(
            _old_components.get("schemas", {}) if isinstance(_old_components, dict) else {},
            _new_components.get("schemas", {}) if isinstance(_new_components, dict) else {},
        )

        # Compare top-level definitions (Swagger 2.0)
        self._compare_schemas(
            old_spec.get("definitions", {}),
            new_spec.get("definitions", {}),
            path_prefix="#/definitions",
        )

        # Compare security schemes
        self._compare_security(
            old_spec.get("components", {}).get("securitySchemes", {}) if isinstance(old_spec.get("components"), dict) else {},
            new_spec.get("components", {}).get("schemas", {}) if False else # Dummy to match gateway structure
            new_spec.get("components", {}).get("securitySchemes", {}) if isinstance(new_spec.get("components"), dict) else {}
        )
        
        return self.changes
    
    def _compare_paths(self, old_paths: Dict, new_paths: Dict):
        """Compare API paths/endpoints."""
        if not isinstance(old_paths, dict):
            self._add_advisory("malformed_node", "paths", f"old spec `paths` is not a dict (got {type(old_paths).__name__}); skipped")
            old_paths = {}
        if not isinstance(new_paths, dict):
            self._add_advisory("malformed_node", "paths", f"new spec `paths` is not a dict (got {type(new_paths).__name__}); skipped")
            new_paths = {}
            
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
            
        # Compare existing endpoints
        for path in old_set & new_set:
            self._compare_methods(path, old_paths[path], new_paths[path])

    HTTP_METHODS = ("get", "post", "put", "delete", "patch", "head", "options", "trace", "query")

    def _compare_methods(self, path: str, old_methods: Dict, new_methods: Dict):
        """Compare HTTP methods for an endpoint."""
        if not isinstance(old_methods, dict):
            self._add_advisory("malformed_node", path, f"old path-item methods at {path} is not a dict (got {type(old_methods).__name__}); skipped")
            old_methods = {}
        if not isinstance(new_methods, dict):
            self._add_advisory("malformed_node", path, f"new path-item methods at {path} is not a dict (got {type(new_methods).__name__}); skipped")
            new_methods = {}

        old_set = set(m for m in old_methods.keys() if m.lower() in self.HTTP_METHODS)
        new_set = set(m for m in new_methods.keys() if m.lower() in self.HTTP_METHODS)
        
        # Check removed methods
        for method in old_set - new_set:
            self.changes.append(Change(
                type=ChangeType.METHOD_REMOVED,
                path=f"{path}:{method.upper()}",
                details={"method": method.upper(), "endpoint": path},
                severity="high",
                message=f"Method {method.upper()} removed from {path}"
            ))
        
        # Check added methods (non-breaking)
        for method in new_set - old_set:
            self.changes.append(Change(
                type=ChangeType.METHOD_ADDED,
                path=f"{path}:{method.upper()}",
                details={"method": method.upper(), "endpoint": path},
                severity="low",
                message=f"Method {method.upper()} added to {path}"
            ))

        # Compare existing methods
        for method in old_set & new_set:
            self._compare_operation(f"{path}:{method.upper()}", old_methods[method], new_methods[method])
                
    def _compare_operation(self, operation_id: str, old_op: Dict, new_op: Dict):
        """Compare specific operations."""
        if not isinstance(old_op, dict):
            self._add_advisory("malformed_node", operation_id, "old operation is not a dict")
            return
        if not isinstance(new_op, dict):
            self._add_advisory("malformed_node", operation_id, "new operation is not a dict")
            return

        # Compare parameters
        old_params = {self._param_key(p): p for p in old_op.get("parameters", []) if isinstance(p, dict) and "name" in p}
        new_params = {self._param_key(p): p for p in new_op.get("parameters", []) if isinstance(p, dict) and "name" in p}
        
        # Check removed parameters
        for param_key in set(old_params.keys()) - set(new_params.keys()):
            param = old_params[param_key]
            self.changes.append(Change(
                type=ChangeType.PARAM_REMOVED,
                path=operation_id,
                details={"parameter": param.get("name"), "in": param.get("in")},
                severity="high",
                message=f"Parameter removed: {param.get('name')} from {operation_id}"
            ))
            
        # Check added required parameters (breaking)
        for param_key in set(new_params.keys()) - set(old_params.keys()):
            param = new_params[param_key]
            if param.get("required", False):
                self.changes.append(Change(
                    type=ChangeType.REQUIRED_PARAM_ADDED,
                    path=operation_id,
                    details={"parameter": param.get("name"), "in": param.get("in")},
                    severity="high",
                    message=f"Required parameter added: {param.get('name')} to {operation_id}"
                ))
        
        # Check added optional parameters (non-breaking)
        for param_key in set(new_params.keys()) - set(old_params.keys()):
            param = new_params[param_key]
            if not param.get("required", False):
                self.changes.append(Change(
                    type=ChangeType.OPTIONAL_PARAM_ADDED,
                    path=operation_id,
                    details={"parameter": param.get("name"), "in": param.get("in")},
                    severity="low",
                    message=f"Optional parameter added: {param.get('name')} to {operation_id}"
                ))

        # Check parameter schema changes
        for param_key in set(old_params.keys()) & set(new_params.keys()):
            self._compare_parameter_schemas(
                operation_id,
                old_params[param_key],
                new_params[param_key]
            )

        # Compare operation-level security
        if "security" in old_op or "security" in new_op:
            self._compare_operation_security(
                operation_id,
                old_op.get("security"),
                new_op.get("security")
            )

        # Check deprecated flag
        if not old_op.get("deprecated", False) and new_op.get("deprecated", False):
            self.changes.append(Change(
                type=ChangeType.DEPRECATED_ADDED,
                path=operation_id,
                details={"target": "operation"},
                severity="low",
                message=f"Operation marked as deprecated: {operation_id}"
            ))

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
        """Compare parameter schemas for type changes, required changes, and constraints."""
        old_schema = old_param.get("schema", {})
        new_schema = new_param.get("schema", {})
        param_name = old_param.get("name", "unknown")

        if old_schema.get("type") != new_schema.get("type"):
            self.changes.append(Change(
                type=ChangeType.PARAM_TYPE_CHANGED,
                path=operation_id,
                details={
                    "parameter": param_name,
                    "old_type": old_schema.get("type"),
                    "new_type": new_schema.get("type")
                },
                severity="high",
                message=f"Parameter type changed: {param_name} from {old_schema.get('type')} to {new_schema.get('type')} in {operation_id}"
            ))
            # Legacy ChangeType for back-compat
            self.changes.append(Change(
                type=ChangeType.TYPE_CHANGED,
                path=operation_id,
                details={
                    "parameter": param_name,
                    "old_type": old_schema.get("type"),
                    "new_type": new_schema.get("type")
                },
                severity="high",
                message=f"Parameter type changed: {param_name} from {old_schema.get('type')} to {new_schema.get('type')}"
            ))

        old_required = old_param.get("required", False)
        new_required = new_param.get("required", False)
        if not old_required and new_required:
            self.changes.append(Change(
                type=ChangeType.PARAM_REQUIRED_CHANGED,
                path=operation_id,
                details={"parameter": param_name, "old_required": False, "new_required": True},
                severity="high",
                message=f"Parameter changed from optional to required: {param_name} in {operation_id}"
            ))

        self._compare_constraints(f"{operation_id}:{param_name}", old_schema, new_schema)

        # Check default value changes
        if "default" in old_schema or "default" in new_schema:
            old_default = old_schema.get("default")
            new_default = new_schema.get("default")
            if old_default != new_default:
                self.changes.append(Change(
                    type=ChangeType.DEFAULT_CHANGED,
                    path=f"{operation_id}:{param_name}",
                    details={"old_default": old_default, "new_default": new_default},
                    severity="low",
                    message=f"Default value changed for {param_name} from {old_default} to {new_default}"
                ))

        # Check enum changes
        if "enum" in old_schema or "enum" in new_schema:
            self._compare_enums(
                f"{operation_id}:{param_name}",
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
            raw_old_content = old_body.get("content", {})
            raw_new_content = new_body.get("content", {})
            old_content = raw_old_content if isinstance(raw_old_content, dict) else {}
            new_content = raw_new_content if isinstance(raw_new_content, dict) else {}

            for content_type in old_content.keys() & new_content.keys():
                self._compare_schema_deep(
                    f"{operation_id}:request",
                    old_content[content_type].get("schema", {}),
                    new_content[content_type].get("schema", {}),
                    context="request",
                )

    def _compare_responses(self, operation_id: str, old_responses: Dict, new_responses: Dict):
        """Compare response definitions."""
        if not isinstance(old_responses, dict):
            self._add_advisory("malformed_node", operation_id, "old 'responses' is not a dict")
            old_responses = {}
        if not isinstance(new_responses, dict):
            self._add_advisory("malformed_node", operation_id, "new 'responses' is not a dict")
            new_responses = {}

        old_codes = set(old_responses.keys())
        new_codes = set(new_responses.keys())
        
        for code in old_codes - new_codes:
            if code.startswith("2"):
                self.changes.append(Change(
                    type=ChangeType.RESPONSE_REMOVED,
                    path=operation_id,
                    details={"response_code": code},
                    severity="high",
                    message=f"Success response {code} removed from {operation_id}"
                ))
        
        for code in old_codes & new_codes:
            old_resp = old_responses[code]
            new_resp = new_responses[code]

            if not isinstance(old_resp, dict):
                self._add_advisory("malformed_node", f"{operation_id}:{code}", f"old response {code} is {type(old_resp).__name__}")
                continue
            if not isinstance(new_resp, dict):
                self._add_advisory("malformed_node", f"{operation_id}:{code}", f"new response {code} is {type(new_resp).__name__}")
                continue

            raw_old_content = old_resp.get("content", {})
            raw_new_content = new_resp.get("content", {})
            old_content = raw_old_content if isinstance(raw_old_content, dict) else {}
            new_content = raw_new_content if isinstance(raw_new_content, dict) else {}

            if old_content or new_content:
                for content_type in old_content.keys() & new_content.keys():
                    self._compare_schema_deep(
                        f"{operation_id}:{code}",
                        old_content[content_type].get("schema", {}),
                        new_content[content_type].get("schema", {}),
                        context="response",
                    )
            elif "schema" in old_resp or "schema" in new_resp:
                # Swagger 2.0 style inline schema
                self._compare_schema_deep(
                    f"{operation_id}:{code}",
                    old_resp.get("schema", {}),
                    new_resp.get("schema", {}),
                    context="response",
                )
    
    def _resolve_local_ref(self, ref: Optional[str], root: Dict) -> Optional[Dict]:
        """Resolve a local JSON-pointer reference (#/a/b/c) against root."""
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return None
        node: Any = root
        for raw in ref[2:].split("/"):
            token = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and token in node:
                node = node[token]
            else:
                return None
        return node if isinstance(node, dict) else None

    def _advise_unverifiable_ref(self, path: str, ref: str, root: Dict) -> None:
        """Record an advisory for a $ref that could not be resolved."""
        if isinstance(ref, str) and ref.startswith("#/"):
            self._add_advisory("unresolved_local_ref", path, f"local $ref '{ref}' could not be resolved")
        else:
            self._add_advisory("external_ref_skipped", path, f"non-local $ref '{ref}' skipped")

    def _resolve_schema(self, schema: Any, root: Dict, path: str) -> Any:
        """Follow a (possibly chained) local $ref to its concrete schema."""
        if not isinstance(schema, dict) or "$ref" not in schema:
            return schema

        chain = set()
        curr = schema
        while isinstance(curr, dict) and "$ref" in curr:
            ref = curr["$ref"]
            if ref in chain:
                break  # Cycle in ref chain
            chain.add(ref)
            
            target = self._resolve_local_ref(ref, root)
            if target is None:
                self._advise_unverifiable_ref(path, ref, root)
                return curr
            curr = target
        return curr

    def _compare_schema_deep(self, path: str, old_schema: Dict, new_schema: Dict, required_fields: Optional[Set[str]] = None, context: Optional[str] = None):
        """Deep comparison of schemas including nested objects."""
        if old_schema is None: old_schema = {}
        if new_schema is None: new_schema = {}

        old_ref = old_schema.get("$ref") if isinstance(old_schema, dict) else None
        new_ref = new_schema.get("$ref") if isinstance(new_schema, dict) else None

        if old_ref or new_ref:
            if old_ref and new_ref and old_ref == new_ref:
                return
            
            seen_key = (old_ref or "", new_ref or "")
            if seen_key in self._ref_stack:
                return  # cycle on current path
                
            resolved_old = self._resolve_schema(old_schema, self._old_root, path) if old_ref else old_schema
            resolved_new = self._resolve_schema(new_schema, self._new_root, path) if new_ref else new_schema

            if (old_ref and resolved_old is old_schema and "$ref" in old_schema) or \
               (new_ref and resolved_new is new_schema and "$ref" in new_schema):
                return # Unresolved ref advisory already added by _resolve_schema

            self._ref_stack.add(seen_key)
            try:
                self._compare_schema_deep(path, resolved_old, resolved_new, required_fields, context)
            finally:
                self._ref_stack.discard(seen_key)
            return

        old_type = old_schema.get("type")
        new_type = new_schema.get("type")

        if old_type != new_type and old_type is not None:
            is_response = bool(":" in path and any(code in path for code in ["200", "201", "202", "204", "301", "400", "401", "403", "404", "500"]))
            if is_response:
                self.changes.append(Change(
                    type=ChangeType.RESPONSE_TYPE_CHANGED,
                    path=path,
                    details={"old_type": old_type, "new_type": new_type},
                    severity="high",
                    message=f"Response type changed from {old_type} to {new_type} at {path}"
                ))
            self.changes.append(Change(
                type=ChangeType.TYPE_CHANGED,
                path=path,
                details={"old_type": old_type, "new_type": new_type},
                severity="high",
                message=f"Type changed from {old_type} to {new_type} at {path}"
            ))
            return

        def _is_object_shaped(s: Any) -> bool:
            return isinstance(s, dict) and ("properties" in s or "required" in s)

        is_object = old_type == "object" or new_type == "object" or _is_object_shaped(old_schema) or _is_object_shaped(new_schema)
        if is_object:
            raw_old_props = old_schema.get("properties", {})
            raw_new_props = new_schema.get("properties", {})
            old_props = raw_old_props if isinstance(raw_old_props, dict) else {}
            new_props = raw_new_props if isinstance(raw_new_props, dict) else {}
            raw_old_required = old_schema.get("required", [])
            raw_new_required = new_schema.get("required", [])
            old_required = set(raw_old_required) if isinstance(raw_old_required, list) else set()
            new_required = set(raw_new_required) if isinstance(raw_new_required, list) else set()

            for prop in set(old_props.keys()) - set(new_props.keys()):
                was_required = prop in old_required
                is_breaking_removal = context != "request"
                self.changes.append(Change(
                    type=ChangeType.FIELD_REMOVED,
                    path=f"{path}.{prop}",
                    details={"field": prop, "was_required": str(was_required).lower(), "context": context or ""},
                    severity="high" if is_breaking_removal else "low",
                    message=f"{'Required' if was_required else 'Optional'} field '{prop}' removed at {path}" + ("" if is_breaking_removal else " (request field; non-breaking for clients)"),
                    context=context
                ))

            for prop in new_required - old_required:
                if prop not in old_props:
                    is_breaking_add = context != "response"
                    self.changes.append(Change(
                        type=ChangeType.REQUIRED_FIELD_ADDED,
                        path=f"{path}.{prop}",
                        details={"field": prop, "context": context or ""},
                        severity="high" if is_breaking_add else "low",
                        message=f"New required field '{prop}' added at {path}" + ("" if is_breaking_add else " (response field; non-breaking for consumers)"),
                        context=context
                    ))
                else:
                    is_breaking_tighten = context != "response"
                    self.changes.append(Change(
                        type=ChangeType.REQUIRED_FIELD_ADDED,
                        path=f"{path}.{prop}",
                        details={"field": prop, "context": context or "", "was_optional": "true"},
                        severity="high" if is_breaking_tighten else "low",
                        message=f"Field '{prop}' changed from optional to required at {path}" + ("" if is_breaking_tighten else " (response field; non-breaking for consumers)"),
                        context=context
                    ))

            for prop in old_required - new_required:
                if prop in new_props:
                    is_breaking_relax = context != "request"
                    self.changes.append(Change(
                        type=ChangeType.FIELD_REQUIREMENT_RELAXED,
                        path=f"{path}.{prop}",
                        details={"field": prop, "context": context or ""},
                        severity="high" if is_breaking_relax else "low",
                        message=f"Field '{prop}' changed from required to optional at {path}" + (" (response field; consumers can no longer rely on its presence)" if is_breaking_relax else " (request field; non-breaking)"),
                        context=context
                    ))

            for prop in set(new_props.keys()) - set(old_props.keys()):
                if prop not in new_required:
                    self.changes.append(Change(
                        type=ChangeType.OPTIONAL_FIELD_ADDED,
                        path=f"{path}.{prop}",
                        details={"field": prop, "context": context or ""},
                        severity="low",
                        message=f"Optional field '{prop}' added at {path}",
                        context=context
                    ))

            for prop in set(old_props.keys()) & set(new_props.keys()):
                old_prop_schema = old_props[prop]
                new_prop_schema = new_props[prop]
                
                if isinstance(old_prop_schema, dict) and isinstance(new_prop_schema, dict):
                    if not old_prop_schema.get("deprecated", False) and new_prop_schema.get("deprecated", False):
                        self.changes.append(Change(
                            type=ChangeType.DEPRECATED_ADDED,
                            path=f"{path}.{prop}",
                            details={"target": "field", "field": prop},
                            severity="low",
                            message=f"Field '{prop}' marked as deprecated at {path}"
                        ))
                    if "default" in old_prop_schema or "default" in new_prop_schema:
                        old_def = old_prop_schema.get("default")
                        new_def = new_prop_schema.get("default")
                        if old_def != new_def:
                            self.changes.append(Change(
                                type=ChangeType.DEFAULT_CHANGED,
                                path=f"{path}.{prop}",
                                details={"old_default": old_def, "new_default": new_def},
                                severity="low",
                                message=f"Default value changed for '{prop}' from {old_def} to {new_def} at {path}"
                            ))
                    self._compare_constraints(f"{path}.{prop}", old_prop_schema, new_prop_schema)

                self._compare_schema_deep(f"{path}.{prop}", old_prop_schema, new_prop_schema, old_required if prop in old_required else None, context)

        elif old_type == "array":
            if "items" in old_schema and "items" in new_schema:
                self._compare_schema_deep(f"{path}[]", old_schema["items"], new_schema["items"], None, context)

        if "enum" in old_schema or "enum" in new_schema:
            self._compare_enums(path, old_schema.get("enum", []), new_schema.get("enum", []))

        if old_type != "object":
            self._compare_constraints(path, old_schema, new_schema)
    
    def _compare_enums(self, path: str, old_enum: List, new_enum: List):
        """Compare enum values."""
        old_set = set(old_enum) if isinstance(old_enum, list) else set()
        new_set = set(new_enum) if isinstance(new_enum, list) else set()
        for value in old_set - new_set:
            self.changes.append(Change(type=ChangeType.ENUM_VALUE_REMOVED, path=path, details={"value": value}, severity="high", message=f"Enum value '{value}' removed at {path}"))
        for value in new_set - old_set:
            self.changes.append(Change(type=ChangeType.ENUM_VALUE_ADDED, path=path, details={"value": value}, severity="low", message=f"Enum value '{value}' added at {path}"))
    
    def _compare_schemas(self, old_schemas: Dict, new_schemas: Dict, path_prefix: str = "#/components/schemas"):
        """Compare a named-schema map."""
        if not isinstance(old_schemas, dict): old_schemas = {}
        if not isinstance(new_schemas, dict): new_schemas = {}
        for schema_name in set(old_schemas.keys()) - set(new_schemas.keys()):
            self.changes.append(Change(type=ChangeType.FIELD_REMOVED, path=f"{path_prefix}/{schema_name}", details={"schema": schema_name}, severity="medium", message=f"Schema '{schema_name}' removed"))
        for schema_name in set(old_schemas.keys()) & set(new_schemas.keys()):
            self._compare_schema_deep(f"{path_prefix}/{schema_name}", old_schemas[schema_name], new_schemas[schema_name])
    
    def _compare_constraints(self, path: str, old_schema: Dict, new_schema: Dict):
        """Compare schema constraints."""
        for prop in ("maxLength", "maxItems"):
            old_val = old_schema.get(prop)
            new_val = new_schema.get(prop)
            if old_val is not None and new_val is not None and new_val < old_val:
                self.changes.append(Change(type=ChangeType.MAX_LENGTH_DECREASED, path=path, details={"constraint": prop, "old_value": old_val, "new_value": new_val}, severity="high", message=f"{prop} decreased from {old_val} to {new_val} at {path}"))
            elif old_val is None and new_val is not None:
                self.changes.append(Change(type=ChangeType.MAX_LENGTH_DECREASED, path=path, details={"constraint": prop, "old_value": None, "new_value": new_val}, severity="high", message=f"{prop} added ({new_val}) at {path} where none existed"))
        for prop in ("minLength", "minItems"):
            old_val = old_schema.get(prop)
            new_val = new_schema.get(prop)
            if old_val is not None and new_val is not None and new_val > old_val:
                self.changes.append(Change(type=ChangeType.MIN_LENGTH_INCREASED, path=path, details={"constraint": prop, "old_value": old_val, "new_value": new_val}, severity="high", message=f"{prop} increased from {old_val} to {new_val} at {path}"))
            elif old_val is None and new_val is not None and new_val > 0:
                self.changes.append(Change(type=ChangeType.MIN_LENGTH_INCREASED, path=path, details={"constraint": prop, "old_value": None, "new_value": new_val}, severity="high", message=f"{prop} added ({new_val}) at {path} where none existed"))

    def _compare_operation_security(self, operation_id: str, old_security: Optional[list], new_security: Optional[list]):
        """Compare operation-level security requirements."""
        def _security_map(sec_list):
            result = {}
            if isinstance(sec_list, list):
                for item in sec_list:
                    if isinstance(item, dict):
                        for scheme, scopes in item.items():
                            result[scheme] = set(scopes) if isinstance(scopes, list) else set()
            return result
        old_map = _security_map(old_security)
        new_map = _security_map(new_security)
        for scheme in set(old_map.keys()) - set(new_map.keys()):
            self.changes.append(Change(type=ChangeType.SECURITY_REMOVED, path=operation_id, details={"scheme": scheme}, severity="high", message=f"Security scheme '{scheme}' removed from {operation_id}"))
        for scheme in set(new_map.keys()) - set(old_map.keys()):
            self.changes.append(Change(type=ChangeType.SECURITY_ADDED, path=operation_id, details={"scheme": scheme}, severity="low", message=f"Security scheme '{scheme}' added to {operation_id}"))
        for scheme in set(old_map.keys()) & set(new_map.keys()):
            removed_scopes = old_map[scheme] - new_map[scheme]
            for scope in removed_scopes:
                self.changes.append(Change(type=ChangeType.SECURITY_SCOPE_REMOVED, path=operation_id, details={"scheme": scheme, "scope": scope}, severity="high", message=f"OAuth scope '{scope}' removed from scheme '{scheme}' at {operation_id}"))

    def _compare_security(self, old_security: Dict, new_security: Dict):
        """Compare security schemes."""
        if not isinstance(old_security, dict): old_security = {}
        if not isinstance(new_security, dict): new_security = {}
        for scheme in set(old_security.keys()) - set(new_security.keys()):
            self.changes.append(Change(type=ChangeType.SECURITY_REMOVED, path=f"#/components/securitySchemes/{scheme}", details={"scheme": scheme}, severity="high", message=f"Security scheme '{scheme}' removed"))
        for scheme in set(new_security.keys()) - set(old_security.keys()):
            self.changes.append(Change(type=ChangeType.SECURITY_ADDED, path=f"#/components/securitySchemes/{scheme}", details={"scheme": scheme}, severity="low", message=f"Security scheme '{scheme}' added"))
    
    def _param_key(self, param: Dict) -> str:
        return f"{param.get('in', 'query')}:{param.get('name', '')}"
    
    def get_breaking_changes(self) -> List[Change]:
        return [c for c in self.changes if c.is_breaking]
    
    def get_summary(self) -> Dict[str, Any]:
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
