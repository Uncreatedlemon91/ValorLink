import discord

from db.base import SessionLocal
from utils.settings import get_config


def base_embed(title: str, description: str = "", color: int | None = None) -> discord.Embed:
    with SessionLocal() as session:
        cfg = get_config(session)
        embed_color = color if color is not None else cfg.brand_color
        footer = cfg.regiment_motto or cfg.regiment_name

    embed = discord.Embed(title=title, description=description, color=embed_color)
    embed.set_footer(text=footer)
    return embed
