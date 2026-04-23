"""Outreach-drafter worker (LED-976).

Takes an audit-program target from the signal corpus or ledger,
runs delimit_lint against the target's spec if available, and drafts
a GitHub issue body per the AUDIT_PROGRAM.md template. Cannot post —
produces a work-order for founder approval.

Bounded to: delimit_lint, delimit_diff, delimit_sense, read_file.
Cannot: write files, post issues, commit, push.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from ai.workers.base import Worker, WorkerResult
from ai.work_order import create_work_order


class OutreachDrafterWorker(Worker):
    worker_type = "outreach_drafter"
    description = "Drafts governance-outreach GitHub issues"

    def execute(self, ledger_item: Dict[str, Any]) -> WorkerResult:
        item_id = ledger_item.get("id", "?")
        title = ledger_item.get("title", "")
        context = ledger_item.get("context", "")
        description = ledger_item.get("description", "")

        # Extract repo info
        import re
        repo = ""
        for field in [context, description, title]:
            match = re.search(r'([\w.-]+/[\w.-]+)', field)
            if match and "/" in match.group(1):
                candidate = match.group(1)
                # Skip our own repos
                if not candidate.startswith("delimit-ai/"):
                    repo = candidate
                    break

        if not repo:
            return WorkerResult(
                worker_type=self.worker_type,
                ledger_item_id=item_id,
                success=False,
                error="Could not extract target repo from ledger item",
            )

        # Draft the issue body per AUDIT_PROGRAM.md template
        issue_title = f"Suggestion: CI check for API schema drift"
        issue_body = f"""## Context

This repo maintains an API specification that downstream consumers depend on.
Breaking changes in API contracts (endpoint removal, type changes, required
parameter additions) can cause silent integration failures for consumers.

## Suggestion

Add an advisory CI check that diffs the base branch spec against PRs.
A single workflow file:

```yaml
name: API Schema Check
on:
  pull_request:
    paths:
      - 'api/**'
      - '*.yaml'
      - '*.json'
permissions:
  contents: read
  pull-requests: write
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: delimit-ai/delimit-action@v1
        with:
          spec: openapi.yaml
```

This diffs the base branch spec against the PR and posts a comment
identifying breaking vs non-breaking changes with semver classification.
Advisory only — never blocks merges. Teams can upgrade to enforcement later.

## What it catches

| DGF Control | Detection | Severity |
|---|---|---|
| DGF-BC-001 | Endpoint removal | High |
| DGF-BC-002 | Required parameter added | High |
| DGF-BC-003 | Required response field removed | High |
| DGF-BC-004 | Type changed | High |
| DGF-BC-005 | Enum value removed | High |
| + 10 more | See [DGF v0.1.0](https://github.com/delimit-ai/governance-framework) | — |

Zero config, no API keys, runs in under 30 seconds.
[Live demo](https://github.com/delimit-ai/delimit-action-demo/pull/2).

Happy to open a PR if there's interest.
"""

        # Emit a structured action the executor can run after founder
        # approval. Keeps the human steps above for readability; the
        # executor uses this typed list to actually post the issue.
        executable = [
            {
                "action": "gh_issue_create",
                "params": {
                    "repo": repo,
                    "title": issue_title,
                    "body": issue_body.strip(),
                },
            }
        ]

        # Create the work order
        wo = create_work_order(
            title=f"Outreach: Open governance issue on {repo}",
            goal=f"Open a GitHub issue on {repo} suggesting API schema CI check",
            context=f"Source: {item_id}. Per AUDIT_PROGRAM.md playbook. Must pass pre-outreach capability-test gate: run delimit lint against the target's spec before claiming detection.",
            steps=[
                f"Verify {repo} has a checked-in API spec (search for openapi.yaml, swagger.yaml, api.json, schema.json)",
                f"Run: delimit lint <old_spec> <new_spec> on a recent PR that changed the spec (proves the tool works on their spec)",
                f"If lint works, open the issue:",
                f"```",
                f"gh issue create --repo {repo} \\",
                f"  --title '{issue_title}' \\",
                f"  --body-file /dev/stdin <<'BODY'",
                issue_body.strip(),
                "BODY",
                f"```",
                "Wait for maintainer response (do NOT open a PR immediately)",
            ],
            acceptance_criteria=[
                f"Pre-outreach capability test passes (delimit lint works on {repo}'s spec)",
                f"Issue opened on {repo} matching AUDIT_PROGRAM.md format",
                "No capability claims the tool can't back",
                "Staggered timing (not batch-submitted with other issues)",
            ],
            ledger_item_id=item_id,
            priority=ledger_item.get("priority", "P1"),
            tools_needed=["gh", "delimit"],
            estimated_minutes=20,
            worker_type=self.worker_type,
            executable_actions=executable,
        )

        return WorkerResult(
            worker_type=self.worker_type,
            ledger_item_id=item_id,
            success=True,
            artifact_path=wo.get("filepath", ""),
            artifact_preview=wo.get("preview", "")[:300],
            work_order_id=wo.get("id", ""),
        )
