"""
Surface Bridge for V12 Gateway Integration
Provides unified interface for CLI, MCP, and CI surfaces
"""

import sys
import json
from pathlib import Path
from typing import Dict, List, Optional, Any

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.gateway_v3 import delimit_run
from schemas.evidence import TaskEvidence


class SurfaceBridge:
    """
    Bridge between different surfaces (CLI, MCP, CI) and V12 gateway.
    Ensures all surfaces produce identical TaskEvidence outputs.
    """
    
    @staticmethod
    def execute_task(task: str, **kwargs) -> Dict[str, Any]:
        """
        Execute a governance task through the V12 gateway.
        
        Args:
            task: Task name (validate-api, check-policy, explain-diff)
            **kwargs: Task-specific parameters
            
        Returns:
            TaskEvidence as dictionary
        """
        return delimit_run(task, **kwargs)
    
    @staticmethod
    def validate_api(old_spec: str, new_spec: str, version: Optional[str] = None) -> Dict[str, Any]:
        """
        Validate API for breaking changes.
        
        Args:
            old_spec: Path to old API specification
            new_spec: Path to new API specification
            version: Task version (default: latest)
            
        Returns:
            APIChangeEvidence as dictionary
        """
        return delimit_run(
            "validate-api",
            files=[old_spec, new_spec],
            version=version
        )
    
    @staticmethod
    def check_policy(spec_files: List[str], 
                    policy_file: Optional[str] = None,
                    policy_inline: Optional[Dict] = None,
                    version: Optional[str] = None) -> Dict[str, Any]:
        """
        Check API specifications against policy rules.
        
        Args:
            spec_files: List of API specification files
            policy_file: Optional path to policy file
            policy_inline: Optional inline policy dict
            version: Task version (default: latest)
            
        Returns:
            PolicyComplianceEvidence as dictionary
        """
        return delimit_run(
            "check-policy",
            files=spec_files,
            policy_file=policy_file,
            policy_inline=policy_inline,
            version=version
        )
    
    @staticmethod
    def explain_diff(old_spec: str, 
                    new_spec: str,
                    detail_level: str = "medium",
                    version: Optional[str] = None) -> Dict[str, Any]:
        """
        Explain differences between two API specifications.
        
        Args:
            old_spec: Path to old API specification
            new_spec: Path to new API specification
            detail_level: Level of detail (summary, medium, detailed)
            version: Task version (default: latest)
            
        Returns:
            DiffExplanationEvidence as dictionary
        """
        return delimit_run(
            "explain-diff",
            files=[old_spec, new_spec],
            detail_level=detail_level,
            version=version
        )
    
    @staticmethod
    def format_for_cli(evidence: Dict[str, Any]) -> str:
        """
        Format TaskEvidence for CLI output.
        
        Args:
            evidence: TaskEvidence dictionary
            
        Returns:
            Formatted string for terminal display
        """
        output = []
        
        # Header
        decision = evidence.get("decision", "unknown")
        task = evidence.get("task", "unknown")
        
        # Color codes
        colors = {
            "pass": "\033[92m",  # Green
            "warn": "\033[93m",  # Yellow
            "fail": "\033[91m",  # Red
            "reset": "\033[0m",
            "bold": "\033[1m"
        }
        
        # Decision banner
        color = colors.get(decision, colors["reset"])
        output.append(f"{color}{colors['bold']}[{decision.upper()}]{colors['reset']} {task}")
        output.append("")
        
        # Summary
        if evidence.get("summary"):
            output.append(f"📊 {evidence['summary']}")
            output.append("")
        
        # Violations
        violations = evidence.get("violations", [])
        if violations:
            output.append(f"⚠️  Violations ({len(violations)}):")
            for v in violations[:5]:  # Show first 5
                severity = v.get("severity", "unknown")
                message = v.get("message", "")
                output.append(f"  • [{severity}] {message}")
            if len(violations) > 5:
                output.append(f"  ... and {len(violations) - 5} more")
            output.append("")
        
        # Remediation
        if evidence.get("remediation"):
            rem = evidence["remediation"]
            output.append("💡 Remediation:")
            output.append(f"  {rem.get('summary', '')}")
            for step in rem.get("steps", [])[:3]:
                output.append(f"  • {step}")
            output.append("")
        
        # Metrics
        if evidence.get("metrics"):
            output.append("📈 Metrics:")
            for key, value in evidence["metrics"].items():
                output.append(f"  • {key}: {value}")
        
        return "\n".join(output)
    
    @staticmethod
    def format_for_mcp(evidence: Dict[str, Any]) -> Dict[str, Any]:
        """
        Format TaskEvidence for MCP tool response.
        
        Args:
            evidence: TaskEvidence dictionary
            
        Returns:
            MCP-compatible response dictionary
        """
        # MCP expects specific format
        return {
            "success": evidence.get("exit_code", 1) == 0,
            "result": evidence,
            "message": evidence.get("summary", "Task completed")
        }
    
    @staticmethod
    def format_for_ci(evidence: Dict[str, Any]) -> Dict[str, Any]:
        """
        Format TaskEvidence for CI/CD systems (GitHub Actions, etc).
        
        Args:
            evidence: TaskEvidence dictionary
            
        Returns:
            CI-compatible response dictionary
        """
        # GitHub Actions format
        annotations = []
        for v in evidence.get("violations", []):
            level = "error" if v.get("severity") == "high" else "warning"
            annotations.append({
                "level": level,
                "message": v.get("message", ""),
                "file": v.get("path", ""),
                "title": v.get("rule", "")
            })
        
        return {
            "conclusion": evidence.get("decision", "fail"),
            "exit_code": evidence.get("exit_code", 1),
            "summary": evidence.get("summary", ""),
            "annotations": annotations,
            "evidence": evidence
        }
    
    @staticmethod
    def parse_cli_args(args: List[str]) -> tuple[str, Dict[str, Any]]:
        """
        Parse CLI arguments into task and parameters.
        
        Args:
            args: Command line arguments
            
        Returns:
            Tuple of (task_name, parameters)
        """
        if not args:
            raise ValueError("No task specified")
        
        task = args[0]
        params = {}
        
        # Parse remaining arguments
        i = 1
        while i < len(args):
            arg = args[i]
            if arg.startswith("--"):
                key = arg[2:]
                if i + 1 < len(args) and not args[i + 1].startswith("--"):
                    params[key] = args[i + 1]
                    i += 2
                else:
                    params[key] = True
                    i += 1
            else:
                # Positional argument
                if "files" not in params:
                    params["files"] = []
                params["files"].append(arg)
                i += 1
        
        return task, params


# Example usage functions for different surfaces
def cli_main(args: List[str]) -> int:
    """CLI entry point."""
    bridge = SurfaceBridge()
    
    try:
        task, params = bridge.parse_cli_args(args)
        evidence = bridge.execute_task(task, **params)
        print(bridge.format_for_cli(evidence))
        return evidence.get("exit_code", 0)
    except Exception as e:
        print(f"Error: {e}")
        return 1


def mcp_handler(task: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """MCP tool handler."""
    bridge = SurfaceBridge()
    
    try:
        evidence = bridge.execute_task(task, **params)
        return bridge.format_for_mcp(evidence)
    except Exception as e:
        return {
            "success": False,
            "result": None,
            "message": str(e)
        }


def ci_handler(task: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """CI/CD handler."""
    bridge = SurfaceBridge()
    
    try:
        evidence = bridge.execute_task(task, **params)
        return bridge.format_for_ci(evidence)
    except Exception as e:
        return {
            "conclusion": "fail",
            "exit_code": 1,
            "summary": f"Error: {e}",
            "annotations": [],
            "evidence": None
        }


if __name__ == "__main__":
    # CLI mode
    sys.exit(cli_main(sys.argv[1:]))