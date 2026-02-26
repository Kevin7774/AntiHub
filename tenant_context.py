from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from auth import AuthIdentity
from billing import BillingRepository

TENANT_CONTEXT_HEADER = "X-Tenant-ID"


@dataclass(frozen=True)
class TenantContext:
    tenant_id: Optional[str]
    source: str

    def as_dict(self) -> dict[str, Optional[str]]:
        return {"tenant_id": self.tenant_id, "source": self.source}


class TenantContextError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = int(status_code)
        self.detail = str(detail)


def _normalize_tenant_id(value: object) -> Optional[str]:
    normalized = str(value or "").strip()
    return normalized or None


def _is_root_role(identity: AuthIdentity) -> bool:
    return str(identity.role or "").strip().lower() == "root"


def resolve_tenant_context(
    *,
    repo: BillingRepository,
    identity: AuthIdentity,
    requested_tenant_id: Optional[str],
    feature_enabled: bool,
) -> TenantContext:
    """
    Resolve active tenant context with compatibility-first behavior.

    Compatibility path:
    - Flag OFF: always fallback to legacy single-tenant binding.
    - Flag ON:
      - No header => legacy binding.
      - Header equals legacy tenant => allow.
      - Header differs:
        - root: allow any active existing tenant.
        - non-root: require active tenant membership.
    """

    user = repo.get_auth_user(identity.username)
    db_tenant_id = _normalize_tenant_id(getattr(user, "tenant_id", None) if user is not None else None)
    claimed_tenant_id = _normalize_tenant_id(identity.tenant_id)
    legacy_tenant_id = db_tenant_id or claimed_tenant_id

    if not feature_enabled:
        return TenantContext(tenant_id=legacy_tenant_id, source="legacy_flag_off")

    requested = _normalize_tenant_id(requested_tenant_id)
    if not requested:
        return TenantContext(tenant_id=legacy_tenant_id, source="legacy_no_header")
    if requested == legacy_tenant_id:
        return TenantContext(tenant_id=requested, source="header_legacy")

    tenant = repo.get_tenant_by_id(requested)
    if tenant is None:
        raise TenantContextError(status_code=404, detail="tenant not found")
    if not bool(getattr(tenant, "active", True)):
        raise TenantContextError(status_code=403, detail="tenant is inactive")

    if _is_root_role(identity):
        return TenantContext(tenant_id=requested, source="header_root")

    member = repo.get_tenant_member(
        tenant_id=requested,
        username=identity.username,
        include_inactive=False,
    )
    if member is None:
        raise TenantContextError(status_code=403, detail="cross-tenant access denied")
    return TenantContext(tenant_id=requested, source="header_membership")
