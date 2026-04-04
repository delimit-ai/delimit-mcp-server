# Demo: Safe API Changes

This example demonstrates API changes that are safe and backward-compatible.

## Safe Changes in This API

1. **New endpoint added**: `/payments/{id}/refund` (v1.1)
   - Existing clients are unaffected by new endpoints
   
2. **Optional fields added**: `description` and `metadata`
   - Clients can ignore optional fields they don't understand
   
3. **Enum value added**: `apple_pay` payment method
   - Existing clients continue using known values

## Expected CI Output

When you make these safe changes, Delimit will:

```
✅ Delimit Check Complete

No breaking changes detected.

Safe changes:
• Added endpoint: POST /payments/{id}/refund
• Added optional field: Payment.description
• Added optional field: Payment.metadata
• Added enum value: payment_method.apple_pay

Your API changes are backward compatible!
```

## Testing This Example

1. Fork this repository
2. Modify `openapi.yaml` to add another optional field
3. Create a pull request
4. See Delimit confirm your changes are safe

## Try a Breaking Change

To see Delimit catch breaking changes, try:

1. Remove a required field from `PaymentRequest`
2. Change a field type (e.g., `amount` from number to string)
3. Remove an existing endpoint

Delimit will immediately flag these as breaking changes.

## Why These Changes Are Safe

### Adding Endpoints
New endpoints don't affect existing client code. Clients calling existing endpoints continue to work.

### Adding Optional Fields
Well-designed clients ignore unknown fields. Optional fields can be safely added without breaking compatibility.

### Expanding Enums (Adding Values)
Existing clients continue using the enum values they know. New values are available for new clients.

## Learn More

- [Breaking vs Safe Changes Guide](../../docs/breaking-vs-safe.md)
- [API Versioning Best Practices](../../docs/versioning.md)
- [Delimit Policy Configuration](../../docs/policies.md)