---
name: api-check
description: Use when explicitly invoked or when the user asks to check API changes before a PR.
---

Find the changed OpenAPI spec and the PR base branch. Materialize its base version with `git show <base>:<spec-path>` into a temporary file with the same YAML or JSON extension; clean it up afterward. If the base has no spec, say that a two-version check is unavailable. Compare the base file (`old_spec`) with the working spec (`new_spec`) using `delimit_lint` with `dry_run: true`, `delimit_diff`, and `delimit_semver`. Report the breaking changes from `delimit_diff.changes` where `is_breaking` is true, the lint violations or errors, and the semver `bump`. Include `next_version` only if you supplied `current_version` to `delimit_semver`. Do not call a clean result proof of compatibility when the diff reports advisories.
