import discord
from discord.ext import commands

from db.base import db_session
from db.context import reset_current_db_url
from tenancy.routing import bind_guild, db_url_for_guild
from utils.embeds import base_embed
from utils.settings import get_config


class Onboarding(commands.Cog):
    """Greets new joins and tags them with the visitor role, and checks a
    brand-new unit's admin access is actually wired up the moment the bot
    joins its server.

    This is separate from the recruitment pipeline (cogs.recruitment) --
    onboarding fires for *every* join, recruitment only starts once someone
    clicks "Apply to Enlist".
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        """A unit can name an existing Discord role for its admins when it
        registers on the website, before the bot is even invited -- that's
        the preferred path, since it means the Command Tent works the
        moment the bot joins with nobody needing to run a command first.
        This just checks that actually landed: if no admin role was set, or
        the id given doesn't match a real role now that the bot can see the
        server, DM the owner so they find out from a friendly message
        instead of everyone getting silently refused later. Nothing here
        creates or assigns a role -- that's on the unit to pick."""
        if db_url_for_guild(guild.id) is None:
            return
        token = bind_guild(guild.id)
        try:
            with db_session() as session:
                cfg = get_config(session)
                admin_role_id = cfg.admin_role_id
                regiment_name = cfg.regiment_name

            if admin_role_id and guild.get_role(admin_role_id):
                return  # configured, and it's a real role in this server

            detail = (
                f"the admin role id you set (`{admin_role_id}`) doesn't match any role in this server"
                if admin_role_id else
                "no admin role was set when you registered"
            )
            owner = guild.owner or await guild.fetch_owner()
            try:
                await owner.send(
                    f"**{regiment_name}** is set up, but {detail}, so nobody has Command Tent "
                    "access yet. Fix it in Discord with server **Administrator** permission: "
                    "`/config set_role key:admin role:@YourAdminRole`. Every command after that "
                    "one can use the role itself instead."
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
