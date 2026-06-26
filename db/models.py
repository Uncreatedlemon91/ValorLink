from datetime import datetime

from sqlalchemy import (
    BigInteger,
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
    thread_id = Column(BigInteger, nullable=True)

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


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=True)
