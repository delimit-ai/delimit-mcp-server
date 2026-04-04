# Delimit Example OpenAPI Specs

Example specs that ship with Delimit for demos, testing, and playground experiences.

## Files

### petstore-v1.yaml
Classic Petstore API v1. Clean, well-documented baseline spec with pets CRUD and a vaccinations endpoint. Use as the "old" spec when demonstrating breaking change detection.

### petstore-v2.yaml
Petstore API v2 with **5 intentional breaking changes** against v1:
1. Endpoint removed (`/pets/{petId}/vaccinations`)
2. Type changed (`Pet.tag`: string to object)
3. Required parameter added (`status` query param on `GET /pets`)
4. Field removed (`Pet.nickname`)
5. Enum value removed (`reserved` from `Pet.status`)

Try it: `delimit lint examples/petstore-v1.yaml examples/petstore-v2.yaml`

### users-api.yaml
A realistic users API with JWT auth, pagination, CRUD operations, and proper error responses. Demonstrates what a well-governed spec looks like.

### minimal-bad.yaml
An intentionally poor spec: no descriptions, no auth, no examples, no error responses, no operationIds. Useful for testing spec health scoring and showing the value of governance.

## Usage

```bash
# Detect breaking changes between Petstore v1 and v2
delimit lint examples/petstore-v1.yaml examples/petstore-v2.yaml

# Diff two specs
delimit diff examples/petstore-v1.yaml examples/petstore-v2.yaml

# Compare a good spec vs a bad spec
delimit lint examples/users-api.yaml examples/minimal-bad.yaml
```
