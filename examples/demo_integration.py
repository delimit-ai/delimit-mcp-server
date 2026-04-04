#!/usr/bin/env python3
"""
Integration test for the complete Delimit governance loop.
"""

import yaml
from core.diff_engine_v2 import OpenAPIDiffEngine
from core.policy_engine import evaluate_with_policy
from core.ci_formatter import format_for_ci

# Create test specs
old_spec = {
    "openapi": "3.0.0",
    "info": {"title": "Test API", "version": "1.0.0"},
    "paths": {
        "/users": {
            "get": {
                "responses": {
                    "200": {
                        "description": "Success",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "required": ["id", "name"],
                                        "properties": {
                                            "id": {"type": "string"},
                                            "name": {"type": "string"},
                                            "email": {"type": "string"}
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
        "/users/{id}": {
            "get": {
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {"200": {"description": "Success"}}
            }
        }
    }
}

new_spec = {
    "openapi": "3.0.0",
    "info": {"title": "Test API", "version": "2.0.0"},
    "paths": {
        "/users": {
            "get": {
                "parameters": [
                    {"name": "limit", "in": "query", "required": True, "schema": {"type": "integer"}}  # Breaking: new required param
                ],
                "responses": {
                    "200": {
                        "description": "Success",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "required": ["id", "name", "created_at"],  # Breaking: new required field
                                        "properties": {
                                            "id": {"type": "integer"},  # Breaking: type change
                                            "name": {"type": "string"},
                                            # Breaking: email field removed
                                            "created_at": {"type": "string"}
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        # Breaking: /users/{id} endpoint removed
    }
}

def test_complete_flow():
    """Test the complete governance flow."""
    
    print("=" * 60)
    print("DELIMIT INTEGRATION TEST")
    print("=" * 60)
    
    # 1. Run diff engine
    print("\n1. Running Diff Engine...")
    diff_engine = OpenAPIDiffEngine()
    changes = diff_engine.compare(old_spec, new_spec)
    print(f"   Found {len(changes)} total changes")
    print(f"   Breaking changes: {len(diff_engine.get_breaking_changes())}")
    
    # 2. Apply policy
    print("\n2. Applying Policy Engine...")
    result = evaluate_with_policy(old_spec, new_spec, ".delimit/policies.yml")
    print(f"   Decision: {result['decision']}")
    print(f"   Violations: {result['summary']['violations']}")
    
    # 3. Format for CI
    print("\n3. Formatting for CI...")
    
    # Text output
    print("\n--- TEXT OUTPUT ---")
    text_output = format_for_ci(result, "text")
    print(text_output)
    
    # PR Comment
    print("\n--- PR COMMENT (MARKDOWN) ---")
    pr_output = format_for_ci(result, "pr_comment")
    print(pr_output)
    
    # GitHub Annotations
    print("\n--- GITHUB ANNOTATIONS ---")
    github_output = format_for_ci(result, "github")
    print(github_output)
    
    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)
    
    return result

if __name__ == "__main__":
    result = test_complete_flow()
    
    # Verify expected violations
    assert result["decision"] == "fail", "Should fail due to breaking changes"
    assert result["summary"]["violations"] > 0, "Should have violations"
    print("\n✅ All assertions passed!")