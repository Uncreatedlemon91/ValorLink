"""Populate a throwaway database with a plausible regiment so the web UI can
be viewed without a live Discord server behind it.

    python -m web.seed_demo            # seeds ./valorlink.db (creates tables)
    DATABASE_URL=sqlite:///demo.db python -m web.seed_demo

This is a convenience for previewing the Headquarters site and for local
development. It only adds rows to an *empty* database and refuses to touch
one that already holds members, so it can never clobber a real deployment.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from db.base import Base, SessionLocal, engine
from db.models import (
    AttendanceRecord,
    AwardType,
    Candidacy,
    Company,
    DisciplinaryRecord,
    Event,
    GuildConfig,
    Member,
    MemberAward,
    Rank,
    ServiceHistoryEntry,
)

random.seed(1863)


def _id():
    return random.randint(10**17, 10**18)


RANKS = [
    ("Private", "Pvt.", "Enlisted"),
    ("Corporal", "Cpl.", "Non-Commissioned"),
    ("Sergeant", "Sgt.", "Non-Commissioned"),
    ("First Sergeant", "1Sgt.", "Non-Commissioned"),
    ("Lieutenant", "Lt.", "Officer"),
    ("Captain", "Capt.", "Officer"),
    ("Major", "Maj.", "Field Officer"),
    ("Colonel", "Col.", "Field Officer"),
]

COMPANIES = ["Color Company", "Company A", "Company B", "Skirmishers"]

FIRST = ["Josiah", "Ambrose", "Elias", "Cyrus", "Barnabas", "Ezra", "Silas",
         "Nathaniel", "Amos", "Rufus", "Thaddeus", "Obadiah", "Lucius",
         "Cornelius", "Increase", "Jedediah", "Alonzo", "Ephraim"]
LAST = ["Hargrove", "Whitfield", "Ashby", "Calloway", "Deverel", "Pennington",
        "Blackwood", "Sutcliffe", "Marchmont", "Halloway", "Fairbanks",
        "Sturgis", "Winslow", "Beauregard", "Chamberlain", "Kearny", "Hobb"]

AWARDS = [
    ("Marksman's Cord", "Awarded for exemplary conduct at the firing line.", "🎯"),
    ("Colours Bearer", "Entrusted to carry the regimental colours.", "🚩"),
    ("Wounded in Action", "Shed blood in the service of the regiment.", "🩸"),
    ("Meritorious Service", "For sustained and faithful service.", "🎖️"),
    ("Drill Instructor", "Qualified to instruct the manual of arms.", "📯"),
]

SERVICE_LINES = [
    "Enlisted and mustered into the regiment.",
    "Promoted for gallant conduct on the field.",
    "Commended by the Colonel in general orders.",
    "Transferred between companies at his own request.",
    "Detailed to the color guard.",
    "Returned from furlough, fit for duty.",
]

EVENTS = [
    ("Evening Drill", "Drill", -18),
    ("Line Battalion Drill", "Drill", -11),
    ("Battle of Hooker's Crossing", "Battle", -6),
    ("Skirmish at Mill Run", "Battle", -2),
    ("Regimental Drill", "Drill", 3),
    ("Grand Field Manoeuvres", "Operation", 9),
]


def seed():
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        if session.query(Member).count() > 0:
            print("Database already holds members — refusing to seed. Nothing changed.")
            return

        cfg = session.get(GuildConfig, 1)
        if cfg is None:
            cfg = GuildConfig(id=1)
            session.add(cfg)
        cfg.regiment_name = "5th Virginia Volunteers"
        cfg.regiment_motto = "Steadfast and Faithful"
        cfg.brand_color = 0x7C1F2B

        for i, (name, abbr, tier) in enumerate(RANKS):
            session.add(Rank(name=name, abbreviation=abbr, tier=tier, position=i))
        for i, name in enumerate(COMPANIES):
            session.add(Company(name=name, is_default=(i == 1)))

        award_rows = []
        for name, desc, emoji in AWARDS:
            at = AwardType(name=name, description=desc, emoji=emoji, created_by=_id())
            session.add(at)
            award_rows.append(at)
        session.flush()

        now = datetime.utcnow()
        members: list[Member] = []
        used_names = set()
        for _ in range(34):
            while True:
                callsign = f"{random.choice(FIRST)} {random.choice(LAST)}"
                if callsign not in used_names:
                    used_names.add(callsign)
                    break
            rank = random.choices(RANKS, weights=[40, 16, 12, 6, 10, 8, 4, 2])[0]
            roll = random.random()
            if roll < 0.72:
                status, last_seen = "active", now - timedelta(days=random.randint(0, 8))
            elif roll < 0.82:
                status, last_seen = "loa", now - timedelta(days=random.randint(5, 20))
            elif roll < 0.93:
                status, last_seen = "inactive", now - timedelta(days=random.randint(31, 90))
            else:
                status, last_seen = "discharged", now - timedelta(days=random.randint(20, 120))

            joined = now - timedelta(days=random.randint(1, 400))
            m = Member(
                discord_id=_id(),
                callsign=callsign,
                rank=rank[0],
                company=random.choice(COMPANIES),
                status=status,
                joined_date=joined,
                last_active_date=last_seen,
                loa_until=(now + timedelta(days=random.randint(3, 21))) if status == "loa" else None,
            )
            session.add(m)
            members.append(m)

            entries = random.randint(1, 4)
            dates = sorted(joined + timedelta(days=random.randint(0, 380)) for _ in range(entries))
            session.add(ServiceHistoryEntry(member_id=m.discord_id, date=joined, entry=SERVICE_LINES[0]))
            for d in dates:
                session.add(ServiceHistoryEntry(
                    member_id=m.discord_id, date=d,
                    entry=random.choice(SERVICE_LINES[1:]), recorded_by=_id(),
                ))

            for at in random.sample(award_rows, k=random.randint(0, 3)):
                session.add(MemberAward(
                    member_id=m.discord_id, award_type_id=at.id,
                    date_awarded=joined + timedelta(days=random.randint(1, 200)),
                    awarded_by=_id(),
                ))

            if random.random() < 0.28:
                kind = random.choices(["note", "warn", "strike"], weights=[5, 3, 1])[0]
                session.add(DisciplinaryRecord(
                    member_id=m.discord_id, record_type=kind,
                    reason={
                        "note": "Absent from evening roll call without leave.",
                        "warn": "Tardy to formation on repeated occasions.",
                        "strike": "Conduct unbecoming; insubordination toward an officer.",
                    }[kind],
                    date=now - timedelta(days=random.randint(1, 120)),
                    issued_by=_id(),
                ))

        session.flush()

        for name, kind, day_offset in EVENTS:
            ev = Event(
                name=name, event_type=kind,
                scheduled_at=now + timedelta(days=day_offset, hours=1),
                created_by=_id(),
            )
            session.add(ev)
            session.flush()
            attendees = random.sample(members, k=random.randint(12, 26))
            past = day_offset < 0
            for m in attendees:
                if past:
                    st = random.choices(
                        ["present", "absent", "excused"], weights=[7, 2, 1]
                    )[0]
                else:
                    st = random.choices(
                        ["accepted", "tentative", "declined"], weights=[6, 3, 2]
                    )[0]
                session.add(AttendanceRecord(
                    event_id=ev.id, member_id=m.discord_id, status=st,
                    responded_at=ev.scheduled_at - timedelta(days=random.randint(1, 5)),
                ))

        for _ in range(3):
            session.add(Candidacy(
                discord_id=_id(),
                callsign=f"{random.choice(FIRST)} {random.choice(LAST)}",
                created_at=now - timedelta(days=random.randint(0, 6)),
            ))

        session.commit()
        print(f"Seeded {len(members)} members, {len(EVENTS)} events, "
              f"{len(AWARDS)} award types into the database.")
    finally:
        session.close()


if __name__ == "__main__":
    seed()
