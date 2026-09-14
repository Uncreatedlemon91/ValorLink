"""Configuration for the Pro Clubs team site.

Deliberately isolated from ValorLink's own config.py / .env, matching the
existing principle for this app: its own venv, own service, own subdomain,
own secrets. Now that the site has accounts and third-party API keys, it
does need a `.env` (the original stats-only tool didn't).
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# --- Site identity --------------------------------------------------------- #
SITE_NAME = os.getenv("SITE_NAME", "YeeHaw FC")
SITE_TAGLINE = os.getenv("SITE_TAGLINE", "Pro Clubs")

# --- Our team, for the locked-in stats dashboard --------------------------- #
# No more "search any club" -- this site is one team's home, so its own EA
# club is configured once here rather than typed into a search box.
CLUB_PLATFORM = os.getenv("CLUB_PLATFORM", "common-gen5")
CLUB_ID = os.getenv("CLUB_ID", "")

# --- League table (auto-built from clubs we actually play) ----------------- #
# EA's API has no real league/region grouping to query (see ea_client.py), so
# there's no way to ask it for "every team in NA East 2" -- instead, poll.py
# grows this roster on its own from real opponents (see db.sync_league_roster),
# capped here so poll runtime and EA API load stay bounded regardless of how
# many different clubs get faced over a season.
LEAGUE_TABLE_MAX_TEAMS = int(os.getenv("LEAGUE_TABLE_MAX_TEAMS", "25"))

# --- Discord OAuth2 (staff sign-in) ---------------------------------------- #
# A single guild, unlike ValorLink's multi-tenant auth -- this site belongs to
# one team's one Discord server, so "is this person staff" is just "do they
# hold the configured role in that one guild."
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

# --- Discord Scheduled Events sync (fixtures) ------------------------------- #
# One-directional: Discord's own Scheduled Events are the source of truth,
# mirrored in as site Events (see discord_events.py / discord_events_poll.py).
# Reuses the same DISCORD_GUILD_ID as OAuth above. DISCORD_BOT_TOKEN is
# deliberately the same token the main ValorLink bot already uses -- a
# reused secret, by explicit choice, not a separately-registered bot (see
# proclubs/README.md for the tradeoff that was accepted here).
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_EVENTS_SYNC_ENABLED = bool(DISCORD_BOT_TOKEN and DISCORD_GUILD_ID)

# --- Discord clips sync (Clips page) ----------------------------------------- #
# One-directional, same shape as the events sync above: video files posted
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

# --- Public site URL ---------------------------------------------------------
# The absolute https URL this site is reachable at. Only needed where an
# absolute link is required rather than a relative one -- currently just the
# Discord announcement above (an embed's url/image fields must be absolute).
# Not derived from a request's Host header: gunicorn/uvicorn here aren't
# configured to trust proxy headers from Caddy, so request.url.scheme would
# report "http" even in production; explicit is more reliable than clever.
SITE_BASE_URL = os.getenv("SITE_BASE_URL", "").rstrip("/")

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
# Defaulted to FC 27 ahead of its 25 Sep 2026 release. Twitch's category
# name for a new title is not published in advance and this one has NOT been
# verified against a live category page -- check the slug on launch day and
# override here if it differs. Until FC 27 streams actually exist, a roster
# that streams FC 26 will read as offline: set TWITCH_GAME_FILTER back to
# "EA Sports FC 26" (or blank, to drop the filter) if that matters before
# the changeover.
TWITCH_GAME_FILTER = os.getenv("TWITCH_GAME_FILTER", "EA Sports FC 27")

# --- Sessions --------------------------------------------------------------- #
# Opt-in, not opt-out -- matching ValorLink's own WEB_HTTPS_ONLY default.
# A "secure" session cookie is silently dropped by browsers/HTTP clients over
# plain HTTP, which would break local dev and the DEV_LOGIN flow if this
# defaulted on. Set HTTPS_ONLY=1 in production (it always terminates behind
# Caddy over HTTPS there).
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
HTTPS_ONLY = os.getenv("HTTPS_ONLY", "").lower() in ("1", "true", "yes")

# Local-only "act as staff" login, mirroring ValorLink's WEB_DEV_LOGIN --
# never reachable in production since it requires this exact env var.
DEV_LOGIN_ENABLED = os.getenv("DEV_LOGIN", "").lower() in ("1", "true", "yes")
