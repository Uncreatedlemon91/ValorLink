"""The action queue that lets the web UI drive the bot.

The web app changes regiment *data* directly in the database, then enqueues
one of these actions to describe the Discord side-effect it needs. The bot's
bridge cog (cogs/bridge.py) drains the queue and applies each one, reusing
the same sync helpers the slash commands use, so a promotion done on the
website swaps roles and rewrites nicknames exactly as `/promote` would.

This module deliberately imports nothing beyond the database layer so both
the bot and the web app can use it without dragging in each other's deps.
"""
import json

from db.models import PendingAction

# --- Action names -------------------------------------------------------- #
SYNC_RANK = "sync_rank"                 # swap rank role + rewrite nickname
SYNC_COMPANY = "sync_company"           # swap company role
DISCIPLINE = "discipline"               # mod-log embed + DM
DISCHARGE = "discharge"                 # strip roles, lock dossier, log
REINSTATE = "reinstate"                 # restore member role, unlock dossier
LOA = "loa"                             # DM + billboard
LOA_END = "loa_end"                     # billboard
APPROVE_CANDIDATE = "approve_candidate" # roles + dossier thread + welcome DM
DENY_CANDIDATE = "deny_candidate"       # admin log + rejection DM
REFRESH_PERSONNEL = "refresh_personnel" # re-render the dossier embed only
ANNOUNCE_EVENT = "announce_event"       # post the RSVP announcement + register view
REFRESH_EVENT = "refresh_event"         # re-render an edited event's existing embed
DELETE_EVENT = "delete_event"           # remove a deleted event's announcement message
AWARD_GRANTED = "award_granted"         # refresh dossier + billboard
AWARD_REVOKED = "award_revoked"         # refresh dossier
POST_ANNOUNCEMENT = "post_announcement" # post an officer's announcement embed
IMPORT_ROSTER = "import_roster"         # create records for current Discord members
ASSIGN_ROLE = "assign_role"             # add a secondary-assignment Discord role
UNASSIGN_ROLE = "unassign_role"         # remove a secondary-assignment Discord role
PLATFORM_BROADCAST = "platform_broadcast"  # platform-wide update posted to admin log

# Statuses
PENDING = "pending"
DONE = "done"
FAILED = "failed"


def enqueue(session, action: str, payload: dict, actor_id: int | None = None) -> PendingAction:
    """Add an action to the queue. Caller is responsible for committing."""
    row = PendingAction(
        action=action,
        payload=json.dumps(payload),
        status=PENDING,
        attempts=0,
        actor_id=actor_id,
    )
    session.add(row)
    return row
