"""
ValorLink configuration.

Only true secrets live here -- everything else (regiment identity, roles,
channels, ranks, companies, behavior tunables) is stored in the database and
configured live in Discord via /config, /rank, and /company. Set
DISCORD_BOT_TOKEN, GUILD_ID, and DATABASE_URL in your environment (see
.env.example), then run the bot and configure the rest from chat -- no file
edits or restarts required.
"""
import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///valorlink.db")
