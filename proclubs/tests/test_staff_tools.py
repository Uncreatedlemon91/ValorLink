"""Staff tools: the action inbox, the squad planner, the trials pipeline and
the set-piece book.

Run with: pytest proclubs/tests/test_staff_tools.py
"""
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import database  # noqa: E402
import recruitment  # noqa: E402
import services  # noqa: E402
import setpieces  # noqa: E402
import staff_tools  # noqa: E402
from formations import FORMATIONS  # noqa: E402
from models import Event  # noqa: E402


@pytest.fixture
def client():
    database.Base.metadata.drop_all(database.engine)
    with TestClient(appmod.app) as c:
        yield c


def _login(client, name, *, staff=False):
    data = {"name": name, "member": "1"}
    if staff:
        data["staff"] = "1"
    client.post("/auth/dev", data=data, follow_redirects=False)


def _csrf(client, path):
    m = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert m, path
    return m.group(1)


def _contract(uid, name, position="Striker", status="Starter", weeks=8):
    with database.get_session() as session:
        services.create_contract(session, discord_id=uid, display_name=name, avatar_url=None,
                                 position=position, squad_status=status, weeks=weeks,
                                 source="recorded", created_by_name="Boss")


# --- The inbox ------------------------------------------------------------------- #
def test_the_inbox_lists_what_needs_a_decision(client):
    _contract("1", "Ann", weeks=1)                       # runs out within the warning window
    with database.get_session() as session:
        session.add(Event(title="Derby", event_type="Match", scheduled_at=datetime.utcnow() + timedelta(hours=30)))
        session.add(Event(title="Cup tie", event_type="Match", scheduled_at=datetime.utcnow() - timedelta(hours=5)))
        session.commit()
        items = staff_tools.action_items(session, management=True)
        texts = " | ".join(i["text"] for i in items)
        assert "Ann's contract" in texts
        assert "No team sheet yet for Derby" in texts
        assert "1 player hasn't answered Derby." in texts
        assert "No match report for Cup tie" in texts
        assert "no gamertag linked" in texts
        assert items[0]["level"] == "warn"
        # Coaches don't see contract business.
        coach_texts = " | ".join(i["text"] for i in staff_tools.action_items(session, management=False))
        assert "contract" not in coach_texts


def test_staff_see_the_inbox_on_the_dashboard_and_members_dont(client):
    _contract("1", "Ann")
    _login(client, "Coach", staff=True)
    assert "Needs a decision" in client.get("/").text
    _login(client, "Fan")
    assert "Needs a decision" not in client.get("/").text


# --- The planner ------------------------------------------------------------------- #
def test_the_planner_shows_a_what_if_without_saving(client):
    _contract("1", "Keeper", position="Goalkeeper")
    _contract("2", "Ann", position="Striker")
    _login(client, "Coach", staff=True)
    html = client.get("/squad/planner", params={
        "status__2": "Release", "new_position__0": "Centre Back", "new_name__0": "Trialist",
        "new_status__0": "Starter"}).text
    assert "With your changes" in html and "Trialist" in html
    with database.get_session() as session:
        assert services.live_contract_for(session, "2").squad_status == "Starter"   # untouched


def test_planned_depth_counts_signings_and_drops_releases():
    from types import SimpleNamespace as C
    contracts = [C(discord_id="1", display_name="Keeper", position="Goalkeeper", secondary_position=None,
                   squad_status="Starter"),
                 C(discord_id="2", display_name="Ann", position="Striker", secondary_position=None,
                   squad_status="Rotation")]
    planned = staff_tools.plan_squad(contracts, {"2": "Release"},
                                     [{"name": "New CB", "position": "Centre Back", "status": "Starter"}])
    summary = staff_tools.plan_summary(planned, FORMATIONS["4-3-3"])
    states = {r["position"]: r["state"] for r in summary["depth"]["rows"]}
    assert states["Goalkeeper"] == "thin" and states["Striker"] == "gap"
    assert summary["by_status"] == {"Starter": 2, "Rotation": 0, "Substitute": 0}
    assert summary["size"] == 2


def test_members_cannot_use_staff_tools(client):
    _login(client, "Fan")
    for path in ("/squad/planner", "/recruitment"):
        assert client.get(path).status_code == 403, path


# --- Recruitment ------------------------------------------------------------------- #
def test_a_prospect_moves_through_the_pipeline(client):
    with database.get_session() as session:
        e = Event(title="Scrim", event_type="Scrim", scheduled_at=datetime.utcnow() - timedelta(days=1))
        session.add(e)
        session.commit()
        p = recruitment.add_prospect(session, name="Dee", discord_id="", gamertag="Dee_GT",
                                     position="Winger", secondary="", source="LFG", added_by="Coach")
        with pytest.raises(services.ServiceError, match="1 to 10"):
            recruitment.add_note(session, p, body="x", rating="11", event_id="", author="Coach")
        recruitment.add_note(session, p, body="Quick, direct", rating="7", event_id=str(e.id), author="Coach")
        assert p.stage == "trial"
        recruitment.add_note(session, p, body="Good again", rating="8", event_id="", author="Coach")
        assert recruitment.average_rating(recruitment.notes_for(session, p.id)) == 7.5
        recruitment.set_stage(session, p, "offered")
        assert [x.name for x in recruitment.board(session)["offered"]] == ["Dee"]


def test_staff_add_prospects_on_the_site(client):
    _login(client, "Coach", staff=True)
    token = _csrf(client, "/recruitment")
    r = client.post("/recruitment", data={"name": "Eli", "position": "Centre Back", "csrf_token": token})
    assert r.status_code == 200 and "Eli added to the pipeline." in r.text
    assert "Eli" in client.get("/recruitment").text


# --- Set pieces -------------------------------------------------------------------- #
def test_the_set_piece_book(client):
    _contract("5", "Bo")
    _login(client, "Coach", staff=True)
    token = _csrf(client, "/set-pieces")
    client.post("/set-pieces", data={"name": "Near-post flick", "kind": "Corner", "side": "Left",
                                     "taker_id": "5", "routine": "Whipped in, CB flicks on",
                                     "csrf_token": token})
    _login(client, "Fan")
    html = client.get("/set-pieces").text
    assert "Near-post flick" in html and "Bo" in html and "Add a routine" not in html
    with database.get_session() as session:
        with pytest.raises(services.ServiceError, match="kind"):
            setpieces.save(session, None, name="x", kind="Header", side="", taker_id="", taker_name="",
                           routine="", targets="", by_name="c")
