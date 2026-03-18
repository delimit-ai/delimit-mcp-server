"""
Bridge to delimit-intel (wireintel) MCP server.
Tier 3 Extended — data intelligence and versioned datasets.
"""

import sys
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("delimit.ai.intel_bridge")

INTEL_PACKAGE = Path("/home/delimit/.delimit_suite/packages/wireintel")


def _ensure_intel_path():
    for p in [str(INTEL_PACKAGE), str(INTEL_PACKAGE / "wireintel")]:
        if p not in sys.path:
            sys.path.insert(0, p)


def dataset_register(name: str, schema: Dict[str, Any], description: Optional[str] = None) -> Dict[str, Any]:
    """Register a new dataset with schema."""
    return {"tool": "wireintel.dataset.register", "name": name, "schema": schema, "description": description}


def dataset_list() -> Dict[str, Any]:
    """List registered datasets."""
    return {"tool": "wireintel.dataset.list"}


def dataset_freeze(dataset_id: str) -> Dict[str, Any]:
    """Mark dataset as immutable."""
    return {"tool": "wireintel.dataset.freeze", "dataset_id": dataset_id}


def dataset_version_create(dataset_id: str, data: Any) -> Dict[str, Any]:
    """Create new version of dataset."""
    return {"tool": "wireintel.dataset.version_create", "dataset_id": dataset_id}


def dataset_get_version(dataset_id: str, version: Optional[str] = None) -> Dict[str, Any]:
    """Get specific dataset version."""
    return {"tool": "wireintel.dataset.get_version", "dataset_id": dataset_id, "version": version}


def snapshot_ingest(data: Dict[str, Any], provenance: Optional[Dict] = None) -> Dict[str, Any]:
    """Store research snapshot with provenance."""
    return {"tool": "wireintel.snapshot.ingest", "data": data, "provenance": provenance}


def snapshot_get(snapshot_id: str) -> Dict[str, Any]:
    """Retrieve snapshot by ID."""
    return {"tool": "wireintel.snapshot.get", "snapshot_id": snapshot_id}


def query_run(dataset_id: str, query: str, parameters: Optional[Dict] = None) -> Dict[str, Any]:
    """Execute deterministic query on dataset."""
    return {"tool": "wireintel.query.run", "dataset_id": dataset_id, "query": query, "parameters": parameters}
