# Monorepo Example

This example demonstrates using Delimit in a monorepo with multiple services.

## Structure

```
services/
├── users/
│   └── api/
│       └── openapi.yaml
└── orders/
    └── api/
        └── openapi.yaml
```

## GitHub Actions Configuration

The workflow uses a matrix strategy to validate each service's API independently:

```yaml
strategy:
  matrix:
    service:
      - users
      - orders
```

## Features Demonstrated

1. **Path filtering**: Only runs when API specs change
2. **Matrix builds**: Parallel validation of multiple services
3. **Service isolation**: Each service validated independently
4. **Auto-baseline**: Each service maintains its own baseline

## Expected CI Output

When you modify a service's API, Delimit will:

1. Run only for the affected service(s)
2. Compare against that service's baseline
3. Report breaking changes specific to that service
4. Comment on the PR with service-specific feedback

## Testing Locally

To test an individual service:

```bash
# Install Delimit CLI
npm install -g @delimit-ai/cli

# Validate users service
delimit validate services/users/api/openapi.yaml

# Validate orders service
delimit validate services/orders/api/openapi.yaml
```

## Breaking Change Example

Try removing a required field from the User schema:

```diff
 required:
   - id
-  - email
   - name
```

Delimit will detect this as a breaking change and warn in your PR.

## Benefits for Monorepos

- **Incremental validation**: Only affected services are checked
- **Parallel execution**: Multiple services validated simultaneously
- **Independent baselines**: Each service evolves at its own pace
- **Clear attribution**: Know exactly which service has issues