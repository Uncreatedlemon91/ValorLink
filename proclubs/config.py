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
OAUTH_ENABLED = bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET
                     and DISCORD_OAUTH_REDIRECT and DISCORD_GUILD_ID)

# --- Twitch (streamer showcase) -------------------------------------------- #
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "")
TWITCH_ENABLED = bool(TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET)

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
