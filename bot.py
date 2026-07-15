import asyncio
import logging

import discord
from discord.ext import commands

import config
from db.base import db_session
from db.context import reset_current_db_url, set_current_db_url
from db.models import Candidacy, Event
from tenancy.registry import init_registry, registry_session
from tenancy.resolve import all_tenants, ensure_default_tenant
from tenancy.routing import registered_guild_ids

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("valorlink")

INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.message_content = True

COGS = (
    "cogs.settings",
    "cogs.lifecycle",
    "cogs.onboarding",
    "cogs.recruitment",
    "cogs.personnel",
    "cogs.roster",
    "cogs.events",
    "cogs.moderation",
    "cogs.awards",
    "cogs.bridge",
)


class ValorLink(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=INTENTS)

    async def setup_hook(self):
        # Make sure the registry exists and this deployment is the default unit,
        # so the bot works whether or not the web app has started.
        init_registry()
        with registry_session() as session:
            ensure_default_tenant(session)

        for cog in COGS:
            await self.load_extension(cog)
            log.info("Loaded %s", cog)

        # Bind every interaction's guild to its unit database before commands run.
        self.tree.interaction_check = self._bind_interaction

        self._register_persistent_views()
        await self._sync_commands()

    async def _bind_interaction(self, interaction: discord.Interaction) -> bool:
        from db.context import set_current_db_url
        from tenancy.routing import db_url_for_guild

        url = db_url_for_guild(interaction.guild_id)
        if url is None:
            # Not a registered unit — refuse rather than touch another unit's data.
            try:
                if interaction.type == discord.InteractionType.application_command:
                    await interaction.response.send_message(
                        "This server isn't a registered ValorLink unit.", ephemeral=True
                    )
            except discord.HTTPException:
                pass
            return False
        set_current_db_url(url)
        return True

    async def _sync_commands(self):
        guild_ids = set(registered_guild_ids())
        if config.GUILD_ID:
            guild_ids.add(config.GUILD_ID)

        if not guild_ids:
            await self.tree.sync()
            return
        for gid in guild_ids:
            obj = discord.Object(id=gid)
            self.tree.copy_global_to(guild=obj)
            await self.tree.sync(guild=obj)
        log.info("Synced commands to %d guild(s)", len(guild_ids))

    def _register_persistent_views(self):
        """Re-register interactive views for every unit, reading each unit's
        own database. A view's button callback binds its guild's database when
        clicked, so the same view class serves every unit correctly."""
        from cogs.events import RSVPView
        from cogs.recruitment import InterviewView

        with registry_session() as rs:
            units = [t.db_url for t in all_tenants(rs)]

        total_events = total_interviews = 0
        for db_url in units:
            token = set_current_db_url(db_url)
            try:
                with db_session() as session:
                    for event in session.query(Event).filter(Event.message_id.isnot(None)).all():
                        self.add_view(RSVPView(event.id), message_id=event.message_id)
                        total_events += 1
                    for c in session.query(Candidacy).filter(Candidacy.message_id.isnot(None)).all():
                        self.add_view(InterviewView(c.discord_id, c.callsign), message_id=c.message_id)
                        total_interviews += 1
            finally:
                reset_current_db_url(token)
        log.info(
            "Re-registered %d RSVP view(s) and %d interview view(s) across %d unit(s)",
            total_events, total_interviews, len(units),
        )

    async def on_guild_join(self, guild: discord.Guild):
        """When invited to a new unit's server, sync commands immediately so
        they appear without waiting for a bot restart."""
        from tenancy.routing import invalidate

        invalidate()  # a new unit may have just been registered for this guild
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to newly joined guild %s (%s)", guild.name, guild.id)
        except discord.HTTPException:
            log.exception("Failed to sync commands to guild %s", guild.id)

    async def on_ready(self):
        log.info("ValorLink is online as %s", self.user)


async def main():
    if not config.BOT_TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")

    bot = ValorLink()
    async with bot:
        await bot.start(config.BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
