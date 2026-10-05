"""Development goals and monthly reviews.

Run with: pytest proclubs/tests/test_development.py
"""
import os
import re
import sys
import zlib
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import config  # noqa: E402
import database  # noqa: E402
import development as dev  # noqa: E402
import discord_notify  # noqa: E402
import services  # noqa: E402

COACH = {"id": 99, "name": "Gaffer"}


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


def _player(uid, name):
    with database.get_session() as session:
        services.ensure_player(session, discord_id=uid, display_name=name)


def test_goals_are_checked_and_capped(client):
    with database.get_session() as session:
        with pytest.raises(services.ServiceError, match="Write the goal"):
            dev.set_goal(session, discord_id="1", text=" ", area="", due_on=None, coach=COACH)
        with pytest.raises(services.ServiceError, match="already passed"):
            dev.set_goal(session, discord_id="1", text="x", area="", due_on=date(2020, 1, 1), coach=COACH)
        for i in range(dev.MAX_OPEN_GOALS):
            dev.set_goal(session, discord_id="1", text=f"Goal {i}", area="Defending", due_on=None, coach=COACH)
        with pytest.raises(services.ServiceError, match="close one first"):
            dev.set_goal(session, discord_id="1", text="One more", area="", due_on=None, coach=COACH)


def test_only_the_player_updates_their_progress(client):
    with database.get_session() as session:
        goal = dev.set_goal(session, discord_id="1", text="Track runners", area="", due_on=None, coach=COACH)
        with pytest.raises(services.ServiceError, match="your own"):
            dev.update_progress(session, goal, "2", progress=50, note="")
        with pytest.raises(services.ServiceError, match="how far"):
            dev.update_progress(session, goal, "1", progress=33, note="")
        dev.update_progress(session, goal, "1", progress=75, note="Better already")
        dev.close_goal(session, goal, status="achieved", by_name="Gaffer")
        assert (goal.progress, goal.status) == (100, "achieved")
        with pytest.raises(services.ServiceError, match="closed"):
            dev.update_progress(session, goal, "1", progress=50, note="")


def test_one_review_per_month(client):
    with database.get_session() as session:
        dev.save_review(session, discord_id="1", month="2026-10", summary="Good month", coach=COACH)
        dev.save_review(session, discord_id="1", month="2026-10", summary="Great month", coach=COACH)
        assert [r.summary for r in dev.reviews_for(session, "1")] == ["Great month"]
        with pytest.raises(services.ServiceError, match="month"):
            dev.save_review(session, discord_id="1", month="October", summary="x", coach=COACH)


def test_a_coach_sets_a_goal_and_the_player_is_dmed(client, monkeypatch):
    dms = []
    monkeypatch.setattr(config, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(config, "NOTIFY_DMS", True)
    monkeypatch.setattr(discord_notify, "send_dm", lambda uid, content, embeds=None: dms.append((uid, content)))
    _player("42", "Cap")
    _login(client, "Coach", staff=True)
    token = _csrf(client, "/players/42")
    r = client.post("/players/42/goals", data={"text": "Track the overlapping full-back",
                                               "area": "Defending", "csrf_token": token})
    assert "Goal set for Cap." in r.text
    assert dms == [("42", dms[0][1])] and "Track the overlapping full-back" in dms[0][1]


def test_goals_are_seen_by_the_player_and_staff_only(client):
    ann = _id("Ann")
    _player(ann, "Ann")
    with database.get_session() as session:
        dev.set_goal(session, discord_id=ann, text="Win more headers", area="", due_on=None, coach=COACH)
        dev.save_review(session, discord_id=ann, month="2026-10", summary="Strong in the air", coach=COACH)
    _login(client, "Ann")
    html = client.get(f"/players/{ann}").text
    assert "Win more headers" in html and "Strong in the air" in html
    assert 'name="progress"' in html            # she can update it
    _login(client, "Ben")
    html = client.get(f"/players/{ann}").text
    assert "Win more headers" not in html and "Strong in the air" not in html


def test_a_player_updates_progress_from_their_file(client):
    ann = _login(client, "Ann")
    _player(ann, "Ann")
    with database.get_session() as session:
        goal = dev.set_goal(session, discord_id=ann, text="Win more headers", area="", due_on=None, coach=COACH)
    token = _csrf(client, f"/players/{ann}")
    client.post(f"/players/{ann}/goals/{goal.id}/progress",
                data={"progress": "50", "note": "Timing the jump", "csrf_token": token})
    with database.get_session() as session:
        assert dev.get_goal(session, goal.id).progress == 50


def test_members_cannot_set_goals(client):
    _player("42", "Cap")
    _login(client, "Fan")
    token = _csrf(client, "/players/me")
    assert client.post("/players/42/goals", data={"text": "x", "csrf_token": token}).status_code == 403
