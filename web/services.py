"""Officer actions, performed from the web UI.

Each function does the *data* half of an operation -- exactly what the
matching slash command writes to the database, including the audit trail --
then enqueues a PendingAction describing the *Discord* half (role swaps,
nickname rewrites, roster refreshes, announcements) for the bot to apply.

Keeping the wording and ordering identical to the cogs means an action taken
on the website is indistinguishable from the same slash command.

Validation failures raise ActionError with a message safe to show the user.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from db.models import (
    Candidacy,
    DisciplinaryRecord,
    Member,
    ServiceHistoryEntry,
)
from utils import queue
from utils import ranks as rank_utils
from utils.settings import (
    company_by_name,
    default_company_name,
    get_config,
    list_companies,
)


class ActionError(Exception):
    """A user-facing validation failure."""


def _member(session, discord_id: int) -> Member:
    record = session.get(Member, discord_id)
    if record is None:
        raise ActionError("That member has no personnel record.")
    return record


def _log(session, member_id: int, entry: str, actor: dict):
    session.add(
        ServiceHistoryEntry(member_id=member_id, entry=entry, recorded_by=actor["id"])
    )


# --- Rank ---------------------------------------------------------------- #
def change_rank(session, actor: dict, discord_id: int, new_rank: str, citation: str = "") -> str:
    new_record = rank_utils.rank_by_name(session, new_rank)
    if new_record is None:
        raise ActionError(f"Unknown rank: {new_rank}")
    record = _member(session, discord_id)
    old_rank = record.rank
    callsign = record.callsign
    if old_rank == new_rank:
        raise ActionError(f"{callsign} already holds {new_rank}.")

    old_record = rank_utils.rank_by_name(session, old_rank)
    old_position = old_record.position if old_record else -1
    is_promotion = new_record.position > old_position

    record.rank = new_rank
    verb = "Promoted" if is_promotion else "Stepped down"
    entry = f"{verb} from {old_rank} to {new_rank} by {actor['name']}."
    if citation:
        entry += f" Citation: {citation}"
    _log(session, discord_id, entry, actor)

    if is_promotion:
        billboard = f"**{callsign}** has been promoted to **{new_rank}**."
    else:
        billboard = f"**{callsign}** has stepped down from the rank of **{old_rank}**."
    if citation:
        billboard += f" {citation}"

    queue.enqueue(
        session,
        queue.SYNC_RANK,
        {"discord_id": discord_id, "callsign": callsign,
         "old_rank": old_rank, "new_rank": new_rank, "billboard": billboard},
        actor_id=actor["id"],
    )
    session.commit()
    action = "promoted to" if is_promotion else "stepped down from"
    return f"{callsign} {action} {new_rank}."


def set_rank(session, actor: dict, discord_id: int, new_rank: str, citation: str = "") -> str:
    if rank_utils.rank_by_name(session, new_rank) is None:
        raise ActionError(f"Unknown rank: {new_rank}")
    record = _member(session, discord_id)
    old_rank = record.rank
    callsign = record.callsign
    record.rank = new_rank
    entry = f"Rank set from {old_rank} to {new_rank} by {actor['name']}."
    if citation:
        entry += f" {citation}"
    _log(session, discord_id, entry, actor)
    queue.enqueue(
        session,
        queue.SYNC_RANK,
        {"discord_id": discord_id, "callsign": callsign,
         "old_rank": old_rank, "new_rank": new_rank, "billboard": None},
        actor_id=actor["id"],
    )
    session.commit()
    return f"{callsign}'s rank set to {new_rank}."


# --- Company ------------------------------------------------------------- #
def assign_company(session, actor: dict, discord_id: int, company: str) -> str:
    if company_by_name(session, company) is None:
        raise ActionError(f"Unknown company: {company}")
    record = _member(session, discord_id)
    old_company = record.company
    callsign = record.callsign
    if old_company == company:
        raise ActionError(f"{callsign} is already in {company}.")
    record.company = company
    _log(session, discord_id,
         f"Transferred from {old_company} to {company} by {actor['name']}.", actor)
    queue.enqueue(
        session,
        queue.SYNC_COMPANY,
        {"discord_id": discord_id, "old_company": old_company, "new_company": company,
         "billboard": f"**{callsign}** has been assigned to **{company}**."},
        actor_id=actor["id"],
    )
    session.commit()
    return f"{callsign} assigned to {company}."


# --- Service log --------------------------------------------------------- #
def service_log(session, actor: dict, discord_id: int, entry: str) -> str:
    record = _member(session, discord_id)
    entry = entry.strip()
    if not entry:
        raise ActionError("The service-log entry can't be empty.")
    _log(session, discord_id, entry, actor)
    queue.enqueue(session, queue.REFRESH_PERSONNEL, {"discord_id": discord_id}, actor_id=actor["id"])
    session.commit()
    return f"Logged entry for {record.callsign}."


# --- Discipline ---------------------------------------------------------- #
def discipline(session, actor: dict, discord_id: int, record_type: str, reason: str) -> str:
    if record_type not in ("note", "warn", "strike"):
        raise ActionError("Unknown record type.")
    record = _member(session, discord_id)
    reason = reason.strip()
    if not reason:
        raise ActionError("A reason is required.")
    session.add(
        DisciplinaryRecord(
            member_id=discord_id, record_type=record_type, reason=reason, issued_by=actor["id"]
        )
    )
    queue.enqueue(
        session,
        queue.DISCIPLINE,
        {"discord_id": discord_id, "record_type": record_type, "reason": reason,
         "issued_by": actor["id"],
         "dm": f"You received a **{record_type}**: {reason}"},
        actor_id=actor["id"],
    )
    session.commit()
    return f"{record_type.capitalize()} issued to {record.callsign}."


# --- Lifecycle ----------------------------------------------------------- #
def discharge(session, actor: dict, discord_id: int, discharge_type: str, reason: str) -> str:
    if discharge_type not in ("honorable", "dishonorable"):
        raise ActionError("Choose an honorable or dishonorable discharge.")
    reason = reason.strip()
    if not reason:
        raise ActionError("A reason is required for a discharge.")
    record = _member(session, discord_id)
    if record.status == "discharged":
        raise ActionError(f"{record.callsign} is already discharged.")

    callsign = record.callsign
    verb = "Honorably" if discharge_type == "honorable" else "Dishonorably"
    record.status = "discharged"
    _log(session, discord_id,
         f"{verb} discharged by {actor['name']}. Reason: {reason}", actor)
    queue.enqueue(
        session,
        queue.DISCHARGE,
        {"discord_id": discord_id, "callsign": callsign, "rank_at_discharge": record.rank,
         "verb": verb, "reason": reason, "actor_id": actor["id"],
         "billboard": f"**{callsign}** has been {verb.lower()} discharged."},
        actor_id=actor["id"],
    )
    session.commit()
    return f"{callsign} has been {verb.lower()} discharged."


def reinstate(session, actor: dict, discord_id: int, reason: str = "") -> str:
    record = _member(session, discord_id)
    if record.status != "discharged":
        raise ActionError(
            f"{record.callsign} is not discharged (current status: {record.status})."
        )
    callsign = record.callsign
    reason = reason.strip()
    record.status = "active"
    record.last_active_date = datetime.utcnow()
    entry = f"Reinstated by {actor['name']}."
    if reason:
        entry += f" Reason: {reason}"
    _log(session, discord_id, entry, actor)
    queue.enqueue(
        session,
        queue.REINSTATE,
        {"discord_id": discord_id, "callsign": callsign, "reason": reason,
         "actor_id": actor["id"],
         "billboard": f"**{callsign}** has been reinstated to active duty."},
        actor_id=actor["id"],
    )
    session.commit()
    return f"{callsign} has been reinstated."


def loa(session, actor: dict, discord_id: int, days: int, reason: str = "") -> str:
    if days < 1 or days > 180:
        raise ActionError("Leave must be between 1 and 180 days.")
    record = _member(session, discord_id)
    if record.status == "loa":
        raise ActionError(f"{record.callsign} is already on leave.")
    if record.status == "discharged":
        raise ActionError(f"{record.callsign} has been discharged.")

    callsign = record.callsign
    loa_until = datetime.utcnow() + timedelta(days=days)
    until_str = loa_until.strftime("%d %b %Y")
    record.status = "loa"
    record.loa_until = loa_until
    reason = reason.strip()
    entry = f"Placed on leave of absence until {until_str} by {actor['name']}."
    if reason:
        entry += f" Reason: {reason}"
    _log(session, discord_id, entry, actor)
    queue.enqueue(
        session,
        queue.LOA,
        {"discord_id": discord_id, "callsign": callsign, "until": until_str,
         "dm": f"You have been placed on leave of absence until **{until_str}**.",
         "billboard": f"**{callsign}** is on leave of absence until {until_str}."},
        actor_id=actor["id"],
    )
    session.commit()
    return f"{callsign} placed on leave until {until_str}."


def loa_end(session, actor: dict, discord_id: int) -> str:
    record = _member(session, discord_id)
    if record.status != "loa":
        raise ActionError(f"{record.callsign} is not currently on leave.")
    callsign = record.callsign
    record.status = "active"
    record.loa_until = None
    record.last_active_date = datetime.utcnow()
    _log(session, discord_id,
         f"Returned from leave early (ended by {actor['name']}).", actor)
    queue.enqueue(
        session,
        queue.LOA_END,
        {"discord_id": discord_id, "callsign": callsign,
         "billboard": f"**{callsign}** has returned from leave of absence."},
        actor_id=actor["id"],
    )
    session.commit()
    return f"{callsign} is back on active duty."


# --- Recruitment --------------------------------------------------------- #
def approve_candidate(session, actor: dict, discord_id: int) -> str:
    candidacy = session.get(Candidacy, discord_id)
    if candidacy is None:
        raise ActionError("That applicant is no longer in the recruitment queue.")
    callsign = candidacy.callsign
    default_rank = rank_utils.default_rank_name(session)
    default_company = default_company_name(session)
    regiment_name = get_config(session).regiment_name

    session.merge(
        Member(
            discord_id=discord_id,
            callsign=callsign,
            rank=default_rank,
            company=default_company,
            status="active",
            thread_id=None,
        )
    )
    _log(session, discord_id,
         f"Enlisted as {default_rank} via recruitment pipeline.", actor)
    session.delete(candidacy)
    queue.enqueue(
        session,
        queue.APPROVE_CANDIDATE,
        {"discord_id": discord_id, "callsign": callsign, "actor_id": actor["id"],
         "default_rank": default_rank, "default_company": default_company,
         "billboard": f"**{callsign}** has enlisted in the regiment.",
         "dm": f"Your application to **{regiment_name}** has been approved. "
               f"Welcome to the regiment!"},
        actor_id=actor["id"],
    )
    session.commit()
    return f"{callsign} enlisted as {default_rank}."


def deny_candidate(session, actor: dict, discord_id: int) -> str:
    candidacy = session.get(Candidacy, discord_id)
    if candidacy is None:
        raise ActionError("That applicant is no longer in the recruitment queue.")
    callsign = candidacy.callsign
    regiment_name = get_config(session).regiment_name
    session.delete(candidacy)
    queue.enqueue(
        session,
        queue.DENY_CANDIDATE,
        {"discord_id": discord_id, "callsign": callsign, "actor_id": actor["id"],
         "dm": f"Your application to **{regiment_name}** has been denied at this time."},
        actor_id=actor["id"],
    )
    session.commit()
    return f"{callsign}'s application was denied."


# --- Option sources for the UI ------------------------------------------ #
def rank_options(session) -> list[str]:
    return rank_utils.rank_names(session)


def company_options(session) -> list[str]:
    return [c.name for c in list_companies(session)]
