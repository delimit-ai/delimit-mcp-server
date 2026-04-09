"""
JSON Schema diff engine (LED-713).

Sibling to core/diff_engine_v2.py. Handles bare JSON Schema files
(Draft 4+), resolving internal $ref to #/definitions. Deliberately
excludes anyOf/oneOf/allOf composition, external refs, discriminators,
and if/then/else — those are deferred past v1.

Dispatched from spec_detector when a file contains a top-level
"$schema" key or a top-level "definitions" key without OpenAPI markers.

Designed for the agents-oss/agentspec integration (issue #21) but
general across any single-file JSON Schema.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class JSONSchemaChangeType(Enum):
    # Breaking
    PROPERTY_REMOVED = "property_removed"
    REQUIRED_ADDED = "required_added"
    TYPE_NARROWED = "type_narrowed"
    ENUM_VALUE_REMOVED = "enum_value_removed"
    CONST_CHANGED = "const_changed"
    ADDITIONAL_PROPERTIES_TIGHTENED = "additional_properties_tightened"
    PATTERN_TIGHTENED = "pattern_tightened"
    MIN_LENGTH_INCREASED = "min_length_increased"
    MAX_LENGTH_DECREASED = "max_length_decreased"
    MINIMUM_INCREASED = "minimum_increased"
    MAXIMUM_DECREASED = "maximum_decreased"
    ITEMS_TYPE_NARROWED = "items_type_narrowed"

    # Non-breaking
    PROPERTY_ADDED = "property_added"
    REQUIRED_REMOVED = "required_removed"
    TYPE_WIDENED = "type_widened"
    ENUM_VALUE_ADDED = "enum_value_added"
    ADDITIONAL_PROPERTIES_LOOSENED = "additional_properties_loosened"
    PATTERN_LOOSENED = "pattern_loosened"
    MIN_LENGTH_DECREASED = "min_length_decreased"
    MAX_LENGTH_INCREASED = "max_length_increased"
    MINIMUM_DECREASED = "minimum_decreased"
    MAXIMUM_INCREASED = "maximum_increased"
    ITEMS_TYPE_WIDENED = "items_type_widened"
    DESCRIPTION_CHANGED = "description_changed"


_BREAKING_TYPES = {
    JSONSchemaChangeType.PROPERTY_REMOVED,
    JSONSchemaChangeType.REQUIRED_ADDED,
    JSONSchemaChangeType.TYPE_NARROWED,
    JSONSchemaChangeType.ENUM_VALUE_REMOVED,
    JSONSchemaChangeType.CONST_CHANGED,
    JSONSchemaChangeType.ADDITIONAL_PROPERTIES_TIGHTENED,
    JSONSchemaChangeType.PATTERN_TIGHTENED,
    JSONSchemaChangeType.MIN_LENGTH_INCREASED,
    JSONSchemaChangeType.MAX_LENGTH_DECREASED,
    JSONSchemaChangeType.MINIMUM_INCREASED,
    JSONSchemaChangeType.MAXIMUM_DECREASED,
    JSONSchemaChangeType.ITEMS_TYPE_NARROWED,
}


@dataclass
class JSONSchemaChange:
    type: JSONSchemaChangeType
    path: str
    details: Dict[str, Any] = field(default_factory=dict)
    message: str = ""

    @property
    def is_breaking(self) -> bool:
        return self.type in _BREAKING_TYPES

    @property
    def severity(self) -> str:
        return "high" if self.is_breaking else "low"


# Type widening hierarchy: a change from "integer" to "number" is widening
# (non-breaking for consumers). The reverse narrows and is breaking.
_TYPE_SUPERSETS = {
    "number": {"integer"},
}


def _is_type_widening(old: str, new: str) -> bool:
    return old in _TYPE_SUPERSETS.get(new, set())


def _is_type_narrowing(old: str, new: str) -> bool:
    return new in _TYPE_SUPERSETS.get(old, set())


class JSONSchemaDiffEngine:
    """Compare two JSON Schema documents.

    Handles internal $ref to #/definitions by resolving refs against the
    document's own definitions block during traversal. External refs
    (http://, file://) are out of scope for v1.
    """

    def __init__(self) -> None:
        self.changes: List[JSONSchemaChange] = []
        self._old_defs: Dict[str, Any] = {}
        self._new_defs: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def compare(self, old_schema: Dict[str, Any], new_schema: Dict[str, Any]) -> List[JSONSchemaChange]:
        self.changes = []
        old_schema = old_schema or {}
        new_schema = new_schema or {}
        self._old_defs = old_schema.get("definitions", {}) or {}
        self._new_defs = new_schema.get("definitions", {}) or {}

        # If the root is a $ref shim (common pattern: {"$ref": "#/definitions/Foo", "definitions": {...}})
        # unwrap both sides so we diff the actual shape.
        old_root = self._resolve(old_schema, self._old_defs)
        new_root = self._resolve(new_schema, self._new_defs)

        self._compare_schema(old_root, new_root, path="")
        return self.changes

    # ------------------------------------------------------------------
    # $ref resolution
    # ------------------------------------------------------------------

    def _resolve(self, node: Any, defs: Dict[str, Any]) -> Any:
        """Resolve internal $ref to #/definitions. Returns node unchanged otherwise."""
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if not ref or not isinstance(ref, str) or not ref.startswith("#/definitions/"):
            return node
        key = ref[len("#/definitions/"):]
        resolved = defs.get(key)
        if resolved is None:
            return node
        # Merge sibling keys from the ref node (e.g. description) onto the resolved.
        merged = dict(resolved)
        for k, v in node.items():
            if k != "$ref":
                merged.setdefault(k, v)
        return merged

    # ------------------------------------------------------------------
    # recursive traversal
    # ------------------------------------------------------------------

    def _compare_schema(self, old: Any, new: Any, path: str) -> None:
        if not isinstance(old, dict) or not isinstance(new, dict):
            return
        old = self._resolve(old, self._old_defs)
        new = self._resolve(new, self._new_defs)

        self._compare_type(old, new, path)
        self._compare_const(old, new, path)
        self._compare_enum(old, new, path)
        self._compare_pattern(old, new, path)
        self._compare_numeric_bounds(old, new, path)
        self._compare_string_length(old, new, path)
        self._compare_additional_properties(old, new, path)
        self._compare_required(old, new, path)
        self._compare_properties(old, new, path)
        self._compare_items(old, new, path)

    # ------------------------------------------------------------------
    # individual comparisons
    # ------------------------------------------------------------------

    def _compare_type(self, old: Dict, new: Dict, path: str) -> None:
        old_t = old.get("type")
        new_t = new.get("type")
        if old_t == new_t or old_t is None or new_t is None:
            return
        if isinstance(old_t, str) and isinstance(new_t, str):
            if _is_type_widening(old_t, new_t):
                self._add(JSONSchemaChangeType.TYPE_WIDENED, path,
                          {"old": old_t, "new": new_t},
                          f"Type widened at {path or '/'}: {old_t} → {new_t}")
                return
            if _is_type_narrowing(old_t, new_t):
                self._add(JSONSchemaChangeType.TYPE_NARROWED, path,
                          {"old": old_t, "new": new_t},
                          f"Type narrowed at {path or '/'}: {old_t} → {new_t}")
                return
            # Unrelated type change — treat as narrowing (breaking)
            self._add(JSONSchemaChangeType.TYPE_NARROWED, path,
                      {"old": old_t, "new": new_t},
                      f"Type changed at {path or '/'}: {old_t} → {new_t}")

    def _compare_const(self, old: Dict, new: Dict, path: str) -> None:
        if "const" in old and "const" in new and old["const"] != new["const"]:
            self._add(JSONSchemaChangeType.CONST_CHANGED, path,
                      {"old": old["const"], "new": new["const"]},
                      f"const value changed at {path or '/'}: {old['const']!r} → {new['const']!r}")

    def _compare_enum(self, old: Dict, new: Dict, path: str) -> None:
        old_enum = old.get("enum")
        new_enum = new.get("enum")
        if not isinstance(old_enum, list) or not isinstance(new_enum, list):
            return
        old_set = {repr(v) for v in old_enum}
        new_set = {repr(v) for v in new_enum}
        for removed in old_set - new_set:
            self._add(JSONSchemaChangeType.ENUM_VALUE_REMOVED, path,
                      {"value": removed},
                      f"enum value removed at {path or '/'}: {removed}")
        for added in new_set - old_set:
            self._add(JSONSchemaChangeType.ENUM_VALUE_ADDED, path,
                      {"value": added},
                      f"enum value added at {path or '/'}: {added}")

    def _compare_pattern(self, old: Dict, new: Dict, path: str) -> None:
        old_p = old.get("pattern")
        new_p = new.get("pattern")
        if old_p == new_p or (old_p is None and new_p is None):
            return
        # We can't prove regex subset relationships, so any pattern change
        # on an existing constraint is conservatively breaking; adding a
        # brand-new pattern is breaking; removing a pattern is non-breaking.
        if old_p and not new_p:
            self._add(JSONSchemaChangeType.PATTERN_LOOSENED, path,
                      {"old": old_p},
                      f"pattern removed at {path or '/'}: {old_p}")
        elif not old_p and new_p:
            self._add(JSONSchemaChangeType.PATTERN_TIGHTENED, path,
                      {"new": new_p},
                      f"pattern added at {path or '/'}: {new_p}")
        else:
            self._add(JSONSchemaChangeType.PATTERN_TIGHTENED, path,
                      {"old": old_p, "new": new_p},
                      f"pattern changed at {path or '/'}: {old_p} → {new_p}")

    def _compare_numeric_bounds(self, old: Dict, new: Dict, path: str) -> None:
        for key, tight_type, loose_type in (
            ("minimum", JSONSchemaChangeType.MINIMUM_INCREASED, JSONSchemaChangeType.MINIMUM_DECREASED),
            ("maximum", JSONSchemaChangeType.MAXIMUM_DECREASED, JSONSchemaChangeType.MAXIMUM_INCREASED),
        ):
            old_v = old.get(key)
            new_v = new.get(key)
            if old_v is None or new_v is None or old_v == new_v:
                continue
            try:
                delta = float(new_v) - float(old_v)
            except (TypeError, ValueError):
                continue
            if key == "minimum":
                if delta > 0:
                    self._add(tight_type, path, {"old": old_v, "new": new_v},
                              f"minimum increased at {path or '/'}: {old_v} → {new_v}")
                else:
                    self._add(loose_type, path, {"old": old_v, "new": new_v},
                              f"minimum decreased at {path or '/'}: {old_v} → {new_v}")
            else:  # maximum
                if delta < 0:
                    self._add(tight_type, path, {"old": old_v, "new": new_v},
                              f"maximum decreased at {path or '/'}: {old_v} → {new_v}")
                else:
                    self._add(loose_type, path, {"old": old_v, "new": new_v},
                              f"maximum increased at {path or '/'}: {old_v} → {new_v}")

    def _compare_string_length(self, old: Dict, new: Dict, path: str) -> None:
        for key, tight_type, loose_type in (
            ("minLength", JSONSchemaChangeType.MIN_LENGTH_INCREASED, JSONSchemaChangeType.MIN_LENGTH_DECREASED),
            ("maxLength", JSONSchemaChangeType.MAX_LENGTH_DECREASED, JSONSchemaChangeType.MAX_LENGTH_INCREASED),
        ):
            old_v = old.get(key)
            new_v = new.get(key)
            if old_v is None or new_v is None or old_v == new_v:
                continue
            if key == "minLength":
                if new_v > old_v:
                    self._add(tight_type, path, {"old": old_v, "new": new_v},
                              f"minLength increased at {path or '/'}: {old_v} → {new_v}")
                else:
                    self._add(loose_type, path, {"old": old_v, "new": new_v},
                              f"minLength decreased at {path or '/'}: {old_v} → {new_v}")
            else:  # maxLength
                if new_v < old_v:
                    self._add(tight_type, path, {"old": old_v, "new": new_v},
                              f"maxLength decreased at {path or '/'}: {old_v} → {new_v}")
                else:
                    self._add(loose_type, path, {"old": old_v, "new": new_v},
                              f"maxLength increased at {path or '/'}: {old_v} → {new_v}")

    def _compare_additional_properties(self, old: Dict, new: Dict, path: str) -> None:
        old_ap = old.get("additionalProperties")
        new_ap = new.get("additionalProperties")
        # Default in JSON Schema is True (additional allowed). Only flag
        # explicit transitions that change the answer.
        if old_ap is None and new_ap is None:
            return
        old_allows = True if old_ap is None else bool(old_ap)
        new_allows = True if new_ap is None else bool(new_ap)
        if old_allows and not new_allows:
            self._add(JSONSchemaChangeType.ADDITIONAL_PROPERTIES_TIGHTENED, path,
                      {"old": old_ap, "new": new_ap},
                      f"additionalProperties tightened at {path or '/'}: {old_ap} → {new_ap}")
        elif not old_allows and new_allows:
            self._add(JSONSchemaChangeType.ADDITIONAL_PROPERTIES_LOOSENED, path,
                      {"old": old_ap, "new": new_ap},
                      f"additionalProperties loosened at {path or '/'}: {old_ap} → {new_ap}")

    def _compare_required(self, old: Dict, new: Dict, path: str) -> None:
        old_req = set(old.get("required", []) or [])
        new_req = set(new.get("required", []) or [])
        for added in new_req - old_req:
            self._add(JSONSchemaChangeType.REQUIRED_ADDED, f"{path}/required/{added}",
                      {"field": added},
                      f"required field added at {path or '/'}: {added}")
        for removed in old_req - new_req:
            self._add(JSONSchemaChangeType.REQUIRED_REMOVED, f"{path}/required/{removed}",
                      {"field": removed},
                      f"required field removed at {path or '/'}: {removed}")

    def _compare_properties(self, old: Dict, new: Dict, path: str) -> None:
        old_props = old.get("properties", {}) or {}
        new_props = new.get("properties", {}) or {}
        if not isinstance(old_props, dict) or not isinstance(new_props, dict):
            return
        for removed in set(old_props) - set(new_props):
            self._add(JSONSchemaChangeType.PROPERTY_REMOVED, f"{path}/properties/{removed}",
                      {"field": removed},
                      f"property removed: {path or '/'}.{removed}")
        for added in set(new_props) - set(old_props):
            self._add(JSONSchemaChangeType.PROPERTY_ADDED, f"{path}/properties/{added}",
                      {"field": added},
                      f"property added: {path or '/'}.{added}")
        for name in set(old_props) & set(new_props):
            self._compare_schema(old_props[name], new_props[name], f"{path}/properties/{name}")

    def _compare_items(self, old: Dict, new: Dict, path: str) -> None:
        old_items = old.get("items")
        new_items = new.get("items")
        if not isinstance(old_items, dict) or not isinstance(new_items, dict):
            return
        self._compare_schema(old_items, new_items, f"{path}/items")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _add(self, change_type: JSONSchemaChangeType, path: str,
             details: Dict[str, Any], message: str) -> None:
        self.changes.append(JSONSchemaChange(
            type=change_type, path=path or "/", details=details, message=message))


def is_json_schema(doc: Dict[str, Any]) -> bool:
    """Detect whether a parsed document should be routed to this engine.

    Heuristic: top-level "$schema" key referencing json-schema.org, OR a
    top-level "definitions" block without OpenAPI markers (paths, components,
    openapi, swagger).
    """
    if not isinstance(doc, dict):
        return False
    if any(marker in doc for marker in ("openapi", "swagger", "paths")):
        return False
    schema_url = doc.get("$schema")
    if isinstance(schema_url, str) and "json-schema.org" in schema_url:
        return True
    if "definitions" in doc and isinstance(doc["definitions"], dict):
        return True
    # Agentspec pattern: {"$ref": "#/definitions/...", "definitions": {...}}
    if doc.get("$ref", "").startswith("#/definitions/"):
        return True
    return False
