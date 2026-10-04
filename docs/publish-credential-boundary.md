# Package publication credential boundary

The existing npm workflow now builds and accepts the package in jobs with
read-only repository permissions and no `id-token: write`. Both checkouts
disable credential persistence, so package tests and lifecycle scripts cannot
read the private gateway checkout token from Git configuration. npm OIDC is
only available to the separate final publisher job.

The final job performs no checkout, dependency installation, native build,
package acceptance execution, or package lifecycle script. It downloads the
immutable Actions artifact by the exact ID returned by the successful build
job. Fixed workflow code verifies source/run/gateway bindings, package identity,
tarball hashes, the acceptance receipt, and the registry record. Package JSON
is parsed as data without extracting or executing the package. `publishConfig`
overrides are refused so artifact metadata cannot redirect publication.

The publisher uses a fresh directory separate from downloaded files and runs
`npm publish <exact-tarball> --ignore-scripts` against the fixed npm registry.
The existing OIDC path and optional existing token fallback are preserved;
the fallback token is supplied only to the publication step. The resulting
registry integrity/shasum must match the accepted tarball. The existing MCP
registry and GitHub Release steps remain after successful npm publication.

Normal existing tag and manual release grants remain unchanged. A nonempty
`fin_order_id` currently produces an explicit HOLD before build. The Fin
executor must first bind an immutable accepted CI artifact, package/version,
old/new channel state and original order to its own existing owner-confirmation
snapshot. Only that trusted executor may consume W9 and issue its fixed
workflow dispatch. A dispatch field or GitHub actor alone is not confirmation.
No new signing keys, remote approval service, or direct Fin npm publisher is
introduced by this change.

Workflow concurrency serializes this workflow's package release attempts and
does not cancel an active publisher. A complete rolling 24-hour release policy
and the exact Fin dispatch adapter are separate remaining integration work;
this security repair does not claim that concurrency alone enforces batching.

References: [npm trusted publishing](https://docs.npmjs.com/trusted-publishers/)
documents OIDC permissions and states that `npm whoami` is not a test of
trusted-publisher permission. [GitHub workflow permissions](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)
supports job-specific privilege reduction. [Actions artifacts](https://github.com/actions/upload-artifact)
documents immutable artifact IDs used across these jobs.
