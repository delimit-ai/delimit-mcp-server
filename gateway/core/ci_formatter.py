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
        semver = result.get("semver")
        all_changes = result.get("all_changes", [])
        migration = result.get("migration")

        bc = summary.get("breaking_changes", 0)
        total = summary.get("total_changes", 0)
        additive = total - bc

        errors = [v for v in violations if v.get("severity") == "error"]
        warnings = [v for v in violations if v.get("severity") == "warning"]

        if bc == 0:
            # ── GREEN PATH ──
            bump_label = "NONE"
            if semver:
                bump_label = semver.get("bump", "none").upper()
            lines.append("\U0001f6e1\ufe0f **Governance Passed**\n")
            if total > 0:
                lines.append(
                    f"> **No breaking API changes detected.** "
                    f"{additive} additive change{'s' if additive != 1 else ''} "
                    f"found \u2014 Semver: **{bump_label}**\n"
                )
            else:
                lines.append("> **No breaking API changes detected.**\n")

            # Additive changes
            safe_changes = [c for c in all_changes if not c.get("is_breaking")]
            if safe_changes and len(safe_changes) <= 15:
                lines.append("<details>")
                lines.append(f"<summary>\u2705 New additions ({len(safe_changes)})</summary>\n")
                for c in safe_changes:
                    lines.append(f"- `{c.get('path', '')}` \u2014 {c.get('message', '')}")
                lines.append("</details>\n")
        else:
            # ── RED PATH ──
            lines.append("\U0001f6e1\ufe0f **Breaking API Changes Detected**\n")

            # Summary card
            parts = [f"\U0001f534 **{bc} breaking change{'s' if bc != 1 else ''}**"]
            parts.append("Semver: **MAJOR**")
            if semver and semver.get("next_version"):
                parts.append(f"Next: `{semver['next_version']}`")
            separator = " \u00b7 "
            lines.append(f"> {separator.join(parts)}\n")

            # Stats table
            lines.append("| | Count |")
            lines.append("|---|---|")
            lines.append(f"| Total changes | {total} |")
            lines.append(f"| Breaking | {bc} |")
            lines.append(f"| Additive | {additive} |")
            if len(warnings) > 0:
                lines.append(f"| Warnings | {len(warnings)} |")
            if summary.get("violations", 0) > 0:
                lines.append(f"| Policy violations | {summary['violations']} |")
            lines.append("")

            # Violations table
            if errors or warnings:
                lines.append("### Breaking Changes\n")
                lines.append("| Severity | Change | Location |")
                lines.append("|----------|--------|----------|")

                for v in errors:
                    desc = v.get("message", "Unknown violation")
                    location = v.get("path", "-")
                    lines.append(f"| \U0001f534 Critical | {desc} | `{location}` |")

                for v in warnings:
                    desc = v.get("message", "Unknown warning")
                    location = v.get("path", "-")
                    lines.append(f"| \U0001f7e1 Warning | {desc} | `{location}` |")

                lines.append("")

            # Migration guidance
            if migration and decision == "fail":
                lines.append("<details>")
                lines.append("<summary>\U0001f4cb Migration guide</summary>\n")
                lines.append(migration)
                lines.append("\n</details>\n")
            elif errors and decision == "fail":
                lines.append("<details>")
                lines.append("<summary>\U0001f4cb Migration guide</summary>\n")
                lines.append("1. **Restore removed endpoints** \u2014 deprecate before removing")
                lines.append("2. **Make parameters optional** \u2014 don't add required params")
                lines.append("3. **Use versioning** \u2014 create `/v2/` for breaking changes")
                lines.append("4. **Gradual migration** \u2014 provide guides and time")
                lines.append("\n</details>\n")

            # Additive changes
            safe_changes = [c for c in all_changes if not c.get("is_breaking")]
            if safe_changes and len(safe_changes) <= 15:
                lines.append("<details>")
                lines.append(f"<summary>\u2705 New additions ({len(safe_changes)})</summary>\n")
                for c in safe_changes:
                    lines.append(f"- `{c.get('path', '')}` \u2014 {c.get('message', '')}")
                lines.append("</details>\n")

            lines.append("> **Fix locally:** `npx delimit-cli lint`\n")

        lines.append("---")
        lines.append(
            "Powered by [Delimit](https://delimit.ai) \u00b7 "
            "[Docs](https://delimit.ai/docs) \u00b7 "
            "[Install](https://github.com/marketplace/actions/delimit-api-governance)"
        )

        if bc == 0:
            lines.append("\nKeep Building.")

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