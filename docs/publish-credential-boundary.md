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

Normal tag and manual standing release grants remain. Fin's fixed executor
first obtains a previously successful build-only artifact, renders exact old/new
`latest` movement for W9, consumes the existing owner confirmation, and submits
that immutable object plus original order/envelope and nonrenewable execution
window. Fin publication never rebuilds the confirmed tarball. Missing, expired,
changed or unsigned local authority stays held by the executor.

The checkout-free selector verifies the GitHub runner's actual dispatch event:
owner account ID 266558014, original repository and exact input
bytes. The previous successful workflow/run/attempt/artifact must be from this
repository's reviewed current main; the publisher verifies the accepted bytes
again. An ordinary collaborator cannot claim to be the owner through an input
field. The actor is transport authentication, never W9 approval by itself.

The workflow's package-wide concurrency lock serializes every standing and Fin
publication. Immediately before publication, the registry is re-read; all version
publication timestamps, including prereleases, count toward the rolling 24-hour
routine limit. Major versions/downgrades and already-published versions are held.
The historical local `publish-guard.sh` entrypoint keeps its existing owner grant
but routes to this sole workflow, pins the expected main SHA and fails on test
failure. It no longer creates a second direct npm publisher.

A Fin order creates one durable existing GitHub Checks record under that same
lock. Any earlier claim (failed, in-progress, completed or uncertain) prevents
reuse. Only the publisher has `checks: write`; claim data binds original order,
object digest, envelope digest, expiry, immutable main, artifact and workflow run.
The record is completed only after exact npm integrity and current `latest` are
verified. The executor independently verifies that receipt and current registry
state before reporting DONE. No network timeout creates an automatic retry.

Residuals requiring combined review: a trusted owner or another holder of an
owner token can dispatch; this design does not provide cryptographic process
attribution. Credential inventory/host isolation must exclude FE/worker/runner
access and protected workflow source must stay immutable. Dispatch is the
irreversible external submission boundary. The remote workflow enforces the
original expiry and current visible-action window, but cannot observe a host
pause/census change after submission. There are no new signing keys, daemons,
listeners or remote approval services. No live Fin publication or arming was
performed as source acceptance.

References: [npm trusted publishing](https://docs.npmjs.com/trusted-publishers/)
documents OIDC permissions and states that `npm whoami` is not a test of
trusted-publisher permission. [GitHub workflow permissions](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)
supports job-specific privilege reduction. [Actions artifacts](https://github.com/actions/upload-artifact)
documents immutable artifact IDs used across these jobs.
