"""Role-based permission checks shared across cogs.

These check role IDs stored in GuildConfig (set live via /config set_role)
rather than Discord permission flags, since regiment hierarchy
(officer/recruiter/admin) is what gates these commands, not server-wide
admin status.
"""
import discord
from discord import app_commands

from db.base import SessionLocal
from utils.settings import get_config


def _has_role(interaction: discord.Interaction, role_id: int | None) -> bool:
    if not role_id:
        return False
    return any(r.id == role_id for r in interaction.user.roles)


def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        with SessionLocal() as session:
            cfg = get_config(session)
        if _has_role(interaction, cfg.admin_role_id):
            return True
        raise app_commands.CheckFailure("You need the admin role to use this command.")

    return app_commands.check(predicate)


def is_officer():
    async def predicate(interaction: discord.Interaction) -> bool:
        with SessionLocal() as session:
            cfg = get_config(session)
        if _has_role(interaction, cfg.admin_role_id) or _has_role(interaction, cfg.officer_role_id):
            return True
        raise app_commands.CheckFailure("You need the officer role to use this command.")

    return app_commands.check(predicate)


def is_recruiter():
    async def predicate(interaction: discord.Interaction) -> bool:
        with SessionLocal() as session:
            cfg = get_config(session)
        if (
            _has_role(interaction, cfg.admin_role_id)
            or _has_role(interaction, cfg.officer_role_id)
            or _has_role(interaction, cfg.recruiter_role_id)
        ):
            return True
        raise app_commands.CheckFailure("You need the recruiter role to use this command.")

    return app_commands.check(predicate)


def is_bot_admin():
    """Gate for /config, /rank, and /company commands. Native Discord
    Administrator permission always works -- this is the bootstrap path,
    since on a fresh setup no admin role has been configured yet. The
    configured admin role works too, once set.
    """

    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        with SessionLocal() as session:
            cfg = get_config(session)
        if _has_role(interaction, cfg.admin_role_id):
            return True
        raise app_commands.CheckFailure(
            "You need server Administrator permission or the configured admin role to use this command."
        )

    return app_commands.check(predicate)
