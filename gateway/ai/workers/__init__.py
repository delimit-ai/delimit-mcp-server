"""Worker pool v1 — read-only bounded agents (LED-975, LED-976).

Workers are specialized and sandboxed per the swarm charter:
  - Each worker has a bounded capability surface
  - Cannot escalate without re-deliberation
  - Cannot write files, commit, push, or modify state
  - Output is always an artifact (work order) for founder approval

The daemon dispatches work to workers. Workers produce artifacts.
The founder approves artifacts from their interactive session or
mobile PWA. Approved artifacts get executed.

This is ledger-based, not time-based: workers pull from the ledger,
produce work orders, and the ledger tracks completion.
"""

from ai.workers.base import Worker, WorkerResult
from ai.workers.pr_drafter import PRDrafterWorker
from ai.workers.outreach_drafter import OutreachDrafterWorker

WORKER_REGISTRY = {
    "pr_drafter": PRDrafterWorker,
    "outreach_drafter": OutreachDrafterWorker,
}

__all__ = [
    "Worker",
    "WorkerResult",
    "PRDrafterWorker",
    "OutreachDrafterWorker",
    "WORKER_REGISTRY",
]
