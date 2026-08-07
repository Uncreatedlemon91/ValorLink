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

# --- Discord article announcements ------------------------------------------
# One-directional (site -> Discord), and unlike the syncs above, sent right
# when an article goes live rather than polled -- this app is the source of
# truth for articles, so there's nothing to notice later. Reuses
# DISCORD_BOT_TOKEN above. See discord_announce.py / app.py's news_new and
# news_edit routes.
NEWS_ANNOUNCE_CHANNEL_ID = os.getenv("NEWS_ANNOUNCE_CHANNEL_ID", "")
NEWS_ANNOUNCE_ENABLED = bool(DISCORD_BOT_TOKEN and NEWS_ANNOUNCE_CHANNEL_ID)

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
TWITCH_GAME_FILTER = os.getenv("TWITCH_GAME_FILTER", "EA Sports FC 26")

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
