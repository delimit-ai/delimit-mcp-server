"""
Delimit Policy Engine - Define and enforce custom governance rules.
Organizations can define policies to control API evolution.
"""

import yaml
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from core.diff_engine_v2 import OpenAPIDiffEngine, Change, ChangeType

class RuleSeverity(Enum):
    ERROR = "error"      # Fails CI
    WARNING = "warning"  # Shows warning but passes
    INFO = "info"        # Informational only

class RuleAction(Enum):
    FORBID = "forbid"    # Forbids the change
    ALLOW = "allow"      # Explicitly allows
    WARN = "warn"        # Warns but allows

@dataclass
class PolicyRule:
    """A single governance rule."""
    id: str
    name: str
    description: str
    change_types: List[ChangeType]
    severity: RuleSeverity
    action: RuleAction
    conditions: Optional[Dict] = None
    message_template: Optional[str] = None
    
    def evaluate(self, change: Change) -> Optional['Violation']:
        """Evaluate if this rule applies to a change."""
        if change.type not in self.change_types:
            return None
        
        # Check additional conditions if specified
        if self.conditions:
            if not self._check_conditions(change, self.conditions):
                return None
        
        # Create violation if rule matches
        if self.action == RuleAction.FORBID:
            return Violation(
                rule_id=self.id,
                rule_name=self.name,
                severity=self.severity.value,
                message=self._format_message(change),
                change=change
            )
        elif self.action == RuleAction.WARN:
            return Violation(
                rule_id=self.id,
                rule_name=self.name,
                severity=RuleSeverity.WARNING.value,
                message=self._format_message(change),
                change=change
            )
        
        return None
    
    def _check_conditions(self, change: Change, conditions: Dict) -> bool:
        """Check if change meets additional conditions."""
        # Example conditions: path patterns, specific fields, etc.
        if "path_pattern" in conditions:
            import re
            pattern = conditions["path_pattern"]
            if not re.match(pattern, change.path):
                return False
        
        if "exclude_paths" in conditions:
            for excluded in conditions["exclude_paths"]:
                if excluded in change.path:
                    return False
        
        return True
    
    def _format_message(self, change: Change) -> str:
        """Format violation message."""
        if self.message_template:
            return self.message_template.format(
                path=change.path,
                details=change.details,
                message=change.message
            )
        return f"{self.name}: {change.message}"

@dataclass
class Violation:
    """A policy violation."""
    rule_id: str
    rule_name: str
    severity: str
    message: str
    change: Change

# Available policy presets
POLICY_PRESETS = ("strict", "default", "relaxed")
_PRESETS_DIR = Path(__file__).resolve().parent / "policies"


class PolicyEngine:
    """Main policy engine for evaluating governance rules."""

    # Default rules that are always active unless overridden
    DEFAULT_RULES = [
        PolicyRule(
            id="no_endpoint_removal",
            name="Forbid Endpoint Removal",
            description="Endpoints cannot be removed without deprecation",
            change_types=[ChangeType.ENDPOINT_REMOVED],
            severity=RuleSeverity.ERROR,
            action=RuleAction.FORBID,
            message_template="Endpoint {path} cannot be removed. Deprecate it first."
        ),
        PolicyRule(
            id="no_method_removal",
            name="Forbid Method Removal",
            description="HTTP methods cannot be removed from endpoints",
            change_types=[ChangeType.METHOD_REMOVED],
            severity=RuleSeverity.ERROR,
            action=RuleAction.FORBID,
            message_template="Method {details[method]} cannot be removed from {details[endpoint]}"
        ),
        PolicyRule(
            id="no_required_param_addition",
            name="Forbid Required Parameter Addition",
            description="New required parameters break existing clients",
            change_types=[ChangeType.REQUIRED_PARAM_ADDED],
            severity=RuleSeverity.ERROR,
            action=RuleAction.FORBID,
            message_template="Cannot add required parameter {details[parameter]} to {path}"
        ),
        PolicyRule(
            id="no_response_field_removal",
            name="Forbid Response Field Removal",
            description="Removing response fields breaks clients",
            change_types=[ChangeType.FIELD_REMOVED],
            severity=RuleSeverity.ERROR,
            action=RuleAction.FORBID,
            conditions={"path_pattern": ".*:2\\d\\d.*"},  # Only 2xx responses
            message_template="Cannot remove field {details[field]} from response"
        ),
        PolicyRule(
            id="warn_type_change",
            name="Warn on Type Changes",
            description="Type changes may break clients",
            change_types=[ChangeType.TYPE_CHANGED],
            severity=RuleSeverity.WARNING,
            action=RuleAction.WARN,
            message_template="Type changed from {details[old_type]} to {details[new_type]} at {path}"
        ),
        PolicyRule(
            id="allow_enum_expansion",
            name="Allow Enum Expansion",
            description="Adding enum values is safe",
            change_types=[ChangeType.ENUM_VALUE_ADDED],
            severity=RuleSeverity.INFO,
            action=RuleAction.ALLOW,
            message_template="Enum value {details[value]} added (non-breaking)"
        )
    ]
    
    def __init__(self, policy_file: Optional[str] = None):
        """Initialize policy engine with optional custom policy file or preset name.

        policy_file can be:
          - A file path to a YAML policy file
          - A preset name: "strict", "default", or "relaxed"
        """
        self.rules: List[PolicyRule] = []
        self.custom_rules: List[PolicyRule] = []

        # Load default rules
        self.rules.extend(self.DEFAULT_RULES)

        # Load custom policy if provided
        if policy_file:
            # Check if it's a preset name
            if policy_file in POLICY_PRESETS:
                preset_path = _PRESETS_DIR / f"{policy_file}.yml"
                if preset_path.exists():
                    self.load_policy(str(preset_path))
                # "default" preset = built-in defaults, no-op
            else:
                self.load_policy(policy_file)
    
    def load_policy(self, policy_file: str):
        """Load custom policy from YAML file."""
        path = Path(policy_file)
        
        # Check common locations if file not found
        if not path.exists():
            for location in [".delimit/policies.yml", ".delimit/policy.yaml", "delimit.yml"]:
                test_path = Path(location)
                if test_path.exists():
                    path = test_path
                    break
        
        if not path.exists():
            return  # No custom policy, use defaults
        
        with open(path, 'r') as f:
            config = yaml.safe_load(f)
        
        if not config:
            return
        
        # Parse rules
        for rule_config in config.get("rules", []):
            rule = self._parse_rule(rule_config)
            if rule:
                self.custom_rules.append(rule)
        
        # Override defaults if specified
        if config.get("override_defaults", False):
            self.rules = self.custom_rules
        else:
            # Merge custom rules with defaults
            # Custom rules take precedence for same IDs
            custom_ids = {r.id for r in self.custom_rules}
            self.rules = self.custom_rules + [r for r in self.DEFAULT_RULES if r.id not in custom_ids]
    
    def _parse_rule(self, config: Dict) -> Optional[PolicyRule]:
        """Parse a rule from configuration."""
        try:
            # Map string change types to enum
            change_type_map = {
                "endpoint_removed": ChangeType.ENDPOINT_REMOVED,
                "method_removed": ChangeType.METHOD_REMOVED,
                "required_param_added": ChangeType.REQUIRED_PARAM_ADDED,
                "param_removed": ChangeType.PARAM_REMOVED,
                "field_removed": ChangeType.FIELD_REMOVED,
                "type_changed": ChangeType.TYPE_CHANGED,
                "enum_value_removed": ChangeType.ENUM_VALUE_REMOVED,
            }
            
            change_types = []
            for ct in config.get("change_types", []):
                if ct in change_type_map:
                    change_types.append(change_type_map[ct])
            
            if not change_types:
                return None
            
            return PolicyRule(
                id=config["id"],
                name=config.get("name", config["id"]),
                description=config.get("description", ""),
                change_types=change_types,
                severity=RuleSeverity(config.get("severity", "error")),
                action=RuleAction(config.get("action", "forbid")),
                conditions=config.get("conditions"),
                message_template=config.get("message")
            )
        except Exception as e:
            print(f"Warning: Failed to parse rule {config.get('id', 'unknown')}: {e}")
            return None
    
    def evaluate(self, changes: List[Change]) -> List[Violation]:
        """Evaluate all changes against policy rules."""
        violations = []
        
        for change in changes:
            for rule in self.rules:
                violation = rule.evaluate(change)
                if violation:
                    violations.append(violation)
        
        return violations
    
    def create_policy_template(self) -> str:
        """Generate a template policy file."""
        template = """# Delimit Policy Configuration
# Define custom governance rules for your API

# Override default rules completely (default: false)
override_defaults: false

# Custom governance rules
rules:
  # Forbid removing any endpoint
  - id: no_endpoint_removal
    name: Forbid Endpoint Removal
    description: Endpoints cannot be removed without deprecation
    change_types:
      - endpoint_removed
    severity: error  # error | warning | info
    action: forbid   # forbid | allow | warn
    message: "Endpoint {path} cannot be removed. Use deprecation headers instead."
  
  # Forbid type changes in responses
  - id: no_response_type_change
    name: Forbid Response Type Changes
    change_types:
      - type_changed
    severity: error
    action: forbid
    conditions:
      path_pattern: ".*:2\\d\\d.*"  # Only 2xx responses
    message: "Type change not allowed in {path}"
  
  # Allow adding optional fields
  - id: allow_optional_fields
    name: Allow Optional Field Addition
    change_types:
      - optional_field_added
    severity: info
    action: allow
    message: "Optional field added (safe change)"
  
  # Warn about enum changes
  - id: warn_enum_removal
    name: Warn on Enum Value Removal
    change_types:
      - enum_value_removed
    severity: warning
    action: warn
    message: "Enum value removed - may break clients using this value"
  
  # Custom rule for specific paths
  - id: protect_v1_api
    name: Protect V1 API
    description: V1 endpoints are frozen
    change_types:
      - endpoint_removed
      - method_removed
      - field_removed
    severity: error
    action: forbid
    conditions:
      path_pattern: "^/v1/.*"
    message: "V1 API is frozen. Changes must be made in V2."

# Organization-specific rules
organization:
  # Require approval for high-risk changes
  require_approval:
    - endpoint_removed
    - method_removed
    - required_param_added
  
  # Deprecation policy
  deprecation:
    min_notice_days: 30
    require_sunset_header: true
"""
        return template


def evaluate_with_policy(
    old_spec: Dict,
    new_spec: Dict,
    policy_file: Optional[str] = None,
    include_semver: bool = False,
    current_version: Optional[str] = None,
    api_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Main entry point for policy evaluation.

    Args:
        old_spec: Old OpenAPI spec dict.
        new_spec: New OpenAPI spec dict.
        policy_file: Optional custom policy file.
        include_semver: Attach semver classification + migration guide.
        current_version: Current version for next-version computation.
        api_name: API name for explainer context.

    Returns:
        Dictionary with violations, summary, decision, and optionally
        semver/migration data for PR comment rendering.
    """
    # Run diff engine
    diff_engine = OpenAPIDiffEngine()
    changes = diff_engine.compare(old_spec, new_spec)

    # Run policy engine
    policy_engine = PolicyEngine(policy_file)
    violations = policy_engine.evaluate(changes)

    # Determine decision
    has_errors = any(v.severity == "error" for v in violations)
    has_warnings = any(v.severity == "warning" for v in violations)

    if has_errors:
        decision = "fail"
        exit_code = 1
    elif has_warnings:
        decision = "warn"
        exit_code = 0
    else:
        decision = "pass"
        exit_code = 0

    result: Dict[str, Any] = {
        "decision": decision,
        "exit_code": exit_code,
        "violations": [
            {
                "rule": v.rule_id,
                "name": v.rule_name,
                "severity": v.severity,
                "message": v.message,
                "path": v.change.path,
                "details": v.change.details
            }
            for v in violations
        ],
        "summary": {
            "total_changes": len(changes),
            "breaking_changes": len([c for c in changes if c.is_breaking]),
            "violations": len(violations),
            "errors": len([v for v in violations if v.severity == "error"]),
            "warnings": len([v for v in violations if v.severity == "warning"])
        },
        "all_changes": [
            {
                "type": c.type.value,
                "path": c.path,
                "message": c.message,
                "is_breaking": c.is_breaking
            }
            for c in changes
        ],
    }

    # Attach semver + migration for PR comment rendering
    if include_semver and changes:
        try:
            from core.semver_classifier import classify_detailed, bump_version, classify
            from core.explainer import explain

            detail = classify_detailed(changes)
            semver_data = {
                "bump": detail["bump"],
                "counts": detail["counts"],
            }
            if current_version:
                semver_data["current_version"] = current_version
                semver_data["next_version"] = bump_version(
                    current_version, classify(changes)
                )
            result["semver"] = semver_data

            # Generate migration guide for breaking changes
            if detail["counts"]["breaking"] > 0:
                old_ver = current_version or old_spec.get("info", {}).get("version")
                new_ver = new_spec.get("info", {}).get("version")
                result["migration"] = explain(
                    changes,
                    template="migration",
                    old_version=old_ver,
                    new_version=new_ver,
                    api_name=api_name,
                )
        except Exception:
            pass  # semver/explainer not available — degrade gracefully

    return result