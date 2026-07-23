"""Create a new unit: a registry row + a provisioned private database.

Shared by the management CLI and the web "register your unit" flow.
"""
import os
import re
from datetime import datetime

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

    from sqlalchemy.exc import IntegrityError

    with registry_session() as session:
        if tenant_by_slug(session, slug):
            raise ProvisionError(f"The handle '{slug}' is already taken.")
        if guild_id and tenant_by_guild(session, guild_id):
            raise ProvisionError("That Discord server is already linked to a unit.")

        url = db_url or unit_db_url_for_slug(slug)
        provision_unit_db(url)
        session.add(Tenant(
            slug=slug, name=name, discord_guild_id=guild_id or None,
            motto=(motto or "").strip() or None, blurb=(blurb or "").strip() or None,
            db_url=url,
        ))
        try:
            session.commit()
        except IntegrityError:
            # Lost a race for the same handle (the slug column is UNIQUE).
            session.rollback()
            raise ProvisionError(f"The handle '{slug}' is already taken.")

    invalidate()
    return url


def rename_unit_slug(old_slug: str, new_slug: str) -> str:
    """Change a unit's subdomain handle. Cascades the rename across every
    registry table that references it as a plain string (alliance
    membership, joint events, RSVPs), the same way a rank/company rename
    updates every member holding it -- otherwise the unit would silently
    drop out of its own alliances. The unit's private database and its
    Discord guild link are untouched; only the routing handle changes.

    Old links to the previous subdomain start 404ing immediately -- there's
    no redirect and the freed handle isn't reserved, so warn the caller
    before calling this."""
    from tenancy.registry import AllianceEvent, AllianceMember, AllianceRSVP, Tenant, registry_session
    from tenancy.resolve import tenant_by_slug
    from tenancy.routing import invalidate

    new_slug = normalize_slug(new_slug)
    with registry_session() as session:
        tenant = tenant_by_slug(session, old_slug)
        if tenant is None:
            raise ProvisionError(f"No unit '{old_slug}' exists.")
        if new_slug == old_slug:
            raise ProvisionError("That's already this unit's handle.")
        if tenant_by_slug(session, new_slug):
            raise ProvisionError(f"The handle '{new_slug}' is already taken.")

        tenant.slug = new_slug
        session.query(AllianceMember).filter(AllianceMember.unit_slug == old_slug).update(
            {AllianceMember.unit_slug: new_slug}, synchronize_session=False
        )
        session.query(AllianceEvent).filter(AllianceEvent.host_slug == old_slug).update(
            {AllianceEvent.host_slug: new_slug}, synchronize_session=False
        )
        session.query(AllianceRSVP).filter(AllianceRSVP.unit_slug == old_slug).update(
            {AllianceRSVP.unit_slug: new_slug}, synchronize_session=False
        )
        session.commit()

    invalidate()
    return new_slug


def slug_available(slug: str) -> tuple[bool, str]:
    """Whether a handle is free to use, for a live check on the form."""
    from tenancy.registry import registry_session
    from tenancy.resolve import tenant_by_slug

    try:
        slug = normalize_slug(slug)
    except ProvisionError as exc:
        return False, str(exc)
    with registry_session() as session:
        if tenant_by_slug(session, slug):
            return False, "That handle is already taken."
    return True, "Available."


def delete_unit(slug: str, purge: bool = False) -> dict:
    """Remove a unit from the platform: delete its registry row (so it leaves
    the directory and stops resolving). The private database file is kept by
    default (archived name if purge is off) so the data is recoverable; pass
    purge=True to delete it outright. The default unit cannot be deleted.
    """
    from tenancy.registry import registry_session
    from tenancy.resolve import tenant_by_slug
    from tenancy.routing import invalidate

    slug = (slug or "").strip().lower()
    with registry_session() as session:
        tenant = tenant_by_slug(session, slug)
        if tenant is None:
            raise ProvisionError(f"No unit '{slug}' exists.")
        if tenant.is_default:
            raise ProvisionError("The default unit can't be deleted.")
        db_url = tenant.db_url
        name = tenant.name
        session.delete(tenant)
        session.commit()

    invalidate()

    # Preserve each member's service in this unit before its database goes away,
    # so it still shows on their cross-unit record. Read while the file exists,
    # i.e. before the archive/purge step below.
    from tenancy.career import snapshot_unit
    try:
        snapshot_unit(db_url, slug, name)
    except Exception:  # noqa: BLE001 -- never block a deletion on the snapshot
        pass

    # Release the cached engine/connections so nothing lingers against the file
    # we're about to archive.
    from tenancy.units import dispose_engine
    dispose_engine(db_url)

    archived_to = None
    if db_url.startswith("sqlite:///"):
        path = db_url[len("sqlite:///"):]
        if os.path.exists(path):
            if purge:
                try:
                    os.remove(path)
                except OSError:
                    pass
            else:
                archived_to = f"{path}.removed-{datetime.utcnow():%Y%m%d%H%M%S}"
                try:
                    os.rename(path, archived_to)
                except OSError:
                    archived_to = None

    return {"slug": slug, "name": name, "db_url": db_url,
            "purged": purge, "archived_to": archived_to}
