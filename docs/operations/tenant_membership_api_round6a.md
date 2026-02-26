# Round 6A Tenant Membership AuthZ Boundary

## Scope
- Only extends authz boundary for existing tenant membership APIs from Round 5.
- No audit log (deferred to Round 6B).
- No auth token/session redesign.
- No billing/payment changes.

## Membership API AuthZ Matrix
- Endpoints:
  - `GET /admin/tenants/{tenant_id}/members`
  - `PUT /admin/tenants/{tenant_id}/members/{username}`
  - `DELETE /admin/tenants/{tenant_id}/members/{username}`
- Behavior:
  - `root`: allow any tenant.
  - `tenant-admin` (`role=admin`): allow same-tenant only.
  - `user`: deny.

## Restricted Actions (tenant-admin)
- Cannot assign non-`member` membership role.
- Cannot manage root user membership (upsert/delete denied).
- Cannot manage users outside same tenant.
- Cannot bypass feature flag.

## Feature Flag Behavior
- `FEATURE_MULTI_TENANT_FOUNDATION=false`:
  - membership APIs return controlled `404 feature disabled` for admin/root callers.
  - existing IAM/admin routes remain available.

## Limitations (intentional)
- Tenant setting APIs remain root-only in this round.
- No audit trail fields/events yet (Round 6B).
