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

import json
from datetime import datetime, timedelta

from sqlalchemy import func

from db.models import (
    Assignment,
    AttendanceRecord,
    AuditEntry,
    AwardType,
    Candidacy,
    Company,
    DisciplinaryRecord,
    Event,
    Member,
    MemberAssignment,
    MemberAward,
    PendingAction,
    Rank,
    RecruitmentQuestion,
    ServiceHistoryEntry,
)
from utils import queue
from utils import ranks as rank_utils
from utils.settings import (
    CHANNEL_KEYS,
    ROLE_KEYS,
    company_by_name,
    default_company_name,
    get_config,
    list_companies,
)

EVENT_TYPES = ["Drill", "Battle", "Operation"]
ATTENDANCE_STATUSES = ["present", "absent", "excused"]
RSVP_STATUSES = {"accepted": "answered the call", "tentative": "marked tentative",
                 "declined": "sent regrets"}


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


# --- Audit trail --------------------------------------------------------- #
# The set of action categories the audit log distinguishes, for filtering.
AUDIT_CATEGORIES = [
    "rank", "company", "service", "discipline", "lifecycle", "leave",
    "recruitment", "awards", "announcement", "roster", "event", "assignment",
]


def _audit(session, actor: dict | None, category: str, summary: str,
           target_id: int | None = None, source: str = "web") -> None:
    """Record an accountability entry alongside the action's own data change.
    Added to the caller's session; committed by the caller."""
    session.add(AuditEntry(
        actor_id=actor.get("id") if actor else None,
        actor_name=actor.get("name") if actor else None,
        source=source, category=category, summary=summary, target_id=target_id,
    ))


def list_audit(session, category: str = "", limit: int = 200) -> list[AuditEntry]:
    """Recent audit entries, newest first, optionally filtered by category."""
    q = session.query(AuditEntry)
    if category:
        q = q.filter(AuditEntry.category == category)
    return q.order_by(AuditEntry.at.desc(), AuditEntry.id.desc()).limit(limit).all()


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
    record.rank_since = datetime.utcnow()
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
    _audit(session, actor, "rank", entry, target_id=discord_id)
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
    record.rank_since = datetime.utcnow()
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
    _audit(session, actor, "rank", entry, target_id=discord_id)
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
    _audit(session, actor, "company",
           f"Transferred {callsign} from {old_company} to {company}.", target_id=discord_id)
    session.commit()
    return f"{callsign} assigned to {company}."


# --- Service log --------------------------------------------------------- #
def service_log(session, actor: dict, discord_id: int, entry: str) -> str:
    record = _member(session, discord_id)
    entry = entry.strip()
    if not entry:
        raise ActionError("The service-log entry can't be empty.")
    _log(session, discord_id, entry, actor)
    _audit(session, actor, "service", f"Logged a note on {record.callsign}: {entry}",
           target_id=discord_id)
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
    _audit(session, actor, "discipline",
           f"Issued a {record_type} to {record.callsign}: {reason}", target_id=discord_id)
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
    _audit(session, actor, "lifecycle",
           f"{verb} discharged {callsign}. Reason: {reason}", target_id=discord_id)
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
    _audit(session, actor, "lifecycle", f"Reinstated {callsign} to active duty.",
           target_id=discord_id)
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
    _audit(session, actor, "leave", f"Placed {callsign} on leave until {until_str}.",
           target_id=discord_id)
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
    _audit(session, actor, "leave", f"Ended {callsign}'s leave early.", target_id=discord_id)
    session.commit()
    return f"{callsign} is back on active duty."


# --- Member profile (self-service) --------------------------------------- #
DAY_CODES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def update_profile(session, actor: dict, discord_id: int, timezone: str = "",
                   ingame_name: str = "", availability: str = "", bio: str = "",
                   reminders_opt_out: bool = False) -> str:
    """A member updates their own profile. Web-only data (no Discord side
    effect). ``availability`` is a comma-separated subset of DAY_CODES."""
    record = _member(session, discord_id)
    record.timezone = (timezone or "").strip() or None
    record.ingame_name = (ingame_name or "").strip() or None
    days = [d for d in (availability or "").split(",") if d in DAY_CODES]
    record.availability = ",".join(days) or None
    record.bio = (bio or "").strip()[:1000] or None
    record.reminders_opt_out = bool(reminders_opt_out)
    session.commit()
    return "Your profile has been updated."


# --- Leave of absence: member self-request ------------------------------- #
def request_loa(session, actor: dict, discord_id: int, days: int, reason: str = "") -> str:
    record = _member(session, discord_id)
    if days < 1 or days > 180:
        raise ActionError("Leave must be between 1 and 180 days.")
    if record.status == "loa":
        raise ActionError("You are already on leave.")
    if record.status == "discharged":
        raise ActionError("A discharged member cannot request leave.")
    record.loa_requested_until = datetime.utcnow() + timedelta(days=days)
    record.loa_reason = (reason or "").strip() or None
    session.commit()
    until = record.loa_requested_until.strftime("%d %b %Y")
    return f"Leave requested until {until}. An officer will review it."


def approve_loa_request(session, actor: dict, discord_id: int) -> str:
    record = _member(session, discord_id)
    if not record.loa_requested_until:
        raise ActionError(f"{record.callsign} has no pending leave request.")
    callsign = record.callsign
    loa_until = record.loa_requested_until
    until_str = loa_until.strftime("%d %b %Y")
    record.status = "loa"
    record.loa_until = loa_until
    reason = record.loa_reason
    record.loa_requested_until = None
    record.loa_reason = None
    entry = f"Leave of absence granted until {until_str} by {actor['name']} (member request)."
    if reason:
        entry += f" Reason: {reason}"
    _log(session, discord_id, entry, actor)
    queue.enqueue(
        session,
        queue.LOA,
        {"discord_id": discord_id, "callsign": callsign, "until": until_str,
         "dm": f"Your leave of absence has been approved until **{until_str}**.",
         "billboard": f"**{callsign}** is on leave of absence until {until_str}."},
        actor_id=actor["id"],
    )
    _audit(session, actor, "leave",
           f"Approved {callsign}'s leave request until {until_str}.", target_id=discord_id)
    session.commit()
    return f"Approved {callsign}'s leave until {until_str}."


def deny_loa_request(session, actor: dict, discord_id: int) -> str:
    record = _member(session, discord_id)
    if not record.loa_requested_until:
        raise ActionError(f"{record.callsign} has no pending leave request.")
    callsign = record.callsign
    record.loa_requested_until = None
    record.loa_reason = None
    _log(session, discord_id, f"Leave request declined by {actor['name']}.", actor)
    queue.enqueue(
        session,
        queue.REFRESH_PERSONNEL,
        {"discord_id": discord_id, "dm": f"Your leave request was declined by {actor['name']}."},
        actor_id=actor["id"],
    )
    _audit(session, actor, "leave", f"Declined {callsign}'s leave request.",
           target_id=discord_id)
    session.commit()
    return f"Declined {callsign}'s leave request."


# --- Recruitment --------------------------------------------------------- #
def approve_candidate(session, actor: dict, discord_id: int,
                      rank: str | None = None, company: str | None = None) -> str:
    candidacy = session.get(Candidacy, discord_id)
    if candidacy is None:
        raise ActionError("That applicant is no longer in the recruitment queue.")
    callsign = candidacy.callsign
    regiment_name = get_config(session).regiment_name

    # The recruiter may pick a starting rank/company; otherwise the defaults
    # apply. A chosen value must be a real rank/company on the books.
    start_rank = rank_utils.default_rank_name(session)
    if rank:
        if rank_utils.rank_by_name(session, rank) is None:
            raise ActionError(f"“{rank}” is not a rank on the ladder.")
        start_rank = rank
    start_company = default_company_name(session)
    if company:
        if company_by_name(session, company) is None:
            raise ActionError(f"“{company}” is not a company on the books.")
        start_company = company

    session.merge(
        Member(
            discord_id=discord_id,
            callsign=callsign,
            rank=start_rank,
            company=start_company,
            status="active",
            thread_id=None,
        )
    )
    _log(session, discord_id,
         f"Enlisted as {start_rank}, {start_company} via recruitment pipeline.", actor)
    session.delete(candidacy)
    queue.enqueue(
        session,
        queue.APPROVE_CANDIDATE,
        {"discord_id": discord_id, "callsign": callsign, "actor_id": actor["id"],
         "default_rank": start_rank, "default_company": start_company,
         "billboard": f"**{callsign}** has enlisted in the regiment.",
         "dm": f"Your application to **{regiment_name}** has been approved. "
               f"Welcome to the regiment!"},
        actor_id=actor["id"],
    )
    _audit(session, actor, "recruitment",
           f"Approved {callsign}'s enlistment as {start_rank}, {start_company}.",
           target_id=discord_id)
    session.commit()
    return f"{callsign} enlisted as {start_rank}, {start_company}."


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
    _audit(session, actor, "recruitment", f"Denied {callsign}'s application.",
           target_id=discord_id)
    session.commit()
    return f"{callsign}'s application was denied."


RECRUIT_STAGES = ("applied", "interviewing", "decision")


def set_candidate_stage(session, actor: dict, discord_id: int, stage: str) -> str:
    if stage not in RECRUIT_STAGES:
        raise ActionError("Unknown recruitment stage.")
    candidacy = session.get(Candidacy, discord_id)
    if candidacy is None:
        raise ActionError("That applicant is no longer in the recruitment queue.")
    candidacy.stage = stage
    session.commit()
    labels = {"applied": "At the Gate", "interviewing": "In Interview",
              "decision": "Awaiting Decision"}
    return f"{candidacy.callsign} moved to {labels[stage]}."


def set_candidate_notes(session, actor: dict, discord_id: int, notes: str) -> str:
    candidacy = session.get(Candidacy, discord_id)
    if candidacy is None:
        raise ActionError("That applicant is no longer in the recruitment queue.")
    candidacy.notes = notes.strip() or None
    session.commit()
    return f"Notes saved for {candidacy.callsign}."


# --- Announcements ------------------------------------------------------- #
def post_announcement(session, actor: dict, title: str, body: str) -> str:
    title = title.strip()
    body = body.strip()
    if not body:
        raise ActionError("The announcement needs a message.")
    if not get_config(session).announcements_channel_id:
        raise ActionError("Set an announcements channel in the Command Tent first.")
    queue.enqueue(
        session,
        queue.POST_ANNOUNCEMENT,
        {"title": title, "body": body, "actor_id": actor["id"], "actor_name": actor["name"]},
        actor_id=actor["id"],
    )
    _audit(session, actor, "announcement",
           f"Posted an announcement: {title or body[:60]}")
    session.commit()
    return "Announcement queued — the bot is posting it now."


# --- Roster import ------------------------------------------------------- #
def import_roster(session, actor: dict, role_id: str = "") -> str:
    """Queue a pull of the current Discord members into the roster. The bot,
    which holds the member list, creates a record for anyone not already on
    the books (default rank + company); it changes no roles or nicknames."""
    if not rank_utils.all_ranks(session):
        raise ActionError("Add at least one rank in the Command Tent first.")
    if not list_companies(session):
        raise ActionError("Add at least one company in the Command Tent first.")
    default_rank = rank_utils.default_rank_name(session)
    default_company = default_company_name(session)
    rid = _parse_id(role_id)
    queue.enqueue(
        session,
        queue.IMPORT_ROSTER,
        {"actor_id": actor["id"], "role_id": rid,
         "default_rank": default_rank, "default_company": default_company},
        actor_id=actor["id"],
    )
    _audit(session, actor, "roster",
           "Queued a roster import from Discord" + (" (filtered by role)" if rid else "."))
    session.commit()
    scope = "members with that role" if rid else "all non-bot members"
    return f"Import queued — the bot is adding {scope} not already on the roster."


# --- Events & attendance ------------------------------------------------- #
# How far ahead the Discord RSVP announcement is posted, entered as a number +
# unit on the create form. Blank/zero means post immediately (on creation).
ANNOUNCE_UNIT_MINUTES = {"hours": 60, "days": 1440, "weeks": 10080}
ANNOUNCE_UNITS = list(ANNOUNCE_UNIT_MINUTES)
ANNOUNCE_MAX_MINUTES = 60 * 24 * 90  # cap the lead at 90 days


def _lead_minutes(value: str | int, unit: str) -> int | None:
    """Convert a 'post N <unit> before' choice into minutes. Blank/zero/invalid
    → None, meaning post the announcement immediately on creation."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    per = ANNOUNCE_UNIT_MINUTES.get(unit, ANNOUNCE_UNIT_MINUTES["days"])
    return min(n * per, ANNOUNCE_MAX_MINUTES)


def create_event(session, actor: dict, name: str, event_type: str, when: str,
                 tz_offset: str | int = 0, repeat_weeks: str | int = 1,
                 lead_value: str | int = 0, lead_unit: str = "days") -> int:
    from utils.terminology import resolve_terms
    name = name.strip()
    if not name:
        raise ActionError("The event needs a name.")
    cfg = get_config(session)
    allowed = resolve_terms(cfg.terminology_custom)["event_types"]
    if event_type not in allowed:
        raise ActionError(f"Choose one of: {', '.join(allowed)}.")
    try:
        local = datetime.strptime(when.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        raise ActionError("Enter a valid date and time.")
    # The officer entered the time in their own timezone; JS sent the offset
    # (minutes) such that UTC = local + offset. Store naive UTC.
    try:
        offset = int(tz_offset)
    except (TypeError, ValueError):
        offset = 0
    try:
        weeks = max(1, min(int(repeat_weeks), 26))
    except (TypeError, ValueError):
        weeks = 1
    base = local + timedelta(minutes=offset)
    lead = _lead_minutes(lead_value, lead_unit)

    first_id = None
    for i in range(weeks):
        scheduled = base + timedelta(weeks=i)
        event = Event(name=name, event_type=event_type, scheduled_at=scheduled,
                      created_by=actor["id"], announce_lead_minutes=lead)
        session.add(event)
        session.commit()
        session.refresh(event)
        # Post now if there's no lead, or if the post-time has already arrived
        # (e.g. an imminent first occurrence). Otherwise the bot's scheduler
        # posts it once its lead window opens — so a recurring series announces
        # one occurrence at a time instead of all at once.
        due_now = lead is None or (scheduled - timedelta(minutes=lead)) <= datetime.utcnow()
        if due_now:
            queue.enqueue(session, queue.ANNOUNCE_EVENT, {"event_id": event.id}, actor_id=actor["id"])
            event.announced = True
        session.commit()
        if first_id is None:
            first_id = event.id
    return first_id


def _parse_when(when: str, tz_offset: str | int = 0) -> datetime:
    """Parse a 'YYYY-MM-DD HH:MM' local time into naive UTC using the browser's
    minute offset (UTC = local + offset)."""
    try:
        local = datetime.strptime(when.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        raise ActionError("Enter a valid date and time.")
    try:
        offset = int(tz_offset)
    except (TypeError, ValueError):
        offset = 0
    return local + timedelta(minutes=offset)


def update_event(session, actor: dict, event_id: int, name: str, event_type: str,
                 when: str, tz_offset: str | int = 0) -> str:
    """Edit an existing event's name, type, and schedule. Re-renders the
    Discord announcement embed if one was posted."""
    from utils.terminology import resolve_terms
    event = session.get(Event, event_id)
    if event is None:
        raise ActionError("That event no longer exists.")
    name = name.strip()
    if not name:
        raise ActionError("The event needs a name.")
    allowed = resolve_terms(get_config(session).terminology_custom)["event_types"]
    if event_type not in allowed:
        raise ActionError(f"Choose one of: {', '.join(allowed)}.")
    event.name = name
    event.event_type = event_type
    event.scheduled_at = _parse_when(when, tz_offset)
    # A rescheduled event should remind afresh.
    event.reminder_sent_at = None
    if event.message_id:
        queue.enqueue(session, queue.REFRESH_EVENT, {"event_id": event.id}, actor_id=actor["id"])
    _audit(session, actor, "event", f"Edited the event '{name}'.")
    session.commit()
    return f"'{name}' updated."


def delete_event(session, actor: dict, event_id: int) -> str:
    """Delete an event and its attendance records, and withdraw the Discord
    announcement if one was posted."""
    event = session.get(Event, event_id)
    if event is None:
        raise ActionError("That event no longer exists.")
    name = event.name
    # The event row is about to vanish, so carry the message coordinates on the
    # queued action itself for the bot to delete.
    if event.channel_id and event.message_id:
        queue.enqueue(
            session, queue.DELETE_EVENT,
            {"channel_id": event.channel_id, "message_id": event.message_id},
            actor_id=actor["id"],
        )
    _audit(session, actor, "event", f"Deleted the event '{name}'.")
    session.delete(event)
    session.commit()
    return f"'{name}' deleted."


def bulk_mark_attendance(session, actor: dict, event_id: int,
                         statuses: dict[int, str]) -> str:
    """Record actual attendance for many members at once. ``statuses`` maps a
    member's discord id to present/absent/excused; blanks are skipped."""
    event = session.get(Event, event_id)
    if event is None:
        raise ActionError("That event no longer exists.")
    changed = 0
    for member_id, status in statuses.items():
        if status not in ATTENDANCE_STATUSES:
            continue
        member = session.get(Member, member_id)
        if member is None:
            continue
        record = (
            session.query(AttendanceRecord)
            .filter(AttendanceRecord.event_id == event_id,
                    AttendanceRecord.member_id == member_id)
            .one_or_none()
        )
        if record:
            if record.status == status:
                continue
            record.status = status
            record.responded_at = datetime.utcnow()
        else:
            session.add(AttendanceRecord(event_id=event_id, member_id=member_id, status=status))
        member.last_active_date = datetime.utcnow()
        changed += 1
    if not changed:
        return "No attendance changes to save."
    _audit(session, actor, "event",
           f"Marked attendance for {changed} member{'s' if changed != 1 else ''} at '{event.name}'.")
    session.commit()
    return f"Recorded attendance for {changed} member{'s' if changed != 1 else ''}."


def record_after_action(session, actor: dict, event_id: int, outcome: str = "",
                        notes: str = "") -> str:
    """Record an event's outcome and after-action notes. Web-only history."""
    event = session.get(Event, event_id)
    if event is None:
        raise ActionError("That muster call no longer exists.")
    event.outcome = (outcome or "").strip() or None
    event.after_action = (notes or "").strip() or None
    session.commit()
    return "After-action report saved."


EVENT_OUTCOMES = ["", "Victory", "Defeat", "Draw", "Stood Down"]


def mark_attendance(session, actor: dict, event_id: int, member_id: int, status: str) -> str:
    if status not in ATTENDANCE_STATUSES:
        raise ActionError("Attendance must be present, absent, or excused.")
    event = session.get(Event, event_id)
    if event is None:
        raise ActionError("That muster call no longer exists.")
    member = session.get(Member, member_id)
    if member is None:
        raise ActionError("That member has no personnel record.")

    record = (
        session.query(AttendanceRecord)
        .filter(AttendanceRecord.event_id == event_id, AttendanceRecord.member_id == member_id)
        .one_or_none()
    )
    if record:
        record.status = status
        record.responded_at = datetime.utcnow()
    else:
        session.add(AttendanceRecord(event_id=event_id, member_id=member_id, status=status))
    member.last_active_date = datetime.utcnow()
    session.commit()
    return f"Marked {member.callsign} as {status} for {event.name}."


# --- Awards -------------------------------------------------------------- #
def create_award_type(session, actor: dict, name: str, description: str = "", emoji: str = "") -> str:
    name = name.strip()
    if not name:
        raise ActionError("The award needs a name.")
    if session.query(AwardType).filter(AwardType.name.ilike(name)).one_or_none():
        raise ActionError(f"'{name}' is already in the catalogue.")
    session.add(
        AwardType(
            name=name,
            description=description.strip() or None,
            emoji=emoji.strip() or None,
            created_by=actor["id"],
        )
    )
    session.commit()
    return f"Added '{name}' to the honors catalogue."


def grant_award(session, actor: dict, discord_id: int, award_type_id: int, notes: str = "") -> str:
    member = _member(session, discord_id)
    award = session.get(AwardType, award_type_id)
    if award is None:
        raise ActionError("That honor isn't in the catalogue.")
    existing = (
        session.query(MemberAward)
        .filter(MemberAward.member_id == discord_id, MemberAward.award_type_id == award_type_id)
        .one_or_none()
    )
    if existing:
        raise ActionError(f"{member.callsign} already holds {award.name}.")
    session.add(
        MemberAward(
            member_id=discord_id,
            award_type_id=award_type_id,
            awarded_by=actor["id"],
            notes=notes.strip() or None,
        )
    )
    queue.enqueue(
        session,
        queue.AWARD_GRANTED,
        {"discord_id": discord_id,
         "billboard": f"**{member.callsign}** has been awarded **{award.name}**."},
        actor_id=actor["id"],
    )
    _audit(session, actor, "awards", f"Awarded {award.name} to {member.callsign}.",
           target_id=discord_id)
    session.commit()
    return f"{member.callsign} was awarded {award.name}."


def revoke_award(session, actor: dict, discord_id: int, award_type_id: int) -> str:
    member = _member(session, discord_id)
    existing = (
        session.query(MemberAward)
        .filter(MemberAward.member_id == discord_id, MemberAward.award_type_id == award_type_id)
        .one_or_none()
    )
    if existing is None:
        raise ActionError("That member doesn't hold that honor.")
    award = session.get(AwardType, award_type_id)
    session.delete(existing)
    queue.enqueue(session, queue.AWARD_REVOKED, {"discord_id": discord_id}, actor_id=actor["id"])
    _audit(session, actor, "awards",
           f"Revoked {award.name if award else 'an honor'} from {member.callsign}.",
           target_id=discord_id)
    session.commit()
    return f"Removed {award.name if award else 'the honor'} from {member.callsign}."


# --- Admin: identity / roles / channels ---------------------------------- #
def _parse_id(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise ActionError(f"'{value}' isn't a valid Discord ID (should be all digits).")


def update_identity(session, name: str, motto: str, brand_hex: str,
                    inactivity_days: int, theme: str = "", discord_invite: str = "") -> str:
    from utils.terminology import THEMES
    cfg = get_config(session)
    name = name.strip()
    if not name:
        raise ActionError("The unit needs a name.")
    try:
        color = int(brand_hex.strip().lstrip("#"), 16)
    except ValueError:
        raise ActionError("Enter a valid hex colour, e.g. #7C1F2B.")
    if not (1 <= inactivity_days <= 365):
        raise ActionError("Inactivity threshold must be between 1 and 365 days.")
    invite = discord_invite.strip()
    if invite and not invite.startswith(("https://", "http://")):
        raise ActionError("The Discord invite must be a full link, e.g. https://discord.gg/abc123.")
    cfg.regiment_name = name
    cfg.regiment_motto = motto.strip() or None
    cfg.brand_color = color & 0xFFFFFF
    cfg.inactivity_days_threshold = inactivity_days
    cfg.discord_invite = invite or None
    if theme and theme in THEMES:
        cfg.theme = theme
    session.commit()
    return "Identity updated."


def set_terminology(session, submitted: dict) -> str:
    """Store per-word overrides on top of the unit's preset. Only values that
    differ from the preset are kept, so unedited words keep following it."""
    from utils.terminology import diff_overrides
    cfg = get_config(session)
    overrides = diff_overrides(submitted)
    cfg.terminology_custom = json.dumps(overrides) if overrides else None
    session.commit()
    return "Custom wording saved." if overrides else "Custom wording cleared."


def reset_terminology(session) -> str:
    """Drop all custom overrides; the unit falls back to its preset's words."""
    cfg = get_config(session)
    cfg.terminology_custom = None
    session.commit()
    return "Reverted to the preset's wording."


def set_roles(session, values: dict[str, str]) -> str:
    cfg = get_config(session)
    for key, col in ROLE_KEYS.items():
        setattr(cfg, col, _parse_id(values.get(key, "")))
    session.commit()
    return "Role bindings saved."


def set_channels(session, values: dict[str, str]) -> str:
    cfg = get_config(session)
    for key, col in CHANNEL_KEYS.items():
        setattr(cfg, col, _parse_id(values.get(key, "")))
    session.commit()
    return "Channel bindings saved."


def set_digest_enabled(session, enabled: bool) -> str:
    cfg = get_config(session)
    cfg.digest_enabled = bool(enabled)
    session.commit()
    return ("Weekly officer digest turned on." if enabled
            else "Weekly officer digest turned off.")


# --- Admin: ranks -------------------------------------------------------- #
def rank_add(session, name: str, abbreviation: str, tier: str = "", role_id: str = "") -> str:
    name = name.strip()
    abbreviation = abbreviation.strip()
    if not name or not abbreviation:
        raise ActionError("A rank needs both a name and an abbreviation.")
    if session.query(Rank).filter(Rank.name == name).one_or_none():
        raise ActionError(f"Rank '{name}' already exists.")
    top = session.query(Rank).order_by(Rank.position.desc()).first()
    session.add(
        Rank(
            name=name,
            abbreviation=abbreviation,
            tier=tier.strip() or None,
            role_id=_parse_id(role_id),
            position=(top.position + 1) if top else 0,
        )
    )
    session.commit()
    return f"Rank '{name}' added at the top of the ladder."


def rank_update(session, rank_id: int, abbreviation: str, tier: str, role_id: str) -> str:
    rank = session.get(Rank, rank_id)
    if rank is None:
        raise ActionError("Unknown rank.")
    abbreviation = abbreviation.strip()
    if not abbreviation:
        raise ActionError("A rank needs an abbreviation.")
    rank.abbreviation = abbreviation
    rank.tier = tier.strip() or None
    rank.role_id = _parse_id(role_id)
    session.commit()
    return f"Rank '{rank.name}' updated."


def rank_remove(session, rank_id: int) -> str:
    rank = session.get(Rank, rank_id)
    if rank is None:
        raise ActionError("Unknown rank.")
    name = rank.name
    session.delete(rank)
    session.commit()
    return f"Rank '{name}' removed. Members holding it keep the label until reassigned."


def rank_move(session, rank_id: int, direction: str) -> str:
    rank = session.get(Rank, rank_id)
    if rank is None:
        raise ActionError("Unknown rank.")
    if direction == "up":
        neighbor = (
            session.query(Rank).filter(Rank.position > rank.position)
            .order_by(Rank.position).first()
        )
    else:
        neighbor = (
            session.query(Rank).filter(Rank.position < rank.position)
            .order_by(Rank.position.desc()).first()
        )
    if neighbor is None:
        raise ActionError(f"'{rank.name}' is already at the {'top' if direction == 'up' else 'bottom'}.")
    # position is UNIQUE, so step through a temporary slot to avoid a
    # transient collision when both rows update in one flush.
    a, b = rank.position, neighbor.position
    rank.position = -1
    session.flush()
    neighbor.position = a
    session.flush()
    rank.position = b
    session.commit()
    return f"Moved '{rank.name}' {direction}."


# --- Admin: companies ---------------------------------------------------- #
def company_add(session, name: str, role_id: str = "", is_default: bool = False) -> str:
    name = name.strip()
    if not name:
        raise ActionError("A company needs a name.")
    if session.query(Company).filter(Company.name == name).one_or_none():
        raise ActionError(f"Company '{name}' already exists.")
    if is_default:
        session.query(Company).update({Company.is_default: False})
    session.add(Company(name=name, role_id=_parse_id(role_id), is_default=is_default))
    session.commit()
    return f"Company '{name}' added."


def company_update(session, company_id: int, role_id: str) -> str:
    company = session.get(Company, company_id)
    if company is None:
        raise ActionError("Unknown company.")
    company.role_id = _parse_id(role_id)
    session.commit()
    return f"Company '{company.name}' updated."


def company_set_default(session, company_id: int) -> str:
    company = session.get(Company, company_id)
    if company is None:
        raise ActionError("Unknown company.")
    session.query(Company).update({Company.is_default: False})
    company.is_default = True
    session.commit()
    return f"'{company.name}' is now the default company for new enlistees."


def company_remove(session, company_id: int) -> str:
    company = session.get(Company, company_id)
    if company is None:
        raise ActionError("Unknown company.")
    name = company.name
    session.delete(company)
    session.commit()
    return f"Company '{name}' removed. Members assigned to it keep the label until reassigned."


# --- Secondary assignments ----------------------------------------------- #
def list_assignments(session) -> list[Assignment]:
    """All assignments, leadership first, then by position and name."""
    return (
        session.query(Assignment)
        .order_by(Assignment.is_leadership.desc(), Assignment.position, Assignment.name)
        .all()
    )


def assignment_add(session, name: str, role_id: str = "", description: str = "",
                   is_leadership: bool = False) -> str:
    name = name.strip()
    if not name:
        raise ActionError("An assignment needs a name.")
    if session.query(Assignment).filter(Assignment.name.ilike(name)).one_or_none():
        raise ActionError(f"'{name}' already exists.")
    top = session.query(func.max(Assignment.position)).scalar() or 0
    session.add(Assignment(
        name=name, role_id=_parse_id(role_id), description=description.strip() or None,
        is_leadership=bool(is_leadership), position=top + 1,
    ))
    session.commit()
    return f"Assignment '{name}' added."


def assignment_update(session, assignment_id: int, role_id: str = "",
                      description: str = "", is_leadership: bool = False) -> str:
    a = session.get(Assignment, assignment_id)
    if a is None:
        raise ActionError("Unknown assignment.")
    a.role_id = _parse_id(role_id)
    a.description = description.strip() or None
    a.is_leadership = bool(is_leadership)
    session.commit()
    return f"Assignment '{a.name}' updated."


def assignment_remove(session, assignment_id: int) -> str:
    a = session.get(Assignment, assignment_id)
    if a is None:
        raise ActionError("Unknown assignment.")
    name = a.name
    session.delete(a)  # cascades to member links (web side only; roles are left as-is)
    session.commit()
    return f"Assignment '{name}' removed."


def assign_member(session, actor: dict, discord_id: int, assignment_id: int) -> str:
    member = _member(session, discord_id)
    a = session.get(Assignment, assignment_id)
    if a is None:
        raise ActionError("Unknown assignment.")
    existing = (
        session.query(MemberAssignment)
        .filter(MemberAssignment.member_id == discord_id,
                MemberAssignment.assignment_id == assignment_id)
        .one_or_none()
    )
    if existing:
        raise ActionError(f"{member.callsign} already holds {a.name}.")
    session.add(MemberAssignment(member_id=discord_id, assignment_id=assignment_id,
                                 assigned_by=actor["id"]))
    if a.role_id:
        queue.enqueue(session, queue.ASSIGN_ROLE,
                      {"discord_id": discord_id, "role_id": a.role_id}, actor_id=actor["id"])
    _audit(session, actor, "assignment", f"Assigned {member.callsign} to {a.name}.",
           target_id=discord_id)
    session.commit()
    return f"{member.callsign} assigned to {a.name}."


def unassign_member(session, actor: dict, discord_id: int, assignment_id: int) -> str:
    member = _member(session, discord_id)
    a = session.get(Assignment, assignment_id)
    link = (
        session.query(MemberAssignment)
        .filter(MemberAssignment.member_id == discord_id,
                MemberAssignment.assignment_id == assignment_id)
        .one_or_none()
    )
    if link is None:
        raise ActionError(f"{member.callsign} isn't assigned to that.")
    name = a.name if a else "that assignment"
    session.delete(link)
    if a and a.role_id:
        queue.enqueue(session, queue.UNASSIGN_ROLE,
                      {"discord_id": discord_id, "role_id": a.role_id}, actor_id=actor["id"])
    _audit(session, actor, "assignment", f"Removed {member.callsign} from {name}.",
           target_id=discord_id)
    session.commit()
    return f"{member.callsign} removed from {name}."


# --- Member self-service: RSVP ------------------------------------------- #
def rsvp(session, event_id: int, discord_id: int, status: str) -> str:
    """A member's own RSVP to a muster call, from the web (mirrors the Discord
    RSVP buttons)."""
    if status not in RSVP_STATUSES:
        raise ActionError("Choose accept, tentative, or decline.")
    event = session.get(Event, event_id)
    if event is None:
        raise ActionError("That muster call no longer exists.")
    member = session.get(Member, discord_id)
    if member is None:
        raise ActionError("Only enlisted members of this unit can RSVP.")

    record = (
        session.query(AttendanceRecord)
        .filter(AttendanceRecord.event_id == event_id, AttendanceRecord.member_id == discord_id)
        .one_or_none()
    )
    if record:
        record.status = status
        record.responded_at = datetime.utcnow()
    else:
        session.add(AttendanceRecord(event_id=event_id, member_id=discord_id, status=status))
    member.last_active_date = datetime.utcnow()
    session.commit()
    return f"You {RSVP_STATUSES[status]}."


# --- Public applications (cross-unit) ------------------------------------ #
def submit_application(session, discord_id: int, callsign: str,
                       answers: list[dict] | None = None) -> str:
    """Record a public application against a unit (its own database session).
    Deduplicates against existing members and pending candidacies. ``answers``
    is a list of {"q": prompt, "a": answer} captured from the unit's questions."""
    callsign = (callsign or "").strip() or "Applicant"
    if session.get(Member, discord_id) is not None:
        raise ActionError("You already have a record with this unit.")
    if session.get(Candidacy, discord_id) is not None:
        raise ActionError("Your application to this unit is already pending.")
    cand = Candidacy(discord_id=discord_id, callsign=callsign)
    if answers:
        cand.answers = json.dumps(answers)
    session.add(cand)
    session.commit()
    return "Application submitted — the unit's recruiters will review it."


# --- Recruitment questions (per-unit config) ----------------------------- #
def list_recruitment_questions(session, enabled_only: bool = False):
    q = session.query(RecruitmentQuestion)
    if enabled_only:
        q = q.filter(RecruitmentQuestion.enabled.is_(True))
    return q.order_by(RecruitmentQuestion.position, RecruitmentQuestion.id).all()


def question_add(session, prompt: str, required: bool = True) -> str:
    prompt = (prompt or "").strip()
    if not prompt:
        raise ActionError("The question can't be empty.")
    if len(prompt) > 200:
        raise ActionError("Keep the question under 200 characters.")
    max_pos = session.query(func.max(RecruitmentQuestion.position)).scalar() or 0
    session.add(RecruitmentQuestion(prompt=prompt, position=max_pos + 1,
                                    required=bool(required), enabled=True))
    session.commit()
    return "Question added."


def question_remove(session, question_id: int) -> str:
    q = session.get(RecruitmentQuestion, question_id)
    if q is None:
        raise ActionError("No such question.")
    session.delete(q)
    session.commit()
    return "Question removed."


def question_move(session, question_id: int, direction: str) -> str:
    if direction not in ("up", "down"):
        raise ActionError("Move must be up or down.")
    ordered = list_recruitment_questions(session)
    idx = next((i for i, x in enumerate(ordered) if x.id == question_id), None)
    if idx is None:
        raise ActionError("No such question.")
    swap = idx - 1 if direction == "up" else idx + 1
    if swap < 0 or swap >= len(ordered):
        return "Already at the end."
    a, b = ordered[idx], ordered[swap]
    a.position, b.position = b.position, a.position
    session.commit()
    return "Question reordered."


def question_toggle(session, question_id: int) -> str:
    q = session.get(RecruitmentQuestion, question_id)
    if q is None:
        raise ActionError("No such question.")
    q.enabled = not q.enabled
    session.commit()
    return f"Question {'shown' if q.enabled else 'hidden'}."


# --- Action queue: visibility & retry ------------------------------------ #
# Friendly labels for the queued Discord side-effects, keyed by action name.
ACTION_LABELS = {
    queue.SYNC_RANK: "Rank change",
    queue.SYNC_COMPANY: "Company transfer",
    queue.DISCIPLINE: "Disciplinary record",
    queue.DISCHARGE: "Discharge",
    queue.REINSTATE: "Reinstatement",
    queue.LOA: "Leave of absence",
    queue.LOA_END: "Return from leave",
    queue.APPROVE_CANDIDATE: "Enlistment approval",
    queue.DENY_CANDIDATE: "Application denial",
    queue.REFRESH_PERSONNEL: "Dossier refresh",
    queue.ANNOUNCE_EVENT: "Event announcement",
    queue.AWARD_GRANTED: "Honor granted",
    queue.AWARD_REVOKED: "Honor revoked",
    queue.POST_ANNOUNCEMENT: "Announcement",
    queue.IMPORT_ROSTER: "Roster import",
}


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action, action.replace("_", " ").capitalize())


def list_recent_actions(session, limit: int = 25) -> list[dict]:
    """The queue's unfinished and recently-finished work, newest first, so an
    admin can see what's stuck or failed and retry it. `done` rows are omitted
    unless recent, keeping the panel focused on what needs attention."""
    cutoff = datetime.utcnow() - timedelta(hours=6)
    rows = (
        session.query(PendingAction)
        .filter(
            (PendingAction.status != queue.DONE)
            | (PendingAction.processed_at >= cutoff)
        )
        .order_by(PendingAction.id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "label": action_label(r.action),
            "status": r.status,
            "attempts": r.attempts,
            "error": r.error,
            "created_at": r.created_at,
            "processed_at": r.processed_at,
        }
        for r in rows
    ]


def action_queue_counts(session) -> dict:
    """Small summary for the badge: how many are pending vs failed."""
    pending = session.query(PendingAction).filter(PendingAction.status == queue.PENDING).count()
    failed = session.query(PendingAction).filter(PendingAction.status == queue.FAILED).count()
    return {"pending": pending, "failed": failed}


def retry_action(session, action_id: int) -> str:
    """Re-queue a single failed action so the bot attempts it again."""
    row = session.get(PendingAction, action_id)
    if row is None:
        raise ActionError("That action is no longer in the queue.")
    if row.status != queue.FAILED:
        raise ActionError("Only failed actions can be retried.")
    row.status = queue.PENDING
    row.attempts = 0
    row.error = None
    row.processed_at = None
    session.commit()
    return "Action re-queued — the bot will try it again shortly."


def retry_all_failed_actions(session) -> str:
    """Re-queue every failed action at once."""
    rows = session.query(PendingAction).filter(PendingAction.status == queue.FAILED).all()
    for row in rows:
        row.status = queue.PENDING
        row.attempts = 0
        row.error = None
        row.processed_at = None
    session.commit()
    n = len(rows)
    if not n:
        return "No failed actions to retry."
    return f"Re-queued {n} failed action{'s' if n != 1 else ''}."


def dismiss_action(session, action_id: int) -> str:
    """Drop a failed action from the queue without retrying it."""
    row = session.get(PendingAction, action_id)
    if row is None:
        raise ActionError("That action is no longer in the queue.")
    if row.status != queue.FAILED:
        raise ActionError("Only failed actions can be dismissed.")
    session.delete(row)
    session.commit()
    return "Action dismissed."


# --- Recruitment funnel metrics ------------------------------------------ #
def recruitment_metrics(session) -> dict:
    """Read-through numbers on the recruitment pipeline: how many sit at each
    stage, how long they've been waiting, and recent enlistment throughput.

    Only truthful, currently-derivable figures — pipeline state plus enlistments
    counted from personnel records — since denied applications aren't retained.
    """
    now = datetime.utcnow()
    candidates = session.query(Candidacy).all()
    stage_counts = {"applied": 0, "interviewing": 0, "decision": 0}
    ages = []
    oldest = None
    for c in candidates:
        stage = c.stage if c.stage in stage_counts else "applied"
        stage_counts[stage] += 1
        if c.created_at:
            days = (now - c.created_at).days
            ages.append(days)
            if oldest is None or days > oldest:
                oldest = days
    ages.sort()
    median_wait = ages[len(ages) // 2] if ages else None
    stale = sum(1 for d in ages if d >= 14)

    # Enlisted recently — the pipeline's output. joined_date is set when a
    # member's record is created (approval), so it's a fair proxy for approvals.
    def _enlisted_since(days):
        cutoff = now - timedelta(days=days)
        return session.query(Member).filter(Member.joined_date >= cutoff).count()

    return {
        "total": len(candidates),
        "stages": stage_counts,
        "median_wait": median_wait,
        "oldest": oldest,
        "stale": stale,
        "enlisted_30d": _enlisted_since(30),
        "enlisted_7d": _enlisted_since(7),
    }


# --- Option sources for the UI ------------------------------------------ #
def rank_options(session) -> list[str]:
    return rank_utils.rank_names(session)


def company_options(session) -> list[str]:
    return [c.name for c in list_companies(session)]
