"""Bind the bot's current-unit context from a Discord guild.

The bot resolves each guild to its unit database via the registry, then binds
it (``db.context``) so all the cog/util DB access for that interaction follows
the right unit. Results are cached because the registry changes rarely; call
:func:`invalidate` after provisioning a unit.
"""
from db.context import set_current_db_url
from tenancy.registry import registry_session
from tenancy.resolve import all_tenants, default_tenant, tenant_by_guild

_guild_cache: dict[int, str | None] = {}


def db_url_for_guild(guild_id: int | None) -> str | None:
    """The unit database URL for a guild, falling back to the default unit."""
    if guild_id in _guild_cache:
        return _guild_cache[guild_id]
    with registry_session() as session:
        tenant = tenant_by_guild(session, guild_id) if guild_id else None
        if tenant is None:
            tenant = default_tenant(session)
        url = tenant.db_url if tenant else None
    _guild_cache[guild_id] = url
    return url


def bind_guild(guild_id: int | None):
    """Bind the current context to a guild's unit database; returns a token."""
    return set_current_db_url(db_url_for_guild(guild_id))


def invalidate():
    _guild_cache.clear()


def registered_guild_ids() -> list[int]:
    """All guild ids that map to a unit (for command sync / view registration)."""
    with registry_session() as session:
        return [t.discord_guild_id for t in all_tenants(session) if t.discord_guild_id]
