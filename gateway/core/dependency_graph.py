"""
Delimit Dependency Graph
Constructs a deterministic service dependency graph from manifests.

The graph maps each API/service to its downstream consumers,
enabling impact analysis when an API contract changes.

Per Jamsons Doctrine:
- Deterministic outputs (sorted, reproducible)
- No telemetry
- Graceful degradation when manifests are missing
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

from .dependency_manifest import discover_manifests, parse_manifest

logger = logging.getLogger("delimit.dependency_graph")


class DependencyGraph:
    """Service dependency graph for API impact analysis.

    The graph tracks two relationships:
    - consumers: api_name -> [services that consume this API]
    - producers: service_name -> [APIs this service produces]
    """

    def __init__(self):
        # api_name -> sorted list of consuming service names
        self._consumers: Dict[str, Set[str]] = {}
        # service_name -> sorted list of APIs it produces
        self._producers: Dict[str, Set[str]] = {}
        # service_name -> sorted list of APIs it consumes
        self._consumes: Dict[str, Set[str]] = {}
        # All known service names
        self._services: Set[str] = set()

    def add_manifest(self, manifest: Dict[str, Any]) -> None:
        """Add a single parsed manifest to the graph.

        Args:
            manifest: Parsed and normalized manifest dictionary.
        """
        service = manifest.get("service")
        if not isinstance(service, str) or not service:
            logger.warning("Skipping manifest with invalid service: %r", service)
            return

        self._services.add(service)

        # Track what this service consumes — validate list of strings
        consumes = manifest.get("consumes", [])
        if not isinstance(consumes, list):
            logger.warning("Manifest %s has non-list consumes, skipping", service)
            consumes = []

        for api in consumes:
            if not isinstance(api, str) or not api:
                continue
            self._consumes.setdefault(service, set()).add(api)
            self._consumers.setdefault(api, set()).add(service)

        # Track what this service produces — validate list of strings
        produces = manifest.get("produces", [])
        if not isinstance(produces, list):
            logger.warning("Manifest %s has non-list produces, skipping", service)
            produces = []

        for api in produces:
            if not isinstance(api, str) or not api:
                continue
            self._producers.setdefault(service, set()).add(api)

    def load_from_manifests(self, manifests: List[Dict[str, Any]]) -> int:
        """Load multiple manifests into the graph.

        Args:
            manifests: List of parsed manifest dictionaries.

        Returns:
            Number of manifests loaded.
        """
        for manifest in manifests:
            self.add_manifest(manifest)
        return len(manifests)

    def load_from_directory(self, root_dir: Union[str, Path]) -> int:
        """Discover and load all manifests from a directory tree.

        Args:
            root_dir: Root directory to search for .delimit/dependencies.yaml files.

        Returns:
            Number of manifests loaded.
        """
        manifests = discover_manifests(root_dir)
        return self.load_from_manifests(manifests)

    def get_consumers(self, api_name: str) -> List[str]:
        """Get all services that consume a given API.

        Args:
            api_name: The API name to look up.

        Returns:
            Sorted list of consumer service names. Empty if none found.
        """
        consumers = self._consumers.get(api_name, set())
        return sorted(consumers)

    def get_all_consumers(self) -> Dict[str, List[str]]:
        """Get the full consumer map: api -> [consumers].

        Returns:
            Dictionary with sorted keys and sorted consumer lists.
        """
        return {
            api: sorted(consumers)
            for api, consumers in sorted(self._consumers.items())
        }

    def get_produced_apis(self, service: str) -> List[str]:
        """Get all APIs produced by a service.

        Returns:
            Sorted list of API names.
        """
        return sorted(self._producers.get(service, set()))

    def get_consumed_apis(self, service: str) -> List[str]:
        """Get all APIs consumed by a service.

        Returns:
            Sorted list of API names.
        """
        return sorted(self._consumes.get(service, set()))

    def get_all_services(self) -> List[str]:
        """Get all known service names.

        Returns:
            Sorted list of service names.
        """
        return sorted(self._services)

    def get_all_apis(self) -> List[str]:
        """Get all known API names (anything that is consumed or produced).

        Returns:
            Sorted list of API names.
        """
        apis: Set[str] = set()
        apis.update(self._consumers.keys())
        for produced in self._producers.values():
            apis.update(produced)
        return sorted(apis)

    def get_service_count(self) -> int:
        """Return total number of known services."""
        return len(self._services)

    def get_api_count(self) -> int:
        """Return total number of known APIs."""
        return len(self.get_all_apis())

    def get_edge_count(self) -> int:
        """Return total number of consumer edges in the graph."""
        return sum(len(consumers) for consumers in self._consumers.values())

    def is_empty(self) -> bool:
        """Return True if no manifests have been loaded."""
        return len(self._services) == 0

    def to_dict(self) -> Dict[str, Any]:
        """Export the graph as a deterministic dictionary.

        Returns:
            Dictionary with sorted keys and values for reproducible output.
        """
        return {
            "services": self.get_all_services(),
            "apis": self.get_all_apis(),
            "consumers": self.get_all_consumers(),
            "service_count": self.get_service_count(),
            "api_count": self.get_api_count(),
            "edge_count": self.get_edge_count(),
        }


def build_graph(manifests: List[Dict[str, Any]]) -> DependencyGraph:
    """Convenience function to build a graph from a list of manifests.

    Args:
        manifests: List of parsed manifest dictionaries.

    Returns:
        Populated DependencyGraph instance.
    """
    graph = DependencyGraph()
    graph.load_from_manifests(manifests)
    return graph


def build_graph_from_directory(root_dir: Union[str, Path]) -> DependencyGraph:
    """Convenience function to build a graph by discovering manifests.

    Args:
        root_dir: Root directory to search.

    Returns:
        Populated DependencyGraph instance. Empty graph if no manifests found.
    """
    graph = DependencyGraph()
    graph.load_from_directory(root_dir)
    return graph
