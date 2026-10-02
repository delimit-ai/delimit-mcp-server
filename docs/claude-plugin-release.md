# Releasing the Delimit Claude plugin

This is the release lane for the plugin in `claude-plugin/`, which is listed in
the Claude plugin directory as **Delimit** by delimit-ai. It has four stages:

1. Pre-release, in this repo.
2. Anthropic acceptance, in the directory portal.
3. Publication, an explicit Publish click with Auto-publish off.
4. Post-publish acceptance, a clean install from the directory.

Each version gets one row in [claude-plugin-releases.md](claude-plugin-releases.md).

Directory listing, a passed scan and a clean install are platform and
distribution checks. They show that the plugin is distributed and installs.
They do not show demand, and they are not a certification or an endorsement by
Anthropic. The directory terms forbid implying partnership, sponsorship or
endorsement. Copy about the listing must say no more than that.

## What ships, and through which channel

| Item | Value |
|---|---|
| Plugin files | `claude-plugin/` (manifest `claude-plugin/.claude-plugin/plugin.json`, MCP config `claude-plugin/.mcp.json`, skills `handoff`, `record`, `resume`, `README.md`, `PRIVACY.md`, icon) |
| Repo marketplace | `.claude-plugin/marketplace.json` (marketplace `delimit`, source `./claude-plugin`) |
| Plugin version | `version` in plugin.json. This is the only version field. It is independent of the delimit-cli package version |
| Runtime | `.mcp.json` runs `npx -y delimit-cli@<exact version> mcp --toolset records` |
| Tags | `delimit--v<version>`, created by `claude plugin tag` |

Users get the plugin through two channels, and they update differently:

- **Directory** (`delimit@anthropic-plugin-directory`, or `delimit@synced` for
  signed-in claude.ai accounts). Users receive only versions that were
  published in the portal.
- **Repo marketplace** (`claude plugin marketplace add delimit-ai/delimit-mcp-server`,
  then `delimit@delimit`). This channel follows the default branch. A merge to
  `main` that changes `claude-plugin/` reaches these users with no directory
  review, and existing installs notice it only if `version` changed. This is
  why every plugin change carries a version bump, which CI enforces.

## Stage 1: pre-release (repo)

1. **Branch and change.** Edit files under `claude-plugin/` or
   `.claude-plugin/` only on a branch that will become a release.
2. **Bump `version`** in plugin.json in the same PR. Never reuse a number, even
   for a version that failed review. As a rule of thumb:
   - patch: wording, links or docs;
   - minor: a new skill, hook, command or MCP tool exposure;
   - major: removing or renaming something users rely on.
3. **Pin delimit-cli exactly** in `.mcp.json`, to a version already on npm.
   Check it with `npm view delimit-cli@X.Y.Z version`, which must print `X.Y.Z`.
   Do not pin a version that is not yet published. Directory policy asks for
   reasonably current dependencies, so prefer the current release.
4. **Add a provenance row** to `docs/claude-plugin-releases.md`. Fill in the
   version and pin. Set the remaining cells to `Pending`, and fill in the
   source commit and tag after merge.
5. **Run local checks** with a throwaway `HOME`, never your real one:
   ```sh
   node scripts/claude-plugin-release-guard.js --base origin/main
   T=$(mktemp -d); HOME=$T claude plugin validate --strict ./claude-plugin
   HOME=$T claude plugin validate --strict .
   python3 scripts/claude-plugin-e2e.py delimit-cli@X.Y.Z
   npm test
   ```
6. **Open the PR and merge** through the normal review. The `Tests` workflow
   runs the guard job **Claude plugin release guard** (see "CI guard" below).
   A red guard blocks the merge.
7. **Tag the merged commit:**
   ```sh
   git checkout <merged main commit>
   claude plugin tag claude-plugin --dry-run      # prints delimit--vX.Y.Z
   claude plugin tag claude-plugin -m "Delimit plugin %s"
   git rev-parse 'delimit--vX.Y.Z^{commit}'      # must equal the merged commit
   ```
   Never pass `--force`, and never move or delete an existing `delimit--v*`
   tag. Pushing the tag is a separate, deliberate step.
8. Record the source commit and tag in the row. This can go in a docs-only PR,
   which the guard allows because the provenance file lives outside
   `claude-plugin/`.

## Stage 2: Anthropic acceptance (directory portal)

9. **Make the directory see the version.** This depends on what the listing
   tracks (Settings tab, "tracked branch or tag"):
   - **A tag:** change the tracked tag to `delimit--vX.Y.Z`. A tag stays on its
     commit, so the directory never sees a new version until the tracked tag
     changes.
   - **The default branch:** select **Check for new commits**, or wait for the
     scheduled check.
10. **Wait for the result.** A version ends in one of three states: it passes,
    it is held for a reviewer, or it doesn't pass. Expect a reviewer hold on
    every version: the directory holds plugins whose launcher runs a pinned
    `npx` package, and the hold can recur on each new version.
11. **Record the result** in the row: the scan result and every warning,
    verbatim from the **Review** and **Versions** tabs.
12. **If the version doesn't pass**, fix the problem on a new branch and
    release a new, higher version (step 2). The published version stays live
    meanwhile. To contest a decision, use **Appeal this decision** or write to
    directory@anthropic.com.

## Stage 3: publication

13. **Auto-publish stays Off.** The owner selects **Publish** for each passing
    version in the portal. Nobody else publishes, and nothing publishes
    automatically.
14. **Record the publication** in the row: the click time, who clicked, and the
    time the portal showed the version Live. The portal says new listings and
    updates can take up to an hour to show in the directory.

## Stage 4: post-publish acceptance (clean install)

15. Repeat this check after every publish, in a fresh throwaway HOME with no
    claude.ai login and no npm config:
    ```sh
    T=$(mktemp -d); E="env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=$T XDG_CONFIG_HOME=$T/.config CLAUDE_CONFIG_DIR=$T/.claude npm_config_cache=$T/npm-cache"
    $E claude plugin marketplace update anthropic-plugin-directory
    # In $T/.claude/plugins/plugin-directory-cache-v2.json, the Delimit listing must
    # show release.version X.Y.Z and source.repository.reviewed_commit = the tagged commit.
    $E claude plugin install delimit@anthropic-plugin-directory --json
    $E claude plugin list --json        # version "X.Y.Z-<first 12 chars of commit>"
    ```
16. **Check the installed files.** Compare the sha256 of every installed file
    with `git show <commit>:claude-plugin/<file>`. Then launch the exact
    `.mcp.json` command from a synthetic project directory and confirm:
    - the MCP handshake completes and exactly the 10 records tools are listed;
    - a ledger write reads back, and a handoff can be created and listed;
    - a second (warm) launch works;
    - `claude mcp list` shows Connected;
    - `claude plugin validate` passes on the installed path.

    `scripts/claude-plugin-e2e.py` covers the MCP part for a given delimit-cli
    spec.
17. **Record the outcome** in the row, including what was not tested (for
    example the interactive /plugin view, Cowork, or a signed-in `@synced`
    install). If acceptance fails, stop and roll back (see "Rollback").

## CI guard

`scripts/claude-plugin-release-guard.js` runs in the `Tests` workflow as the job
**Claude plugin release guard**, on pull requests. Its unit tests are
`tests/claude-plugin-release-guard.test.js`, which run under `npm test`.

On every run it checks, offline:
- plugin.json, marketplace.json and `.mcp.json` parse;
- the plugin and the marketplace are named `delimit`;
- the marketplace source is `./claude-plugin`;
- `.mcp.json` pins exactly one `delimit-cli@X.Y.Z`;
- provenance rows strictly increase, include the current version, and no row
  recorded at the base was removed.

When the PR changes `claude-plugin/**` or `.claude-plugin/**`, it also requires:
- the plugin.json version is greater than both the base version and every
  version recorded at the base;
- a provenance row exists for the new version;
- `npm view delimit-cli@<pin>` confirms the pin exists. A lookup error fails
  the job.

The guard does not tag, push, submit or publish anything.

## Lifecycle facts

### DOCUMENTED

These were read on 2026-10-02. Sources:

- [submit] https://claude.com/docs/plugins/submit
- [checklist] https://claude.com/docs/plugins/pre-submission-checklist
- [overview] https://claude.com/docs/plugins/overview
- [after] https://claude.com/docs/connectors/building/after-publishing
- [admin] https://claude.com/docs/plugins/admin
- [cc-install] https://code.claude.com/docs/en/plugins/install
- [cc-measure] https://code.claude.com/docs/en/plugins/measure
- [policy] https://support.claude.com/en/articles/13145358-anthropic-software-directory-policy
- [terms] https://support.claude.com/en/articles/13145338-anthropic-software-directory-terms

**How versions reach the directory**
- The directory follows a tracked branch or tag, by default the repository's
  default branch. It "scans the newest commit on the tracked branch or tag".
  For a plugin in a subfolder it "reads and scans only that folder" [submit].
- New commits are found "on a schedule", on push when the GitHub push webhook
  is set up, and on demand with **Check for new commits** [submit].
- The plugin.json `version` is not documented as the trigger. You still
  "raise it with every release", because installed copies use it to tell that
  an update exists [submit].

**Scan, review and publishing**
- Every version gets the same validation and security scan as the first one.
  Results are: passes, held for a reviewer, or doesn't pass [submit].
- "A launcher that runs a package pinned to an exact version … is still held",
  and "The scan can raise the same hold again on each new version" [checklist].
- Publish settings are set by Anthropic. The default is "An Anthropic reviewer
  publishes each version". The others are "The reviewer publishes only the
  first version" and "You publish the first version". "Auto-publish doesn't
  apply while a reviewer publishes each version" [submit].
- "The listing keeps serving the last published version until a new version is
  published, including when a new version doesn't pass or is held for a
  reviewer." If the security scan fails a new version, "later versions also
  wait until an Anthropic reviewer clears the plugin" [submit].

**Rollback and delisting**
- **Delist plugin** stops the listing. People who installed it "stop getting
  updates, and their copy may be removed". **Relist plugin** is a request that
  can be declined [submit].
- "A version that is already live stays up when you change the tracked branch
  or tag" [submit].
- "Revert to this version" exists only for org-uploaded items, and is "not
  available for an item synced from a repository" [admin].

**How users get updates**
- On claude.ai and Cowork, a synced new version arrives "automatically, with
  nothing to accept" [overview].
- In Claude Code, a directory install syncs in the background on start and
  prompts `Plugins changed. Run /reload-plugins to activate.` [cc-install].
- Users of a third-party marketplace update through `claude plugin update`, or
  auto-update, which is off by default for third-party marketplaces
  [cc-install].

**Usage figures and policy**
- Usage figures (installs, versions, components, errors and the directory
  funnel) appear on the portal Usage tab, for up to 90 days [after]. "Claude
  Code doesn't report a plugin's usage back to its author" [cc-measure].
- The directory policy requires reasonably current dependencies for local MCP
  servers and a way to receive vulnerability reports (this repo has
  `SECURITY.md`) [policy].
- The terms forbid implying partnership with, sponsorship by or endorsement by
  Anthropic [terms].

### OBSERVED

These come from the owner portal (screenshots, 2026-10-02) and from local checks.

- The listing was submitted 2026-09-26. The ledger records 10:20 ET, at tag
  `delimit--v1.0.4` (LED-5721). It was held for content-policy review because
  of the pinned npx package.
- About 20 hours before 2026-10-02 16:27 ET, version `v1.0.4 · 9b417f8` showed
  "Scan passed, with directory policy warnings" and "Version passed, ready to
  publish".
- The owner clicked Publish at 16:27 ET. The portal showed the listing as
  Published/Live at 16:28 ET, for Claude Code and Cowork. Auto-publish was Off
  ("you publish each version yourself").
- The portal shows 3 skills and 1 connector. The Usage tab says "Usage numbers
  appear here once the directory has data".
- Portal text: the directory checks "about every 6 hours"; a push webhook
  notifies it on push; and "New listings and updates can take up to an hour to
  show in the directory". No webhook is configured on the repository
  (`gh api repos/delimit-ai/delimit-mcp-server/hooks` returns `[]`).
- Nine commits landed on `main` after `9b417f8` (2026-09-26 to 09-29). None of
  them touched `claude-plugin/` or `.claude-plugin/`, and the `claude-plugin`
  tree hash is identical at `9b417f8`, at `delimit--v1.0.4` and at `main`. The
  portal still showed v1.0.4 · 9b417f8 as the latest version, so those commits
  produced no new version candidate.
- About 16:43 ET, a clean install from the built-in `anthropic-plugin-directory`
  marketplace worked with no claude.ai login, on Claude Code 2.1.288.
  - The directory cache showed listing id `plugin_018cApt644QshHNw7fmjQn64`,
    `release.version` 1.0.4, and `reviewed_commit`
    `9b417f8061bf40bbf3bf93abd18cab0234154bac`.
  - The cache also showed `checks.review.state: "none"`, even though the portal
    reported policy warnings.
  - The installed version string is `1.0.4-9b417f8061bf`, and the installed
    files match the commit by sha256.
- `claude plugin tag claude-plugin --dry-run` refuses to recreate an existing
  tag. It says "Bump the version".

### UNKNOWN, and how the next release cycle measures each

| Unknown | Measurement in the next cycle |
|---|---|
| Whether the listing tracks `main` or the tag `delimit--v1.0.4`. Either explains the absent candidate, since the plugin folder is unchanged on both | Read the Settings tab before step 9 and record it in the row |
| Whether a commit outside the plugin folder can create a candidate | Already observed: none in nine commits. Keep noting whether any candidate appears that does not match a plugin change |
| Whether the directory requires a plugin.json version bump | Not tested on purpose. The guard always requires a bump |
| Real interval of the scheduled check | If the listing tracks a branch, record the merge time and the time the candidate appears without clicking **Check for new commits**. If you clicked, record the click time |
| Scan and review duration | Record the times from candidate visible, to scan result, to "ready to publish" |
| Whether the pinned-npx hold recurs | Record held or not held in the row |
| Propagation time from Publish to the directory | After the click, poll `claude plugin marketplace update anthropic-plugin-directory` in a throwaway HOME until `release.version` changes. Record the delay |
| What "directory policy warnings" means, and which warnings v1.0.4 has | Copy the Review tab text verbatim into the row (1.0.4: pending the owner's screenshot) |
| Which publish setting Anthropic applied to the listing | Copy the Overview tab's Auto-publish row text verbatim |
| Whether `@synced` installs key on the manifest version or a directory-recorded version | After the next publish, check the version string in a signed-in Claude Code install and record it |
| Webhook payload, events and permissions | Not measured, since no webhook is set up (see below) |
| When the Usage tab shows real figures | Record the first date it shows non-preview data. These figures are diagnostics, not demand |

## Recommendations

**Push webhook: do not set it up now.** The only documented effect is that the
directory checks on push instead of waiting for the schedule. It publishes
nothing, and the scheduled check still runs. Setting it up needs repository
admin access and a repository settings change, which is a separate decision
outside this lane. With releases days apart, **Check for new commits** (or
changing the tracked tag) gives the same immediacy. If the listing tracks a
tag, a branch push webhook may add nothing at all. Revisit only if plugin
releases become frequent enough that the scheduled-check delay blocks a real
user fix.

**Auto-publish: keep it Off.** There is no documented way to revert a directory
listing to an earlier version. Recovery is either a forward fix or delisting,
and delisting can remove installed copies. The manual Publish click is the last
point where a bad version can be stopped. Auto-publish also would not remove
the expected reviewer hold, so it would save little. Revisit only when all of
the following hold:
- at least three consecutive versions went through this lane with no
  post-publish acceptance failure;
- the post-publish acceptance in stage 4 runs automatically against the
  directory cache, with no manual steps;
- the listing tracks a release tag, so only a deliberate tag creates a
  candidate;
- the change is recorded as an explicit decision in the provenance file.

## Rollback

There is no documented revert to an earlier published directory version.
1. **Forward fix (default).** Restore the last good content, for example with
   `git revert`, under a **higher** version. Then run all four stages. The bad
   version stays live until the fix is published. Repo-marketplace users get
   the fix on `main` with their next update.
2. **Delist** (portal page menu or Settings tab). Use this only for a
   harmful version that cannot wait for a fix. It stops updates for everyone,
   may remove installed copies, and relisting can be declined. It is an owner
   decision.
3. **Changing the tracked branch or tag** does not take a live version down.

## Provider-neutral checklist

Use the same seven concerns for any other plugin or extension directory. Fill
in each provider's answer, with a citation or an observation, before its first
submission.

| Concern | Question to answer | Claude directory answer |
|---|---|---|
| Listing identity | What identifies our listing, and from which source? | Listing `plugin_018cApt644QshHNw7fmjQn64`, "Delimit" by delimit-ai. Source: repo `delimit-ai/delimit-mcp-server`, path `claude-plugin` |
| Update detection | What makes the provider see a new version? | The tracked branch or tag: scheduled check, optional push webhook, or **Check for new commits**. The plugin.json version is raised for clients |
| Provider scan | What review runs on each version, and what holds it? | Validation plus a security scan on every version. The pinned-npx launcher is held for a reviewer |
| Publication gate | Who or what makes a version live? | The owner's Publish click. Auto-publish is Off; the publish setting is applied by Anthropic |
| Install acceptance | How do we prove the published artifact installs and works? | Stage 4: a clean-HOME install from the provider's own channel, a file hash match, and the MCP handshake plus a records round trip |
| Receipts | Where is each version's evidence kept? | One row per version in `docs/claude-plugin-releases.md`: commit, tag, pin, scan result, publication time and actor, acceptance |
| Rollback | How do we undo a bad version, and what does it cost users? | A forward fix under a higher version. Delisting is the last resort and may remove installed copies. There is no documented revert |
