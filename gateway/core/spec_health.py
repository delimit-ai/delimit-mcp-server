"""
Spec Health Score Engine.

Scores an OpenAPI spec on five dimensions (0-100 each):
  - completeness: endpoints with descriptions, examples, response schemas
  - security: auth schemes, HTTPS, no PII patterns
  - consistency: naming convention uniformity, response structure patterns
  - documentation: info metadata, contact, license, tag descriptions
  - best_practices: $ref reuse, schema depth, proper HTTP methods

Returns an overall weighted score and letter grade (A-F).
"""

import re
from typing import Any, Dict, List, Optional, Set, Tuple


# Weights for the overall score
DIMENSION_WEIGHTS = {
    "completeness": 0.30,
    "security": 0.20,
    "consistency": 0.20,
    "documentation": 0.15,
    "best_practices": 0.15,
}

# PII patterns to flag
PII_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN
    re.compile(r"\b\d{16}\b"),  # Credit card (simple)
    re.compile(r"\b[A-Za-z0-9._%+-]+@(?!example\.com\b)[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),  # Email (excludes example.com)
    re.compile(r"password\s*[:=]\s*['\"][^'\"]+['\"]", re.IGNORECASE),  # Hardcoded passwords
]

# HTTP methods that are standard for REST.
# LED-290: "trace" (OpenAPI 3.x) and "query" (OpenAPI 3.2.0) are recognized
# but not required. The QUERY method allows safe, idempotent requests with
# a request body, so it is intentionally absent from NO_BODY_METHODS.
STANDARD_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace", "query"}

# Methods that should not have request bodies per HTTP semantics
NO_BODY_METHODS = {"get", "head", "delete"}


def _letter_grade(score: float) -> str:
    """Convert a 0-100 score to a letter grade."""
    if score >= 90:
        return "A"
    elif score >= 80:
        return "B"
    elif score >= 70:
        return "C"
    elif score >= 60:
        return "D"
    else:
        return "F"


def _get_all_operations(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract all operations from the spec with their path and method."""
    ops = []
    paths = spec.get("paths") or {}
    for path, path_obj in paths.items():
        if not isinstance(path_obj, dict):
            continue
        for method in STANDARD_METHODS:
            if method in path_obj and isinstance(path_obj[method], dict):
                ops.append({
                    "path": path,
                    "method": method,
                    "operation": path_obj[method],
                })
    return ops


def _count_refs(obj: Any, depth: int = 0) -> int:
    """Count $ref usages in an object tree."""
    if depth > 50:
        return 0
    count = 0
    if isinstance(obj, dict):
        if "$ref" in obj:
            count += 1
        for v in obj.values():
            count += _count_refs(v, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            count += _count_refs(item, depth + 1)
    return count


def _max_inline_depth(obj: Any, current: int = 0, limit: int = 20) -> int:
    """Find the maximum nesting depth of inline (non-$ref) schemas."""
    if current > limit or not isinstance(obj, dict):
        return current
    if "$ref" in obj:
        return current  # refs don't count as inline depth
    max_d = current
    # Check properties (object nesting)
    props = obj.get("properties", {})
    if isinstance(props, dict):
        for v in props.values():
            if isinstance(v, dict) and "$ref" not in v:
                d = _max_inline_depth(v, current + 1, limit)
                max_d = max(max_d, d)
    # Check items (array nesting)
    items = obj.get("items")
    if isinstance(items, dict) and "$ref" not in items:
        d = _max_inline_depth(items, current + 1, limit)
        max_d = max(max_d, d)
    # Check additionalProperties
    addl = obj.get("additionalProperties")
    if isinstance(addl, dict) and "$ref" not in addl:
        d = _max_inline_depth(addl, current + 1, limit)
        max_d = max(max_d, d)
    return max_d


def _extract_path_segments(path: str) -> List[str]:
    """Extract non-parameter segments from a path like /users/{id}/posts."""
    segments = []
    for part in path.strip("/").split("/"):
        if part and not part.startswith("{"):
            segments.append(part)
    return segments


def _detect_naming_style(name: str) -> Optional[str]:
    """Detect if a name is camelCase, snake_case, kebab-case, or PascalCase."""
    if "_" in name:
        return "snake_case"
    if "-" in name:
        return "kebab-case"
    if name and name[0].isupper() and any(c.islower() for c in name):
        return "PascalCase"
    if name and name[0].islower() and any(c.isupper() for c in name):
        return "camelCase"
    return None  # single word or ambiguous


def score_completeness(spec: Dict[str, Any]) -> Tuple[int, List[str]]:
    """Score completeness: descriptions, examples, response schemas."""
    ops = _get_all_operations(spec)
    if not ops:
        return 0, ["No endpoints found in spec"]

    recommendations = []
    total_checks = 0
    passed_checks = 0

    ops_without_description = []
    ops_without_response_schema = []
    ops_without_examples = []

    for op_info in ops:
        op = op_info["operation"]
        label = f"{op_info['method'].upper()} {op_info['path']}"

        # Check: operation has description or summary
        total_checks += 1
        if op.get("description") or op.get("summary"):
            passed_checks += 1
        else:
            ops_without_description.append(label)

        # Check: at least one response has a schema
        total_checks += 1
        responses = op.get("responses") or {}
        has_schema = False
        for resp in responses.values():
            if isinstance(resp, dict):
                content = resp.get("content", {})
                if isinstance(content, dict):
                    for media in content.values():
                        if isinstance(media, dict) and "schema" in media:
                            has_schema = True
                            break
                # Also check old-style schema
                if "schema" in resp:
                    has_schema = True
            if has_schema:
                break
        if has_schema:
            passed_checks += 1
        else:
            ops_without_response_schema.append(label)

        # Check: has examples (in parameters or request body or responses)
        total_checks += 1
        has_example = False
        # Check parameters
        for param in op.get("parameters", []):
            if isinstance(param, dict) and ("example" in param or "examples" in param):
                has_example = True
                break
            schema = param.get("schema", {}) if isinstance(param, dict) else {}
            if isinstance(schema, dict) and "example" in schema:
                has_example = True
                break
        # Check request body
        if not has_example:
            rb = op.get("requestBody", {})
            if isinstance(rb, dict):
                for media in (rb.get("content") or {}).values():
                    if isinstance(media, dict) and ("example" in media or "examples" in media):
                        has_example = True
                        break
        # Check response examples
        if not has_example:
            for resp in responses.values():
                if isinstance(resp, dict):
                    for media in (resp.get("content") or {}).values():
                        if isinstance(media, dict) and ("example" in media or "examples" in media):
                            has_example = True
                            break
                if has_example:
                    break
        if has_example:
            passed_checks += 1
        else:
            ops_without_examples.append(label)

    if ops_without_description:
        if len(ops_without_description) <= 3:
            recommendations.append(f"Add description/summary to: {', '.join(ops_without_description)}")
        else:
            recommendations.append(f"{len(ops_without_description)} of {len(ops)} endpoints lack description/summary")

    if ops_without_response_schema:
        if len(ops_without_response_schema) <= 3:
            recommendations.append(f"Add response schema to: {', '.join(ops_without_response_schema)}")
        else:
            recommendations.append(f"{len(ops_without_response_schema)} of {len(ops)} endpoints lack response schemas")

    if ops_without_examples:
        if len(ops_without_examples) <= 3:
            recommendations.append(f"Add examples to: {', '.join(ops_without_examples)}")
        else:
            recommendations.append(f"{len(ops_without_examples)} of {len(ops)} endpoints lack examples")

    score = round((passed_checks / total_checks) * 100) if total_checks > 0 else 0
    return score, recommendations


def score_security(spec: Dict[str, Any]) -> Tuple[int, List[str]]:
    """Score security: auth schemes, HTTPS, PII patterns."""
    recommendations = []
    points = 0
    max_points = 0

    # Check: security schemes defined
    max_points += 30
    components = spec.get("components") or {}
    security_schemes = components.get("securitySchemes") or {}
    if security_schemes:
        points += 30
    else:
        recommendations.append("Define securitySchemes in components (e.g., bearerAuth, apiKey, oauth2)")

    # Check: global security applied
    max_points += 20
    global_security = spec.get("security")
    if global_security and isinstance(global_security, list) and len(global_security) > 0:
        points += 20
    else:
        recommendations.append("Add global security requirement (e.g., security: [bearerAuth: []])")

    # Check: server URLs use HTTPS
    max_points += 25
    servers = spec.get("servers") or []
    if not servers:
        # No servers defined -- partial credit (relative URLs are fine)
        points += 10
        recommendations.append("Define server URLs with HTTPS")
    else:
        all_https = True
        for s in servers:
            url = s.get("url", "") if isinstance(s, dict) else ""
            if url and url.startswith("http://"):
                all_https = False
                break
        if all_https:
            points += 25
        else:
            recommendations.append("Use HTTPS for all server URLs")

    # Check: no PII patterns in examples or descriptions
    max_points += 25
    spec_text = _spec_to_text(spec)
    pii_found = []
    for pattern in PII_PATTERNS:
        if pattern.search(spec_text):
            pii_found.append(pattern.pattern)
    if not pii_found:
        points += 25
    else:
        recommendations.append("Potential PII detected in spec content -- use placeholder values for examples")

    score = round((points / max_points) * 100) if max_points > 0 else 0
    return score, recommendations


def _spec_to_text(spec: Dict[str, Any], depth: int = 0) -> str:
    """Convert a spec to flat text for pattern scanning. Limits recursion depth."""
    if depth > 15:
        return ""
    parts = []
    if isinstance(spec, dict):
        for k, v in spec.items():
            if k == "$ref":
                continue
            parts.append(str(k))
            parts.append(_spec_to_text(v, depth + 1))
    elif isinstance(spec, list):
        for item in spec:
            parts.append(_spec_to_text(item, depth + 1))
    elif isinstance(spec, str):
        parts.append(spec)
    return " ".join(parts)


def score_consistency(spec: Dict[str, Any]) -> Tuple[int, List[str]]:
    """Score consistency: naming conventions, response structure patterns."""
    recommendations = []
    points = 0
    max_points = 0

    # Check: path segment naming consistency
    max_points += 35
    paths = spec.get("paths") or {}
    all_segments = []
    for path in paths:
        all_segments.extend(_extract_path_segments(path))

    if all_segments:
        styles = {}
        for seg in all_segments:
            style = _detect_naming_style(seg)
            if style:
                styles[style] = styles.get(style, 0) + 1
        if styles:
            total_styled = sum(styles.values())
            dominant_style = max(styles, key=styles.get)
            dominant_count = styles[dominant_style]
            consistency_ratio = dominant_count / total_styled
            points += round(35 * consistency_ratio)
            if consistency_ratio < 0.9:
                recommendations.append(
                    f"Path naming inconsistency: mixed {', '.join(styles.keys())}. "
                    f"Standardize on {dominant_style}"
                )
        else:
            points += 35  # single-word segments, no inconsistency
    else:
        points += 35

    # Check: parameter naming consistency
    max_points += 35
    ops = _get_all_operations(spec)
    param_names = []
    for op_info in ops:
        for param in op_info["operation"].get("parameters", []):
            if isinstance(param, dict) and "name" in param:
                param_names.append(param["name"])
    # Also check schema property names
    schemas = (spec.get("components") or {}).get("schemas") or {}
    for schema_name, schema in schemas.items():
        if isinstance(schema, dict):
            for prop_name in (schema.get("properties") or {}).keys():
                param_names.append(prop_name)

    if param_names:
        styles = {}
        for name in param_names:
            style = _detect_naming_style(name)
            if style:
                styles[style] = styles.get(style, 0) + 1
        if styles:
            total_styled = sum(styles.values())
            dominant_style = max(styles, key=styles.get)
            dominant_count = styles[dominant_style]
            consistency_ratio = dominant_count / total_styled
            points += round(35 * consistency_ratio)
            if consistency_ratio < 0.9:
                recommendations.append(
                    f"Parameter/property naming inconsistency: mixed {', '.join(styles.keys())}. "
                    f"Standardize on {dominant_style}"
                )
        else:
            points += 35
    else:
        points += 35

    # Check: response structure consistency (all success responses have similar shape)
    max_points += 30
    response_shapes: List[str] = []
    for op_info in ops:
        responses = op_info["operation"].get("responses") or {}
        for code, resp in responses.items():
            if not isinstance(resp, dict):
                continue
            if str(code).startswith("2"):
                content = resp.get("content") or {}
                for media_type, media in content.items():
                    if isinstance(media, dict) and "schema" in media:
                        schema = media["schema"]
                        # Classify shape: object, array, ref, primitive
                        if "$ref" in schema:
                            response_shapes.append("ref")
                        elif schema.get("type") == "array":
                            response_shapes.append("array")
                        elif schema.get("type") == "object" or "properties" in schema:
                            response_shapes.append("object")
                        else:
                            response_shapes.append("primitive")

    if len(response_shapes) >= 2:
        # Check if responses use a consistent wrapper pattern
        unique_shapes = set(response_shapes)
        if len(unique_shapes) <= 2:
            points += 30
        else:
            points += 15
            recommendations.append(
                "Response structures use mixed shapes. Consider a consistent envelope pattern"
            )
    else:
        points += 30  # too few to judge

    score = round((points / max_points) * 100) if max_points > 0 else 0
    return score, recommendations


def score_documentation(spec: Dict[str, Any]) -> Tuple[int, List[str]]:
    """Score documentation: info metadata, contact, license, tags."""
    recommendations = []
    points = 0
    max_points = 0

    info = spec.get("info") or {}

    # Check: info.description
    max_points += 25
    if info.get("description"):
        points += 25
    else:
        recommendations.append("Add info.description to explain what this API does")

    # Check: info.contact
    max_points += 20
    if info.get("contact") and isinstance(info["contact"], dict):
        points += 20
    else:
        recommendations.append("Add info.contact with name and email/url")

    # Check: info.license
    max_points += 20
    if info.get("license") and isinstance(info["license"], dict):
        points += 20
    else:
        recommendations.append("Add info.license to specify API license terms")

    # Check: tags defined and described
    max_points += 20
    tags = spec.get("tags") or []
    if tags and isinstance(tags, list):
        described = sum(1 for t in tags if isinstance(t, dict) and t.get("description"))
        if described == len(tags):
            points += 20
        elif described > 0:
            points += 10
            recommendations.append("Add descriptions to all tags")
        else:
            points += 5
            recommendations.append("Add descriptions to tags")
    else:
        recommendations.append("Define tags with descriptions to organize endpoints")

    # Check: info.version follows semver
    max_points += 15
    version = info.get("version", "")
    if version and re.match(r"^\d+\.\d+\.\d+", str(version)):
        points += 15
    elif version:
        points += 5
        recommendations.append("Use semantic versioning for info.version (e.g., 1.0.0)")
    else:
        recommendations.append("Set info.version")

    score = round((points / max_points) * 100) if max_points > 0 else 0
    return score, recommendations


def score_best_practices(spec: Dict[str, Any]) -> Tuple[int, List[str]]:
    """Score best practices: $ref reuse, schema depth, HTTP methods."""
    recommendations = []
    points = 0
    max_points = 0

    # Check: uses $ref for reuse
    max_points += 30
    ref_count = _count_refs(spec)
    schemas = (spec.get("components") or {}).get("schemas") or {}
    if ref_count >= 3 or len(schemas) >= 2:
        points += 30
    elif ref_count >= 1 or len(schemas) >= 1:
        points += 15
        recommendations.append("Increase use of $ref and component schemas to reduce duplication")
    else:
        recommendations.append("Define reusable schemas in components/schemas and reference with $ref")

    # Check: no deeply nested inline schemas (>3 levels)
    max_points += 25
    max_depth = 0
    paths_obj = spec.get("paths") or {}
    for path_key, path_val in paths_obj.items():
        if not isinstance(path_val, dict):
            continue
        for method in STANDARD_METHODS:
            op = path_val.get(method)
            if not isinstance(op, dict):
                continue
            # Check request body schemas
            rb = op.get("requestBody", {})
            if isinstance(rb, dict):
                for media in (rb.get("content") or {}).values():
                    if isinstance(media, dict) and "schema" in media:
                        d = _max_inline_depth(media["schema"])
                        max_depth = max(max_depth, d)
            # Check response schemas
            for resp in (op.get("responses") or {}).values():
                if isinstance(resp, dict):
                    for media in (resp.get("content") or {}).values():
                        if isinstance(media, dict) and "schema" in media:
                            d = _max_inline_depth(media["schema"])
                            max_depth = max(max_depth, d)

    if max_depth <= 3:
        points += 25
    elif max_depth <= 5:
        points += 15
        recommendations.append(f"Inline schema nesting depth of {max_depth} -- extract nested schemas to components")
    else:
        recommendations.append(f"Deeply nested inline schemas (depth {max_depth}) -- refactor to $ref components")

    # Check: proper HTTP method usage
    max_points += 25
    ops = _get_all_operations(spec)
    method_issues = []
    for op_info in ops:
        method = op_info["method"]
        op = op_info["operation"]
        # GET/HEAD/DELETE should not have requestBody
        if method in NO_BODY_METHODS and op.get("requestBody"):
            method_issues.append(f"{method.upper()} {op_info['path']} has requestBody")

    if not method_issues:
        points += 25
    else:
        points += 10
        if len(method_issues) <= 2:
            recommendations.append(f"HTTP method misuse: {'; '.join(method_issues)}")
        else:
            recommendations.append(f"{len(method_issues)} endpoints misuse HTTP methods (e.g., GET with requestBody)")

    # Check: operationId defined for all operations
    max_points += 20
    if ops:
        with_id = sum(1 for o in ops if o["operation"].get("operationId"))
        ratio = with_id / len(ops)
        points += round(20 * ratio)
        if ratio < 1.0:
            missing = len(ops) - with_id
            recommendations.append(f"{missing} endpoint(s) missing operationId -- needed for SDK generation")
    else:
        points += 20

    score = round((points / max_points) * 100) if max_points > 0 else 0
    return score, recommendations


def score_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Score an OpenAPI spec across all dimensions.

    Returns:
        Dict with overall_score, grade, dimensions, and recommendations.
    """
    dimensions = {}
    all_recommendations = []

    scorers = {
        "completeness": score_completeness,
        "security": score_security,
        "consistency": score_consistency,
        "documentation": score_documentation,
        "best_practices": score_best_practices,
    }

    for name, scorer in scorers.items():
        score, recs = scorer(spec)
        dimensions[name] = {
            "score": score,
            "grade": _letter_grade(score),
            "weight": DIMENSION_WEIGHTS[name],
        }
        for rec in recs:
            all_recommendations.append({"dimension": name, "recommendation": rec})

    # Weighted average
    overall = sum(
        dimensions[d]["score"] * DIMENSION_WEIGHTS[d]
        for d in dimensions
    )
    overall_score = round(overall)

    # Count endpoints
    ops = _get_all_operations(spec)

    return {
        "overall_score": overall_score,
        "grade": _letter_grade(overall_score),
        "dimensions": dimensions,
        "recommendations": all_recommendations,
        "endpoint_count": len(ops),
        "spec_version": spec.get("openapi") or spec.get("swagger") or "unknown",
        "api_title": (spec.get("info") or {}).get("title", "Unknown API"),
    }
