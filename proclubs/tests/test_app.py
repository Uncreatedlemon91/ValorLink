"""End-to-end tests for app.py's routes: auth gating, CSRF, and the article/
event/streamer flows through the actual HTTP layer.

Run with: pytest proclubs/tests/test_app.py
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="proclubs-app-")
os.environ["SITE_DB_PATH"] = os.path.join(_TMP, "site.db")
os.environ["DEV_LOGIN"] = "1"
os.environ["SESSION_SECRET"] = "test-secret"
os.environ["HTTPS_ONLY"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import database  # noqa: E402


@pytest.fixture
def client():
    database.Base.metadata.drop_all(database.engine)
    with TestClient(appmod.app) as c:
        yield c


def _login_staff(client, name="Coach"):
    r = client.post("/auth/dev", data={"name": name, "staff": "1"}, follow_redirects=False)
    assert r.status_code == 303
    return client


def _login_fan(client, name="Fan"):
    r = client.post("/auth/dev", data={"name": name}, follow_redirects=False)
    assert r.status_code == 303
    return client


def _csrf(client, path):
    html = client.get(path).text
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, f"no csrf token found on {path}"
    return m.group(1)


def test_public_pages_load_signed_out(client):
    for path in ["/", "/news", "/events", "/streamers", "/stats", "/login"]:
        assert client.get(path).status_code == 200


def test_anonymous_staff_route_redirects_to_login(client):
    r = client.get("/news/new", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_signed_in_fan_cannot_reach_staff_route(client):
    _login_fan(client)
    r = client.get("/news/new")
    assert r.status_code == 403


def test_staff_can_publish_an_article(client):
    _login_staff(client)
    token = _csrf(client, "/news/new")
    r = client.post("/news/new", data={
        "title": "Season Opener", "summary": "We're back",
        "body_md": "# Big news\n\nHere we go.", "published": "1", "csrf_token": token,
    }, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/news/season-opener"

    detail = client.get("/news/season-opener")
    assert detail.status_code == 200
    assert "<h1>Big news</h1>" in detail.text

    listing = client.get("/news")
    assert "Season Opener" in listing.text


def test_draft_article_hidden_from_fans_visible_to_staff(client):
    _login_staff(client)
    token = _csrf(client, "/news/new")
    client.post("/news/new", data={
        "title": "Unfinished Draft", "summary": "", "body_md": "wip",
        "published": "", "csrf_token": token,
    }, follow_redirects=False)

    fan = TestClient(appmod.app)
    with fan:
        _login_fan(fan)
        r = fan.get("/news/unfinished-draft")
        assert r.status_code == 404
        assert "Unfinished Draft" not in fan.get("/news").text

    r = client.get("/news/unfinished-draft")
    assert r.status_code == 200


def test_csrf_token_is_required_on_writes(client):
    _login_staff(client)
    r = client.post("/streamers/add", data={
        "display_name": "Bad", "twitch_login": "bad", "csrf_token": "not-the-real-token",
    })
    assert r.status_code == 400


def test_event_crud_round_trip(client):
    _login_staff(client)
    token = _csrf(client, "/events/new")
    r = client.post("/events/new", data={
        "title": "League Match", "event_type": "Match", "opponent": "Rivals FC",
        "description": "", "scheduled_at": "2027-01-15T18:00", "result": "",
        "csrf_token": token,
    }, follow_redirects=False)
    assert r.status_code == 303

    listing = client.get("/events")
    assert "Rivals FC" in listing.text

    m = re.search(r"/events/(\d+)/edit", listing.text)
    assert m, "expected an edit link for staff"
    event_id = m.group(1)

    edit_token = _csrf(client, f"/events/{event_id}/edit")
    r = client.post(f"/events/{event_id}/delete", data={"csrf_token": edit_token}, follow_redirects=False)
    assert r.status_code == 303
    assert "Rivals FC" not in client.get("/events").text


def test_duplicate_streamer_is_rejected(client):
    _login_staff(client)
    token = _csrf(client, "/streamers")
    client.post("/streamers/add", data={
        "display_name": "Cap", "twitch_login": "shroud", "csrf_token": token,
    }, follow_redirects=False)

    token = _csrf(client, "/streamers")
    r = client.post("/streamers/add", data={
        "display_name": "Cap Again", "twitch_login": "shroud", "csrf_token": token,
    })
    assert r.status_code == 400


def test_logout_clears_session(client):
    _login_staff(client)
    assert client.get("/news/new").status_code == 200
    client.get("/logout", follow_redirects=False)
    r = client.get("/news/new", follow_redirects=False)
    assert r.status_code == 303
