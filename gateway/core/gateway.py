import time
import logging
from typing import Any, Dict, List, Optional
from schemas.base import TaskRequest, TaskResponse, ErrorResponse, TaskStatus, ErrorDetails
from .registry import task_registry

logger = logging.getLogger(__name__)

class Gateway:
    """Main gateway implementing V10 architecture with advisor recommendations"""
    
    def __init__(self, max_file_size: int = 10 * 1024 * 1024, timeout: int = 30):
        self.registry = task_registry
        self.max_file_size = max_file_size
        self.timeout = timeout
        self._load_tasks()
    
    def _load_tasks(self):
        """Load all task modules to register handlers"""
        try:
            import tasks.validate_api
            import tasks.check_policy
            import tasks.explain_diff
        except ImportError as e:
            logger.warning(f"Could not load all tasks: {e}")
    
    def run(self, task: str, files: List[str], **kwargs) -> Dict[str, Any]:
        """Main entry point - the single gateway function"""
        start_time = time.time()
        
        # Build request with Codex's recommendation for strict typing
        try:
            request = TaskRequest(
                task=task,
                files=files,
                config=kwargs,
                correlation_id=kwargs.get("correlation_id"),
                version=kwargs.get("version", "v1")
            )
        except Exception as e:
            return self._error_response("invalid_request", str(e))
        
        # Check if task exists
        if not self.registry.has_task(task):
            return self._error_response(
                "unknown_task",
                f"Task '{task}' not recognized",
                available_tasks=self.registry.list_tasks()
            )
        
        # Get handler - use None for default version if v1 requested
        version_to_use = None if request.version == "v1" else request.version
        handler = self.registry.get_handler(task, version_to_use)
        if not handler:
            return self._error_response(
                "version_not_found",
                f"Version {request.version} not found for task '{task}'"
            )
        
        # Execute with timeout and error handling per Codex's guardrails
        try:
            # Validate file constraints
            for file_path in files:
                if not self._validate_file(file_path):
                    return self._error_response(
                        "file_validation_failed",
                        f"File validation failed for: {file_path}"
                    )
            
            # Execute task
            result = handler(request)
            
            # Build response with observability (Codex requirement #5)
            duration_ms = int((time.time() - start_time) * 1000)
            
            response = TaskResponse(
                status=TaskStatus.SUCCESS,
                task=task,
                result=result,
                duration_ms=duration_ms,
                correlation_id=request.correlation_id
            )
            
            logger.info(f"Task {task} completed in {duration_ms}ms")
            return response.model_dump(mode='json')
            
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(f"Task {task} failed after {duration_ms}ms: {e}")
            
            return TaskResponse(
                status=TaskStatus.ERROR,
                task=task,
                errors=[ErrorDetails(
                    code="execution_failed",
                    message=str(e),
                    retryable=True
                )],
                duration_ms=duration_ms,
                correlation_id=request.correlation_id
            ).model_dump(mode='json')
    
    def _validate_file(self, file_path: str) -> bool:
        """Validate file constraints"""
        try:
            import os
            if not os.path.exists(file_path):
                return False
            file_size = os.path.getsize(file_path)
            return file_size <= self.max_file_size
        except:
            return False
    
    def _error_response(self, code: str, message: str, **kwargs) -> Dict[str, Any]:
        """Build standardized error response (Codex requirement #2)"""
        return ErrorResponse(
            code=code,
            message=message,
            details=kwargs.get("details"),
            available_tasks=kwargs.get("available_tasks")
        ).model_dump(mode='json')

# Global gateway instance
gateway = Gateway()

def delimit_run(task: str, files: List[str], **kwargs) -> Dict[str, Any]:
    """The main gateway function - V10 architecture entry point"""
    return gateway.run(task, files, **kwargs)