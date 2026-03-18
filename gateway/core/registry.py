from typing import Callable, Dict, Optional
from functools import wraps
import logging

logger = logging.getLogger(__name__)

class TaskRegistry:
    """Registry for task handlers following Gemini's recommendation"""
    
    def __init__(self):
        self._tasks: Dict[str, Callable] = {}
        self._task_metadata: Dict[str, Dict] = {}
    
    def register(self, task_name: str, version: str = "v1", **metadata):
        """Decorator to register a task handler"""
        def decorator(func: Callable):
            full_name = f"{task_name}:{version}" if version != "v1" else task_name
            
            @wraps(func)
            def wrapper(*args, **kwargs):
                return func(*args, **kwargs)
            
            self._tasks[full_name] = wrapper
            self._tasks[task_name] = wrapper  # Default version alias
            self._task_metadata[full_name] = {
                "name": task_name,
                "version": version,
                "handler": func.__name__,
                **metadata
            }
            
            logger.info(f"Registered task: {full_name}")
            return wrapper
        return decorator
    
    def get_handler(self, task_name: str, version: Optional[str] = None) -> Optional[Callable]:
        """Get a task handler by name and optional version"""
        if version:
            full_name = f"{task_name}:{version}"
            return self._tasks.get(full_name)
        return self._tasks.get(task_name)
    
    def list_tasks(self) -> list:
        """List all registered tasks"""
        return list(self._tasks.keys())
    
    def has_task(self, task_name: str) -> bool:
        """Check if a task is registered"""
        return task_name in self._tasks

# Global registry instance
task_registry = TaskRegistry()