import asyncio
import logging

import discord
from discord.ext import commands

import config

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("valorlink")

INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.message_content = True

COGS = (
    "cogs.onboarding",
    "cogs.recruitment",
    "cogs.personnel",
    "cogs.roster",
    "cogs.events",
    "cogs.moderation",
)


class ValorLink(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=INTENTS)

    async def setup_hook(self):
        for cog in COGS:
            await self.load_extension(cog)
            log.info("Loaded %s", cog)

        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        log.info("%s is online as %s", config.REGIMENT_NAME or "ValorLink", self.user)


async def main():
    if not config.BOT_TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")

    bot = ValorLink()
    async with bot:
        await bot.start(config.BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
