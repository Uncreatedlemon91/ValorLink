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
    AttendanceRecord,
    AwardType,
    Candidacy,
    Company,
    DisciplinaryRecord,
    Event,
    Member,
    MemberAward,
    Rank,
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


# --- Events & attendance ------------------------------------------------- #
def create_event(session, actor: dict, name: str, event_type: str, when: str,
                 tz_offset: str | int = 0) -> int:
    name = name.strip()
    if not name:
        raise ActionError("The muster call needs a name.")
    if event_type not in EVENT_TYPES:
        raise ActionError("Choose a drill, battle, or operation.")
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
    scheduled_at = local + timedelta(minutes=offset)
    event = Event(
        name=name, event_type=event_type, scheduled_at=scheduled_at, created_by=actor["id"]
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    queue.enqueue(session, queue.ANNOUNCE_EVENT, {"event_id": event.id}, actor_id=actor["id"])
    session.commit()
    return event.id


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


def update_identity(session, name: str, motto: str, brand_hex: str, inactivity_days: int) -> str:
    cfg = get_config(session)
    name = name.strip()
    if not name:
        raise ActionError("The regiment needs a name.")
    try:
        color = int(brand_hex.strip().lstrip("#"), 16)
    except ValueError:
        raise ActionError("Enter a valid hex colour, e.g. #7C1F2B.")
    if not (1 <= inactivity_days <= 365):
        raise ActionError("Inactivity threshold must be between 1 and 365 days.")
    cfg.regiment_name = name
    cfg.regiment_motto = motto.strip() or None
    cfg.brand_color = color & 0xFFFFFF
    cfg.inactivity_days_threshold = inactivity_days
    session.commit()
    return "Regiment identity updated."


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


# --- Public applications (cross-unit) ------------------------------------ #
def submit_application(session, discord_id: int, callsign: str) -> str:
    """Record a public application against a unit (its own database session).
    Deduplicates against existing members and pending candidacies."""
    callsign = (callsign or "").strip() or "Applicant"
    if session.get(Member, discord_id) is not None:
        raise ActionError("You already have a record with this unit.")
    if session.get(Candidacy, discord_id) is not None:
        raise ActionError("Your application to this unit is already pending.")
    session.add(Candidacy(discord_id=discord_id, callsign=callsign))
    session.commit()
    return "Application submitted — the unit's recruiters will review it."


# --- Option sources for the UI ------------------------------------------ #
def rank_options(session) -> list[str]:
    return rank_utils.rank_names(session)


def company_options(session) -> list[str]:
    return [c.name for c in list_companies(session)]
