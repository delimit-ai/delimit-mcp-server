"""
Delimit Complexity Analyzer
Deterministic API complexity scoring for upgrade signal generation.
"""

import json
import yaml
from typing import Dict, Any, Optional, List, Union
from pathlib import Path


class ComplexityAnalyzer:
    """Analyze OpenAPI specification complexity to determine governance needs."""
    
    def __init__(self):
        """Initialize the analyzer with scoring thresholds."""
        # Scoring thresholds for each metric
        self.scoring_rules = {
            'endpoint_count': [
                (10, 5),   # 0-10 endpoints = 5 points
                (25, 10),  # 11-25 = 10 points
                (50, 20),  # 26-50 = 20 points
                (float('inf'), 30)  # 50+ = 30 points
            ],
            'schema_count': [
                (5, 5),    # 0-5 = 5 points
                (20, 10),  # 6-20 = 10 points
                (50, 20),  # 21-50 = 20 points
                (float('inf'), 30)  # 50+ = 30 points
            ],
            'parameter_count': [
                (20, 5),   # 0-20 = 5 points
                (50, 10),  # 21-50 = 10 points
                (float('inf'), 20)  # 50+ = 20 points
            ],
            'response_variants': [
                (5, 5),    # ≤5 = 5 points
                (10, 10),  # 6-10 = 10 points
                (float('inf'), 20)  # >10 = 20 points
            ],
            'nested_schema_depth': [
                (3, 5),    # ≤3 = 5 points
                (6, 10),   # 4-6 = 10 points
                (float('inf'), 20)  # >6 = 20 points
            ],
            'security_schemes': [
                (0, 0),    # 0 = 0 points
                (1, 5),    # 1 = 5 points
                (3, 10),   # 2-3 = 10 points
                (float('inf'), 20)  # >3 = 20 points
            ],
            'example_count': [
                (0, 0),    # 0 = 0 points
                (10, 5),   # 1-10 = 5 points
                (30, 10),  # 11-30 = 10 points
                (float('inf'), 20)  # >30 = 20 points
            ]
        }
        
        self.classification_thresholds = [
            (25, "Simple API"),
            (50, "Moderate API"),
            (75, "Complex API"),
            (100, "Enterprise-scale API")
        ]
    
    def analyze_openapi_complexity(self, spec: Union[str, Dict, Path]) -> Dict[str, Any]:
        """
        Analyze OpenAPI specification complexity.
        
        Args:
            spec: OpenAPI specification as string, dict, or file path
            
        Returns:
            Dictionary with score, classification, and metrics
        """
        # Parse specification if needed
        parsed_spec = self._parse_spec(spec)
        
        # Extract metrics
        metrics = self._extract_metrics(parsed_spec)
        
        # Calculate score
        score = self._calculate_score(metrics)
        
        # Determine classification
        classification = self._classify_complexity(score)
        
        return {
            "score": score,
            "classification": classification,
            "metrics": metrics
        }
    
    def _parse_spec(self, spec: Union[str, Dict, Path]) -> Dict:
        """Parse OpenAPI specification from various input formats."""
        if isinstance(spec, dict):
            return spec
        
        if isinstance(spec, Path):
            spec = spec.read_text()
        
        if isinstance(spec, str):
            # Try JSON first
            try:
                return json.loads(spec)
            except json.JSONDecodeError:
                # Try YAML
                try:
                    return yaml.safe_load(spec)
                except yaml.YAMLError:
                    # Assume it's a file path
                    with open(spec, 'r') as f:
                        content = f.read()
                        try:
                            return json.loads(content)
                        except json.JSONDecodeError:
                            return yaml.safe_load(content)
        
        raise ValueError("Unable to parse OpenAPI specification")
    
    def _extract_metrics(self, spec: Dict) -> Dict[str, int]:
        """Extract complexity metrics from OpenAPI specification."""
        metrics = {
            'endpoint_count': self._count_endpoints(spec),
            'schema_count': self._count_schemas(spec),
            'parameter_count': self._count_parameters(spec),
            'response_variants': self._count_response_variants(spec),
            'nested_schema_depth': self._calculate_max_schema_depth(spec),
            'security_schemes': self._count_security_schemes(spec),
            'example_count': self._count_examples(spec)
        }
        return metrics
    
    def _count_endpoints(self, spec: Dict) -> int:
        """Count total HTTP methods across all paths."""
        count = 0
        paths = spec.get('paths', {})
        
        for path, path_item in paths.items():
            if isinstance(path_item, dict):
                # Count HTTP methods (excluding parameters and other non-method keys)
                http_methods = ['get', 'post', 'put', 'delete', 'patch', 'head', 'options', 'trace']
                for method in http_methods:
                    if method in path_item:
                        count += 1
        
        return count
    
    def _count_schemas(self, spec: Dict) -> int:
        """Count number of schemas in components."""
        components = spec.get('components', {})
        schemas = components.get('schemas', {})
        return len(schemas)
    
    def _count_parameters(self, spec: Dict) -> int:
        """Count total parameters across all endpoints."""
        count = 0
        paths = spec.get('paths', {})
        
        for path, path_item in paths.items():
            if isinstance(path_item, dict):
                # Path-level parameters
                count += len(path_item.get('parameters', []))
                
                # Method-level parameters
                http_methods = ['get', 'post', 'put', 'delete', 'patch', 'head', 'options', 'trace']
                for method in http_methods:
                    if method in path_item:
                        operation = path_item[method]
                        if isinstance(operation, dict):
                            count += len(operation.get('parameters', []))
        
        # Component parameters
        components = spec.get('components', {})
        count += len(components.get('parameters', {}))
        
        return count
    
    def _count_response_variants(self, spec: Dict) -> int:
        """Count unique response status codes across API."""
        status_codes = set()
        paths = spec.get('paths', {})
        
        for path, path_item in paths.items():
            if isinstance(path_item, dict):
                http_methods = ['get', 'post', 'put', 'delete', 'patch', 'head', 'options', 'trace']
                for method in http_methods:
                    if method in path_item:
                        operation = path_item[method]
                        if isinstance(operation, dict):
                            responses = operation.get('responses', {})
                            status_codes.update(responses.keys())
        
        return len(status_codes)
    
    def _calculate_max_schema_depth(self, spec: Dict) -> int:
        """Calculate maximum nested depth of JSON schema objects."""
        max_depth = 0
        components = spec.get('components', {})
        schemas = components.get('schemas', {})
        
        for schema_name, schema in schemas.items():
            if isinstance(schema, dict):
                depth = self._get_schema_depth(schema, schemas)
                max_depth = max(max_depth, depth)
        
        return max_depth
    
    def _get_schema_depth(self, schema: Dict, all_schemas: Dict, visited: Optional[set] = None) -> int:
        """Recursively calculate schema depth."""
        if visited is None:
            visited = set()
        
        # Prevent infinite recursion
        schema_id = id(schema)
        if schema_id in visited:
            return 0
        visited.add(schema_id)
        
        depth = 1
        
        # Check properties
        if 'properties' in schema:
            for prop_name, prop_schema in schema['properties'].items():
                if isinstance(prop_schema, dict):
                    # Handle $ref
                    if '$ref' in prop_schema:
                        ref_name = prop_schema['$ref'].split('/')[-1]
                        if ref_name in all_schemas:
                            prop_depth = 1 + self._get_schema_depth(all_schemas[ref_name], all_schemas, visited)
                            depth = max(depth, prop_depth)
                    # Handle nested object
                    elif prop_schema.get('type') == 'object':
                        prop_depth = 1 + self._get_schema_depth(prop_schema, all_schemas, visited)
                        depth = max(depth, prop_depth)
                    # Handle array of objects
                    elif prop_schema.get('type') == 'array':
                        items = prop_schema.get('items', {})
                        if isinstance(items, dict):
                            if '$ref' in items:
                                ref_name = items['$ref'].split('/')[-1]
                                if ref_name in all_schemas:
                                    prop_depth = 1 + self._get_schema_depth(all_schemas[ref_name], all_schemas, visited)
                                    depth = max(depth, prop_depth)
                            elif items.get('type') == 'object':
                                prop_depth = 1 + self._get_schema_depth(items, all_schemas, visited)
                                depth = max(depth, prop_depth)
        
        # Check allOf, oneOf, anyOf
        for composition_key in ['allOf', 'oneOf', 'anyOf']:
            if composition_key in schema:
                for sub_schema in schema[composition_key]:
                    if isinstance(sub_schema, dict):
                        if '$ref' in sub_schema:
                            ref_name = sub_schema['$ref'].split('/')[-1]
                            if ref_name in all_schemas:
                                sub_depth = self._get_schema_depth(all_schemas[ref_name], all_schemas, visited)
                                depth = max(depth, sub_depth)
                        else:
                            sub_depth = self._get_schema_depth(sub_schema, all_schemas, visited)
                            depth = max(depth, sub_depth)
        
        return depth
    
    def _count_security_schemes(self, spec: Dict) -> int:
        """Count number of defined authentication methods."""
        components = spec.get('components', {})
        security_schemes = components.get('securitySchemes', {})
        return len(security_schemes)
    
    def _count_examples(self, spec: Dict) -> int:
        """Count number of request/response examples."""
        count = 0
        paths = spec.get('paths', {})
        
        for path, path_item in paths.items():
            if isinstance(path_item, dict):
                http_methods = ['get', 'post', 'put', 'delete', 'patch', 'head', 'options', 'trace']
                for method in http_methods:
                    if method in path_item:
                        operation = path_item[method]
                        if isinstance(operation, dict):
                            # Request examples
                            request_body = operation.get('requestBody', {})
                            if isinstance(request_body, dict):
                                content = request_body.get('content', {})
                                for media_type, media_obj in content.items():
                                    if isinstance(media_obj, dict):
                                        if 'example' in media_obj:
                                            count += 1
                                        if 'examples' in media_obj:
                                            count += len(media_obj['examples'])
                            
                            # Response examples
                            responses = operation.get('responses', {})
                            for status_code, response in responses.items():
                                if isinstance(response, dict):
                                    content = response.get('content', {})
                                    for media_type, media_obj in content.items():
                                        if isinstance(media_obj, dict):
                                            if 'example' in media_obj:
                                                count += 1
                                            if 'examples' in media_obj:
                                                count += len(media_obj['examples'])
        
        # Component examples
        components = spec.get('components', {})
        examples = components.get('examples', {})
        count += len(examples)
        
        return count
    
    def _calculate_score(self, metrics: Dict[str, int]) -> int:
        """Calculate complexity score based on metrics."""
        total_score = 0
        
        for metric_name, metric_value in metrics.items():
            if metric_name in self.scoring_rules:
                rules = self.scoring_rules[metric_name]
                for threshold, points in rules:
                    if metric_value <= threshold:
                        total_score += points
                        break
        
        # Cap at 100
        return min(total_score, 100)
    
    def _classify_complexity(self, score: int) -> str:
        """Classify API complexity based on score."""
        for threshold, classification in self.classification_thresholds:
            if score <= threshold:
                return classification
        return "Enterprise-scale API"
    
    def format_ci_output(self, analysis: Dict[str, Any]) -> Optional[str]:
        """
        Format analysis output for CI logs.
        Only shows output for complex APIs (score >= 50).
        """
        if analysis['score'] < 50:
            return None
        
        metrics = analysis['metrics']
        
        output = []
        output.append("-" * 50)
        output.append("DELIMIT COMPLEXITY ANALYSIS")
        output.append("-" * 50)
        output.append("")
        output.append(f"Complexity Score: {analysis['score']} / 100")
        output.append(f"Classification: {analysis['classification'].upper()}")
        output.append("")
        output.append("Detected:")
        output.append(f"• {metrics['endpoint_count']} endpoints")
        output.append(f"• {metrics['schema_count']} schemas")
        output.append(f"• {metrics['parameter_count']} parameters")
        output.append(f"• Nested schema depth: {metrics['nested_schema_depth']}")
        output.append("")
        output.append("As your API grows, managing change impact becomes harder.")
        output.append("")
        output.append("Consider enabling the Delimit Governance Dashboard:")
        output.append("https://delimit.ai")
        output.append("")
        output.append("This provides:")
        output.append("• Cross-service impact analysis")
        output.append("• Governance policy visualization")
        output.append("• Audit logs for API evolution")
        output.append("")
        output.append("-" * 50)
        
        return "\n".join(output)


def analyze_openapi_complexity(spec: Union[str, Dict, Path]) -> Dict[str, Any]:
    """
    Main entry point for complexity analysis.
    
    Args:
        spec: OpenAPI specification as string, dict, or file path
        
    Returns:
        Dictionary with score, classification, and metrics
    """
    analyzer = ComplexityAnalyzer()
    return analyzer.analyze_openapi_complexity(spec)