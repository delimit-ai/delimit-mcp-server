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
    # NOTE (LOUD): this is the ONE new ChangeType added for LED-1600. Prior
    # canon pinned the enum at 27; it is now 28. No existing value was renamed
    # or removed (the four-corner add/remove/required-add cases were already
    # covered); required->optional simply had NO representation before, which
    # is exactly the silent-leak this LED closes.
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
    # LED-1600: request/response context. The breaking-ness of a FIELD change
    # flips with direction (see is_breaking). Values: "request", "response",
    # or None when the engine cannot determine direction (e.g. a bare
    # component-schema comparison, or a hand-constructed Change). When None,
    # is_breaking falls back to the conservative, direction-agnostic verdict
    # that the engine has always produced — so existing callers and stored
    # Change objects are unaffected. This field is ADDITIVE (defaulted), so the
    # public construction signature and the delimit_diff return shape are
    # unchanged.
    context: Optional[str] = None

    @property
    def is_breaking(self) -> bool:
        ct = self.type

        if ct in _ALWAYS_BREAKING:
            return True

        # ── LED-1600: context-aware classification ──────────────────────
        # REQUIRED_FIELD_ADDED:
        #   REQUEST  -> breaking (clients must now send it)
        #   RESPONSE -> non-breaking (server returns MORE; consumers ignore it)
        #   unknown  -> breaking (conservative: never silently downgrade)
        if ct == ChangeType.REQUIRED_FIELD_ADDED:
            return self.context != "response"

        # FIELD_REMOVED:
        #   RESPONSE -> breaking (consumers lose the field)
        #   REQUEST  -> non-breaking (server stops requiring/accepting it)
        #   unknown  -> breaking (conservative; covers component schemas, which
        #               may back a response, and matches pre-LED-1600 behavior)
        if ct == ChangeType.FIELD_REMOVED:
            return self.context != "request"

        # FIELD_REQUIREMENT_RELAXED (required -> optional):
        #   RESPONSE -> breaking (consumers can no longer rely on its presence)
        #   REQUEST  -> non-breaking (server demands less)
        #   unknown  -> breaking (conservative)
        if ct == ChangeType.FIELD_REQUIREMENT_RELAXED:
            return self.context != "request"

        return False

class OpenAPIDiffEngine:
    """Advanced diff engine for OpenAPI specifications."""
    
    def __init__(self):
        self.changes: List[Change] = []
        # LED-1588: fail-open skips (unresolvable refs, malformed nodes) are
        # surfaced here as a structured side-channel so a clean `changes` list
        # is not mistaken for "proven safe". Advisories are NOT Change objects
        # and are never appended to self.changes.
        self.advisories: List[Dict[str, Any]] = []
        self._old_spec: Dict = {}
        self._new_spec: Dict = {}

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
        # Store full specs so $ref pointers (#/components/schemas/*) can be
        # resolved during deep schema comparison.
        self._old_spec = old_spec
        self._new_spec = new_spec

        # Compare paths
        self._compare_paths(old_spec.get("paths", {}), new_spec.get("paths", {}))
        
        # Compare components/schemas (OpenAPI 3.x)
        _old_components = old_spec.get("components", {})
        _new_components = new_spec.get("components", {})
        self._compare_schemas(
            _old_components.get("schemas", {}) if isinstance(_old_components, dict) else {},
            _new_components.get("schemas", {}) if isinstance(_new_components, dict) else {},
        )

        # Compare top-level definitions (Swagger 2.0). v2 stores schemas here,
        # not under components/schemas, so without this a breaking change
        # inside a v2 definition — and behind a #/definitions/X ref — is missed
        # entirely (the ref's same-target rule defers to this comparison).
        self._compare_schemas(
            old_spec.get("definitions", {}),
            new_spec.get("definitions", {}),
            path_prefix="#/definitions",
        )

        # Honesty advisory (LED-1588 channel): the path/operation comparison is
        # OpenAPI-3.x-shaped (responses[].content, requestBody). A Swagger 2.0
        # spec's definitions are now compared, but v2-style inline schemas
        # (responses[].schema, in:body parameters) are not yet deep-compared —
        # flag it so a clean diff isn't mistaken for full v2 coverage.
        if "swagger" in old_spec or "swagger" in new_spec:
            self._add_advisory(
                "partial_spec_support", "(spec)",
                "Swagger 2.0 detected: top-level definitions are compared, but "
                "v2-style inline path/response/body schemas (responses[].schema, "
                "in:body parameters) are not yet deep-compared",
            )

        # Compare security schemes
        self._compare_security(
            old_spec.get("components", {}).get("securitySchemes", {}),
            new_spec.get("components", {}).get("securitySchemes", {})
        )
        
        return self.changes
    
    def _compare_paths(self, old_paths: Dict, new_paths: Dict):
        """Compare API paths/endpoints."""
        # Defend against malformed specs where `paths` is a list rather
        # than the spec-required dict (Map[string, PathItem]). Same family
        # as the Kong-class properties-as-list fix; treat as empty rather
        # than crashing on `.keys()`.
        if not isinstance(old_paths, dict):
            self._add_advisory(
                "malformed_node", "paths",
                f"old spec `paths` is not a dict (got {type(old_paths).__name__}); skipped",
            )
            old_paths = {}
        if not isinstance(new_paths, dict):
            self._add_advisory(
                "malformed_node", "paths",
                f"new spec `paths` is not a dict (got {type(new_paths).__name__}); skipped",
            )
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
        
        # Check modified endpoints
        for path in old_set & new_set:
            self._compare_methods(path, old_paths[path], new_paths[path])
    
    # LED-290: include "trace" (OpenAPI 3.0+) and "query" (OpenAPI 3.2.0
    # adds the QUERY HTTP method for safe, idempotent requests with bodies).
    HTTP_METHODS = ("get", "post", "put", "delete", "patch", "head", "options", "trace", "query")

    def _compare_methods(self, path: str, old_methods: Dict, new_methods: Dict):
        """Compare HTTP methods for an endpoint."""
        # Same defensive pattern as _compare_paths — methods at a path
        # MUST be a dict per spec, but malformed inputs see real-world.
        if not isinstance(old_methods, dict):
            self._add_advisory(
                "malformed_node", path,
                f"old path-item methods at {path} is not a dict "
                f"(got {type(old_methods).__name__}); skipped",
            )
            old_methods = {}
        if not isinstance(new_methods, dict):
            self._add_advisory(
                "malformed_node", path,
                f"new path-item methods at {path} is not a dict "
                f"(got {type(new_methods).__name__}); skipped",
            )
            new_methods = {}
        old_set = set(m for m in old_methods.keys() if m in self.HTTP_METHODS)
        new_set = set(m for m in new_methods.keys() if m in self.HTTP_METHODS)
        
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
        
        # Compare parameters — skip unresolved $ref entries (common in Swagger 2.0)
        # which lack inline name/in fields and would crash downstream accessors.
        old_params = {self._param_key(p): p for p in old_op.get("parameters", []) if "name" in p}
        new_params = {self._param_key(p): p for p in new_op.get("parameters", []) if "name" in p}
        
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
        
        # Check added optional parameters (non-breaking)
        for param_key in set(new_params.keys()) - set(old_params.keys()):
            param = new_params[param_key]
            if not param.get("required", False):
                self.changes.append(Change(
                    type=ChangeType.OPTIONAL_PARAM_ADDED,
                    path=operation_id,
                    details={"parameter": param["name"], "in": param["in"]},
                    severity="low",
                    message=f"Optional parameter added: {param['name']} to {operation_id}"
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
        old_deprecated = old_op.get("deprecated", False)
        new_deprecated = new_op.get("deprecated", False)
        if not old_deprecated and new_deprecated:
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
        param_name = old_param.get("name", old_param.get("$ref", "unknown"))

        old_ref = old_schema.get("$ref") if isinstance(old_schema, dict) else None
        new_ref = new_schema.get("$ref") if isinstance(new_schema, dict) else None

        if old_ref is not None and new_ref is not None and old_ref == new_ref:
            # Both sides reference the same component schema; it is deep-compared
            # once in _compare_schemas. Don't re-emit its internal changes here.
            return

        # Resolve local $refs so a ref-vs-inline (or differing-ref) parameter
        # schema change is detected. Unresolvable refs fall through unchanged
        # and simply won't match — never crash, never fabricate.
        if old_ref is not None:
            resolved = self._resolve_schema(old_schema, self._old_spec)
            if isinstance(resolved, dict) and "$ref" not in resolved:
                old_schema = resolved
            else:
                self._advise_unverifiable_ref(operation_id, old_ref, self._old_spec)
        if new_ref is not None:
            resolved = self._resolve_schema(new_schema, self._new_spec)
            if isinstance(resolved, dict) and "$ref" not in resolved:
                new_schema = resolved
            else:
                self._advise_unverifiable_ref(operation_id, new_ref, self._new_spec)

        # Check type changes — emit both PARAM_TYPE_CHANGED (specific) and TYPE_CHANGED (legacy)
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

        # Check required changed (optional -> required)
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

        # Check constraint changes
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
            raw_old_content = old_body.get("content", {})
            raw_new_content = new_body.get("content", {})
            if not isinstance(raw_old_content, dict):
                self._add_advisory(
                    "malformed_node", f"{operation_id}:request",
                    f"old requestBody `content` at {operation_id} is not a dict "
                    f"(got {type(raw_old_content).__name__}); skipped",
                )
            if not isinstance(raw_new_content, dict):
                self._add_advisory(
                    "malformed_node", f"{operation_id}:request",
                    f"new requestBody `content` at {operation_id} is not a dict "
                    f"(got {type(raw_new_content).__name__}); skipped",
                )
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
        # Defend against malformed specs where `responses` is a list.
        if not isinstance(old_responses, dict):
            self._add_advisory(
                "malformed_node", operation_id,
                f"old `responses` at {operation_id} is not a dict "
                f"(got {type(old_responses).__name__}); skipped",
            )
            old_responses = {}
        if not isinstance(new_responses, dict):
            self._add_advisory(
                "malformed_node", operation_id,
                f"new `responses` at {operation_id} is not a dict "
                f"(got {type(new_responses).__name__}); skipped",
            )
            new_responses = {}
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

            # A response item must itself be a dict (Response Object).
            if not isinstance(old_resp, dict):
                self._add_advisory(
                    "malformed_node", f"{operation_id}:{code}",
                    f"old response item {code} at {operation_id} is not a dict "
                    f"(got {type(old_resp).__name__}); skipped",
                )
                old_resp = {}
            if not isinstance(new_resp, dict):
                self._add_advisory(
                    "malformed_node", f"{operation_id}:{code}",
                    f"new response item {code} at {operation_id} is not a dict "
                    f"(got {type(new_resp).__name__}); skipped",
                )
                new_resp = {}

            if "content" in old_resp or "content" in new_resp:
                raw_old_content = old_resp.get("content", {})
                raw_new_content = new_resp.get("content", {})
                if not isinstance(raw_old_content, dict):
                    self._add_advisory(
                        "malformed_node", f"{operation_id}:{code}",
                        f"old response {code} `content` at {operation_id} is not a dict "
                        f"(got {type(raw_old_content).__name__}); skipped",
                    )
                if not isinstance(raw_new_content, dict):
                    self._add_advisory(
                        "malformed_node", f"{operation_id}:{code}",
                        f"new response {code} `content` at {operation_id} is not a dict "
                        f"(got {type(raw_new_content).__name__}); skipped",
                    )
                old_content = raw_old_content if isinstance(raw_old_content, dict) else {}
                new_content = raw_new_content if isinstance(raw_new_content, dict) else {}

                for content_type in old_content.keys() & new_content.keys():
                    self._compare_schema_deep(
                        f"{operation_id}:{code}",
                        old_content[content_type].get("schema", {}),
                        new_content[content_type].get("schema", {}),
                        context="response",
                    )
    
    @staticmethod
    def _unescape_json_pointer_token(token: str) -> str:
        """Decode a JSON Pointer reference token (~1 -> /, ~0 -> ~)."""
        return token.replace("~1", "/").replace("~0", "~")

    def _resolve_ref(self, ref: str, spec: Dict) -> Optional[Dict]:
        """Resolve a single local JSON-pointer $ref against ``spec``.

        Returns the referenced object (one hop, not chain-followed), or None
        when the ref is non-local, malformed, or its target does not exist.
        Never raises.
        """
        if not isinstance(ref, str) or not ref.startswith("#/"):
            # External URL, relative-file, or non-pointer ref: unresolvable.
            return None
        if not isinstance(spec, dict):
            return None
        node: Any = spec
        for raw_token in ref[2:].split("/"):
            token = self._unescape_json_pointer_token(raw_token)
            if isinstance(node, dict) and token in node:
                node = node[token]
            else:
                return None
        return node if isinstance(node, dict) else None

    def _advise_unverifiable_ref(self, path: str, ref: str, spec: Dict) -> None:
        """Record an advisory for a $ref that could not be resolved/compared.

        Classifies into `external_ref_skipped` (non-local URL/relative file —
        an expected limitation) vs `unresolved_local_ref` (a `#/` pointer whose
        target is missing or whose chain is circular — a likely spec bug).
        """
        if isinstance(ref, str) and ref.startswith("#/"):
            self._add_advisory(
                "unresolved_local_ref", path,
                f"local $ref '{ref}' could not be resolved (target missing or "
                f"circular); comparison skipped",
            )
        else:
            self._add_advisory(
                "external_ref_skipped", path,
                f"non-local $ref '{ref}' cannot be resolved (external URL or "
                f"relative file); comparison skipped",
            )

    def _resolve_schema(self, schema: Any, spec: Dict, _seen: Optional[Set[str]] = None) -> Any:
        """Follow a (possibly chained) $ref to its concrete schema.

        - Follows ref -> ref -> ... -> concrete schema.
        - On an unresolvable ref, returns the schema as-is (the ref dict).
        - On a circular chain, returns the original schema dict (the ref),
          so callers can detect the cycle without hanging.
        """
        if not isinstance(schema, dict) or "$ref" not in schema:
            return schema
        if _seen is None:
            _seen = set()
        ref = schema["$ref"]
        if ref in _seen:
            # Circular ref chain — return as-is rather than looping forever.
            return schema
        _seen.add(ref)
        target = self._resolve_ref(ref, spec)
        if target is None:
            # Unresolvable — return the original ref dict unchanged.
            return schema
        if isinstance(target, dict) and "$ref" in target:
            return self._resolve_schema(target, spec, _seen)
        return target

    def _compare_schema_deep(
        self,
        path: str,
        old_schema: Dict,
        new_schema: Dict,
        required_fields: Optional[Set[str]] = None,
        _visited: Optional[Set[Tuple[Optional[str], Optional[str]]]] = None,
        context: Optional[str] = None,
    ):
        """Deep comparison of schemas including nested objects.

        ``context`` is the request/response direction ("request" / "response"
        / None) propagated from the operation entry point. It is threaded
        through nested objects and arrays so a field change keeps its
        direction, which LED-1600 uses to classify breaking-ness correctly
        (a removed RESPONSE field is breaking; a removed REQUEST field is not).
        Bare component-schema comparisons pass None — the conservative path.
        """
        # Guard against None schemas
        if old_schema is None:
            old_schema = {}
        if new_schema is None:
            new_schema = {}

        if _visited is None:
            _visited = set()

        # Handle references. Resolve local #/ pointers and recurse into the
        # concrete schemas so breaking changes behind a $ref are detected.
        old_ref = old_schema.get("$ref") if isinstance(old_schema, dict) else None
        new_ref = new_schema.get("$ref") if isinstance(new_schema, dict) else None

        if old_ref is not None or new_ref is not None:
            # Cycle guard: if we've already descended through this exact
            # (old_ref, new_ref) pair, stop to avoid infinite recursion.
            visit_key = (old_ref, new_ref)
            if visit_key in _visited:
                return
            _visited = _visited | {visit_key}

            # Both sides reference the SAME target. If that target resolves to
            # a concrete schema it is already deep-compared once in
            # _compare_schemas, so don't re-emit its internal changes here (no
            # double-counting) and don't advise. But if the shared target is
            # itself unverifiable (missing local target, external, or circular),
            # nothing compares it elsewhere — surface that as an advisory once.
            if old_ref is not None and new_ref is not None and old_ref == new_ref:
                resolved_same = self._resolve_schema(old_schema, self._old_spec)
                if isinstance(resolved_same, dict) and "$ref" in resolved_same:
                    self._advise_unverifiable_ref(path, old_ref, self._old_spec)
                return

            resolved_old = self._resolve_schema(old_schema, self._old_spec) if old_ref is not None else old_schema
            resolved_new = self._resolve_schema(new_schema, self._new_spec) if new_ref is not None else new_schema

            # If either side is still a ref (unresolvable or circular), skip
            # safely rather than fabricating a change or crashing. Surface the
            # skip per unverifiable side (LED-1588) so a clean diff isn't
            # mistaken for "proven safe".
            old_unresolved = isinstance(resolved_old, dict) and "$ref" in resolved_old
            new_unresolved = isinstance(resolved_new, dict) and "$ref" in resolved_new
            if old_unresolved or new_unresolved:
                if old_unresolved and old_ref is not None:
                    self._advise_unverifiable_ref(path, old_ref, self._old_spec)
                if new_unresolved and new_ref is not None:
                    self._advise_unverifiable_ref(path, new_ref, self._new_spec)
                return

            self._compare_schema_deep(
                path, resolved_old, resolved_new, required_fields, _visited, context
            )
            return

        # Compare types
        old_type = old_schema.get("type")
        new_type = new_schema.get("type")

        if old_type != new_type and old_type is not None:
            # Determine if this is a response context for RESPONSE_TYPE_CHANGED
            is_response = bool(
                ":" in path and any(
                    code in path for code in
                    ["200", "201", "202", "204", "301", "400", "401", "403", "404", "500"]
                )
            )
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

        # Compare object properties.
        #
        # LED-1597: a schema may carry `properties`/`required` WITHOUT an
        # explicit `type: "object"`. This is valid JSON Schema and is
        # explicitly common in OpenAPI 3.1 (where `type` is optional), and it
        # is exactly the shape the real EU TED v3 spec uses for the
        # NoticeResponse component reached via a response $ref. Gating object
        # comparison solely on `old_type == "object"` silently skipped all
        # field-level diffing for such schemas, so removals/retypes/required
        # additions behind a $ref returned 0 changes. Treat a schema as an
        # object when it declares object-shaped keys, regardless of `type`.
        def _is_object_shaped(s: Any) -> bool:
            return isinstance(s, dict) and (
                "properties" in s or "required" in s
            )

        is_object = (
            old_type == "object"
            or new_type == "object"
            or _is_object_shaped(old_schema)
            or _is_object_shaped(new_schema)
        )
        if is_object:
            raw_old_props = old_schema.get("properties", {})
            raw_new_props = new_schema.get("properties", {})
            # Defend against malformed specs where `properties` is a list of
            # field-objects rather than the spec-required dict (Kong-class:
            # OpenAPI requires `properties: Map[string, Schema]`, but some
            # generators emit `properties: [{name: "a", type: "string"}, ...]`).
            # Treat as empty rather than crashing on `.keys()`.
            old_props = raw_old_props if isinstance(raw_old_props, dict) else {}
            new_props = raw_new_props if isinstance(raw_new_props, dict) else {}
            # Defend against malformed specs where `required` is a bool (legal in
            # parameter objects but not in object schemas — some real-world specs
            # leak the parameter-style boolean into nested schemas).
            raw_old_required = old_schema.get("required", [])
            raw_new_required = new_schema.get("required", [])
            old_required = set(raw_old_required) if isinstance(raw_old_required, list) else set()
            new_required = set(raw_new_required) if isinstance(raw_new_required, list) else set()

            # Check removed fields.
            #
            # LED-1597: removing a property from a schema is breaking for any
            # consumer that reads it (response) or whose payload the server
            # validates (request). Previously only REQUIRED-field removal was
            # flagged, so dropping an optional response field — the TED
            # NoticeResponse.sme-part case — silently produced 0 changes. Flag
            # all property removals as FIELD_REMOVED (breaking), recording
            # whether the field was required so downstream consumers keep the
            # required/optional distinction. The `was_required` flag is purely
            # additive to `details`; no return-schema key is renamed/removed.
            for prop in set(old_props.keys()) - set(new_props.keys()):
                was_required = prop in old_required
                # LED-1600: severity is direction-aware. Removing a field from
                # a RESPONSE (or a context-unknown schema, e.g. a component that
                # may back a response) is breaking — high. Removing it from a
                # REQUEST is non-breaking for clients (the server simply stops
                # requiring/accepting it) — low. The is_breaking property reads
                # `context` to give the authoritative verdict; severity tracks
                # it so the two never disagree (the silent-leak guard).
                is_breaking_removal = context != "request"
                self.changes.append(Change(
                    type=ChangeType.FIELD_REMOVED,
                    path=f"{path}.{prop}",
                    details={
                        "field": prop,
                        # The Evidence model requires every details value to be
                        # a string; coerce bool/None at the producer (LED-1600).
                        "was_required": str(was_required).lower(),
                        "context": context or "",
                    },
                    severity="high" if is_breaking_removal else "low",
                    message=(
                        f"{'Required' if was_required else 'Optional'} field "
                        f"'{prop}' removed at {path}"
                        + ("" if is_breaking_removal
                           else " (request field; non-breaking for clients)")
                    ),
                    context=context,
                ))

            # Check new required fields
            for prop in new_required - old_required:
                if prop not in old_props:
                    # LED-1600: adding a NEW REQUIRED field is breaking for a
                    # REQUEST (clients must now send it) and for a
                    # context-unknown schema (conservative). For a RESPONSE it
                    # is non-breaking — the server merely returns one more
                    # always-present field, which existing consumers ignore.
                    is_breaking_add = context != "response"
                    self.changes.append(Change(
                        type=ChangeType.REQUIRED_FIELD_ADDED,
                        path=f"{path}.{prop}",
                        details={"field": prop, "context": context or ""},
                        severity="high" if is_breaking_add else "low",
                        message=(
                            f"New required field '{prop}' added at {path}"
                            + ("" if is_breaking_add
                               else " (response field; non-breaking for consumers)")
                        ),
                        context=context,
                    ))
                else:
                    # The field already existed but was OPTIONAL and is now
                    # REQUIRED. In a REQUEST this is breaking (clients that
                    # omitted it now fail validation). Surface it rather than
                    # letting it fall through silently. Reuse REQUIRED_FIELD_ADDED
                    # (no new type) but mark via details that it was a
                    # requirement tightening on an existing field.
                    is_breaking_tighten = context != "response"
                    self.changes.append(Change(
                        type=ChangeType.REQUIRED_FIELD_ADDED,
                        path=f"{path}.{prop}",
                        details={
                            "field": prop,
                            "context": context or "",
                            "was_optional": "true",
                        },
                        severity="high" if is_breaking_tighten else "low",
                        message=(
                            f"Field '{prop}' changed from optional to required "
                            f"at {path}"
                            + ("" if is_breaking_tighten
                               else " (response field; non-breaking for consumers)")
                        ),
                        context=context,
                    ))

            # LED-1600: a field that WAS required is now OPTIONAL. For a
            # RESPONSE this is BREAKING — consumers that relied on the field
            # always being present can no longer do so (the silent-leak case
            # the engine previously did not detect AT ALL). For a REQUEST it is
            # non-breaking (the server relaxes its demand). Only flag fields
            # that still exist (a removed field is handled above).
            for prop in old_required - new_required:
                if prop in new_props:
                    is_breaking_relax = context != "request"
                    self.changes.append(Change(
                        type=ChangeType.FIELD_REQUIREMENT_RELAXED,
                        path=f"{path}.{prop}",
                        details={"field": prop, "context": context or ""},
                        severity="high" if is_breaking_relax else "low",
                        message=(
                            f"Field '{prop}' changed from required to optional "
                            f"at {path}"
                            + (" (response field; consumers can no longer rely "
                               "on its presence)" if is_breaking_relax
                               else " (request field; non-breaking)")
                        ),
                        context=context,
                    ))

            # Check new optional fields (additive, non-breaking). LED-1597:
            # surface added optional properties so the additive case is
            # observable in `all_changes` rather than silently producing zero
            # changes. New OPTIONAL_FIELD_ADDED entries are additive to the
            # change list; no return-schema key changes.
            for prop in set(new_props.keys()) - set(old_props.keys()):
                if prop not in new_required:
                    self.changes.append(Change(
                        type=ChangeType.OPTIONAL_FIELD_ADDED,
                        path=f"{path}.{prop}",
                        details={"field": prop, "context": context or ""},
                        severity="low",
                        message=f"Optional field '{prop}' added at {path}",
                        context=context,
                    ))

            # Recursively compare nested properties
            for prop in set(old_props.keys()) & set(new_props.keys()):
                old_prop_schema = old_props[prop]
                new_prop_schema = new_props[prop]

                # Check deprecated on fields
                if not old_prop_schema.get("deprecated", False) and new_prop_schema.get("deprecated", False):
                    self.changes.append(Change(
                        type=ChangeType.DEPRECATED_ADDED,
                        path=f"{path}.{prop}",
                        details={"target": "field", "field": prop},
                        severity="low",
                        message=f"Field '{prop}' marked as deprecated at {path}"
                    ))

                # Check default value changes on fields
                if "default" in old_prop_schema or "default" in new_prop_schema:
                    old_default = old_prop_schema.get("default")
                    new_default = new_prop_schema.get("default")
                    if old_default != new_default:
                        self.changes.append(Change(
                            type=ChangeType.DEFAULT_CHANGED,
                            path=f"{path}.{prop}",
                            details={"old_default": old_default, "new_default": new_default},
                            severity="low",
                            message=f"Default value changed for '{prop}' from {old_default} to {new_default} at {path}"
                        ))

                # Check constraint changes on fields
                self._compare_constraints(f"{path}.{prop}", old_prop_schema, new_prop_schema)

                self._compare_schema_deep(
                    f"{path}.{prop}",
                    old_prop_schema,
                    new_prop_schema,
                    old_required if prop in old_required else None,
                    _visited,
                    context,
                )

        # Compare arrays
        elif old_type == "array":
            if "items" in old_schema and "items" in new_schema:
                self._compare_schema_deep(
                    f"{path}[]",
                    old_schema["items"],
                    new_schema["items"],
                    None,
                    _visited,
                    context,
                )

        # Compare enums
        if "enum" in old_schema or "enum" in new_schema:
            self._compare_enums(path, old_schema.get("enum", []), new_schema.get("enum", []))

        # Compare constraints at top level of schema (non-object)
        if old_type != "object":
            self._compare_constraints(path, old_schema, new_schema)
    
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
    
    def _compare_schemas(self, old_schemas: Dict, new_schemas: Dict,
                         path_prefix: str = "#/components/schemas"):
        """Compare a named-schema map.

        `path_prefix` is the JSON-pointer root of the map so reported paths
        match the ref scheme: "#/components/schemas" for OpenAPI 3.x and
        "#/definitions" for Swagger 2.0.
        """
        # Defend against malformed specs where the schema map is not a dict.
        if not isinstance(old_schemas, dict):
            old_schemas = {}
        if not isinstance(new_schemas, dict):
            new_schemas = {}
        # Schema removal is breaking if referenced
        for schema_name in set(old_schemas.keys()) - set(new_schemas.keys()):
            self.changes.append(Change(
                type=ChangeType.FIELD_REMOVED,
                path=f"{path_prefix}/{schema_name}",
                details={"schema": schema_name},
                severity="medium",
                message=f"Schema '{schema_name}' removed"
            ))

        # Compare existing schemas
        for schema_name in set(old_schemas.keys()) & set(new_schemas.keys()):
            self._compare_schema_deep(
                f"{path_prefix}/{schema_name}",
                old_schemas[schema_name],
                new_schemas[schema_name]
            )
    
    def _compare_constraints(self, path: str, old_schema: Dict, new_schema: Dict):
        """Compare schema constraints (maxLength, minLength, maxItems, minItems)."""
        # maxLength / maxItems decreased = breaking (stricter)
        for prop in ("maxLength", "maxItems"):
            old_val = old_schema.get(prop)
            new_val = new_schema.get(prop)
            if old_val is not None and new_val is not None and new_val < old_val:
                self.changes.append(Change(
                    type=ChangeType.MAX_LENGTH_DECREASED,
                    path=path,
                    details={"constraint": prop, "old_value": old_val, "new_value": new_val},
                    severity="high",
                    message=f"{prop} decreased from {old_val} to {new_val} at {path}"
                ))
            elif old_val is None and new_val is not None:
                # Adding a max constraint where there was none is also stricter
                self.changes.append(Change(
                    type=ChangeType.MAX_LENGTH_DECREASED,
                    path=path,
                    details={"constraint": prop, "old_value": None, "new_value": new_val},
                    severity="high",
                    message=f"{prop} added ({new_val}) at {path} where none existed"
                ))

        # minLength / minItems increased = breaking (stricter)
        for prop in ("minLength", "minItems"):
            old_val = old_schema.get(prop)
            new_val = new_schema.get(prop)
            if old_val is not None and new_val is not None and new_val > old_val:
                self.changes.append(Change(
                    type=ChangeType.MIN_LENGTH_INCREASED,
                    path=path,
                    details={"constraint": prop, "old_value": old_val, "new_value": new_val},
                    severity="high",
                    message=f"{prop} increased from {old_val} to {new_val} at {path}"
                ))
            elif old_val is None and new_val is not None and new_val > 0:
                # Adding a min constraint where there was none is stricter
                self.changes.append(Change(
                    type=ChangeType.MIN_LENGTH_INCREASED,
                    path=path,
                    details={"constraint": prop, "old_value": None, "new_value": new_val},
                    severity="high",
                    message=f"{prop} added ({new_val}) at {path} where none existed"
                ))

    def _compare_operation_security(self, operation_id: str, old_security: Optional[list], new_security: Optional[list]):
        """Compare operation-level security requirements."""
        if old_security is None:
            old_security = []
        if new_security is None:
            new_security = []

        # Build maps: scheme_name -> set of scopes
        def _security_map(sec_list):
            result = {}
            for item in sec_list:
                for scheme, scopes in item.items():
                    result[scheme] = set(scopes) if scopes else set()
            return result

        old_map = _security_map(old_security)
        new_map = _security_map(new_security)

        # Removed security schemes from operation
        for scheme in set(old_map.keys()) - set(new_map.keys()):
            self.changes.append(Change(
                type=ChangeType.SECURITY_REMOVED,
                path=operation_id,
                details={"scheme": scheme},
                severity="high",
                message=f"Security scheme '{scheme}' removed from {operation_id}"
            ))

        # Added security schemes to operation
        for scheme in set(new_map.keys()) - set(old_map.keys()):
            self.changes.append(Change(
                type=ChangeType.SECURITY_ADDED,
                path=operation_id,
                details={"scheme": scheme},
                severity="low",
                message=f"Security scheme '{scheme}' added to {operation_id}"
            ))

        # Check scope changes for shared schemes
        for scheme in set(old_map.keys()) & set(new_map.keys()):
            removed_scopes = old_map[scheme] - new_map[scheme]
            for scope in removed_scopes:
                self.changes.append(Change(
                    type=ChangeType.SECURITY_SCOPE_REMOVED,
                    path=operation_id,
                    details={"scheme": scheme, "scope": scope},
                    severity="high",
                    message=f"OAuth scope '{scope}' removed from scheme '{scheme}' at {operation_id}"
                ))

    def _compare_security(self, old_security: Dict, new_security: Dict):
        """Compare security schemes."""
        # Security scheme removal is breaking
        for scheme in set(old_security.keys()) - set(new_security.keys()):
            self.changes.append(Change(
                type=ChangeType.SECURITY_REMOVED,
                path=f"#/components/securitySchemes/{scheme}",
                details={"scheme": scheme},
                severity="high",
                message=f"Security scheme '{scheme}' removed"
            ))

        # Security scheme addition is non-breaking
        for scheme in set(new_security.keys()) - set(old_security.keys()):
            self.changes.append(Change(
                type=ChangeType.SECURITY_ADDED,
                path=f"#/components/securitySchemes/{scheme}",
                details={"scheme": scheme},
                severity="low",
                message=f"Security scheme '{scheme}' added"
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