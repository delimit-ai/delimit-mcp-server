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
    # LED-3792: newly-required auth on a previously-open operation (top-level
    # or operation-level `security`). ALWAYS breaking — every existing
    # unauthenticated client now gets 401. Distinct from SECURITY_ADDED, which
    # remains the non-breaking "a scheme DEFINITION was added" / "an alternative
    # auth scheme was offered" signal.
    SECURITY_REQUIREMENT_ADDED = "security_requirement_added"
    # LED-3792: a new required OAuth scope on an existing scheme. Tokens lacking
    # it now fail. ALWAYS breaking.
    SECURITY_SCOPE_ADDED = "security_scope_added"
    # LED-3792: a media type (e.g. application/json) removed from a request or
    # response body. Every client using it breaks. ALWAYS breaking.
    MEDIA_TYPE_REMOVED = "media_type_removed"
    # LED-3792: discriminator.propertyName changed or a mapping entry removed —
    # breaks polymorphic (de)serialization. ALWAYS breaking.
    DISCRIMINATOR_CHANGED = "discriminator_changed"
    # LED-3792: an allOf member / oneOf|anyOf branch removed in the breaking
    # direction (see _compare_composition). Only emitted when breaking, so it is
    # ALWAYS breaking; the non-breaking direction is surfaced as an advisory.
    COMPOSITION_MEMBER_REMOVED = "composition_member_removed"
    # LED-3792: a request-side numeric/pattern constraint was tightened
    # (minimum raised, maximum lowered, exclusive bound tightened, multipleOf
    # added/changed, pattern added/changed). Context-sensitive: breaking in a
    # request (previously-valid inputs now 400) unless the field is in a
    # response.
    CONSTRAINT_TIGHTENED = "constraint_tightened"
    # LED-3792: additionalProperties transitioned true/absent -> false.
    # Context-sensitive: breaking in a request (extra keys now rejected).
    ADDITIONAL_PROPERTIES_TIGHTENED = "additional_properties_tightened"
    # LED-3792: a field GAINED nullability (3.0 nullable:false->true).
    # Context-sensitive: breaking in a RESPONSE (consumers may now receive null
    # where they never did); non-breaking in a request.
    NULLABILITY_ADDED = "nullability_added"
    # LED-3792: a field LOST nullability (3.0 nullable:true->false).
    # Context-sensitive: breaking in a REQUEST (server now rejects null);
    # non-breaking in a response.
    NULLABILITY_REMOVED = "nullability_removed"
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
    # LED-3792: a format change in the widening (safe) direction
    # (int32->int64, float->double, date->date-time), or a format constraint
    # added on a response / removed on a request. Non-breaking.
    FORMAT_WIDENED = "format_widened"

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
    # LED-3792 always-breaking additions.
    ChangeType.SECURITY_REQUIREMENT_ADDED,
    ChangeType.SECURITY_SCOPE_ADDED,
    ChangeType.MEDIA_TYPE_REMOVED,
    ChangeType.DISCRIMINATOR_CHANGED,
    ChangeType.COMPOSITION_MEMBER_REMOVED,
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

    def __post_init__(self):
        # LED-2294: every Change.details value MUST be a str. The downstream
        # Evidence / Violation pydantic models (schemas/evidence.py) declare
        # Dict[str, str], so a non-str value crashes validation
        # (execution_failure -> misclassification). Non-str producers existed at
        # several sites: PARAM_REQUIRED_CHANGED (bool), the maxLength/minLength
        # constraint changes (int / None), and DEFAULT_CHANGED (any JSON type).
        # Coerce at this single choke point so every producer site — current and
        # future — is covered, rather than hand-patching each call (the gap that
        # left this latent after LED-1600). Booleans render lowercase
        # ("true"/"false") to match the LED-1600 convention; None becomes ""
        # (mirrors LED-1600's `context or ""`).
        if self.details:
            self.details = {
                k: (
                    "" if v is None
                    else ("true" if v else "false") if isinstance(v, bool)
                    else v if isinstance(v, str)
                    else str(v)
                )
                for k, v in self.details.items()
            }

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

        # LED-3792 context-sensitive types.
        if ct == ChangeType.CONSTRAINT_TIGHTENED:
            # Tightening a request bound rejects previously-valid inputs.
            # Response-side tightening is safe for consumers.
            return self.context != "response"

        if ct == ChangeType.ADDITIONAL_PROPERTIES_TIGHTENED:
            # true/absent -> false in a request rejects previously-accepted
            # extra keys; response-side lock-down is non-breaking.
            return self.context != "response"

        if ct == ChangeType.NULLABILITY_ADDED:
            # A response field gaining nullability breaks consumers that never
            # saw null; a request field gaining it is a relaxation.
            return self.context != "request"

        if ct == ChangeType.NULLABILITY_REMOVED:
            # A request field losing null is breaking (server rejects null);
            # a response field losing it is non-breaking.
            return self.context != "response"

        if ct == ChangeType.ENUM_VALUE_ADDED:
            # A new value in a RESPONSE enum breaks clients with exhaustive
            # handling; a new accepted value in a REQUEST enum is additive.
            return self.context == "response"

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

        # LED-3792: top-level `security` is inherited by any operation that
        # does not declare its own. Captured here so _compare_operation can
        # compute the EFFECTIVE per-operation requirement and detect a
        # previously-open operation becoming authenticated (silent 401 outage).
        self._old_top_security = old_spec.get("security")
        self._new_top_security = new_spec.get("security")

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

        # LED-3792: webhooks (3.1) / callbacks are not yet deep-diffed. Emit a
        # fail-visible advisory when they change so the gate never silently
        # reports "no breaking changes" while an event-consumer contract moved.
        # (Full breaking-classification deferred to a follow-up; the existing
        # TestWebhooks suite pins current non-detection behaviour.)
        self._advise_webhooks(old_spec.get("webhooks"), new_spec.get("webhooks"))

        return self.changes

    def _advise_webhooks(self, old_wh: Any, new_wh: Any) -> None:
        """Fail-visible advisory for changed top-level webhooks."""
        if not isinstance(old_wh, dict):
            old_wh = {} if old_wh is None else old_wh
        if not isinstance(new_wh, dict):
            new_wh = {} if new_wh is None else new_wh
        if not isinstance(old_wh, dict) or not isinstance(new_wh, dict):
            return
        if old_wh == new_wh:
            return
        removed = set(old_wh.keys()) - set(new_wh.keys())
        detail = "webhooks changed but not deep-compared (LED-3792 follow-up)"
        if removed:
            detail = f"webhook(s) removed and not deep-compared: {sorted(removed)} (LED-3792 follow-up)"
        self._add_advisory("webhooks_not_compared", "(webhooks)", detail)
    
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

        # LED-3792: path-item-level `parameters` apply to every operation on
        # the path (OpenAPI 3.x). They were previously dropped, so a newly
        # required shared query/header param shipped as a green check. Capture
        # them here and merge into each operation before diffing.
        old_path_params = old_methods.get("parameters", []) if isinstance(old_methods.get("parameters"), list) else []
        new_path_params = new_methods.get("parameters", []) if isinstance(new_methods.get("parameters"), list) else []

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
            self._compare_operation(
                f"{path}:{method.upper()}", old_methods[method], new_methods[method],
                old_path_params=old_path_params, new_path_params=new_path_params,
            )

    def _resolve_param(self, param: Any, root: Dict, path: str) -> Optional[Dict]:
        """Resolve a possibly-$ref'd parameter object to a concrete dict.

        LED-3792: object-level `{$ref: '#/components/parameters/X'}` params were
        skipped entirely (the old code only kept dicts carrying a `name`), so a
        breaking change behind a reusable parameter was invisible. Emits a
        fail-visible advisory when a $ref cannot be resolved.
        """
        if not isinstance(param, dict):
            return None
        if "$ref" in param:
            resolved = self._resolve_local_ref(param["$ref"], root)
            if resolved is None:
                self._advise_unverifiable_ref(path, param["$ref"], root)
                return None
            return resolved if isinstance(resolved, dict) else None
        return param

    def _resolve_body_or_response(self, node: Any, root: Dict, path: str) -> Optional[Dict]:
        """Resolve a possibly-$ref'd requestBody / response object."""
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            resolved = self._resolve_local_ref(node["$ref"], root)
            if resolved is None:
                self._advise_unverifiable_ref(path, node["$ref"], root)
                return None
            return resolved if isinstance(resolved, dict) else None
        return node

    def _compare_operation(self, operation_id: str, old_op: Dict, new_op: Dict,
                           old_path_params: Optional[list] = None,
                           new_path_params: Optional[list] = None):
        """Compare specific operations."""
        if not isinstance(old_op, dict):
            self._add_advisory("malformed_node", operation_id, "old operation is not a dict")
            return
        if not isinstance(new_op, dict):
            self._add_advisory("malformed_node", operation_id, "new operation is not a dict")
            return

        # LED-3792: build the EFFECTIVE parameter list = path-item-level params
        # (shared across methods) overridden by operation-level params, with
        # object-level $refs resolved. Operation params win on (in, name).
        def _merged_params(op, path_params, root):
            merged = {}
            for p in (path_params or []):
                rp = self._resolve_param(p, root, f"{operation_id}:param")
                if isinstance(rp, dict) and "name" in rp:
                    merged[self._param_key(rp)] = rp
            for p in op.get("parameters", []):
                rp = self._resolve_param(p, root, f"{operation_id}:param")
                if isinstance(rp, dict) and "name" in rp:
                    merged[self._param_key(rp)] = rp
            return merged

        old_params = _merged_params(old_op, old_path_params, self._old_root)
        new_params = _merged_params(new_op, new_path_params, self._new_root)
        
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

        # Compare EFFECTIVE operation security (LED-3792). An operation without
        # its own `security` inherits the top-level requirement, so turning on
        # global auth silently required every previously-open operation. Diff
        # the effective requirement, not just the operation-local one.
        old_eff_sec = old_op.get("security") if "security" in old_op else self._old_top_security
        new_eff_sec = new_op.get("security") if "security" in new_op else self._new_top_security
        self._compare_operation_security(operation_id, old_eff_sec, new_eff_sec)

        # Check deprecated flag
        if not old_op.get("deprecated", False) and new_op.get("deprecated", False):
            self.changes.append(Change(
                type=ChangeType.DEPRECATED_ADDED,
                path=operation_id,
                details={"target": "operation"},
                severity="low",
                message=f"Operation marked as deprecated: {operation_id}"
            ))

        # Compare request body (LED-3792: resolve object-level $ref first).
        if "requestBody" in old_op or "requestBody" in new_op:
            old_rb = self._resolve_body_or_response(old_op.get("requestBody"), self._old_root, f"{operation_id}:requestBody")
            new_rb = self._resolve_body_or_response(new_op.get("requestBody"), self._new_root, f"{operation_id}:requestBody")
            self._compare_request_body(operation_id, old_rb, new_rb)
        
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

        # Parameters are request-side inputs.
        self._compare_constraints(f"{operation_id}:{param_name}", old_schema, new_schema, context="request")

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

        # Check enum changes (request context — added values are additive).
        if "enum" in old_schema or "enum" in new_schema:
            self._compare_enums(
                f"{operation_id}:{param_name}",
                old_schema.get("enum", []),
                new_schema.get("enum", []),
                context="request",
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

            # LED-3792: a removed media type (e.g. application/json dropped)
            # breaks every client sending it.
            for content_type in set(old_content.keys()) - set(new_content.keys()):
                self.changes.append(Change(
                    type=ChangeType.MEDIA_TYPE_REMOVED,
                    path=f"{operation_id}:request",
                    details={"media_type": content_type},
                    severity="high",
                    message=f"Request media type '{content_type}' removed from {operation_id}",
                    context="request",
                ))

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
            # LED-3792: resolve object-level $ref responses before diffing.
            old_resp = self._resolve_body_or_response(old_responses[code], self._old_root, f"{operation_id}:{code}")
            new_resp = self._resolve_body_or_response(new_responses[code], self._new_root, f"{operation_id}:{code}")

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

            # LED-3792: a removed response media type breaks consumers of it.
            if str(code).startswith("2"):
                for content_type in set(old_content.keys()) - set(new_content.keys()):
                    self.changes.append(Change(
                        type=ChangeType.MEDIA_TYPE_REMOVED,
                        path=f"{operation_id}:{code}",
                        details={"media_type": content_type},
                        severity="high",
                        message=f"Response media type '{content_type}' removed from {operation_id} ({code})",
                        context="response",
                    ))

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

    def _resolve_ref(self, ref: Optional[str], root: Dict) -> Optional[Dict]:
        """Backward compatibility alias for tests."""
        return self._resolve_local_ref(ref, root)

    def _advise_unverifiable_ref(self, path: str, ref: str, root: Dict) -> None:
        """Record an advisory for a $ref that could not be resolved."""
        if isinstance(ref, str) and ref.startswith("#/"):
            self._add_advisory("unresolved_local_ref", path, f"local $ref '{ref}' could not be resolved")
        else:
            self._add_advisory("external_ref_skipped", path, f"non-local $ref '{ref}' skipped")

    def _resolve_schema(self, schema: Any, root: Dict, path: str = "") -> Any:
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
            seen_key = (old_ref or "", new_ref or "")
            if seen_key in self._ref_stack:
                return  # cycle on current path
                
            resolved_old = self._resolve_schema(old_schema, self._old_root, path) if old_ref else old_schema
            resolved_new = self._resolve_schema(new_schema, self._new_root, path) if new_ref else new_schema

            if (old_ref and resolved_old is old_schema and "$ref" in old_schema) or \
               (new_ref and resolved_new is new_schema and "$ref" in new_schema):
                return # Unresolved ref advisory already added by _resolve_schema

            if old_ref and new_ref and old_ref == new_ref:
                return

            self._ref_stack.add(seen_key)
            try:
                self._compare_schema_deep(path, resolved_old, resolved_new, required_fields, context)
            finally:
                self._ref_stack.discard(seen_key)
            return

        # LED-3792: these run regardless of the schema's type shape so they are
        # never silently skipped (composition schemas often have no `type`).
        self._compare_nullability(path, old_schema, new_schema, context)
        self._compare_additional_properties(path, old_schema, new_schema, context)
        self._compare_composition(path, old_schema, new_schema, context)

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
                    self._compare_constraints(f"{path}.{prop}", old_prop_schema, new_prop_schema, context=context)

                self._compare_schema_deep(f"{path}.{prop}", old_prop_schema, new_prop_schema, old_required if prop in old_required else None, context)

        elif old_type == "array":
            if "items" in old_schema and "items" in new_schema:
                self._compare_schema_deep(f"{path}[]", old_schema["items"], new_schema["items"], None, context)

        if "enum" in old_schema or "enum" in new_schema:
            self._compare_enums(path, old_schema.get("enum", []), new_schema.get("enum", []), context=context)

        if old_type != "object":
            self._compare_constraints(path, old_schema, new_schema, context=context)
    
    def _compare_enums(self, path: str, old_enum: List, new_enum: List, context: Optional[str] = None):
        """Compare enum values.

        LED-3792: enum ADDITION is direction-aware. A new value in a RESPONSE
        enum breaks clients with exhaustive/closed handling (breaking); a new
        accepted value in a REQUEST enum is additive. Removal stays breaking in
        both directions (a value clients relied on / sent is gone).
        """
        old_set = set(old_enum) if isinstance(old_enum, list) else set()
        new_set = set(new_enum) if isinstance(new_enum, list) else set()
        for value in old_set - new_set:
            self.changes.append(Change(type=ChangeType.ENUM_VALUE_REMOVED, path=path, details={"value": value}, severity="high", message=f"Enum value '{value}' removed at {path}", context=context))
        added_breaking = context == "response"
        for value in new_set - old_set:
            self.changes.append(Change(
                type=ChangeType.ENUM_VALUE_ADDED,
                path=path,
                details={"value": value},
                severity="high" if added_breaking else "low",
                message=f"Enum value '{value}' added at {path}" + (" (response enum; breaks closed/exhaustive consumers)" if added_breaking else ""),
                context=context,
            ))
    
    def _compare_schemas(self, old_schemas: Dict, new_schemas: Dict, path_prefix: str = "#/components/schemas"):
        """Compare a named-schema map."""
        if not isinstance(old_schemas, dict): old_schemas = {}
        if not isinstance(new_schemas, dict): new_schemas = {}
        for schema_name in set(old_schemas.keys()) - set(new_schemas.keys()):
            self.changes.append(Change(type=ChangeType.FIELD_REMOVED, path=f"{path_prefix}/{schema_name}", details={"schema": schema_name}, severity="medium", message=f"Schema '{schema_name}' removed"))
        for schema_name in set(old_schemas.keys()) & set(new_schemas.keys()):
            self._compare_schema_deep(f"{path_prefix}/{schema_name}", old_schemas[schema_name], new_schemas[schema_name])
    
    def _compare_constraints(self, path: str, old_schema: Dict, new_schema: Dict, context: Optional[str] = None):
        """Compare schema constraints."""
        if not isinstance(old_schema, dict):
            old_schema = {}
        if not isinstance(new_schema, dict):
            new_schema = {}
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

        # LED-3792: numeric / pattern / multipleOf tightening. Emitted as the
        # context-sensitive CONSTRAINT_TIGHTENED (breaking in a request; safe in
        # a response). Mirrors core/json_schema_diff.py's numeric-bounds logic.
        self._compare_numeric_bound(path, old_schema, new_schema, "minimum", "increase", context)
        self._compare_numeric_bound(path, old_schema, new_schema, "maximum", "decrease", context)
        self._compare_exclusive_bound(path, old_schema, new_schema, "exclusiveMinimum", "increase", context)
        self._compare_exclusive_bound(path, old_schema, new_schema, "exclusiveMaximum", "decrease", context)
        self._compare_multiple_of(path, old_schema, new_schema, context)
        self._compare_pattern(path, old_schema, new_schema, context)
        self._compare_format(path, old_schema, new_schema, context)

    def _tightened(self, path: str, constraint: str, old_val, new_val, context: Optional[str], message: str) -> None:
        self.changes.append(Change(
            type=ChangeType.CONSTRAINT_TIGHTENED,
            path=path,
            details={"constraint": constraint, "old_value": old_val, "new_value": new_val, "context": context or ""},
            severity="high" if context != "response" else "low",
            message=message,
            context=context,
        ))

    def _compare_numeric_bound(self, path, old_schema, new_schema, key, tighten_dir, context):
        old_val = old_schema.get(key)
        new_val = new_schema.get(key)
        try:
            if old_val is not None and new_val is not None:
                if tighten_dir == "increase" and float(new_val) > float(old_val):
                    self._tightened(path, key, old_val, new_val, context, f"{key} raised from {old_val} to {new_val} at {path} (tighter input bound)")
                elif tighten_dir == "decrease" and float(new_val) < float(old_val):
                    self._tightened(path, key, old_val, new_val, context, f"{key} lowered from {old_val} to {new_val} at {path} (tighter input bound)")
            elif old_val is None and new_val is not None:
                # A bound added where none existed narrows the accepted domain.
                float(new_val)
                self._tightened(path, key, None, new_val, context, f"{key} constraint ({new_val}) added at {path} where none existed")
        except (TypeError, ValueError):
            return

    def _compare_exclusive_bound(self, path, old_schema, new_schema, key, tighten_dir, context):
        old_val = old_schema.get(key)
        new_val = new_schema.get(key)
        # 3.1 numeric form only (3.0 boolean form is paired with minimum/maximum,
        # already covered). Ignore boolean values to avoid false positives on
        # the 3.0->3.1 representation change.
        if isinstance(old_val, bool) or isinstance(new_val, bool):
            return
        try:
            if old_val is not None and new_val is not None:
                if tighten_dir == "increase" and float(new_val) > float(old_val):
                    self._tightened(path, key, old_val, new_val, context, f"{key} raised from {old_val} to {new_val} at {path}")
                elif tighten_dir == "decrease" and float(new_val) < float(old_val):
                    self._tightened(path, key, old_val, new_val, context, f"{key} lowered from {old_val} to {new_val} at {path}")
            elif old_val is None and new_val is not None:
                float(new_val)
                self._tightened(path, key, None, new_val, context, f"{key} ({new_val}) added at {path} where none existed")
        except (TypeError, ValueError):
            return

    def _compare_multiple_of(self, path, old_schema, new_schema, context):
        old_val = old_schema.get("multipleOf")
        new_val = new_schema.get("multipleOf")
        if new_val is None:
            return
        try:
            new_f = float(new_val)
        except (TypeError, ValueError):
            return
        if old_val is None:
            self._tightened(path, "multipleOf", None, new_val, context, f"multipleOf ({new_val}) added at {path} (rejects previously-valid values)")
            return
        try:
            old_f = float(old_val)
        except (TypeError, ValueError):
            return
        if new_f == old_f:
            return
        # A new divisor that is not a divisor of the old one rejects values that
        # were valid before. Conservatively treat any change to a larger divisor
        # (or a non-multiple divisor) as tightening.
        if old_f == 0 or (new_f % old_f != 0) or new_f > old_f:
            self._tightened(path, "multipleOf", old_val, new_val, context, f"multipleOf changed from {old_val} to {new_val} at {path}")

    def _compare_pattern(self, path, old_schema, new_schema, context):
        old_p = old_schema.get("pattern")
        new_p = new_schema.get("pattern")
        if old_p == new_p:
            return
        # Regex subset relationships are undecidable in general; any new or
        # changed pattern on an existing field is conservatively tightening.
        # Removing a pattern loosens (non-breaking) — not flagged.
        if new_p and not old_p:
            self._tightened(path, "pattern", None, new_p, context, f"pattern '{new_p}' added at {path} (rejects previously-valid values)")
        elif old_p and new_p and old_p != new_p:
            self._tightened(path, "pattern", old_p, new_p, context, f"pattern changed from '{old_p}' to '{new_p}' at {path}")

    # LED-3792: format widening lattice. old->new is WIDENING when new is the
    # wider (superset) format. Narrowing / unrelated changes are breaking.
    _FORMAT_WIDENS = {
        ("int32", "int64"),
        ("float", "double"),
        ("date", "date-time"),
    }

    def _compare_format(self, path: str, old_schema: Dict, new_schema: Dict, context: Optional[str]) -> None:
        """Detect format changes. FORMAT_CHANGED (always-breaking) is emitted
        only in the breaking direction; safe widenings become FORMAT_WIDENED."""
        old_f = old_schema.get("format")
        new_f = new_schema.get("format")
        if old_f == new_f:
            return

        def _breaking(msg):
            self.changes.append(Change(type=ChangeType.FORMAT_CHANGED, path=path, details={"old_format": old_f, "new_format": new_f, "context": context or ""}, severity="high", message=msg, context=context))

        def _widened(msg):
            self.changes.append(Change(type=ChangeType.FORMAT_WIDENED, path=path, details={"old_format": old_f, "new_format": new_f, "context": context or ""}, severity="low", message=msg, context=context))

        if old_f and new_f:
            if (old_f, new_f) in self._FORMAT_WIDENS:
                _widened(f"format widened from {old_f} to {new_f} at {path}")
            elif (new_f, old_f) in self._FORMAT_WIDENS:
                _breaking(f"format narrowed from {old_f} to {new_f} at {path} (possible data truncation / rejection)")
            else:
                _breaking(f"format changed from {old_f} to {new_f} at {path}")
        elif new_f and not old_f:
            # Adding a format constraint to a REQUEST field rejects previously
            # accepted values (breaking); on a response it just documents (safe).
            if context == "request":
                _breaking(f"format '{new_f}' added to request field at {path} (rejects previously-valid values)")
            else:
                _widened(f"format '{new_f}' added at {path}")
        elif old_f and not new_f:
            # Removing a format from a RESPONSE field weakens the guarantee the
            # consumer relied on (breaking); on a request it loosens (safe).
            if context == "response":
                _breaking(f"format '{old_f}' removed from response field at {path} (weakens value guarantee)")
            else:
                _widened(f"format '{old_f}' removed at {path}")

    @staticmethod
    def _is_nullable(schema: Dict) -> bool:
        """Normalize nullability across 3.0 (`nullable: true`) and 3.1
        (`type` array containing 'null')."""
        if not isinstance(schema, dict):
            return False
        if schema.get("nullable") is True:
            return True
        t = schema.get("type")
        if isinstance(t, list) and "null" in t:
            return True
        return False

    def _compare_nullability(self, path: str, old_schema: Dict, new_schema: Dict, context: Optional[str]) -> None:
        """Detect a nullability flip on a field whose base type is otherwise
        unchanged. The 3.1 type-array case where the base type also changes is
        already covered by the raw TYPE_CHANGED path; here we catch the pure
        `nullable: false <-> true` (3.0) flip that leaves `type` untouched."""
        old_null = self._is_nullable(old_schema)
        new_null = self._is_nullable(new_schema)
        if old_null == new_null:
            return
        # Only act when the raw `type` value is unchanged, so we never
        # double-report alongside TYPE_CHANGED and never false-positive on a
        # type-array reorder / migration (which the type path already handles).
        if old_schema.get("type") != new_schema.get("type"):
            return
        if not old_null and new_null:
            self.changes.append(Change(
                type=ChangeType.NULLABILITY_ADDED,
                path=path,
                details={"context": context or ""},
                severity="high" if context != "request" else "low",
                message=f"Field at {path} became nullable" + (" (response; consumers may now receive null)" if context != "request" else " (request; non-breaking)"),
                context=context,
            ))
        elif old_null and not new_null:
            self.changes.append(Change(
                type=ChangeType.NULLABILITY_REMOVED,
                path=path,
                details={"context": context or ""},
                severity="high" if context != "response" else "low",
                message=f"Field at {path} is no longer nullable" + (" (request; server now rejects null)" if context != "response" else " (response; non-breaking)"),
                context=context,
            ))

    def _compare_additional_properties(self, path: str, old_schema: Dict, new_schema: Dict, context: Optional[str]) -> None:
        """Port of json_schema_diff._compare_additional_properties (LED-3792).

        Request true/absent -> false rejects previously-accepted extra keys
        (breaking). A schema-valued additionalProperties (typed map /
        Dict[str, Model]) is recursed into so narrowing inside it is visible."""
        old_ap = old_schema.get("additionalProperties")
        new_ap = new_schema.get("additionalProperties")
        if old_ap is None and new_ap is None:
            return
        old_allows = True if old_ap is None else (old_ap if isinstance(old_ap, bool) else True)
        new_allows = True if new_ap is None else (new_ap if isinstance(new_ap, bool) else True)
        if old_allows and not new_allows:
            self.changes.append(Change(
                type=ChangeType.ADDITIONAL_PROPERTIES_TIGHTENED,
                path=path,
                details={"context": context or ""},
                severity="high" if context != "response" else "low",
                message=f"additionalProperties tightened to false at {path}" + (" (request; extra keys now rejected)" if context != "response" else " (response; non-breaking)"),
                context=context,
            ))
        # Typed-map value schema — recurse so narrowing inside it is not silent.
        if isinstance(old_ap, dict) and isinstance(new_ap, dict):
            self._compare_schema_deep(f"{path}.additionalProperties", old_ap, new_ap, None, context)

    def _compare_composition(self, path: str, old_schema: Dict, new_schema: Dict, context: Optional[str]) -> None:
        """Handle allOf / oneOf / anyOf / discriminator (LED-3792).

        Removals in the breaking direction emit COMPOSITION_MEMBER_REMOVED
        (always-breaking); the non-breaking direction and anything we cannot
        confidently align emit a fail-visible advisory so composition is never
        silently skipped. Positionally-aligned members are recursed into."""
        for keyword in ("allOf", "oneOf", "anyOf"):
            old_members = old_schema.get(keyword)
            new_members = new_schema.get(keyword)
            if old_members is None and new_members is None:
                continue
            old_members = old_members if isinstance(old_members, list) else []
            new_members = new_members if isinstance(new_members, list) else []

            # allOf members are AND-combined: dropping one removes guaranteed
            # fields (breaking in a RESPONSE) / relaxes constraints (safe in a
            # REQUEST). oneOf/anyOf are OR variants: dropping one narrows the
            # accepted set (breaking in a REQUEST) / returned set (safe in a
            # RESPONSE).
            removed_count = len(old_members) - len(new_members)
            if removed_count > 0:
                if keyword == "allOf":
                    breaking = context != "request"
                else:
                    breaking = context != "response"
                if breaking:
                    self.changes.append(Change(
                        type=ChangeType.COMPOSITION_MEMBER_REMOVED,
                        path=f"{path}.{keyword}",
                        details={"keyword": keyword, "removed": removed_count, "context": context or ""},
                        severity="high",
                        message=f"{removed_count} {keyword} member(s) removed at {path} (breaking for {'response consumers' if keyword == 'allOf' else 'request clients'})",
                        context=context,
                    ))
                else:
                    self._add_advisory("composition_change", f"{path}.{keyword}", f"{removed_count} {keyword} member(s) removed (non-breaking direction; not deep-diffed)")
            elif len(new_members) > len(old_members):
                self._add_advisory("composition_change", f"{path}.{keyword}", f"{keyword} member(s) added; not deep-diffed (LED-3792 follow-up)")

            # Recurse into positionally-aligned members for deeper field diffs.
            for i in range(min(len(old_members), len(new_members))):
                if isinstance(old_members[i], dict) and isinstance(new_members[i], dict):
                    self._compare_schema_deep(f"{path}.{keyword}[{i}]", old_members[i], new_members[i], None, context)

        # Discriminator: propertyName change or mapping-key removal breaks
        # polymorphic (de)serialization.
        old_disc = old_schema.get("discriminator")
        new_disc = new_schema.get("discriminator")
        if isinstance(old_disc, dict) and isinstance(new_disc, dict):
            if old_disc.get("propertyName") != new_disc.get("propertyName"):
                self.changes.append(Change(
                    type=ChangeType.DISCRIMINATOR_CHANGED,
                    path=f"{path}.discriminator",
                    details={"old": old_disc.get("propertyName"), "new": new_disc.get("propertyName")},
                    severity="high",
                    message=f"discriminator.propertyName changed at {path}",
                    context=context,
                ))
            old_map = old_disc.get("mapping", {}) if isinstance(old_disc.get("mapping"), dict) else {}
            new_map = new_disc.get("mapping", {}) if isinstance(new_disc.get("mapping"), dict) else {}
            removed_keys = set(old_map.keys()) - set(new_map.keys())
            if removed_keys:
                self.changes.append(Change(
                    type=ChangeType.DISCRIMINATOR_CHANGED,
                    path=f"{path}.discriminator",
                    details={"removed_mapping_keys": ", ".join(sorted(removed_keys))},
                    severity="high",
                    message=f"discriminator mapping key(s) removed at {path}: {sorted(removed_keys)}",
                    context=context,
                ))
        elif (old_disc is None) != (new_disc is None):
            self._add_advisory("composition_change", f"{path}.discriminator", "discriminator added or removed; not deep-diffed (LED-3792 follow-up)")

    @staticmethod
    def _security_is_open(sec_list: Optional[list]) -> bool:
        """A security requirement is 'open' (anonymous access allowed) when it
        is absent, an empty list, or contains an empty `{}` requirement object
        (the OpenAPI idiom for 'no auth needed')."""
        if not sec_list:
            return True
        if not isinstance(sec_list, list):
            return True
        return any(item == {} for item in sec_list if isinstance(item, dict))

    def _compare_operation_security(self, operation_id: str, old_security: Optional[list], new_security: Optional[list]):
        """Compare operation-level (effective) security requirements."""
        def _security_map(sec_list):
            result = {}
            if isinstance(sec_list, list):
                for item in sec_list:
                    if isinstance(item, dict):
                        for scheme, scopes in item.items():
                            result[scheme] = set(scopes) if isinstance(scopes, list) else set()
            return result

        old_open = self._security_is_open(old_security)
        new_open = self._security_is_open(new_security)

        # LED-3792: a previously-anonymous operation that now requires auth is a
        # 100% outage for existing unauthenticated clients — BREAKING. This is
        # the top-level-security-added case (inherited effective requirement)
        # and the operation-level open->auth case.
        if old_open and not new_open:
            schemes = sorted(_security_map(new_security).keys())
            self.changes.append(Change(
                type=ChangeType.SECURITY_REQUIREMENT_ADDED,
                path=operation_id,
                details={"schemes": ", ".join(schemes)},
                severity="high",
                message=f"Authentication now required on previously-open operation {operation_id} (scheme(s): {', '.join(schemes)})",
            ))
            return

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
            # LED-3792: an ADDED required scope breaks tokens that lack it.
            added_scopes = new_map[scheme] - old_map[scheme]
            for scope in added_scopes:
                self.changes.append(Change(type=ChangeType.SECURITY_SCOPE_ADDED, path=operation_id, details={"scheme": scheme, "scope": scope}, severity="high", message=f"OAuth scope '{scope}' now required by scheme '{scheme}' at {operation_id}"))

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
