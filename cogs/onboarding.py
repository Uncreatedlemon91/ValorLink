import discord
from discord.ext import commands

import config
from db.base import SessionLocal
from utils.embeds import base_embed
from utils.settings import get_config


class Onboarding(commands.Cog):
    """Greets new joins and tags them with the visitor role.

    This is separate from the recruitment pipeline (cogs.recruitment) --
    onboarding fires for *every* join, recruitment only starts once someone
    clicks "Apply to Enlist".
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if config.GUILD_ID and member.guild.id != config.GUILD_ID:
            return

        with SessionLocal() as session:
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
