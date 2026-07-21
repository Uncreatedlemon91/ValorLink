import base64
import io

import discord

from db.base import db_session
from utils.settings import get_config

_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


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


def _file_from_data_uri(image_uri: str):
    """Decode a data URI into a discord.File + its attachment filename, or
    (None, None) if it isn't a usable image."""
    if not image_uri or not image_uri.startswith("data:"):
        return None, None
    try:
        header, b64 = image_uri.split(",", 1)
        mime = header[5:].split(";")[0]
        name = f"insignia.{_EXT.get(mime, 'png')}"
        return discord.File(io.BytesIO(base64.b64decode(b64)), filename=name), name
    except Exception:  # noqa: BLE001 -- a malformed image just means no picture
        return None, None


async def post_billboard_notice(guild: discord.Guild, description: str,
                                image_uri: str | None = None, title: str | None = None,
                                color: int | None = None):
    """Post a rich embed to the billboard channel, with an uploaded insignia/
    medal image shown as the thumbnail when one is provided."""
    with db_session() as session:
        channel_id = get_config(session).billboard_channel_id
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return
    embed = discord.Embed(description=description,
                          color=color if color is not None else discord.Color.gold().value)
    if title:
        embed.title = title
    file, name = _file_from_data_uri(image_uri) if image_uri else (None, None)
    if name:
        embed.set_thumbnail(url=f"attachment://{name}")
    try:
        if file is not None:
            await channel.send(embed=embed, file=file)
        else:
            await channel.send(embed=embed)
    except discord.HTTPException:
        pass
