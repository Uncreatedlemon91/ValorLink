"""Role-based permission checks shared across cogs.

These check config.py role IDs rather than Discord permission flags, since
regiment hierarchy (officer/recruiter/admin) is what gates these commands,
not server-wide admin status.
"""
import discord
from discord import app_commands

import config


def _has_role(interaction: discord.Interaction, role_id: int) -> bool:
    if not role_id:
        return False
    return any(r.id == role_id for r in interaction.user.roles)


def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if _has_role(interaction, config.ADMIN_ROLE_ID):
            return True
        raise app_commands.CheckFailure("You need the admin role to use this command.")

    return app_commands.check(predicate)


def is_officer():
    async def predicate(interaction: discord.Interaction) -> bool:
        if _has_role(interaction, config.ADMIN_ROLE_ID) or _has_role(interaction, config.OFFICER_ROLE_ID):
            return True
        raise app_commands.CheckFailure("You need the officer role to use this command.")

    return app_commands.check(predicate)


def is_recruiter():
    async def predicate(interaction: discord.Interaction) -> bool:
        if (
            _has_role(interaction, config.ADMIN_ROLE_ID)
            or _has_role(interaction, config.OFFICER_ROLE_ID)
            or _has_role(interaction, config.RECRUITER_ROLE_ID)
        ):
            return True
        raise app_commands.CheckFailure("You need the recruiter role to use this command.")

    return app_commands.check(predicate)
