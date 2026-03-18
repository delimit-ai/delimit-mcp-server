"""
Delimit Impact Analyzer
Determines downstream consumers affected by an API change
and produces informational impact summaries for CI output.

Per Jamsons Doctrine:
- Impact analysis is INFORMATIONAL ONLY
- NEVER affects CI pass/fail outcome
- Deterministic outputs
- Graceful degradation when no dependency data exists
"""

import logging
from typing import Any, Dict, List, Optional

from .dependency_graph import DependencyGraph

logger = logging.getLogger("delimit.impact_analyzer")


class ImpactAnalyzer:
    """Analyze the downstream impact of API changes."""

    def __init__(self, graph: DependencyGraph):
        """Initialize with a dependency graph.

        Args:
            graph: Populated DependencyGraph instance.
        """
        self._graph = graph

    def analyze(self, api_name: str) -> Dict[str, Any]:
        """Analyze the impact of a change to an API.

        Args:
            api_name: The API that changed.

        Returns:
            Deterministic impact summary dictionary.
        """
        downstream = self._graph.get_consumers(api_name)

        return {
            "api": api_name,
            "downstream_services": downstream,
            "impact_count": len(downstream),
            "graph_available": not self._graph.is_empty(),
        }

    def analyze_multiple(self, api_names: List[str]) -> List[Dict[str, Any]]:
        """Analyze impact for multiple APIs.

        Args:
            api_names: List of API names that changed.

        Returns:
            Sorted list of impact summaries.
        """
        results = [self.analyze(api) for api in api_names]
        results.sort(key=lambda r: r["api"])
        return results

    def get_blast_radius(self, api_name: str) -> int:
        """Get the number of downstream services affected.

        Args:
            api_name: The API that changed.

        Returns:
            Number of affected downstream services.
        """
        return len(self._graph.get_consumers(api_name))

    def format_ci_output(self, api_name: str) -> str:
        """Format impact analysis for CI log output.

        This output is informational only and NEVER affects CI outcome.

        Args:
            api_name: The API that changed.

        Returns:
            Formatted string for CI logs.
        """
        impact = self.analyze(api_name)
        lines = []

        lines.append("")
        lines.append("--------------------------------------")
        lines.append("DELIMIT IMPACT ANALYSIS")
        lines.append("--------------------------------------")
        lines.append("")
        lines.append(f"API changed: {api_name}")
        lines.append("")

        if not impact["graph_available"]:
            lines.append("No dependency manifests found.")
            lines.append("Add .delimit/dependencies.yaml to enable impact analysis.")
        elif impact["impact_count"] == 0:
            lines.append("No known downstream consumers.")
        else:
            lines.append("Potential downstream consumers:")
            lines.append("")
            for service in impact["downstream_services"]:
                lines.append(f"  * {service}")
            lines.append("")
            lines.append(f"Blast radius: {impact['impact_count']} service(s)")

        lines.append("")
        lines.append("--------------------------------------")
        lines.append("")

        return "\n".join(lines)


def analyze_impact(
    graph: DependencyGraph,
    api_name: str,
) -> Dict[str, Any]:
    """Convenience function for CI pipeline integration.

    Called after event_backbone in the pipeline:
        diff_engine → policy_engine → complexity_analyzer
        → event_backbone → dependency_graph → impact_analyzer

    CRITICAL: This function NEVER raises exceptions.
    Impact analysis is informational only.

    Args:
        graph: Dependency graph (may be empty).
        api_name: The API that changed.

    Returns:
        Impact summary dictionary.
    """
    try:
        analyzer = ImpactAnalyzer(graph)
        return analyzer.analyze(api_name)
    except Exception as e:
        logger.warning("Impact analysis failed: %s — continuing", e)
        return {
            "api": api_name,
            "downstream_services": [],
            "impact_count": 0,
            "graph_available": False,
            "error": str(e),
        }


def format_impact_for_ci(
    graph: DependencyGraph,
    api_name: str,
) -> str:
    """Convenience function to format impact analysis for CI output.

    NEVER raises. Returns empty string on failure.
    """
    try:
        analyzer = ImpactAnalyzer(graph)
        return analyzer.format_ci_output(api_name)
    except Exception as e:
        logger.warning("Impact formatting failed: %s", e)
        return ""
