"""
CI Output Formatter - Creates clear, actionable output for developers.
Supports GitHub Actions annotations and PR comments.
"""

from typing import Dict, List, Any, Optional
from enum import Enum
import json

class OutputFormat(Enum):
    TEXT = "text"
    MARKDOWN = "markdown"
    GITHUB_ANNOTATION = "github_annotation"
    JSON = "json"

class CIFormatter:
    """Format Delimit output for different CI environments."""
    
    def __init__(self, format_type: OutputFormat = OutputFormat.TEXT):
        self.format_type = format_type
    
    def format_result(self, result: Dict[str, Any]) -> str:
        """Format the complete result based on output type."""
        if self.format_type == OutputFormat.JSON:
            return json.dumps(result, indent=2)
        elif self.format_type == OutputFormat.MARKDOWN:
            return self._format_markdown(result)
        elif self.format_type == OutputFormat.GITHUB_ANNOTATION:
            return self._format_github_annotations(result)
        else:
            return self._format_text(result)
    
    def _format_text(self, result: Dict[str, Any]) -> str:
        """Format as plain text for terminal output."""
        lines = []
        
        decision = result.get("decision", "unknown")
        violations = result.get("violations", [])
        summary = result.get("summary", {})
        
        # Header
        if decision == "fail":
            lines.append("❌ API Governance Check Failed")
        elif decision == "warn":
            lines.append("⚠️  API Governance Check Passed with Warnings")
        else:
            lines.append("✅ API Governance Check Passed")
        
        lines.append("=" * 50)
        
        # Summary
        if summary:
            lines.append(f"Total Changes: {summary.get('total_changes', 0)}")
            lines.append(f"Breaking Changes: {summary.get('breaking_changes', 0)}")
            lines.append(f"Policy Violations: {summary.get('violations', 0)}")
            lines.append("")
        
        # Violations
        if violations:
            lines.append("Violations Found:")
            lines.append("-" * 40)
            
            # Group by severity
            errors = [v for v in violations if v.get("severity") == "error"]
            warnings = [v for v in violations if v.get("severity") == "warning"]
            
            if errors:
                lines.append("\n🔴 ERRORS (Must Fix):")
                for v in errors:
                    lines.append(f"  • {v.get('message', 'Unknown violation')}")
                    if v.get("path"):
                        lines.append(f"    Location: {v['path']}")
            
            if warnings:
                lines.append("\n🟡 WARNINGS:")
                for v in warnings:
                    lines.append(f"  • {v.get('message', 'Unknown warning')}")
                    if v.get("path"):
                        lines.append(f"    Location: {v['path']}")
        
        # Remediation
        if violations and decision == "fail":
            lines.append("\n" + "=" * 50)
            lines.append("Suggested Fixes:")
            lines.append("1. Restore removed endpoints/fields")
            lines.append("2. Make new parameters optional")
            lines.append("3. Use API versioning (e.g., /v2/)")
            lines.append("4. Add deprecation notices before removing")
        
        return "\n".join(lines)
    
    def _format_markdown(self, result: Dict[str, Any]) -> str:
        """Format as Markdown for PR comments.

        Includes semver classification badge and migration guidance when
        the result carries semver/explainer data.
        """
        lines = []

        decision = result.get("decision", "unknown")
        violations = result.get("violations", [])
        summary = result.get("summary", {})
        semver = result.get("semver")  # optional dict from semver_classifier

        # Header — include semver badge when available
        bump_badge = ""
        if semver:
            bump = semver.get("bump", "unknown")
            bump_badge = {"major": " `MAJOR`", "minor": " `MINOR`", "patch": " `PATCH`", "none": ""}.get(bump, "")

        if decision == "fail":
            lines.append(f"## 🚨 Delimit: Breaking Changes{bump_badge}\n")
        elif decision == "warn":
            lines.append(f"## ⚠️ Delimit: Potential Issues{bump_badge}\n")
        else:
            lines.append(f"## ✅ API Changes Look Good{bump_badge}\n")

        # Semver + summary table
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        if semver:
            lines.append(f"| Semver bump | `{semver.get('bump', 'unknown')}` |")
            if semver.get("next_version"):
                lines.append(f"| Next version | `{semver['next_version']}` |")
        lines.append(f"| Total changes | {summary.get('total_changes', 0)} |")
        lines.append(f"| Breaking | {summary.get('breaking_changes', 0)} |")
        if summary.get("violations", 0) > 0:
            lines.append(f"| Policy violations | {summary['violations']} |")
        lines.append("")

        # Violations table
        if violations:
            errors = [v for v in violations if v.get("severity") == "error"]
            warnings = [v for v in violations if v.get("severity") == "warning"]

            if errors or warnings:
                lines.append("### Violations\n")
                lines.append("| Severity | Rule | Description | Location |")
                lines.append("|----------|------|-------------|----------|")

                for v in errors:
                    rule = v.get("name", v.get("rule", "Unknown"))
                    desc = v.get("message", "Unknown violation")
                    location = v.get("path", "-")
                    lines.append(f"| 🔴 **Error** | {rule} | {desc} | `{location}` |")

                for v in warnings:
                    rule = v.get("name", v.get("rule", "Unknown"))
                    desc = v.get("message", "Unknown warning")
                    location = v.get("path", "-")
                    lines.append(f"| 🟡 Warning | {rule} | {desc} | `{location}` |")

                lines.append("")

        # Detailed changes
        all_changes = result.get("all_changes", [])
        if all_changes and len(all_changes) <= 10:
            lines.append("<details>")
            lines.append("<summary>All changes</summary>\n")
            lines.append("```")
            for change in all_changes:
                breaking = "BREAKING" if change.get("is_breaking") else "safe"
                lines.append(f"[{breaking}] {change.get('message', 'Unknown change')}")
            lines.append("```")
            lines.append("</details>\n")

        # Migration guidance (from explainer) when available
        migration = result.get("migration")
        if migration and decision == "fail":
            lines.append("<details>")
            lines.append("<summary>Migration guide</summary>\n")
            lines.append(migration)
            lines.append("\n</details>\n")

        # Remediation
        if violations and decision == "fail" and not migration:
            lines.append("### 💡 How to Fix\n")
            lines.append("1. **Restore removed endpoints** — deprecate before removing")
            lines.append("2. **Make parameters optional** — don't add required params")
            lines.append("3. **Use versioning** — create `/v2/` for breaking changes")
            lines.append("4. **Gradual migration** — provide guides and time")
            lines.append("")

        lines.append("---")
        lines.append("*Generated by [Delimit](https://github.com/delimit-ai/delimit) — ESLint for API contracts*")

        return "\n".join(lines)
    
    def _format_github_annotations(self, result: Dict[str, Any]) -> str:
        """Format as GitHub Actions annotations."""
        annotations = []
        
        violations = result.get("violations", [])
        
        for v in violations:
            severity = v.get("severity", "warning")
            message = v.get("message", "Unknown violation")
            path = v.get("path", "")
            
            # GitHub annotation format
            if severity == "error":
                level = "error"
            elif severity == "warning":
                level = "warning"
            else:
                level = "notice"
            
            # Extract file and line if possible
            file = "openapi.yaml"  # Default, would need to map from path
            
            # GitHub annotation syntax
            annotation = f"::{level} file={file},title=API Governance::{message}"
            annotations.append(annotation)
        
        # Also output summary
        decision = result.get("decision", "unknown")
        summary = result.get("summary", {})
        
        if decision == "fail":
            annotations.append(f"::error::Delimit found {summary.get('violations', 0)} policy violations")
        elif decision == "warn":
            annotations.append(f"::warning::Delimit found {summary.get('violations', 0)} warnings")
        
        return "\n".join(annotations)


class PRCommentGenerator:
    """Generate PR comments for GitHub."""
    
    @staticmethod
    def generate_comment(result: Dict[str, Any], pr_number: Optional[int] = None) -> str:
        """Generate a complete PR comment."""
        formatter = CIFormatter(OutputFormat.MARKDOWN)
        content = formatter.format_result(result)
        
        # Add PR-specific header if PR number provided
        if pr_number:
            header = f"### Delimit Report for PR #{pr_number}\n\n"
            content = header + content
        
        return content
    
    @staticmethod
    def generate_inline_comment(violation: Dict[str, Any]) -> str:
        """Generate inline comment for specific line."""
        severity = violation.get("severity", "warning")
        message = violation.get("message", "Unknown issue")
        
        icon = "🔴" if severity == "error" else "⚠️"
        
        return f"{icon} **Delimit**: {message}"


def format_for_ci(result: Dict[str, Any], ci_environment: str = "github") -> str:
    """
    Main entry point for CI formatting.
    
    Args:
        result: The Delimit check result
        ci_environment: The CI platform (github, gitlab, jenkins, etc.)
    
    Returns:
        Formatted output string
    """
    if ci_environment == "github":
        # Use GitHub annotations for inline warnings
        formatter = CIFormatter(OutputFormat.GITHUB_ANNOTATION)
        annotations = formatter.format_result(result)
        
        # Also output readable summary
        formatter = CIFormatter(OutputFormat.TEXT)
        summary = formatter.format_result(result)
        
        return annotations + "\n\n" + summary
    
    elif ci_environment == "pr_comment":
        # Generate markdown for PR comment
        return PRCommentGenerator.generate_comment(result)
    
    else:
        # Default text output
        formatter = CIFormatter(OutputFormat.TEXT)
        return formatter.format_result(result)