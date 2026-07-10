# Identity Guard

`.github/workflows/identity-guard.yml` is a **fail-closed CI check** that stops any
commit carrying a non-anonymized git identity from landing on this repository.

## Why

This is a **public** repository. Commits expose their git **author** and **committer**
email addresses forever, and GitHub's squash-merge preserves the author email of the
merged PR. A personal or org email on a public commit is a permanent, irreversible
identity leak. Local git hooks can be bypassed (`--no-verify`) and ephemeral CI /
agent worktrees can be misconfigured, so this enforcement lives **server-side in CI**
where it cannot be skipped.

## What it does

On every `pull_request` and `push`, the guard enumerates the git author **and**
committer email of every commit in the range and fails the job if any email is not
on the allowlist. On failure it prints the offending commit SHA and a **redacted**
email (local-part masked, e.g. `***@example.com`) — never the full address.

**Allowlist (only these pass):**

| Pattern | Example |
| --- | --- |
| `*@users.noreply.github.com` | `12345678+octocat@users.noreply.github.com` |
| `*@delimit.ai` | `team@delimit.ai` |
| GitHub system committer (exact) | `noreply@github.com`, `actions@github.com` |

The GitHub system identities are exempt because GitHub itself sets them on
squash-merges, merge commits, and web-UI edits — they are public, non-personal, and
without the exemption every legitimate squash-merge would fail the check.

The guard uses **pure bash + git only** — zero external actions or dependencies
beyond `actions/checkout`.

## How to fix a flagged commit

First, set an anonymized email for future commits (get your GitHub noreply address
from GitHub → Settings → Emails → "Keep my email addresses private"):

```bash
git config user.email 'ID+USERNAME@users.noreply.github.com'
```

Then rewrite the flagged commit(s):

- **Tip commit only:**
  ```bash
  git commit --amend --author='NAME <ID+USERNAME@users.noreply.github.com>' --reset-author
  ```
- **Older commits in the branch:**
  ```bash
  git rebase -i <base-sha>     # mark each flagged commit 'edit'
  # at each stop:
  git commit --amend --author='NAME <ID+USERNAME@users.noreply.github.com>' --reset-author
  git rebase --continue
  ```
- Force-push **your own feature branch** (never a shared/protected branch) and let CI re-run.

To rewrite author **and** committer across a whole branch in one shot:

```bash
git rebase <base-sha> \
  --exec "git commit --amend --no-edit --reset-author \
          --author='NAME <ID+USERNAME@users.noreply.github.com>'"
```

## Scope / non-goals

- The guard is **additive**: it does not modify any other workflow, read any secret,
  or change repository runtime behavior.
- Making this a **required** status check (so a red guard blocks merge) is a
  branch-protection / ruleset change and is intentionally **not** performed by the
  guard itself — that is a separate, deliberate repo-settings decision.
