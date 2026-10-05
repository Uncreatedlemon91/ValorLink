"""One-shot poller: widen each upcoming event's thread on schedule.

An event's sign-up thread starts closed to everyone but the first tier
(see config.EVENT_INVITE_TIERS). This run walks the upcoming fixtures and,
for each rung of the ladder whose moment has arrived, adds that role's
members to the thread and pings them in it.

Run on a schedule via systemd (deploy/proclubs-event-invites-poll.service
+ .timer) rather than inside the FastAPI process -- same reasoning as the
other pollers here: this app has no always-on bot, so a timer is what
makes time-based work happen.

The first tier does NOT come from here. It fires the moment staff announce
the event (app.py's _announce_event), because waiting up to ten minutes to
tell the people who get first pick would be a strange way to run it.

Run it manually to test: python event_invites_poll.py
"""
import config
import discord_rsvp
import services
from database import get_session, init_db


def invite_tier(event, tier: dict, site_url: str) -> int:
    """Add one tier's role to an event's thread and ping it there.

    Returns how many members were added. The add happens before the ping so
    that nobody is notified about a thread they still cannot open.
    """
    added = discord_rsvp.invite_role_to_thread(event.discord_channel_id, tier["role_id"])
    discord_rsvp.ping_tier(event.discord_channel_id, tier["role_id"], event, site_url)
    return added


def main():
    if not config.EVENT_STAGED_INVITES_ENABLED:
        print("staged invites not configured -- need DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, "
              "EVENT_THREAD_CHANNEL_ID and EVENT_INVITE_TIERS")
        return
    if not config.SITE_BASE_URL:
        print("SITE_BASE_URL is not set -- the ping needs an absolute link to the event")
        return

    init_db()
    tiers = config.EVENT_INVITE_TIERS
    print(f"invite ladder: {', '.join(t['key'] for t in tiers)}")

    invited = 0
    with get_session() as session:
        for event in services.events_awaiting_invites(session):
            for tier in services.due_invite_tiers(session, event, tiers):
                site_url = f"{config.SITE_BASE_URL}/events/{event.id}"
                try:
                    added = invite_tier(event, tier, site_url)
                except discord_rsvp.DiscordApiError as exc:
                    # Leave the tier unrecorded so the next run retries it.
                    # A tier that never fires is recoverable; one recorded
                    # as done without anyone being told is not.
                    print(f"event {event.id} tier {tier['key']}: {exc}")
                    continue
                services.record_tier_invite(session, event, tier, added)
                invited += 1
                print(f"event {event.id} ({event.title!r}) tier {tier['key']}: "
                      f"added {added} member(s)")
                if added == 0:
                    print("  added nobody -- if that role has members, the bot is probably "
                          "missing the GUILD_MEMBERS privileged intent")

    print(f"done -- {invited} tier(s) invited")


if __name__ == "__main__":
    main()
