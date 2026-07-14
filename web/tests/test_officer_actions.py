"""End-to-end tests for the officer-facing write features.

Exercises the web endpoints against a temporary database (data changes +
audit trail + queued Discord actions), permission gating, CSRF, and the
bot-side bridge dispatch with mocked Discord objects.

Runs under pytest or directly:  python -m web.tests.test_officer_actions
"""
import asyncio
import os
import re
import tempfile
from unittest.mock import AsyncMock, MagicMock

# A throwaway database, wired up before any app/db import reads DATABASE_URL.
_TMP = tempfile.mkdtemp(prefix="valorlink-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["REGISTRY_DATABASE_URL"] = f"sqlite:///{_TMP}/registry.db"
os.environ.pop("PLATFORM_BASE_DOMAIN", None)   # single-tenant: everything resolves to default
os.environ["WEB_DEV_LOGIN"] = "1"
os.environ["WEB_SESSION_SECRET"] = "test-secret"

import config  # noqa: E402
config.DATABASE_URL = os.environ["DATABASE_URL"]

from fastapi.testclient import TestClient  # noqa: E402

from db.base import Base, SessionLocal, engine  # noqa: E402
from db.models import (  # noqa: E402
    AttendanceRecord,
    AwardType,
    Candidacy,
    Company,
    DisciplinaryRecord,
    Event,
    Member,
    MemberAward,
    PendingAction,
    Rank,
    ServiceHistoryEntry,
)
from utils import queue  # noqa: E402
from utils.settings import get_config  # noqa: E402
from web.app import app  # noqa: E402

MEMBER_ID = 100
CANDIDATE_ID = 200


def _seed():
    with SessionLocal() as s:
        get_config(s)  # creates the singleton config row
        for i, (name, abbr) in enumerate([("Private", "Pvt"), ("Corporal", "Cpl"), ("Sergeant", "Sgt")]):
            s.add(Rank(name=name, abbreviation=abbr, position=i))
        s.add(Company(name="Alpha", is_default=True))
        s.add(Company(name="Bravo"))
        s.add(Member(discord_id=MEMBER_ID, callsign="Testman", rank="Private", company="Alpha", status="active"))
        s.add(Candidacy(discord_id=CANDIDATE_ID, callsign="Applicant"))
        s.add(AwardType(name="Marksman", created_by=1))
        s.commit()


def _reset():
    """Fresh schema + seed data before each test, so tests are independent."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    _seed()


try:
    import pytest

    @pytest.fixture(autouse=True)
    def _fresh_db():
        _reset()
        yield
except ImportError:  # running without pytest
    pass


def _login(client, tier="officer", discord_id=1, name="Officer Test"):
    r = client.post("/auth/dev", data={"discord_id": discord_id, "name": name, "tier": tier},
                    follow_redirects=False)
    assert r.status_code == 303


def _csrf(client, path=f"/dossier/{MEMBER_ID}"):
    html = client.get(path).text
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no CSRF token found on page"
    return m.group(1)


def _actions(action=None):
    with SessionLocal() as s:
        q = s.query(PendingAction)
        if action:
            q = q.filter(PendingAction.action == action)
        return q.all()


def _member():
    with SessionLocal() as s:
        return s.get(Member, MEMBER_ID)


# --------------------------------------------------------------------------- #

def test_unauthenticated_write_is_rejected():
    client = TestClient(app)
    r = client.post(f"/members/{MEMBER_ID}/rank",
                    data={"csrf": "x", "rank": "Sergeant"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert _member().rank == "Private"  # unchanged
    assert _actions() == []


def test_promote_changes_data_and_queues_sync():
    client = TestClient(app)
    _login(client, "officer")
    token = _csrf(client)
    r = client.post(f"/members/{MEMBER_ID}/rank",
                    data={"csrf": token, "rank": "Sergeant", "citation": "For valor", "mode": "promote"})
    assert r.status_code == 200

    with SessionLocal() as s:
        m = s.get(Member, MEMBER_ID)
        assert m.rank == "Sergeant"
        entries = [e.entry for e in m.service_history]
        assert any("Promoted from Private to Sergeant" in e and "For valor" in e for e in entries)

    acts = _actions(queue.SYNC_RANK)
    assert len(acts) == 1
    import json
    payload = json.loads(acts[0].payload)
    assert payload["new_rank"] == "Sergeant"
    assert "promoted to" in payload["billboard"]
    assert acts[0].actor_id == 1


def test_bad_csrf_makes_no_change():
    client = TestClient(app)
    _login(client, "officer")
    before = _member().company
    r = client.post(f"/members/{MEMBER_ID}/company", data={"csrf": "wrong", "company": "Bravo"})
    assert r.status_code == 200
    assert "session expired" in r.text.lower()
    assert _member().company == before  # unchanged
    assert _actions(queue.SYNC_COMPANY) == []


def test_company_service_discipline_loa_lifecycle():
    client = TestClient(app)
    _login(client, "officer")

    # company transfer
    client.post(f"/members/{MEMBER_ID}/company", data={"csrf": _csrf(client), "company": "Bravo"})
    assert _member().company == "Bravo"
    assert len(_actions(queue.SYNC_COMPANY)) == 1

    # service log
    client.post(f"/members/{MEMBER_ID}/service-log",
                data={"csrf": _csrf(client), "entry": "Detailed to the color guard."})
    assert len(_actions(queue.REFRESH_PERSONNEL)) >= 1

    # discipline
    client.post(f"/members/{MEMBER_ID}/discipline",
                data={"csrf": _csrf(client), "record_type": "strike", "reason": "AWOL"})
    with SessionLocal() as s:
        strikes = s.query(DisciplinaryRecord).filter_by(member_id=MEMBER_ID, record_type="strike").count()
        assert strikes == 1
    assert len(_actions(queue.DISCIPLINE)) == 1

    # leave of absence, then end it
    client.post(f"/members/{MEMBER_ID}/loa", data={"csrf": _csrf(client), "days": "5", "reason": "travel"})
    assert _member().status == "loa"
    assert len(_actions(queue.LOA)) == 1
    client.post(f"/members/{MEMBER_ID}/loa-end", data={"csrf": _csrf(client)})
    assert _member().status == "active"
    assert len(_actions(queue.LOA_END)) == 1

    # discharge then reinstate
    client.post(f"/members/{MEMBER_ID}/discharge",
                data={"csrf": _csrf(client), "discharge_type": "honorable", "reason": "end of term"})
    assert _member().status == "discharged"
    assert len(_actions(queue.DISCHARGE)) == 1
    client.post(f"/members/{MEMBER_ID}/reinstate", data={"csrf": _csrf(client), "reason": "returned"})
    assert _member().status == "active"
    assert len(_actions(queue.REINSTATE)) == 1


def test_recruiter_can_approve_candidate():
    client = TestClient(app)
    _login(client, "recruiter")
    token = _csrf(client, "/recruits")
    r = client.post(f"/recruits/{CANDIDATE_ID}/approve", data={"csrf": token})
    assert r.status_code == 200
    with SessionLocal() as s:
        assert s.get(Candidacy, CANDIDATE_ID) is None
        m = s.get(Member, CANDIDATE_ID)
        assert m is not None and m.status == "active" and m.rank == "Private"
    assert len(_actions(queue.APPROVE_CANDIDATE)) == 1


def test_insufficient_tier_is_forbidden():
    client = TestClient(app)
    _login(client, "none")
    # The tier check runs before CSRF, so the token value is irrelevant here.
    r = client.post(f"/members/{MEMBER_ID}/rank",
                    data={"csrf": "x", "rank": "Corporal"}, follow_redirects=False)
    assert r.status_code == 403
    assert _member().rank == "Private"


def test_bridge_dispatch_applies_discord_side_effects():
    from cogs.bridge import Bridge

    bridge = object.__new__(Bridge)  # skip __init__ (which would start the loop)
    bridge.bot = MagicMock()

    guild = MagicMock()
    member = AsyncMock()
    member.mention = "<@100>"
    member.roles = []
    member.guild = guild
    guild.get_member.return_value = member
    guild.get_role.return_value = None
    guild.get_channel.return_value = None

    async def run():
        # a known action dispatches without error
        await bridge._dispatch(guild, queue.DISCIPLINE, {
            "discord_id": MEMBER_ID, "record_type": "note", "reason": "x",
            "issued_by": 1, "dm": "hello",
        })
        member.send.assert_awaited()  # DM attempted
        # an unknown action raises (so the queue marks it failed)
        raised = False
        try:
            await bridge._dispatch(guild, "no_such_action", {})
        except ValueError:
            raised = True
        assert raised

    asyncio.run(run())

    # _finish transitions: retries then FAILED
    with SessionLocal() as s:
        row = queue.enqueue(s, "bad", {})
        s.commit()
        rid = row.id
    for _ in range(3):
        bridge._finish(rid, None, error="boom")
    with SessionLocal() as s:
        assert s.get(PendingAction, rid).status == queue.FAILED


def test_officer_can_create_event_and_mark_attendance():
    client = TestClient(app)
    _login(client, "officer")
    token = _csrf(client, "/muster-calls")
    r = client.post("/muster-calls/create",
                    data={"csrf": token, "name": "Regimental Drill", "event_type": "Drill",
                          "date": "2099-01-02", "time": "19:00"})
    assert r.status_code == 200
    with SessionLocal() as s:
        ev = s.query(Event).filter_by(name="Regimental Drill").one()
        event_id = ev.id
    assert len(_actions(queue.ANNOUNCE_EVENT)) == 1

    # mark attendance for the member
    token = _csrf(client, f"/muster-calls/{event_id}")
    client.post(f"/muster-calls/{event_id}/attendance",
                data={"csrf": token, "member_id": MEMBER_ID, "status": "present"})
    with SessionLocal() as s:
        rec = s.query(AttendanceRecord).filter_by(event_id=event_id, member_id=MEMBER_ID).one()
        assert rec.status == "present"


def test_award_grant_and_revoke():
    client = TestClient(app)
    _login(client, "officer")
    with SessionLocal() as s:
        award_id = s.query(AwardType).filter_by(name="Marksman").one().id

    token = _csrf(client)
    client.post(f"/members/{MEMBER_ID}/award",
                data={"csrf": token, "award_type_id": award_id, "notes": "sharp eye"})
    with SessionLocal() as s:
        assert s.query(MemberAward).filter_by(member_id=MEMBER_ID, award_type_id=award_id).count() == 1
    assert len(_actions(queue.AWARD_GRANTED)) == 1

    token = _csrf(client)
    client.post(f"/members/{MEMBER_ID}/award/{award_id}/revoke", data={"csrf": token})
    with SessionLocal() as s:
        assert s.query(MemberAward).filter_by(member_id=MEMBER_ID, award_type_id=award_id).count() == 0
    assert len(_actions(queue.AWARD_REVOKED)) == 1


def test_officer_can_create_award_type():
    client = TestClient(app)
    _login(client, "officer")
    client.post("/honors/award-type",
                data={"csrf": _csrf(client, "/honors"), "name": "Valor Medal", "emoji": "🎖️",
                      "description": "For gallantry."})
    with SessionLocal() as s:
        assert s.query(AwardType).filter_by(name="Valor Medal").count() == 1


def test_admin_updates_identity_roles_and_ladder():
    client = TestClient(app)
    _login(client, "admin")

    # identity
    client.post("/admin/identity",
                data={"csrf": _csrf(client, "/command-tent"), "regiment_name": "1st Texas",
                      "motto": "Onward", "brand_color": "#123456", "inactivity_days": "45"})
    with SessionLocal() as s:
        cfg = get_config(s)
        assert cfg.regiment_name == "1st Texas" and cfg.brand_color == 0x123456
        assert cfg.inactivity_days_threshold == 45

    # roles
    client.post("/admin/roles",
                data={"csrf": _csrf(client, "/command-tent"), "admin": "111", "officer": "222"})
    with SessionLocal() as s:
        cfg = get_config(s)
        assert cfg.admin_role_id == 111 and cfg.officer_role_id == 222

    # add a rank, move it, then remove it
    client.post("/admin/ranks/add",
                data={"csrf": _csrf(client, "/command-tent"), "name": "Colonel", "abbreviation": "Col"})
    with SessionLocal() as s:
        rank = s.query(Rank).filter_by(name="Colonel").one()
        assert rank.position == 3  # top of a 3-rung ladder
        rid = rank.id
    client.post(f"/admin/ranks/{rid}/move", data={"csrf": _csrf(client, "/command-tent"), "direction": "down"})
    with SessionLocal() as s:
        assert s.query(Rank).filter_by(name="Colonel").one().position == 2
    client.post(f"/admin/ranks/{rid}/remove", data={"csrf": _csrf(client, "/command-tent")})
    with SessionLocal() as s:
        assert s.query(Rank).filter_by(name="Colonel").count() == 0


def test_admin_company_default_and_add():
    client = TestClient(app)
    _login(client, "admin")
    with SessionLocal() as s:
        bravo_id = s.query(Company).filter_by(name="Bravo").one().id
    client.post(f"/admin/companies/{bravo_id}/default", data={"csrf": _csrf(client, "/command-tent")})
    with SessionLocal() as s:
        defaults = [c.name for c in s.query(Company).filter_by(is_default=True).all()]
        assert defaults == ["Bravo"]
    client.post("/admin/companies/add",
                data={"csrf": _csrf(client, "/command-tent"), "name": "Skirmishers", "is_default": "1"})
    with SessionLocal() as s:
        assert [c.name for c in s.query(Company).filter_by(is_default=True).all()] == ["Skirmishers"]


def test_command_tent_is_admin_only():
    client = TestClient(app)
    _login(client, "officer")
    assert client.get("/command-tent", follow_redirects=False).status_code == 403
    r = client.post("/admin/identity",
                    data={"csrf": "x", "regiment_name": "X", "brand_color": "#000000", "inactivity_days": "30"},
                    follow_redirects=False)
    assert r.status_code == 403


def test_bridge_announces_event_to_channel():
    from cogs.bridge import Bridge

    with SessionLocal() as s:
        get_config(s).announcements_channel_id = 555
        ev = Event(name="Battle", event_type="Battle",
                   scheduled_at=__import__("datetime").datetime(2099, 1, 1), created_by=1)
        s.add(ev)
        s.commit()
        event_id = ev.id

    bridge = object.__new__(Bridge)
    bridge.bot = MagicMock()
    guild = MagicMock()
    channel = AsyncMock()
    channel.id = 555
    sent = MagicMock()
    sent.id = 987654321
    channel.send.return_value = sent
    guild.get_channel.return_value = channel

    asyncio.run(bridge._dispatch(guild, queue.ANNOUNCE_EVENT, {"event_id": event_id}))

    channel.send.assert_awaited()
    bridge.bot.add_view.assert_called()
    with SessionLocal() as s:
        assert s.get(Event, event_id).message_id == 987654321


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        _reset()
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)} tests passed.")


if __name__ == "__main__":
    _run_all()
