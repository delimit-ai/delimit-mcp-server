"""
Automatic OpenAPI specification detector for zero-config installation.
"""

import os
from typing import List, Optional, Tuple
from pathlib import Path
import yaml

class SpecDetector:
    """Auto-detect OpenAPI specifications in common locations."""
    
    COMMON_SPEC_PATTERNS = [
        "openapi.yaml",
        "openapi.yml", 
        "swagger.yaml",
        "swagger.yml",
        "api/openapi.yaml",
        "api/openapi.yml",
        "api/swagger.yaml",
        "api/swagger.yml",
        "spec/openapi.yaml",
        "spec/openapi.yml",
        "docs/openapi.yaml",
        "docs/api.yaml",
        "api-spec.yaml",
        "api-spec.yml",
        "**/openapi.yaml",  # Recursive search
        "**/swagger.yaml",
        "services/*/api/openapi.yaml",  # Monorepo pattern
    ]
    
    def __init__(self, root_path: str = "."):
        self.root = Path(root_path)
    
    def detect_specs(self) -> Tuple[List[str], Optional[str]]:
        """
        Detect OpenAPI specifications.
        
        Returns:
            (spec_files, message): List of found specs and optional message
        """
        found_specs = []
        
        # Check each common pattern
        for pattern in self.COMMON_SPEC_PATTERNS:
            if "**" in pattern:
                # Recursive glob
                for spec_file in self.root.glob(pattern):
                    if self._is_valid_openapi(spec_file):
                        found_specs.append(str(spec_file.relative_to(self.root)))
            elif "*" in pattern:
                # Simple glob
                for spec_file in self.root.glob(pattern):
                    if self._is_valid_openapi(spec_file):
                        found_specs.append(str(spec_file.relative_to(self.root)))
            else:
                # Direct path
                spec_file = self.root / pattern
                if spec_file.exists() and self._is_valid_openapi(spec_file):
                    found_specs.append(pattern)
        
        # Remove duplicates while preserving order
        found_specs = list(dict.fromkeys(found_specs))
        
        # Generate appropriate message
        if len(found_specs) == 0:
            message = "No OpenAPI specifications found. Please specify 'files' or create openapi.yaml"
        elif len(found_specs) == 1:
            message = f"Auto-detected spec: {found_specs[0]}"
        else:
            message = f"Multiple specs found: {', '.join(found_specs[:3])}. Please specify 'files' parameter."
        
        return found_specs, message
    
    def _is_valid_openapi(self, file_path: Path) -> bool:
        """Check if file is a valid OpenAPI specification."""
        if not file_path.is_file():
            return False
        
        try:
            with open(file_path, 'r') as f:
                data = yaml.safe_load(f)
                # Check for OpenAPI/Swagger markers
                if isinstance(data, dict):
                    return 'openapi' in data or 'swagger' in data
        except:
            return False
        
        return False
    
    def get_default_specs(self) -> Tuple[Optional[str], Optional[str]]:
        """
        Get default old_spec and new_spec for auto-detection.
        
        Returns:
            (old_spec, new_spec): Paths or None if not found
        """
        specs, _ = self.detect_specs()
        
        if len(specs) == 0:
            return None, None
        
        # Use the first found spec as both old and new (baseline mode)
        default_spec = specs[0]
        return default_spec, default_spec


def auto_detect_specs(root_path: str = ".") -> dict:
    """
    Main entry point for spec auto-detection.
    
    Returns dict with:
        - detected: List of detected spec files
        - old_spec: Suggested old spec path
        - new_spec: Suggested new spec path  
        - message: User-friendly message
        - requires_input: Whether user must specify paths
    """
    detector = SpecDetector(root_path)
    specs, message = detector.detect_specs()
    old_spec, new_spec = detector.get_default_specs()
    
    return {
        "detected": specs,
        "old_spec": old_spec,
        "new_spec": new_spec,
        "message": message,
        "requires_input": len(specs) != 1
    }