"""
Hardened Gateway with Evidence Contract
V12 Core Hardening
"""

import time
import logging
from typing import Optional, Dict, Any
from pathlib import Path

from schemas.evidence import TaskEvidence, Decision
from schemas.requests import ValidateAPIRequest, CheckPolicyRequest, ExplainDiffRequest
from .registry_v2 import task_registry

logger = logging.getLogger(__name__)


class HardenedGateway:
    """
    V12 Hardened Gateway with strict evidence contract
    """
    
    def __init__(self):
        self.registry = task_registry
        self._load_tasks()
    
    def _load_tasks(self):
        """Load all task modules to register handlers"""
        try:
            # Import refactored tasks with evidence contract
            import tasks.validate_api_v2
            import tasks.check_policy_v2
            import tasks.explain_diff_v2
        except ImportError as e:
            logger.warning(f"Could not load all tasks: {e}")
    
    def run_validate_api(self, request: ValidateAPIRequest) -> TaskEvidence:
        """Execute validate-api task with typed request/response"""
        handler = self.registry.get_handler("validate-api", request.version)
        if not handler:
            raise ValueError(f"Task handler not found: validate-api:{request.version or 'latest'}")
        
        return handler(request)
    
    def run_check_policy(self, request: CheckPolicyRequest) -> TaskEvidence:
        """Execute check-policy task with typed request/response"""
        handler = self.registry.get_handler("check-policy", request.version)
        if not handler:
            raise ValueError(f"Task handler not found: check-policy:{request.version or 'latest'}")
        
        return handler(request)
    
    def run_explain_diff(self, request: ExplainDiffRequest) -> TaskEvidence:
        """Execute explain-diff task with typed request/response"""
        handler = self.registry.get_handler("explain-diff", request.version)
        if not handler:
            raise ValueError(f"Task handler not found: explain-diff:{request.version or 'latest'}")
        
        return handler(request)
    
    def run(self, task: str, **kwargs) -> Dict[str, Any]:
        """
        Main gateway entry point - maintains backward compatibility
        Returns Evidence Contract as dict
        """
        start_time = time.time()
        
        try:
            # Route to typed handlers based on task
            if task == "validate-api":
                request = ValidateAPIRequest(
                    task=task,
                    old_spec=kwargs.get("old_spec") or kwargs.get("files", [])[0],
                    new_spec=kwargs.get("new_spec") or kwargs.get("files", [])[1],
                    version=kwargs.get("version"),
                    correlation_id=kwargs.get("correlation_id")
                )
                evidence = self.run_validate_api(request)
                
            elif task == "check-policy":
                request = CheckPolicyRequest(
                    task=task,
                    spec_files=kwargs.get("spec_files") or kwargs.get("files", []),
                    policy_file=kwargs.get("policy_file"),
                    policy_inline=kwargs.get("policy_inline"),
                    version=kwargs.get("version"),
                    correlation_id=kwargs.get("correlation_id")
                )
                evidence = self.run_check_policy(request)
                
            elif task == "explain-diff":
                request = ExplainDiffRequest(
                    task=task,
                    old_spec=kwargs.get("old_spec") or kwargs.get("files", [])[0],
                    new_spec=kwargs.get("new_spec") or kwargs.get("files", [])[1],
                    detail_level=kwargs.get("detail_level", "medium"),
                    version=kwargs.get("version"),
                    correlation_id=kwargs.get("correlation_id")
                )
                evidence = self.run_explain_diff(request)
                
            else:
                # Unknown task - return error evidence
                return {
                    "task": task,
                    "task_version": "unknown",
                    "decision": "fail",
                    "exit_code": 1,
                    "summary": f"Unknown task: {task}",
                    "violations": [{
                        "rule": "task_exists",
                        "severity": "high",
                        "message": f"Task '{task}' not found"
                    }]
                }
            
            # Add timing
            duration_ms = int((time.time() - start_time) * 1000)
            evidence_dict = evidence.model_dump(mode='json')
            evidence_dict["duration_ms"] = duration_ms
            
            return evidence_dict
            
        except Exception as e:
            logger.error(f"Task execution failed: {e}")
            
            # Return error evidence
            return {
                "task": task,
                "task_version": "error",
                "decision": "fail",
                "exit_code": 1,
                "summary": f"Execution failed: {str(e)}",
                "violations": [{
                    "rule": "execution",
                    "severity": "high",
                    "message": str(e)
                }],
                "duration_ms": int((time.time() - start_time) * 1000)
            }


# Global instance
gateway = HardenedGateway()


def delimit_run(task: str, files: list = None, **kwargs) -> Dict[str, Any]:
    """
    Main entry point maintaining backward compatibility
    Returns Evidence Contract as dictionary
    """
    if files:
        kwargs["files"] = files
    return gateway.run(task, **kwargs)