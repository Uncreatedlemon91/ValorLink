"""Resolve an incoming request to a unit.

The web resolves by subdomain (`slug.valorlink.co` → `slug`); the bot resolves
by Discord guild id. Both look the unit up in the registry.
"""
import os

from tenancy.registry import Tenant

# The platform's base domain, e.g. "valorlink.co". Subdomains of it are units;
# the bare domain and "www" are the public directory, not a unit.
PLATFORM_BASE_DOMAIN = os.getenv("PLATFORM_BASE_DOMAIN", "").lower()


def slug_from_host(host: str | None) -> str | None:
    """Extract a unit slug from a Host header, or None for the public site.

    "5thva.valorlink.co" -> "5thva"; "valorlink.co"/"www.valorlink.co" -> None.
    With no PLATFORM_BASE_DOMAIN configured (single-tenant mode) always None.
    """
    if not host or not PLATFORM_BASE_DOMAIN:
        return None
    host = host.split(":")[0].strip().lower().rstrip(".")
    if host in (PLATFORM_BASE_DOMAIN, f"www.{PLATFORM_BASE_DOMAIN}"):
        return None
    suffix = "." + PLATFORM_BASE_DOMAIN
    if host.endswith(suffix):
        label = host[: -len(suffix)]
        # only a single label counts as a unit slug (no deeper subdomains)
        return label if label and "." not in label else None
    return None


def tenant_by_slug(session, slug: str) -> Tenant | None:
    if not slug:
        return None
    return session.query(Tenant).filter(Tenant.slug == slug).one_or_none()


def tenant_by_guild(session, guild_id: int) -> Tenant | None:
    if not guild_id:
        return None
    return session.query(Tenant).filter(Tenant.discord_guild_id == guild_id).one_or_none()


def all_tenants(session) -> list[Tenant]:
    return session.query(Tenant).all()


def listed_tenants(session) -> list[Tenant]:
    """Units that opt in to the public directory."""
    return (
        session.query(Tenant)
        .filter(Tenant.listed.is_(True))
        .order_by(Tenant.name)
        .all()
    )


def default_tenant(session) -> Tenant | None:
    """The apex / legacy unit — what the bare domain serves and what a
    single-tenant deployment resolves to."""
    return session.query(Tenant).filter(Tenant.is_default.is_(True)).first()


def ensure_default_tenant(session, name: str = "Headquarters") -> Tenant:
    """Represent an existing single-tenant deployment as the default unit,
    pointing at the current DATABASE_URL / GUILD_ID. Idempotent."""
    import config

    existing = default_tenant(session)
    if existing:
        return existing

    slug = os.getenv("PLATFORM_DEFAULT_SLUG", "hq")
    tenant = Tenant(
        slug=slug,
        name=name,
        db_url=config.DATABASE_URL,
        discord_guild_id=config.GUILD_ID or None,
        is_default=True,
    )
    session.add(tenant)
    session.commit()
    return tenant
