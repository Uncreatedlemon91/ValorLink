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
os.environ["WEB_DEV_LOGIN"] = "1"
os.environ["WEB_SESSION_SECRET"] = "test-secret"

import config  # noqa: E402
config.DATABASE_URL = os.environ["DATABASE_URL"]

from fastapi.testclient import TestClient  # noqa: E402

from db.base import Base, SessionLocal, engine  # noqa: E402
from db.models import (  # noqa: E402
    Candidacy,
    Company,
    DisciplinaryRecord,
    Member,
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


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        _reset()
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)} tests passed.")


if __name__ == "__main__":
    _run_all()
