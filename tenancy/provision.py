"""Create a new unit: a registry row + a provisioned private database.

Shared by the management CLI and the web "register your unit" flow.
"""
import re

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?$")
RESERVED_SLUGS = {"www", "api", "static", "auth", "admin", "app", "help", "status"}


class ProvisionError(ValueError):
    """A user-facing provisioning failure."""


def normalize_slug(slug: str) -> str:
    slug = (slug or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise ProvisionError(
            "Use 2–30 characters: lowercase letters, numbers, and hyphens."
        )
    if slug in RESERVED_SLUGS:
        raise ProvisionError(f"'{slug}' is reserved. Choose another name.")
    return slug


def create_unit(slug: str, name: str, guild_id: int | None = None,
                motto: str | None = None, blurb: str | None = None,
                db_url: str | None = None) -> str:
    """Register and provision a unit. Returns the unit's database URL."""
    from tenancy.registry import Tenant, registry_session
    from tenancy.resolve import tenant_by_guild, tenant_by_slug
    from tenancy.routing import invalidate
    from tenancy.units import provision_unit_db, unit_db_url_for_slug

    slug = normalize_slug(slug)
    name = (name or "").strip()
    if not name:
        raise ProvisionError("The unit needs a display name.")

    with registry_session() as session:
        if tenant_by_slug(session, slug):
            raise ProvisionError(f"A unit '{slug}' already exists.")
        if guild_id and tenant_by_guild(session, guild_id):
            raise ProvisionError("That Discord server is already linked to a unit.")

        url = db_url or unit_db_url_for_slug(slug)
        provision_unit_db(url)
        session.add(Tenant(
            slug=slug, name=name, discord_guild_id=guild_id or None,
            motto=(motto or "").strip() or None, blurb=(blurb or "").strip() or None,
            db_url=url,
        ))
        session.commit()

    invalidate()
    return url
