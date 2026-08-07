"""One-shot poller: mirror Discord's Guild Scheduled Events into site
Events. Run on a schedule via systemd (see
deploy/proclubs-discord-events-poll.service + .timer) -- deliberately NOT
part of the FastAPI app process, same reasoning as poll.py: this app has
no always-on bot/gateway connection, so polling is the only way to notice
a change made in Discord.

Run it manually to test: python discord_events_poll.py
"""
import config
import discord_events
import services
from database import get_session, init_db


def main():
    if not config.DISCORD_EVENTS_SYNC_ENABLED:
        print("DISCORD_BOT_TOKEN/DISCORD_GUILD_ID not set -- nothing to sync")
        return

    init_db()

    try:
        events = discord_events.list_scheduled_events()
    except discord_events.DiscordApiError as exc:
        print(f"could not fetch Discord's scheduled events: {exc}")
        return

    with get_session() as session:
        result = services.sync_discord_events(session, events)

    print(
        f"synced {len(events)} Discord event(s): "
        f"{result['created']} created, {result['updated']} updated, {result['removed']} removed"
    )


if __name__ == "__main__":
    main()
