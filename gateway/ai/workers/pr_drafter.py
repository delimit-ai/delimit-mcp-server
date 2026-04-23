"""PR-drafter worker (LED-975).

Takes a ledger item describing an outreach target (repo with checked-in
spec) and produces a draft PR body + workflow YAML as a work-order
artifact the founder can copy-paste into a gh pr create command.

Bounded to: delimit_lint, delimit_diff, delimit_spec_health, read_file.
Cannot: write files, commit, push, post comments.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from ai.workers.base import Worker, WorkerResult
from ai.work_order import create_work_order


class PRDrafterWorker(Worker):
    worker_type = "pr_drafter"
    description = "Drafts PR bodies for governance check workflows"

    def execute(self, ledger_item: Dict[str, Any]) -> WorkerResult:
        item_id = ledger_item.get("id", "?")
        title = ledger_item.get("title", "")
        context = ledger_item.get("context", "")
        description = ledger_item.get("description", "")

        # Extract repo info from the ledger item
        repo = ""
        spec_path = ""
        for field in [context, description, title]:
            # Look for owner/repo pattern
            import re
            match = re.search(r'([\w.-]+/[\w.-]+)', field)
            if match and "/" in match.group(1):
                repo = match.group(1)
                break

        if not repo:
            return WorkerResult(
                worker_type=self.worker_type,
                ledger_item_id=item_id,
                success=False,
                error="Could not extract repo from ledger item",
            )

        # Try to detect the spec path
        common_spec_paths = [
            "api/openapi.yaml",
            "api/openapi.yml",
            "api/swagger.yaml",
            "api/swagger.yml",
            "openapi.yaml",
            "openapi.yml",
            "swagger.yaml",
            "swagger.yml",
            "api/v2.0/swagger.yaml",
            "schema.json",
            "api.json",
        ]
        spec_path = common_spec_paths[0]  # default, will be refined

        # Generate the workflow YAML
        workflow_yaml = f"""name: API Schema Check
on:
  pull_request:
    paths:
      - '{spec_path.rsplit("/", 1)[0] if "/" in spec_path else "."}/**'
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
          spec: {spec_path}
"""

        # Generate the PR body
        pr_body = f"""## Summary

Add an API schema drift check for `{spec_path}`. Advisory only — never blocks merges.

The action diffs the base branch spec against the PR and posts a comment identifying breaking vs non-breaking changes with semver classification. Detects 27 change types including endpoint removal, type changes, required parameter additions, and enum value removals.

## Changes

- Add `.github/workflows/api-schema-check.yml`

## Details

- Scoped to `{spec_path.rsplit("/", 1)[0] if "/" in spec_path else "."}/**` — only runs on PRs that modify the spec
- Zero config, no API keys, runs in under 30 seconds
- [Live demo](https://github.com/delimit-ai/delimit-action-demo/pull/2) showing 23 breaking changes detected

## References

- [Delimit Governance Framework (DGF)](https://github.com/delimit-ai/governance-framework) — 15 citeable controls
- [Delimit Action on Marketplace](https://github.com/marketplace/actions/delimit-api-governance)
"""

        # Create the work order
        wo = create_work_order(
            title=f"PR: Add governance check to {repo}",
            goal=f"Open a PR on {repo} adding .github/workflows/api-schema-check.yml scoped to {spec_path}",
            context=f"Source ledger item: {item_id}. Target: {repo}.",
            steps=[
                f"Fork {repo} (if not already forked)",
                f"Create branch: add-api-schema-check",
                f"Add file .github/workflows/api-schema-check.yml with content below",
                "```yaml",
                workflow_yaml.strip(),
                "```",
                f"Commit with: git commit -s -m 'ci: add API schema drift check for {spec_path}'",
                f"Push and open PR with body below",
                "```",
                pr_body.strip(),
                "```",
                f"Or one-liner: gh pr create --title 'ci: add API schema drift check' --body-file <(cat <<'BODY'\n{pr_body.strip()}\nBODY\n)",
            ],
            acceptance_criteria=[
                f"PR opened on {repo}",
                "Workflow file is advisory-only (never blocks merges)",
                "DCO sign-off included",
                "Maintainer response within 7 days",
            ],
            ledger_item_id=item_id,
            priority=ledger_item.get("priority", "P1"),
            tools_needed=["gh", "git"],
            estimated_minutes=15,
            worker_type=self.worker_type,
        )

        return WorkerResult(
            worker_type=self.worker_type,
            ledger_item_id=item_id,
            success=True,
            artifact_path=wo.get("filepath", ""),
            artifact_preview=wo.get("preview", "")[:300],
            work_order_id=wo.get("id", ""),
        )
