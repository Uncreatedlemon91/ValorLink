import discord

from db.base import db_session
from utils.settings import get_config


async def post_billboard(guild: discord.Guild, message: str):
    """Post a one-liner to the configured billboard channel, if set."""
    with db_session() as session:
        channel_id = get_config(session).billboard_channel_id
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel:
        try:
            await channel.send(message)
        except discord.HTTPException:
            pass
