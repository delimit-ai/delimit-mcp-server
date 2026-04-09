"""Generator drift detection (LED-713).

Detects when a committed generated artifact (e.g. agentspec's
schemas/v1/agent.schema.json regenerated from a Zod source) has drifted
from what its generator script would produce today.

Use case: a maintainer changes the source of truth (Zod schema, OpenAPI
generator, protobuf, etc.) but forgets to regenerate and commit the
artifact. CI catches the drift before the stale generated file ships.

Generic over generators — caller supplies the regen command and the
artifact path. Returns a structured drift report that can be merged into
the standard delimit-action PR comment.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class DriftResult:
    drifted: bool
    artifact_path: str
    regen_command: str
    changes: List[Any] = field(default_factory=list)  # JSONSchemaChange list when drift detected
    error: Optional[str] = None
    runtime_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "drifted": self.drifted,
            "artifact_path": self.artifact_path,
            "regen_command": self.regen_command,
            "change_count": len(self.changes),
            "changes": [
                {
                    "type": c.type.value,
                    "path": c.path,
                    "message": c.message,
                    "is_breaking": c.is_breaking,
                }
                for c in self.changes
            ],
            "error": self.error,
            "runtime_seconds": round(self.runtime_seconds, 3),
        }


def detect_drift(
    repo_root: str,
    artifact_path: str,
    regen_command: str,
    timeout_seconds: int = 60,
) -> DriftResult:
    """Check whether the committed artifact matches its generator output.

    Args:
        repo_root: Absolute path to the repo checkout.
        artifact_path: Path to the generated artifact, relative to repo_root.
        regen_command: Shell command that regenerates the artifact in place.
            Example: "pnpm -r run build" or "node packages/sdk/dist/scripts/export-schema.js"
        timeout_seconds: Hard timeout for the generator (default 60).

    Returns:
        DriftResult with drift status, classified changes, and runtime.
    """
    import time

    repo_root_p = Path(repo_root).resolve()
    artifact_p = (repo_root_p / artifact_path).resolve()

    if not artifact_p.exists():
        return DriftResult(
            drifted=False,
            artifact_path=artifact_path,
            regen_command=regen_command,
            error=f"Artifact not found: {artifact_path}",
        )

    # Snapshot the committed artifact before regen
    try:
        committed_text = artifact_p.read_text()
        committed_doc = json.loads(committed_text)
    except (OSError, json.JSONDecodeError) as e:
        return DriftResult(
            drifted=False,
            artifact_path=artifact_path,
            regen_command=regen_command,
            error=f"Failed to read committed artifact: {e}",
        )

    # Parse the command safely — shell=False to avoid command injection.
    # Users needing shell features (&&, |, env vars, etc.) should point
    # generator_command at a script file instead of an inline chain.
    try:
        argv = shlex.split(regen_command)
    except ValueError as e:
        return DriftResult(
            drifted=False,
            artifact_path=artifact_path,
            regen_command=regen_command,
            error=f"Could not parse generator_command: {e}",
        )
    if not argv:
        return DriftResult(
            drifted=False,
            artifact_path=artifact_path,
            regen_command=regen_command,
            error="generator_command is empty",
        )
    # Reject obvious shell metacharacters — force users to use a script
    # file if they need chaining or redirection.
    SHELL_META = set("&|;><`$")
    if any(ch in token for token in argv for ch in SHELL_META):
        return DriftResult(
            drifted=False,
            artifact_path=artifact_path,
            regen_command=regen_command,
            error="generator_command contains shell metacharacters (&|;><`$). Point it at a script file instead of chaining inline.",
        )

    # Run the regenerator
    start = time.time()
    try:
        result = subprocess.run(
            argv,
            shell=False,
            cwd=str(repo_root_p),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return DriftResult(
            drifted=False,
            artifact_path=artifact_path,
            regen_command=regen_command,
            error=f"Generator timed out after {timeout_seconds}s",
            runtime_seconds=time.time() - start,
        )
    except FileNotFoundError as e:
        return DriftResult(
            drifted=False,
            artifact_path=artifact_path,
            regen_command=regen_command,
            error=f"Generator executable not found: {e}",
            runtime_seconds=time.time() - start,
        )

    runtime = time.time() - start

    if result.returncode != 0:
        return DriftResult(
            drifted=False,
            artifact_path=artifact_path,
            regen_command=regen_command,
            error=f"Generator exited {result.returncode}: {result.stderr.strip()[:500]}",
            runtime_seconds=runtime,
        )

    # Read the regenerated artifact
    try:
        regen_text = artifact_p.read_text()
        regen_doc = json.loads(regen_text)
    except (OSError, json.JSONDecodeError) as e:
        # Restore committed version so we don't leave the workspace dirty
        artifact_p.write_text(committed_text)
        return DriftResult(
            drifted=False,
            artifact_path=artifact_path,
            regen_command=regen_command,
            error=f"Failed to read regenerated artifact: {e}",
            runtime_seconds=runtime,
        )

    # Restore the committed file before diffing — leave the workspace clean
    artifact_p.write_text(committed_text)

    # Quick equality check first
    if committed_doc == regen_doc:
        return DriftResult(
            drifted=False,
            artifact_path=artifact_path,
            regen_command=regen_command,
            runtime_seconds=runtime,
        )

    # Drift detected — classify the changes via the JSON Schema diff engine
    from .json_schema_diff import JSONSchemaDiffEngine

    engine = JSONSchemaDiffEngine()
    changes = engine.compare(committed_doc, regen_doc)
    return DriftResult(
        drifted=True,
        artifact_path=artifact_path,
        regen_command=regen_command,
        changes=changes,
        runtime_seconds=runtime,
    )


def format_drift_report(result: DriftResult) -> str:
    """Render a drift report as a markdown block for PR comments."""
    if result.error:
        return (
            f"### Generator drift check\n\n"
            f"Artifact: `{result.artifact_path}`  \n"
            f"Status: error  \n"
            f"Detail: {result.error}\n"
        )
    if not result.drifted:
        return (
            f"### Generator drift check\n\n"
            f"Artifact: `{result.artifact_path}`  \n"
            f"Status: clean (committed artifact matches generator output)  \n"
            f"Generator runtime: {result.runtime_seconds:.2f}s\n"
        )
    breaking = sum(1 for c in result.changes if c.is_breaking)
    non_breaking = len(result.changes) - breaking
    lines = [
        "### Generator drift check",
        "",
        f"Artifact: `{result.artifact_path}`  ",
        f"Status: drifted ({len(result.changes)} change(s) — {breaking} breaking, {non_breaking} non-breaking)  ",
        f"Generator runtime: {result.runtime_seconds:.2f}s  ",
        "",
        "The committed artifact does not match what the generator produces today. Re-run the generator and commit the result, or revert the source change.",
        "",
    ]
    for c in result.changes:
        marker = "breaking" if c.is_breaking else "ok"
        lines.append(f"- [{marker}] {c.type.value} at `{c.path}` — {c.message}")
    return "\n".join(lines) + "\n"
