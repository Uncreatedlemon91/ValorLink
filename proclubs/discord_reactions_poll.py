"""One-shot poller: refresh cached reaction counts on articles' Discord
announcement messages. Run on a schedule via systemd (see
deploy/proclubs-reactions-poll.service + .timer) -- same reasoning as
poll.py/discord_clips_poll.py: this app has no always-on bot/gateway
connection, so REST polling is the only way to notice someone reacted.

Every emoji is summed into one total (see discord_announce.fetch_reaction_count)
-- it doesn't track which specific emoji people used, just how many
reactions the post got, shown on the site as a single heart count.

Run it manually to test: python discord_reactions_poll.py
"""
import config
import discord_announce
import services
from database import get_session, init_db


def main():
    if not config.NEWS_ANNOUNCE_ENABLED:
        print("DISCORD_BOT_TOKEN/NEWS_ANNOUNCE_CHANNEL_ID not set -- nothing to poll")
        return

    init_db()

    checked = updated = failed = 0
    with get_session() as session:
        articles = services.articles_with_discord_message(session, config.DISCORD_REACTIONS_POLL_LIMIT)
        for article in articles:
            checked += 1
            try:
                count = discord_announce.fetch_reaction_count(
                    config.NEWS_ANNOUNCE_CHANNEL_ID, article.discord_message_id,
                )
            except discord_announce.DiscordApiError as exc:
                # One article's message being gone/unreachable (e.g.
                # deleted from Discord) shouldn't stop the rest from
                # updating -- keep whatever count it last had.
                print(f"[{article.slug}] could not fetch reactions: {exc}")
                failed += 1
                continue
            if count != article.discord_reaction_count:
                article.discord_reaction_count = count
                updated += 1
        session.commit()

    print(f"checked {checked} article(s): {updated} reaction count(s) changed, {failed} failed")


if __name__ == "__main__":
    main()
