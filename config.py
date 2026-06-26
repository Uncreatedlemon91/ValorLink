"""
ValorLink configuration.

This bot is wired for a SINGLE War of Rights regiment. Replace every
PLACEHOLDER value below with the real role/channel IDs from your Discord
server (enable Developer Mode in Discord settings, then right-click a
role/channel -> Copy ID).

Secrets (bot token, database URL) come from environment variables -- see
.env.example. Everything else below is plain config because this bot is
meant to be edited per-deployment, not driven by a setup wizard.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Secrets / environment
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///valorlink.db")

# ---------------------------------------------------------------------------
# Regiment identity -- edit these for your unit
# ---------------------------------------------------------------------------
REGIMENT_NAME = "YOUR REGIMENT"
REGIMENT_MOTTO = "YOUR MOTTO"
BRAND_COLOR = 0x2F3136

# ---------------------------------------------------------------------------
# Discord IDs -- PLACEHOLDER_* must be replaced before running the bot
# ---------------------------------------------------------------------------
ADMIN_ROLE_ID = 0            # PLACEHOLDER: full bot admin (config, overrides)
OFFICER_ROLE_ID = 0          # PLACEHOLDER: can promote/demote/discipline/assign
RECRUITER_ROLE_ID = 0        # PLACEHOLDER: can process applications
MEMBER_ROLE_ID = 0           # PLACEHOLDER: base "enlisted" role granted on approval
CANDIDATE_ROLE_ID = 0        # PLACEHOLDER: applied, awaiting interview
VISITOR_ROLE_ID = 0          # PLACEHOLDER: auto-assigned to new joins (onboarding)
INACTIVE_ROLE_ID = 0         # PLACEHOLDER: tagged onto members flagged inactive

RECRUITMENT_CHANNEL_ID = 0   # PLACEHOLDER: where the "Apply" button lives
PERSONNEL_FORUM_ID = 0       # PLACEHOLDER: forum/channel for member dossiers
ROSTER_CHANNEL_ID = 0        # PLACEHOLDER: live auto-updating roster embed
MOD_LOG_CHANNEL_ID = 0       # PLACEHOLDER: warn/note/strike audit log
ADMIN_LOG_CHANNEL_ID = 0     # PLACEHOLDER: enlistment/denial audit log
ANNOUNCEMENTS_CHANNEL_ID = 0 # PLACEHOLDER: /event posts go here
WELCOME_CHANNEL_ID = 0       # PLACEHOLDER: onboarding welcome message

# ---------------------------------------------------------------------------
# Rank ladder -- ordered LOWEST to HIGHEST. Add, remove, rename, or reorder
# freely. `tier` is for display grouping only; command logic (promote/
# demote) uses list order. `role_id` is the Discord role auto-synced onto a
# member when they hold that rank -- leave as 0 to skip role sync for a rank.
# ---------------------------------------------------------------------------
RANKS = [
    {"name": "Private", "abbreviation": "Pvt", "tier": "Enlisted", "role_id": 0},
    {"name": "Corporal", "abbreviation": "Cpl", "tier": "NCO", "role_id": 0},
    {"name": "Sergeant", "abbreviation": "Sgt", "tier": "NCO", "role_id": 0},
    {"name": "Lieutenant", "abbreviation": "Lt", "tier": "Officer", "role_id": 0},
    {"name": "Captain", "abbreviation": "Capt", "tier": "Officer", "role_id": 0},
]
DEFAULT_RANK = RANKS[0]["name"]

# ---------------------------------------------------------------------------
# Companies / sub-units -- edit to match your regiment's structure.
# COMPANY_ROLES maps each company to the Discord role auto-synced onto its
# members; leave a company's value as 0 (or omit it) to skip role sync.
# ---------------------------------------------------------------------------
COMPANIES = ["Company A", "Company B", "Headquarters"]
DEFAULT_COMPANY = "Unassigned"
COMPANY_ROLES = {
    "Company A": 0,    # PLACEHOLDER
    "Company B": 0,    # PLACEHOLDER
    "Headquarters": 0, # PLACEHOLDER
}

# ---------------------------------------------------------------------------
# Behavior tunables
# ---------------------------------------------------------------------------
INACTIVITY_DAYS_THRESHOLD = 30
