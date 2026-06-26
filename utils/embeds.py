import discord

import config


def base_embed(title: str, description: str = "", color: int | None = None) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color or config.BRAND_COLOR)
    embed.set_footer(text=config.REGIMENT_MOTTO or config.REGIMENT_NAME)
    return embed
