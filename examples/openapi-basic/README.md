# OpenAPI Governance Example

This example shows how to use Delimit to prevent breaking changes in OpenAPI specifications.

## Setup

1. Copy `.github/workflows/delimit.yml` to your repository
2. Update the paths to match your API spec location
3. Open a PR with API changes

## What Delimit Catches

- ❌ Removing endpoints
- ❌ Removing required fields
- ❌ Changing field types
- ❌ Adding new required parameters

## Auto-Baseline

On first run, Delimit creates a baseline of existing issues and only flags NEW problems in future PRs.

This means you can adopt Delimit immediately without fixing legacy code.