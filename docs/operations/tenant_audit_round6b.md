# Round 6B Tenant Membership/Settings Audit Logging

## 1) Scope
- Only adds append-only audit records for tenant membership/settings mutating APIs.
- No authz changes (keeps Round 6A behavior).
- No auth token/session changes.
- No billing/payment logic changes.

## 2) Storage Schema (reused, no new table)
Reuses existing `billing_audit_logs` with:
- `provider`: `tenant`
- `event_type`: tenant-domain event name
- `raw_payload`: bounded JSON payload (actor/tenant/target/before/after)
- `outcome`: `ok`
- `detail`: bounded summary text
- `occurred_at`: event timestamp

## 3) Event Types
- `tenant.membership.upsert`
- `tenant.membership.deactivate`
- `tenant.setting.upsert`

## 4) Payload Fields (minimal)
`raw_payload` JSON fields:
- `domain`
- `tenant_id`
- `actor.username`
- `actor.role`
- `target.kind` + (`target.username` or `target.key`)
- `before` (minimal snapshot)
- `after` (minimal snapshot)

## 5) Bounded/Limit Rules
- `raw_payload` length is capped (truncated with marker if needed).
- `detail` length is capped.
- snapshot value fields use preview/truncated representation.

## 6) Operational Notes
- Audit write is best-effort and isolated after primary mutation path to avoid changing API status-code behavior.
- If audit write fails, request behavior remains unchanged and warning log is emitted.

## 7) Limitations
- No dedicated tenant-audit query API in this round.
- Existing `/admin/billing/audit` remains the retrieval path.
- No retention policy automation yet (document-only limitation).
