# Demo: Breaking API Changes

This example demonstrates how Delimit detects breaking changes in pull requests.

## Breaking Changes in This API

1. **Endpoint path changed**: `/users/{id}` → `/users/profile/{id}`
   - Existing clients will get 404 errors
   
2. **Required field added**: `created_at` is now required in User schema
   - Clients not sending this field will get validation errors
   
3. **Field removed**: `username` was removed from User schema
   - Clients expecting this field will break

## How to Test

1. Create a PR that modifies `openapi.yaml`
2. Delimit will automatically detect and comment on breaking changes
3. The CI check will warn but not fail (advisory mode by default)

## Example PR Comment

```
🚨 Delimit found 3 breaking changes

┌─────────────┬──────────────────────────────────┬──────────────────┐
│ Severity    │ Change                           │ Location         │
├─────────────┼──────────────────────────────────┼──────────────────┤
│ 🔴 Breaking │ Endpoint path changed            │ /users/{id}      │
│ 🔴 Breaking │ Required field 'created_at' added│ User schema      │
│ 🔴 Breaking │ Field 'username' removed         │ User schema      │
└─────────────┴──────────────────────────────────┴──────────────────┘
```

## Try It Yourself

1. Fork this repository
2. Modify `openapi.yaml`
3. Create a pull request
4. See Delimit in action!