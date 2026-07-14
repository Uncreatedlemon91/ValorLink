"""Per-request tenant resolution for the web app (Phase 2).

Every web request is served in the context of one unit. The unit is chosen by
the request's ``Host`` header:

* a subdomain (``5thva.valorlink.co``) → that unit, via the registry;
* the apex / ``www`` → the **default** unit (the original single-regiment
  deployment), so existing behaviour is unchanged.

The resolved unit is cached on ``request.state.tenant`` and drives which
database the request's session opens. With no ``PLATFORM_BASE_DOMAIN`` set
(single-tenant mode) every request resolves to the default unit.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from tenancy.registry import init_registry, registry_session
from tenancy.resolve import (
    default_tenant,
    ensure_default_tenant,
    slug_from_host,
    tenant_by_slug,
)


@dataclass
class TenantCtx:
    slug: str
    db_url: str
    guild_id: int | None
    is_default: bool


class TenantNotFound(Exception):
    """The Host names a subdomain that isn't a registered unit."""

    def __init__(self, slug: str | None):
        self.slug = slug


_ready = False


def ensure_ready():
    """Create the registry and make sure the current deployment is represented
    as the default unit. Idempotent; safe to call on every startup."""
    global _ready
    if _ready:
        return
    init_registry()
    with registry_session() as session:
        ensure_default_tenant(session)
    _ready = True


def _ctx(tenant) -> TenantCtx:
    return TenantCtx(
        slug=tenant.slug,
        db_url=tenant.db_url,
        guild_id=tenant.discord_guild_id,
        is_default=tenant.is_default,
    )


def resolve_tenant(request: Request) -> TenantCtx:
    """Resolve (and cache) the unit for this request."""
    cached = getattr(request.state, "tenant", None)
    if cached is not None:
        return cached

    ensure_ready()
    slug = slug_from_host(request.headers.get("host", ""))
    with registry_session() as session:
        tenant = default_tenant(session) if slug is None else tenant_by_slug(session, slug)
        if tenant is None:
            raise TenantNotFound(slug)
        ctx = _ctx(tenant)

    request.state.tenant = ctx
    return ctx


def get_tenant(request: Request) -> TenantCtx:
    """FastAPI dependency form of :func:`resolve_tenant`."""
    return resolve_tenant(request)


def tenant_by_slug_ctx(slug: str) -> TenantCtx | None:
    """Look up a unit by slug outside of a request (used by the OAuth callback,
    which runs on the central host and can't read the unit from the Host)."""
    ensure_ready()
    with registry_session() as session:
        tenant = tenant_by_slug(session, slug)
        return _ctx(tenant) if tenant else None
