"""Configuration for the Pro Clubs team site.

Everything the site reads from the environment, in one place. It has its
own venv, its own service and its own secrets; the original stats-only
tool needed no `.env` at all, but a site with accounts and third-party
API keys does.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# --- Site identity --------------------------------------------------------- #
SITE_NAME = os.getenv("SITE_NAME", "YeeHaw FC")
SITE_TAGLINE = os.getenv("SITE_TAGLINE", "Pro Clubs")
# The short code that prefixes every page's kicker ("YFC / SQUAD") and the
# line under the crest in the sidebar.
SITE_SHORT = os.getenv("SITE_SHORT", "YFC")
SITE_MOTTO = os.getenv("SITE_MOTTO", "Earn the shirt. Play the system.")

# --- Our team, for the locked-in stats dashboard --------------------------- #
# No more "search any club" -- this site is one team's home, so its own EA
# club is configured once here rather than typed into a search box.
CLUB_PLATFORM = os.getenv("CLUB_PLATFORM", "common-gen5")
CLUB_ID = os.getenv("CLUB_ID", "")
# The club's name as it appears in-game -- what season.py searches EA for,
# and the label our club gets in the stats history and the league table.
# Separate from SITE_NAME, which is the site's own branding and needn't
# match EA's casing.
#
# EA issues a brand-new club ID every title: the FC 26 club and the FC 27
# club are different clubs as far as the API is concerned, even under the
# same name. `python season.py find` looks the current one up; see there.
CLUB_NAME = os.getenv("CLUB_NAME", "Yeehaw FC")

# --- League table (auto-built from clubs we actually play) ----------------- #
# EA's API has no real league/region grouping to query (see ea_client.py), so
# there's no way to ask it for "every team in NA East 2" -- instead, poll.py
# grows this roster on its own from real opponents (see db.sync_league_roster),
# capped here so poll runtime and EA API load stay bounded regardless of how
# many different clubs get faced over a season.
LEAGUE_TABLE_MAX_TEAMS = int(os.getenv("LEAGUE_TABLE_MAX_TEAMS", "25"))

# --- Discord OAuth2 (staff sign-in) ---------------------------------------- #
# A single guild -- this site belongs to one team's one Discord server, so
# "is this person staff" is just "do they hold the configured role in that
# one guild."
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_OAUTH_REDIRECT = os.getenv("DISCORD_OAUTH_REDIRECT", "")
DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0") or "0")
DISCORD_STAFF_ROLE_ID = int(os.getenv("DISCORD_STAFF_ROLE_ID", "0") or "0")
# Public invite link, shown to anyone who isn't a guild member yet (signed
# out, or signed in with Discord but not in our server) -- see base.html's
# banner and the "Connect with us" button on the home page. Unlike the rest
# of this block, this isn't a secret, so it's fine to ship a real default.
DISCORD_INVITE_URL = os.getenv("DISCORD_INVITE_URL", "https://discord.gg/J4d7D5kDX8")
OAUTH_ENABLED = bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET
                     and DISCORD_OAUTH_REDIRECT and DISCORD_GUILD_ID)

# --- Discord bot ------------------------------------------------------------ #
# DISCORD_BOT_TOKEN is the club bot's token: full bot access, so it is the
# most sensitive value in this file (see discord_api.py). Everything below
# that talks to Discord reuses it.
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")

# --- Discord clips sync (Clips page) ----------------------------------------- #
# One-directional: video files posted
# in one configured Discord channel get mirrored onto the site's Clips
# page (see discord_clips.py / discord_clips_poll.py). Reuses
# DISCORD_BOT_TOKEN above -- no separate credential needed.
CLIPS_CHANNEL_ID = os.getenv("CLIPS_CHANNEL_ID", "")
CLIPS_SYNC_ENABLED = bool(DISCORD_BOT_TOKEN and CLIPS_CHANNEL_ID)

# --- Discord event RSVP announcements ---------------------------------------
# Two-directional, unlike everything else here. The site posts an event's
# announcement with sign-up buttons (site -> Discord), and Discord delivers
# each button press straight back to /discord/interactions (Discord -> site)
# as a signed HTTP request -- a webhook, not a gateway connection, which is
# why this still needs no always-on bot process.
#
# DISCORD_PUBLIC_KEY is the Discord *application's* public key (Developer
# Portal -> General Information), used to verify those requests really came
# from Discord. It is not a secret -- verification is a signature check, not
# a shared password -- but interactions are refused without it, since an
# unverified endpoint would let anyone forge sign-ups.
EVENTS_ANNOUNCE_CHANNEL_ID = os.getenv("EVENTS_ANNOUNCE_CHANNEL_ID", "")
DISCORD_PUBLIC_KEY = os.getenv("DISCORD_PUBLIC_KEY", "")

# --- Staged event threads -------------------------------------------------- #
# An event's sign-up post can live in its own thread rather than loose in a
# channel, so each fixture keeps its own conversation and its own audience.
# Set this to the parent channel's ID and announcing an event creates a
# PRIVATE thread in it; leave it blank and the post goes straight into
# EVENTS_ANNOUNCE_CHANNEL_ID as before.
#
# Read before EVENT_RSVP_ENABLED below because that gate depends on it:
# either channel is somewhere to put the post, and demanding the one you
# aren't using is how you get "sign-ups aren't configured" while staring at
# a perfectly good configuration.
EVENT_THREAD_CHANNEL_ID = os.getenv("EVENT_THREAD_CHANNEL_ID", "")


def event_rsvp_missing() -> list[str]:
    """Which settings sign-ups are still waiting on, named individually.

    A single "see X and Y in .env" message sends people to check settings
    they have already filled in; this reports only what is actually
    missing. Recomputed on call rather than frozen at import so tests (and
    anyone poking at config in a shell) see the truth after a monkeypatch.
    """
    missing = []
    if not DISCORD_BOT_TOKEN:
        missing.append("DISCORD_BOT_TOKEN")
    if not (EVENTS_ANNOUNCE_CHANNEL_ID or EVENT_THREAD_CHANNEL_ID):
        # Either is a place to post; naming both makes the choice clear.
        missing.append("EVENTS_ANNOUNCE_CHANNEL_ID or EVENT_THREAD_CHANNEL_ID")
    if not DISCORD_PUBLIC_KEY:
        missing.append("DISCORD_PUBLIC_KEY")
    return missing


EVENT_RSVP_ENABLED = not event_rsvp_missing()


def _parse_invite_tiers(raw: str) -> list[dict]:
    """Parse EVENT_INVITE_TIERS into the staged invite ladder.

    Format is a comma-separated list of `<when>:<role_id>`, where `<when>`
    is either `create` (fire as soon as the event is announced) or a number
    of hours before kick-off:

        EVENT_INVITE_TIERS=create:111,48:222,24:333

    Each tier mentions its role in the thread and adds that role's members
    to it, so access widens as the fixture approaches -- first pick to the
    first tier, then the next group, and so on.

    Malformed entries are dropped rather than raised: a typo in one tier
    shouldn't stop the whole app booting, and the poller logs what it
    actually loaded. Ordered earliest-acting first (create, then the
    largest hours-before), which is the order they fire in.
    """
    tiers: list[dict] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        when, _, role_id = chunk.partition(":")
        when, role_id = when.strip().lower(), role_id.strip()
        if not role_id.isdigit():
            continue
        if when == "create":
            tiers.append({"key": "create", "hours_before": None, "role_id": role_id})
            continue
        try:
            hours = float(when)
        except ValueError:
            continue
        if hours < 0:
            continue
        tiers.append({"key": when, "hours_before": hours, "role_id": role_id})
    # `create` first, then furthest-out hours down to nearest kick-off.
    tiers.sort(key=lambda t: (t["hours_before"] is not None, -(t["hours_before"] or 0)))
    return tiers


EVENT_INVITE_TIERS = _parse_invite_tiers(os.getenv("EVENT_INVITE_TIERS", ""))

# Listing a role's members needs the privileged GUILD_MEMBERS intent on the
# bot (Developer Portal -> Bot -> Server Members Intent). Without it the
# staged invites can still mention each role, but can't add anyone to a
# private thread -- so the whole ladder is gated on the pieces it needs.
EVENT_STAGED_INVITES_ENABLED = bool(
    DISCORD_BOT_TOKEN and DISCORD_GUILD_ID and EVENT_THREAD_CHANNEL_ID and EVENT_INVITE_TIERS
)

# --- Discord article announcements ------------------------------------------
# The announcement itself is one-directional (site -> Discord) and sent
# right when an article goes live rather than polled -- this app is the
# source of truth for the article. Reuses DISCORD_BOT_TOKEN above. See
# discord_announce.py / app.py's news_new and news_edit routes.
NEWS_ANNOUNCE_CHANNEL_ID = os.getenv("NEWS_ANNOUNCE_CHANNEL_ID", "")
NEWS_ANNOUNCE_ENABLED = bool(DISCORD_BOT_TOKEN and NEWS_ANNOUNCE_CHANNEL_ID)
# Reactions on that message are the other direction (Discord -> site) and
# DO need polling, since people react whenever -- see
# discord_reactions_poll.py. Bounded to the N most-recently-announced
# articles per run, not every article ever announced (see
# services.articles_with_discord_message).
DISCORD_REACTIONS_POLL_LIMIT = int(os.getenv("DISCORD_REACTIONS_POLL_LIMIT", "20"))

# --- Squad move announcements (contracts, staff roles, departures) ----------
# One-directional (site -> Discord), same shape as the article
# announcement above: staff pick somebody out of the Discord member list
# on /roster and publish an offer or a departure.
#
# Falls back to NEWS_ANNOUNCE_CHANNEL_ID so a deployment that already has
# an announcements channel needs no new setting; point this somewhere else
# if squad news should be separated from site news.
#
# Reading the member list needs the privileged GUILD_MEMBERS intent on the
# bot (Developer Portal -> Bot -> Server Members Intent) -- there is no
# setting for that here, it's a checkbox on Discord's side, and without it
# the page reports the 403 rather than showing an empty roster.
#
# NOTE: announcing never changes anyone's Discord roles -- see
# discord_roster.py for why that's deliberate.
ROSTER_ANNOUNCE_CHANNEL_ID = (os.getenv("ROSTER_ANNOUNCE_CHANNEL_ID", "")
                              or NEWS_ANNOUNCE_CHANNEL_ID)


def roster_moves_missing() -> list[str]:
    """Which settings squad announcements are still waiting on, named
    individually -- same reasoning as event_rsvp_missing() above."""
    missing = []
    if not DISCORD_BOT_TOKEN:
        missing.append("DISCORD_BOT_TOKEN")
    if not DISCORD_GUILD_ID:
        missing.append("DISCORD_GUILD_ID")
    if not ROSTER_ANNOUNCE_CHANNEL_ID:
        missing.append("ROSTER_ANNOUNCE_CHANNEL_ID or NEWS_ANNOUNCE_CHANNEL_ID")
    return missing


ROSTER_MOVES_ENABLED = not roster_moves_missing()

# --- Recruitment ----------------------------------------------------------- #
# Every trial note a coach writes on a prospect's file is also posted here,
# so the rest of the staff see the feedback without opening the site. A
# staff-only channel: these are frank assessments of people who aren't in
# the club yet. The bot needs View Channel, Send Messages and Embed Links.
# Set it empty to keep feedback on the site only.
RECRUITMENT_CHANNEL_ID = os.getenv("RECRUITMENT_CHANNEL_ID", "1546267802791452772").strip()
RECRUITMENT_FEEDBACK_ENABLED = bool(DISCORD_BOT_TOKEN and RECRUITMENT_CHANNEL_ID)

# When a player ACCEPTS an offer, they're given the "Squad" role set on the
# site's Discord roles page (role_settings.squad_role_id) -- triggered by
# the player themselves, and add-only. Leave that role unset and accepting
# is recorded on the site and nothing else happens in Discord. Needs the
# bot to have Manage Roles, AND its own highest role to sit ABOVE this one
# in Server Settings -> Roles; /roster shows Discord's refusal otherwise.
#
# This variable is only the old .env fallback for that role.
ROSTER_SQUAD_ROLE_ID = os.getenv("ROSTER_SQUAD_ROLE_ID", "")

# --- Discord roles the site manages (see role_sync.py) ---------------------- #
# Which Discord role is which is set by management on the site's Discord
# roles page (role_settings.py), not here. These variables are only read
# for a role that page has never saved, so an install that set them in
# .env keeps working until the page is saved once. The site never manages
# DISCORD_STAFF_ROLE_ID, whatever is configured.
MANAGED_ROLE_SETTINGS = {
    "Starter": os.getenv("ROLE_STARTER_ID", ""),
    "Rotation": os.getenv("ROLE_ROTATION_ID", ""),
    "Substitute": os.getenv("ROLE_SUBSTITUTE_ID", ""),
    "Club President": os.getenv("ROLE_CLUB_PRESIDENT_ID", ""),
    "Head Coach": os.getenv("ROLE_HEAD_COACH_ID", ""),
    "Coach": os.getenv("ROLE_COACH_ID", ""),
    "Trialist": os.getenv("ROLE_TRIALIST_ID", ""),
    "Squad": ROSTER_SQUAD_ROLE_ID,
}


def managed_role_ids(settings: dict[str, str], staff_role_id: int) -> dict[str, str]:
    """The configured roles, minus blanks, junk and the staff role -- the
    way into the site's management is never something the site removes."""
    return {
        key: value.strip() for key, value in settings.items()
        if value and value.strip().isdigit() and int(value.strip()) != staff_role_id
    }


# --- Weekly AI-written article ------------------------------------------------
# weekly_article.py (every ROUNDUP_DAYS days, via proclubs-weekly-article.timer)
# has Claude write up the period from the stats poll.py collected and publishes
# it straight to the site. It runs the Claude Code CLI headless, billed to a
# Claude subscription rather than API credits: CLAUDE_CODE_OAUTH_TOKEN (from
# `claude setup-token`) is read by the CLI itself from the environment,
# which load_dotenv() above has already populated from .env.
CLAUDE_CODE_OAUTH_TOKEN = os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "")
WEEKLY_ARTICLE_ENABLED = bool(CLAUDE_CODE_OAUTH_TOKEN)
# systemd's PATH won't include the per-user install dir, so production sets
# the absolute path (see deploy/README.md).
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
# Blank = the CLI's own default model.
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "")
# The byline these articles carry -- also how weekly_article.py recognizes
# its own previous posts, so a re-run in the same week doesn't post twice.
WEEKLY_ARTICLE_AUTHOR = os.getenv("WEEKLY_ARTICLE_AUTHOR") or f"{SITE_NAME} Desk"
# How often the roundup goes out, in days -- and so how far back each one
# looks. The timer fires daily; weekly_article.py skips the days between.
# (The "weekly" in the names above predates this and is kept so existing
# .env files and installed timers keep working.)
try:
    ROUNDUP_DAYS = max(1, int(os.getenv("ROUNDUP_DAYS", "2") or 2))
except ValueError:
    ROUNDUP_DAYS = 2

# --- Public site URL ---------------------------------------------------------
# The absolute https URL this site is reachable at. Only needed where an
# absolute link is required rather than a relative one -- currently just the
# Discord announcement above (an embed's url/image fields must be absolute).
# Not derived from a request's Host header: gunicorn/uvicorn here aren't
# configured to trust proxy headers from Caddy, so request.url.scheme would
# report "http" even in production; explicit is more reliable than clever.
SITE_BASE_URL = os.getenv("SITE_BASE_URL", "").rstrip("/")

# --- Match-week notifications (see discord_notify.py / notify_poll.py) ------ #
# Where the team sheet, the post-match vote and the match report go when a
# fixture has no Discord thread of its own. Falls back to the events
# channel, then the news channel, so an existing deployment needs nothing.
MATCHDAY_CHANNEL_ID = (os.getenv("MATCHDAY_CHANNEL_ID", "") or EVENTS_ANNOUNCE_CHANNEL_ID
                       or NEWS_ANNOUNCE_CHANNEL_ID)
# Personal messages: your shirt, a reminder to answer, a nudge to set your
# usual nights. "0" turns DMs off and keeps the channel posts.
NOTIFY_DMS = os.getenv("NOTIFY_DMS", "1").strip() not in ("0", "false", "no", "")
NOTIFY_ENABLED = bool(DISCORD_BOT_TOKEN)

# --- Twitch (streamer showcase) -------------------------------------------- #
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "")
TWITCH_ENABLED = bool(TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET)
# A showcased streamer only counts as "live" here if Twitch reports them
# playing this category -- someone from the roster live on some other game
# doesn't light up the site as if they were playing Pro Clubs. Matched
# case-insensitively (see twitch_client.py). Set to an empty string to
# disable the filter entirely (any live stream counts, regardless of game --
# the old behavior). Twitch's exact category name changes with each yearly
# title; verify it at twitch.tv/directory/category/<slug> if this ever looks
# wrong (a mismatched string just makes everyone look offline, not an error).
#
# The FC 27 category name has NOT been verified against Twitch's live
# category page -- if the whole roster reads as offline while streaming,
# check the slug and override TWITCH_GAME_FILTER in .env.
TWITCH_GAME_FILTER = os.getenv("TWITCH_GAME_FILTER", "EA Sports FC 27")

# --- Sessions --------------------------------------------------------------- #
# Opt-in, not opt-out. A "secure" session cookie is silently dropped by browsers/HTTP clients over
# plain HTTP, which would break local dev and the DEV_LOGIN flow if this
# defaulted on. Set HTTPS_ONLY=1 in production (it always terminates behind
# Caddy over HTTPS there).
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
HTTPS_ONLY = os.getenv("HTTPS_ONLY", "").lower() in ("1", "true", "yes")

# Local-only "act as staff" login -- never reachable in production, since
# it requires this exact env var.
DEV_LOGIN_ENABLED = os.getenv("DEV_LOGIN", "").lower() in ("1", "true", "yes")
