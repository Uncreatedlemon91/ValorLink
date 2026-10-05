"""Training sessions: plans, role briefs, reviews and suggestions.

Run with: pytest proclubs/tests/test_training.py
"""
import os
import re
import sys
import zlib
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import database  # noqa: E402
import discord_rsvp  # noqa: E402
import services  # noqa: E402
import training  # noqa: E402
from models import Event  # noqa: E402


@pytest.fixture
def client():
    database.Base.metadata.drop_all(database.engine)
    with TestClient(appmod.app) as c:
        yield c


def _id(name):
    return str(zlib.crc32(name.encode()))


def _login(client, name, *, staff=False):
    data = {"name": name, "member": "1"}
    if staff:
        data["staff"] = "1"
    client.post("/auth/dev", data=data, follow_redirects=False)
    return _id(name)


def _csrf(client, path):
    m = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert m, path
    return m.group(1)


def _session_event(hours=24, event_type="Training") -> int:
    with database.get_session() as session:
        e = Event(title="Pressing drills", event_type=event_type, formation="4-3-3",
                  scheduled_at=datetime.utcnow() + timedelta(hours=hours))
        session.add(e)
        session.commit()
        return e.id


def test_a_plan_keeps_only_real_positions_and_sane_lengths(client):
    eid = _session_event()
    with database.get_session() as session:
        event = services.get_event(session, eid)
        with pytest.raises(services.ServiceError, match="isn't a position"):
            training.save_plan(session, event, objective="x", plan="", role_briefs={"XX": "go"})
        with pytest.raises(services.ServiceError, match="objective"):
            training.save_plan(session, event, objective="x" * 201, plan="", role_briefs={})
        training.save_plan(session, event, objective="Press together", plan="1. Rondos",
                           role_briefs={"ST": "Curve your run to cut the pass", "LW": "  "})
        assert training.briefs(event) == {"ST": "Curve your run to cut the pass"}


def test_a_player_sees_their_own_brief(client):
    eid = _session_event()
    uid = _login(client, "Ann")
    with database.get_session() as session:
        event = services.get_event(session, eid)
        training.save_plan(session, event, objective="Press together", plan="",
                           role_briefs={"ST": "Curve your run", "GK": "Sweep behind"})
        services.claim_slot(session, event, discord_user_id=int(uid), discord_name="Ann",
                            discord_avatar=None, slot_key="ST", source="site")
    html = client.get(f"/events/{eid}").text
    mine = html[html.index('class="my-brief"'):]
    assert "Your brief · ST" in mine[:200] and "Curve your run" in mine[:400]
    assert "Sweep behind" in html             # everyone sees the full list


def test_staff_write_the_plan_and_it_reaches_the_discord_post(client):
    eid = _session_event()
    _login(client, "Coach", staff=True)
    token = _csrf(client, f"/events/{eid}/plan")
    r = client.post(f"/events/{eid}/plan", data={
        "objective": "Win it back in five seconds", "plan": "1. Rondos\n2. Match",
        "brief__CM2": "Screen the pass into their 10", "action": "plan", "csrf_token": token})
    assert "Session plan saved." in r.text
    with database.get_session() as session:
        event = services.get_event(session, eid)
        embed = discord_rsvp.build_embed(event, [], {}, "https://x", services.event_slots(event))
    assert "**Objective:** Win it back in five seconds" in embed["description"]
    assert {"name": "Role briefs", "value": "**CM** — Screen the pass into their 10",
            "inline": False} in embed["fields"]


def test_members_cannot_edit_a_plan(client):
    eid = _session_event()
    _login(client, "Fan")
    assert client.get(f"/events/{eid}/plan").status_code == 403


def test_a_review_waits_for_the_session(client):
    eid = _session_event(hours=5)
    with database.get_session() as session:
        with pytest.raises(services.ServiceError, match="after it has happened"):
            training.save_review(session, services.get_event(session, eid), "Good", "Coach")


def test_suggestions_are_private_to_their_author_and_staff(client):
    _login(client, "Ann")
    token = _csrf(client, "/training")
    client.post("/training/suggestions", data={"body": "Overlapping full-backs", "csrf_token": token})
    assert "Overlapping full-backs" in client.get("/training").text
    _login(client, "Ben")
    assert "Overlapping full-backs" not in client.get("/training").text
    _login(client, "Coach", staff=True)
    html = client.get("/training").text
    assert "Overlapping full-backs" in html and "Ann" in html


def test_staff_answer_a_suggestion_and_the_player_sees_it(client):
    with database.get_session() as session:
        row = training.add_suggestion(session, discord_id=_id("Ann"), name="Ann", body="Set pieces")
    _login(client, "Coach", staff=True)
    token = _csrf(client, "/training")
    client.post(f"/training/suggestions/{row.id}", data={
        "status": "planned", "staff_note": "Thursday session", "csrf_token": token})
    _login(client, "Ann")
    html = client.get("/training").text
    assert "Thursday session" in html and "Planned" in html
    with database.get_session() as session:
        with pytest.raises(services.ServiceError, match="status"):
            training.set_suggestion_status(session, row.id, status="maybe", note="", by_name="x")


def test_sessions_are_listed_on_the_training_tab(client):
    _session_event(hours=30)
    _session_event(hours=30, event_type="Match")
    _login(client, "Fan")
    html = client.get("/training").text
    assert html.count('class="session-row"') == 1


def test_plan_a_session_preselects_training(client):
    _login(client, "Coach", staff=True)
    html = client.get("/events/new?type=Training").text
    assert '<option value="Training" selected' in html
