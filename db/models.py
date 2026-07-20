from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from db.base import Base


def _utcnow() -> datetime:
    # Naive UTC -- kept consistent (not timezone-aware) so SQLite round-trips
    # comparisons cleanly; all datetimes in this app are implicitly UTC.
    return datetime.utcnow()


class Member(Base):
    __tablename__ = "members"

    discord_id = Column(BigInteger, primary_key=True)
    callsign = Column(String, nullable=False)
    rank = Column(String, nullable=False)
    company = Column(String, nullable=False, default="Unassigned")
    status = Column(String, nullable=False, default="active")  # active | inactive | discharged
    joined_date = Column(DateTime, default=_utcnow)
    last_active_date = Column(DateTime, default=_utcnow)
    rank_since = Column(DateTime, default=_utcnow)  # when they took their current rank
    thread_id = Column(BigInteger, nullable=True)
    loa_until = Column(DateTime, nullable=True)
    avatar = Column(String, nullable=True)  # Discord avatar hash, for CDN URLs

    # Member self-service profile
    timezone = Column(String, nullable=True)      # IANA name or free text, e.g. "America/New_York"
    ingame_name = Column(String, nullable=True)   # War of Rights in-game name
    availability = Column(String, nullable=True)  # nights they play (comma-separated day codes)
    bio = Column(Text, nullable=True)

    # A member-requested leave awaiting an officer's decision
    loa_requested_until = Column(DateTime, nullable=True)
    loa_reason = Column(String, nullable=True)

    # Opt out of the bot's pre-event reminder DMs.
    reminders_opt_out = Column(Boolean, nullable=False, default=False, server_default="0")

    service_history = relationship(
        "ServiceHistoryEntry", back_populates="member", cascade="all, delete-orphan"
    )
    disciplinary_records = relationship(
        "DisciplinaryRecord", back_populates="member", cascade="all, delete-orphan"
    )
    attendance_records = relationship(
        "AttendanceRecord", back_populates="member", cascade="all, delete-orphan"
    )
    awards = relationship(
        "MemberAward", back_populates="member", cascade="all, delete-orphan"
    )
    assignments = relationship(
        "MemberAssignment", back_populates="member", cascade="all, delete-orphan"
    )


class ServiceHistoryEntry(Base):
    __tablename__ = "service_history_entries"

    id = Column(Integer, primary_key=True)
    member_id = Column(BigInteger, ForeignKey("members.discord_id"), nullable=False)
    date = Column(DateTime, default=_utcnow)
    entry = Column(Text, nullable=False)
    recorded_by = Column(BigInteger, nullable=True)

    member = relationship("Member", back_populates="service_history")


class DisciplinaryRecord(Base):
    __tablename__ = "disciplinary_records"

    id = Column(Integer, primary_key=True)
    member_id = Column(BigInteger, ForeignKey("members.discord_id"), nullable=False)
    date = Column(DateTime, default=_utcnow)
    record_type = Column(String, nullable=False)  # note | warn | strike
    reason = Column(Text, nullable=False)
    issued_by = Column(BigInteger, nullable=False)

    member = relationship("Member", back_populates="disciplinary_records")


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    event_type = Column(String, nullable=False, default="Drill")  # Drill | Battle | Operation
    scheduled_at = Column(DateTime, nullable=False)
    created_by = Column(BigInteger, nullable=False)
    channel_id = Column(BigInteger, nullable=True)
    message_id = Column(BigInteger, nullable=True)
    outcome = Column(String, nullable=True)       # after-action result, e.g. Victory/Defeat/Draw
    after_action = Column(Text, nullable=True)     # after-action notes
    reminder_sent_at = Column(DateTime, nullable=True)  # when the bot DM'd RSVPs the pre-event reminder
    # How far ahead of the event to post the Discord RSVP announcement. NULL =
    # post immediately on creation. `announced` guards against double-posting.
    announce_lead_minutes = Column(Integer, nullable=True)
    announced = Column(Boolean, nullable=False, default=False, server_default="0")

    attendance_records = relationship(
        "AttendanceRecord", back_populates="event", cascade="all, delete-orphan"
    )


class AttendanceRecord(Base):
    __tablename__ = "attendance_records"
    __table_args__ = (UniqueConstraint("event_id", "member_id", name="uq_event_member"),)

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    member_id = Column(BigInteger, ForeignKey("members.discord_id"), nullable=False)
    status = Column(String, nullable=False, default="pending")  # accepted|declined|tentative|present|absent|excused
    responded_at = Column(DateTime, default=_utcnow)

    event = relationship("Event", back_populates="attendance_records")
    member = relationship("Member", back_populates="attendance_records")


class AwardType(Base):
    """Catalog of award/qualification types. Managed dynamically via
    /award_type_create rather than config.py, since officers add new
    courses/medals over time without wanting a code deploy."""

    __tablename__ = "award_types"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    description = Column(Text, nullable=True)
    emoji = Column(String, nullable=True)
    created_by = Column(BigInteger, nullable=False)

    awards = relationship("MemberAward", back_populates="award_type", cascade="all, delete-orphan")


class MemberAward(Base):
    """A member holding an award/qualification. One-time per member per
    award type -- not a repeatable log of multiple grants."""

    __tablename__ = "member_awards"
    __table_args__ = (UniqueConstraint("member_id", "award_type_id", name="uq_member_award"),)

    id = Column(Integer, primary_key=True)
    member_id = Column(BigInteger, ForeignKey("members.discord_id"), nullable=False)
    award_type_id = Column(Integer, ForeignKey("award_types.id"), nullable=False)
    date_awarded = Column(DateTime, default=_utcnow)
    awarded_by = Column(BigInteger, nullable=False)
    notes = Column(Text, nullable=True)

    member = relationship("Member", back_populates="awards")
    award_type = relationship("AwardType", back_populates="awards")


class Assignment(Base):
    """A secondary group a member can hold in addition to their company —
    High Command, Staff, a training cadre, a colour guard. Cross-cutting and
    optional (a member holds zero or more), each with an optional Discord role
    the bot keeps in sync. Managed from the Command Tent, like Companies."""

    __tablename__ = "assignments"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    role_id = Column(BigInteger, nullable=True)          # Discord role synced on assign
    description = Column(Text, nullable=True)
    is_leadership = Column(Boolean, nullable=False, default=False)  # sorts to the top of the staff roster
    position = Column(Integer, nullable=False, default=0)

    members = relationship(
        "MemberAssignment", back_populates="assignment", cascade="all, delete-orphan"
    )


class MemberAssignment(Base):
    """A member holding a secondary assignment. Many-to-many between members
    and assignments."""

    __tablename__ = "member_assignments"
    __table_args__ = (UniqueConstraint("member_id", "assignment_id", name="uq_member_assignment"),)

    id = Column(Integer, primary_key=True)
    member_id = Column(BigInteger, ForeignKey("members.discord_id"), nullable=False)
    assignment_id = Column(Integer, ForeignKey("assignments.id"), nullable=False)
    assigned_at = Column(DateTime, default=_utcnow)
    assigned_by = Column(BigInteger, nullable=True)

    member = relationship("Member", back_populates="assignments")
    assignment = relationship("Assignment", back_populates="members")


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=True)


class GuildConfig(Base):
    """Singleton row (id=1) holding everything that used to be hardcoded in
    config.py. Populated with defaults on first access and edited live via
    the /config command -- no file edits or restarts required.
    """

    __tablename__ = "guild_config"

    id = Column(Integer, primary_key=True)

    regiment_name = Column(String, nullable=False, default="Unconfigured Regiment")
    regiment_motto = Column(String, nullable=True)
    # Optional short tag prefixed onto every member's Discord nickname, e.g.
    # "5thVA" → "5thVA A Cpl. John". Empty means no unit tag.
    unit_tag = Column(String, nullable=True)
    discord_invite = Column(String, nullable=True)  # public invite to the unit's Discord
    crest = Column(Text, nullable=True)             # unit emblem, stored as a data URI
    brand_color = Column(Integer, nullable=False, default=0x2F3136)
    inactivity_days_threshold = Column(Integer, nullable=False, default=30)
    # Vocabulary preset (see utils/terminology.py): "wor", "modern", …
    terminology = Column(String, nullable=False, default="wor", server_default="wor")
    # Visual theme (CSS skin): "parchment", "modern".
    theme = Column(String, nullable=False, default="parchment", server_default="parchment")
    # JSON map of per-word overrides applied on top of the terminology preset.
    terminology_custom = Column(Text, nullable=True)

    admin_role_id = Column(BigInteger, nullable=True)
    officer_role_id = Column(BigInteger, nullable=True)
    recruiter_role_id = Column(BigInteger, nullable=True)
    member_role_id = Column(BigInteger, nullable=True)
    candidate_role_id = Column(BigInteger, nullable=True)
    visitor_role_id = Column(BigInteger, nullable=True)
    inactive_role_id = Column(BigInteger, nullable=True)

    recruitment_channel_id = Column(BigInteger, nullable=True)
    personnel_forum_id = Column(BigInteger, nullable=True)
    roster_channel_id = Column(BigInteger, nullable=True)
    mod_log_channel_id = Column(BigInteger, nullable=True)
    admin_log_channel_id = Column(BigInteger, nullable=True)
    announcements_channel_id = Column(BigInteger, nullable=True)
    welcome_channel_id = Column(BigInteger, nullable=True)
    billboard_channel_id = Column(BigInteger, nullable=True)
    digest_channel_id = Column(BigInteger, nullable=True)

    # Weekly "state of the regiment" digest posted to officers. Enabled by
    # default; falls back to the admin-log channel when no digest channel is set.
    digest_enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    digest_last_sent_at = Column(DateTime, nullable=True)


class Rank(Base):
    """One rung of the rank ladder, lowest to highest by `position`.
    Managed via /rank_add, /rank_remove, /rank_move, /rank_set_role.
    """

    __tablename__ = "ranks"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    abbreviation = Column(String, nullable=False)
    tier = Column(String, nullable=True)
    role_id = Column(BigInteger, nullable=True)
    position = Column(Integer, nullable=False, unique=True)


class Company(Base):
    """A sub-unit members can be assigned to. Managed via /company_add,
    /company_remove, /company_set_role, /company_set_default.
    """

    __tablename__ = "companies"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    role_id = Column(BigInteger, nullable=True)
    is_default = Column(Boolean, nullable=False, default=False)
    # Optional short tag shown in the nickname for members of this company,
    # sitting between the unit tag and the rank, e.g. "A" or "Co.B".
    tag = Column(String, nullable=True)


class PendingAction(Base):
    """A unit of Discord work requested by the web UI and applied by the bot.

    The web app is the source of truth for regiment *data* (it writes members,
    ranks, records directly to this database), but it can't touch Discord --
    only the bot holds the gateway connection. So a web action commits its
    data change and enqueues one of these rows describing the Discord
    side-effect (swap a role, rewrite a nickname, refresh the roster embed,
    post to the billboard). The bot's bridge cog drains the queue and applies
    each one, reusing the same sync helpers the slash commands use.
    """

    __tablename__ = "pending_actions"

    id = Column(Integer, primary_key=True)
    action = Column(String, nullable=False)          # see utils/queue.py
    payload = Column(Text, nullable=False, default="{}")  # JSON
    status = Column(String, nullable=False, default="pending")  # pending|done|failed
    attempts = Column(Integer, nullable=False, default=0)
    actor_id = Column(BigInteger, nullable=True)     # Discord id of the officer who triggered it
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    processed_at = Column(DateTime, nullable=True)


class Candidacy(Base):
    """Tracks in-progress recruitment interviews so InterviewView buttons
    can be re-registered as persistent views after a bot restart."""

    __tablename__ = "candidacies"

    discord_id = Column(BigInteger, primary_key=True)
    callsign = Column(String, nullable=False)
    thread_id = Column(BigInteger, nullable=True)
    message_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    # Recruitment pipeline stage: applied | interviewing | decision.
    stage = Column(String, nullable=False, default="applied", server_default="applied")
    notes = Column(Text, nullable=True)
    # Snapshot of the applicant's answers to the unit's recruitment questions,
    # as JSON [{"q": prompt, "a": answer}, ...]. Captured at apply time.
    answers = Column(Text, nullable=True)


class AuditEntry(Base):
    """An accountability record of a unit-changing action: who did what, when,
    and from where (the website or a Discord command). Written at the same
    choke points that mutate regiment data, so a unit's leaders can answer
    'who changed this?' without piecing it together from scattered logs.
    """

    __tablename__ = "audit_entries"

    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=_utcnow, index=True)
    actor_id = Column(BigInteger, nullable=True)      # Discord id of who acted
    actor_name = Column(String, nullable=True)        # their name at the time
    source = Column(String, nullable=False, default="web")  # web | discord
    category = Column(String, nullable=False)         # rank|company|discipline|…
    summary = Column(Text, nullable=False)            # human-readable description
    target_id = Column(BigInteger, nullable=True)     # member the action concerns


class RecruitmentQuestion(Base):
    """A question a unit asks applicants during enlistment. Managed from the
    Command Tent; presented on the web apply form and in the Discord modal."""

    __tablename__ = "recruitment_questions"

    id = Column(Integer, primary_key=True)
    prompt = Column(String, nullable=False)
    position = Column(Integer, nullable=False, default=0)
    required = Column(Boolean, nullable=False, default=True)
    enabled = Column(Boolean, nullable=False, default=True)
