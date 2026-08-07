"""One-shot poller: mirror video clips from a Discord channel onto the
site's Clips page. Run on a schedule via systemd (see
deploy/proclubs-clips-poll.service + .timer) -- deliberately NOT part of
the FastAPI app process, same reasoning as poll.py/discord_events_poll.py:
this app has no always-on bot/gateway connection, so polling is the only
way to notice a new clip -- and the only way to refresh a clip's video URL
before Discord's signed link expires.

Run it manually to test: python discord_clips_poll.py
"""
import config
import discord_clips
import services
from database import get_session, init_db


def main():
    if not config.CLIPS_SYNC_ENABLED:
        print("DISCORD_BOT_TOKEN/CLIPS_CHANNEL_ID not set -- nothing to sync")
        return

    init_db()

    try:
        messages = discord_clips.list_recent_messages(config.CLIPS_CHANNEL_ID)
    except discord_clips.DiscordApiError as exc:
        print(f"could not fetch messages from the clips channel: {exc}")
        return

    with get_session() as session:
        result = services.sync_clips(session, config.CLIPS_CHANNEL_ID, messages)

    print(
        f"scanned {len(messages)} message(s) in the clips channel: "
        f"{result['created']} new clip(s), {result['updated']} refreshed"
    )


if __name__ == "__main__":
    main()
