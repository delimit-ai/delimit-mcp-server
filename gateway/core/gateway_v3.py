"""
Hardened Gateway with Evidence Contract - Final Version
V12 Complete Implementation
"""

import time
import logging
from typing import Optional, Dict
from pathlib import Path

from schemas.evidence import TaskEvidence, Decision, Violation, ViolationSeverity, Remediation
from schemas.requests_v2 import ValidateAPIRequest, CheckPolicyRequest, ExplainDiffRequest
from .registry_v3 import task_registry

logger = logging.getLogger(__name__)


class HardenedGateway:
    """
    V12 Hardened Gateway with strict evidence contract
    All paths return TaskEvidence
    """
    
    def __init__(self):
        self.registry = task_registry
        self._load_tasks()
    
    def _load_tasks(self):
        """Load all task modules to register handlers"""
        try:
            # Import refactored tasks with evidence contract
            import tasks.validate_api_v3
            import tasks.check_policy_v3
            import tasks.explain_diff_v2
        except ImportError as e:
            logger.warning(f"Could not load all tasks: {e}")
    
    def run_validate_api(self, request: ValidateAPIRequest) -> TaskEvidence:
        """Execute validate-api task with typed request/response"""
        handler = self.registry.get_handler("validate-api", request.version)
        if not handler:
            # Return error evidence instead of raising
            return self._create_error_evidence(
                "validate-api",
                f"Task handler not found: validate-api:{request.version or 'latest'}",
                request.correlation_id
            )
        
        try:
            return handler(request)
        except Exception as e:
            return self._create_error_evidence("validate-api", str(e), request.correlation_id)
    
    def run_check_policy(self, request: CheckPolicyRequest) -> TaskEvidence:
        """Execute check-policy task with typed request/response"""
        handler = self.registry.get_handler("check-policy", request.version)
        if not handler:
            return self._create_error_evidence(
                "check-policy",
                f"Task handler not found: check-policy:{request.version or 'latest'}",
                request.correlation_id
            )
        
        try:
            return handler(request)
        except Exception as e:
            return self._create_error_evidence("check-policy", str(e), request.correlation_id)
    
    def run_explain_diff(self, request: ExplainDiffRequest) -> TaskEvidence:
        """Execute explain-diff task with typed request/response"""
        handler = self.registry.get_handler("explain-diff", request.version)
        if not handler:
            return self._create_error_evidence(
                "explain-diff",
                f"Task handler not found: explain-diff:{request.version or 'latest'}",
                request.correlation_id
            )
        
        try:
            return handler(request)
        except Exception as e:
            return self._create_error_evidence("explain-diff", str(e), request.correlation_id)
    
    def _create_error_evidence(self, task: str, error_message: str, correlation_id: Optional[str] = None) -> TaskEvidence:
        """Create proper TaskEvidence for errors - never return raw dicts"""
        return TaskEvidence(
            task=task,
            task_version="error",
            decision=Decision.FAIL,
            exit_code=1,
            violations=[
                Violation(
                    rule="execution_error",
                    severity=ViolationSeverity.HIGH,
                    message=error_message,
                    details={"error_type": "execution_failure"}
                )
            ],
            evidence=[],
            remediation=Remediation(
                summary="Task execution failed",
                steps=["Check input parameters", "Verify file paths exist", "Review error message"],
                documentation="https://docs.delimit.ai/troubleshooting"
            ),
            summary=f"Task execution failed: {error_message}",
            correlation_id=correlation_id,
            metrics={}
        )
    
    def run(self, task: str, **kwargs) -> Dict[str, str]:
        """
        Main gateway entry point - maintains backward compatibility
        Returns Evidence Contract as dict
        ALL PATHS RETURN TaskEvidence
        """
        start_time = time.time()
        correlation_id = kwargs.get("correlation_id")
        
        try:
            # Route to typed handlers based on task
            if task == "validate-api":
                # Handle both old and new parameter styles
                files = kwargs.get("files", [])
                old_spec = kwargs.get("old_spec") or (files[0] if len(files) > 0 else None)
                new_spec = kwargs.get("new_spec") or (files[1] if len(files) > 1 else None)
                
                if not old_spec or not new_spec:
                    evidence = self._create_error_evidence(
                        task,
                        "validate-api requires two files: old_spec and new_spec",
                        correlation_id
                    )
                else:
                    request = ValidateAPIRequest(
                        task=task,
                        old_spec=old_spec,
                        new_spec=new_spec,
                        version=kwargs.get("version"),
                        correlation_id=correlation_id
                    )
                    evidence = self.run_validate_api(request)
                
            elif task == "check-policy":
                files = kwargs.get("spec_files") or kwargs.get("files", [])
                
                if not files:
                    evidence = self._create_error_evidence(
                        task,
                        "check-policy requires at least one spec file",
                        correlation_id
                    )
                else:
                    request = CheckPolicyRequest(
                        task=task,
                        spec_files=files,
                        policy_file=kwargs.get("policy_file"),
                        policy_inline=kwargs.get("policy_inline"),
                        version=kwargs.get("version"),
                        correlation_id=correlation_id
                    )
                    evidence = self.run_check_policy(request)
                
            elif task == "explain-diff":
                files = kwargs.get("files", [])
                old_spec = kwargs.get("old_spec") or (files[0] if len(files) > 0 else None)
                new_spec = kwargs.get("new_spec") or (files[1] if len(files) > 1 else None)
                
                if not old_spec or not new_spec:
                    evidence = self._create_error_evidence(
                        task,
                        "explain-diff requires two files: old_spec and new_spec",
                        correlation_id
                    )
                else:
                    request = ExplainDiffRequest(
                        task=task,
                        old_spec=old_spec,
                        new_spec=new_spec,
                        detail_level=kwargs.get("detail_level", "medium"),
                        version=kwargs.get("version"),
                        correlation_id=correlation_id
                    )
                    evidence = self.run_explain_diff(request)
                
            else:
                # Unknown task - return error evidence
                evidence = self._create_error_evidence(
                    task,
                    f"Unknown task: {task}. Available tasks: validate-api, check-policy, explain-diff",
                    correlation_id
                )
            
            # Add timing
            duration_ms = int((time.time() - start_time) * 1000)
            evidence_dict = evidence.model_dump(mode='json')
            evidence_dict["duration_ms"] = duration_ms
            
            return evidence_dict
            
        except Exception as e:
            logger.error(f"Task execution failed: {e}")
            
            # Always return TaskEvidence, never raw dict
            error_evidence = self._create_error_evidence(task, str(e), correlation_id)
            duration_ms = int((time.time() - start_time) * 1000)
            evidence_dict = error_evidence.model_dump(mode='json')
            evidence_dict["duration_ms"] = duration_ms
            
            return evidence_dict


# Global instance
gateway = HardenedGateway()


def delimit_run(task: str, files: list = None, **kwargs) -> Dict[str, str]:
    """
    Main entry point maintaining backward compatibility
    Returns Evidence Contract as dictionary
    ALL PATHS RETURN VALID TaskEvidence
    """
    if files:
        kwargs["files"] = files
    return gateway.run(task, **kwargs)