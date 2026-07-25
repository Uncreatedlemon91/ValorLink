import discord
from discord.ext import commands

from db.base import db_session
from db.context import reset_current_db_url
from tenancy.routing import bind_guild, db_url_for_guild
from utils.embeds import base_embed
from utils.settings import get_config


class Onboarding(commands.Cog):
    """Greets new joins and tags them with the visitor role, and bootstraps a
    brand-new unit's admin access the moment the bot joins its server.

    This is separate from the recruitment pipeline (cogs.recruitment) --
    onboarding fires for *every* join, recruitment only starts once someone
    clicks "Apply to Enlist".
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        """A unit registers on the website (linking a Discord server id)
        before ever inviting the bot, so by the time the bot joins, this
        guild already resolves to a unit -- but nobody in it holds the
        ValorLink admin role yet, since that's normally configured from the
        Command Tent, which itself requires the admin role. Break that
        chicken-and-egg loop here: create the role and hand it to the
        server owner, so they land in a working Command Tent immediately
        instead of needing to run a slash command first. Only acts once --
        a unit that already has an admin role configured is left alone,
        including if the bot is removed and re-invited later."""
        if db_url_for_guild(guild.id) is None:
            return
        token = bind_guild(guild.id)
        try:
            with db_session() as session:
                cfg = get_config(session)
                if cfg.admin_role_id:
                    return
                regiment_name = cfg.regiment_name

            owner = guild.owner or await guild.fetch_owner()
            try:
                role = await guild.create_role(
                    name="ValorLink Admin",
                    reason="ValorLink setup: bootstrap admin access for the unit's owner",
                )
                await owner.add_roles(role, reason="ValorLink setup: initial admin")
            except discord.HTTPException:
                return  # likely missing Manage Roles; nothing to bind

            with db_session() as session:
                get_config(session).admin_role_id = role.id
                session.commit()

            try:
                await owner.send(
                    f"**{regiment_name}** is set up! You've been given the **{role.name}** role, "
                    "so you have full admin access in ValorLink. Head to the Command Tent on "
                    "the website (or `/config`, `/rank`, and `/company` in Discord) to finish "
                    "configuring your unit -- roles, channels, the rank ladder, and companies."
                )
            except discord.Forbidden:
                pass
        finally:
            reset_current_db_url(token)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        # Only greet in guilds that map to a unit.
        if db_url_for_guild(member.guild.id) is None:
            return
        bind_guild(member.guild.id)

        with db_session() as session:
            cfg = get_config(session)
            visitor_role_id = cfg.visitor_role_id
            welcome_channel_id = cfg.welcome_channel_id
            regiment_name = cfg.regiment_name

        visitor_role = member.guild.get_role(visitor_role_id) if visitor_role_id else None
        if visitor_role:
            try:
                await member.add_roles(visitor_role, reason="Onboarding: auto-assigned visitor role")
            except discord.HTTPException:
                pass

        channel = member.guild.get_channel(welcome_channel_id) if welcome_channel_id else None
        if channel:
            embed = base_embed(
                title=f"Welcome to {regiment_name}",
                description=(
                    f"{member.mention} has joined the server.\n\n"
                    "Check the recruitment channel to apply for enlistment, "
                    "or read the rules to get oriented first."
                ),
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            await channel.send(embed=embed)

        try:
            await member.send(
                f"Welcome to **{regiment_name}**! Head back to the server to find out how to apply."
            )
        except discord.Forbidden:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Onboarding(bot))
