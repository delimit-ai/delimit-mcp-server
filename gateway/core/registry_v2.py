"""
Fixed Task Registry with proper versioning support
V12 Core Hardening
"""

from typing import Callable, Dict, Optional, List, Tuple
from functools import wraps
import logging
from packaging import version

logger = logging.getLogger(__name__)


class TaskRegistry:
    """
    Registry for task handlers with proper versioning
    Key format: task_name:version (e.g., "validate-api:1.0")
    """
    
    def __init__(self):
        self._tasks: Dict[str, Callable] = {}
        self._task_metadata: Dict[str, Dict] = {}
        self._latest_versions: Dict[str, str] = {}  # task_name -> latest_version
    
    def register(self, task_name: str, version: str, **metadata):
        """
        Decorator to register a task handler with explicit version
        
        Args:
            task_name: Name of the task (e.g., "validate-api")
            version: Version string (e.g., "1.0", "2.0")
            **metadata: Additional metadata (description, etc.)
        """
        def decorator(func: Callable):
            # Create versioned key
            task_key = f"{task_name}:{version}"
            
            @wraps(func)
            def wrapper(*args, **kwargs):
                return func(*args, **kwargs)
            
            # Store handler with versioned key
            self._tasks[task_key] = wrapper
            
            # Store metadata
            self._task_metadata[task_key] = {
                "name": task_name,
                "version": version,
                "handler": func.__name__,
                **metadata
            }
            
            # Update latest version tracking
            if task_name not in self._latest_versions:
                self._latest_versions[task_name] = version
            else:
                # Compare versions properly
                try:
                    current = version.parse(self._latest_versions[task_name])
                    new = version.parse(version)
                    if new > current:
                        self._latest_versions[task_name] = version
                except:
                    # Fallback to string comparison if not semantic versioning
                    if version > self._latest_versions[task_name]:
                        self._latest_versions[task_name] = version
            
            logger.info(f"Registered task: {task_key}")
            return wrapper
        return decorator
    
    def get_handler(self, task_name: str, version: Optional[str] = None) -> Optional[Callable]:
        """
        Get a task handler by name and optional version
        
        Args:
            task_name: Task name (e.g., "validate-api")
            version: Optional version (e.g., "1.0"). If None, returns latest.
        
        Returns:
            Task handler callable or None if not found
        """
        if version:
            # Explicit version requested
            task_key = f"{task_name}:{version}"
            return self._tasks.get(task_key)
        else:
            # No version specified, return latest
            if task_name in self._latest_versions:
                latest_version = self._latest_versions[task_name]
                task_key = f"{task_name}:{latest_version}"
                return self._tasks.get(task_key)
            return None
    
    def list_tasks(self) -> List[str]:
        """List all registered task keys (with versions)"""
        return sorted(self._tasks.keys())
    
    def list_task_names(self) -> List[str]:
        """List unique task names (without versions)"""
        return sorted(set(self._latest_versions.keys()))
    
    def get_task_versions(self, task_name: str) -> List[str]:
        """Get all registered versions for a task"""
        versions = []
        for key in self._tasks.keys():
            if key.startswith(f"{task_name}:"):
                version = key.split(":", 1)[1]
                versions.append(version)
        return sorted(versions)
    
    def has_task(self, task_name: str, version: Optional[str] = None) -> bool:
        """Check if a task is registered"""
        if version:
            task_key = f"{task_name}:{version}"
            return task_key in self._tasks
        else:
            return task_name in self._latest_versions
    
    def get_metadata(self, task_name: str, version: Optional[str] = None) -> Optional[Dict]:
        """Get metadata for a task"""
        if version:
            task_key = f"{task_name}:{version}"
        elif task_name in self._latest_versions:
            task_key = f"{task_name}:{self._latest_versions[task_name]}"
        else:
            return None
        return self._task_metadata.get(task_key)


# Global registry instance
task_registry = TaskRegistry()