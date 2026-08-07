"""End-to-end tests for app.py's routes: auth gating, CSRF, and the article/
event/streamer flows through the actual HTTP layer.

Run with: pytest proclubs/tests/test_app.py
"""
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta

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
import services  # noqa: E402
from models import Event  # noqa: E402


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


def _seed_event(*, title="League Match", opponent="Rivals FC", scheduled_at=None,
                 event_type="Match", discord_event_id=None) -> int:
    """Events are read-only from the site now (Discord-sync only, see
    services.sync_discord_events) -- tests that need one on the page seed
    it directly rather than going through a since-removed /events/new."""
    with database.get_session() as session:
        event = Event(
            title=title, event_type=event_type, opponent=opponent,
            scheduled_at=scheduled_at or (datetime.utcnow() + timedelta(days=7)),
            discord_event_id=discord_event_id,
        )
        session.add(event)
        session.commit()
        session.refresh(event)
        return event.id


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


def test_home_shows_most_recent_article_as_featured(client):
    _login_staff(client)
    for title in ["First Post", "Second Post"]:
        token = _csrf(client, "/news/new")
        client.post("/news/new", data={
            "title": title, "summary": "", "body_md": "x",
            "published": "1", "csrf_token": token,
        }, follow_redirects=False)

    home = client.get("/")
    # The most recently published article leads as the featured story...
    assert 'href="/news/second-post"' in home.text
    assert home.text.index("second-post") < home.text.index("first-post")
    # ...linked twice within the hero itself (headline + CTA button), but
    # not a third time from the "Latest news" rail below it.
    assert home.text.count('href="/news/second-post"') == 2


def test_home_uses_real_crest_color_when_ea_data_available(client, monkeypatch):
    _seed_event()

    monkeypatch.setattr(appmod.config, "CLUB_ID", "8481799")
    monkeypatch.setattr(appmod.ea_client, "division_stats", lambda platform, club_id: None)
    monkeypatch.setattr(appmod.ea_client, "crest_colors", lambda platform, club_id: {
        "crest": "#C91B1B", "kit1": "#F2F2F2", "kit2": "#DB1812",
    })
    home = client.get("/")
    assert 'style="background:#C91B1B;"' in home.text
    assert "crest-branded" in home.text


def test_home_standing_band_shows_countup_points_and_accent_colored_ring(client, monkeypatch):
    monkeypatch.setattr(appmod.config, "CLUB_ID", "8481799")
    monkeypatch.setattr(appmod.ea_client, "division_stats", lambda platform, club_id: {
        "currentDivision": 3, "bestDivision": 1, "points": 1450,
    })
    monkeypatch.setattr(appmod.ea_client, "crest_colors", lambda platform, club_id: {
        "crest": "#C91B1B", "kit1": "#F2F2F2", "kit2": "#DB1812",
        "accent": "#6CACDE", "accent_trim": "#F2F2F2",
    })
    home = client.get("/")
    assert 'class="standing-band"' in home.text
    assert 'data-countup="1450"' in home.text
    # Uses the third-kit accent duo (blue + white), not the crest red.
    assert 'border-color: #6CACDE;' in home.text
    assert 'border-color: #F2F2F2;' in home.text
    assert '#C91B1B' not in home.text


def test_home_standing_band_handles_missing_points_gracefully(client, monkeypatch):
    monkeypatch.setattr(appmod.config, "CLUB_ID", "8481799")
    monkeypatch.setattr(appmod.ea_client, "division_stats", lambda platform, club_id: {
        "currentDivision": 3, "bestDivision": None, "points": None,
    })
    monkeypatch.setattr(appmod.ea_client, "crest_colors", lambda platform, club_id: None)
    home = client.get("/")
    assert "data-countup" not in home.text
    assert 'class="standing-band"' in home.text


def test_home_falls_back_to_neutral_crest_without_ea_data(client, monkeypatch):
    monkeypatch.setattr(appmod.config, "CLUB_ID", "")
    home = client.get("/")
    assert "crest-branded" not in home.text


def test_article_category_defaults_and_can_be_set(client):
    _login_staff(client)
    token = _csrf(client, "/news/new")
    r = client.post("/news/new", data={
        "title": "Transfer Window Update", "category": "Transfer", "summary": "",
        "body_md": "x", "published": "1", "csrf_token": token,
    }, follow_redirects=False)
    assert r.status_code == 303

    detail = client.get("/news/transfer-window-update")
    assert "Transfer" in detail.text


def test_news_list_filters_by_category(client):
    _login_staff(client)
    for title, category in [("News Item", "News"), ("Transfer Item", "Transfer")]:
        token = _csrf(client, "/news/new")
        client.post("/news/new", data={
            "title": title, "category": category, "summary": "",
            "body_md": "x", "published": "1", "csrf_token": token,
        }, follow_redirects=False)

    filtered = client.get("/news?category=Transfer")
    assert "Transfer Item" in filtered.text
    assert "News Item" not in filtered.text


def test_csrf_token_is_required_on_writes(client):
    _login_staff(client)
    r = client.post("/streamers/add", data={
        "display_name": "Bad", "twitch_login": "bad", "csrf_token": "not-the-real-token",
    })
    assert r.status_code == 400


def test_events_page_shows_events_but_has_no_editing_ui_even_for_staff(client):
    _login_staff(client)
    _seed_event(title="League Match", opponent="Rivals FC")

    listing = client.get("/events")
    assert "Rivals FC" in listing.text
    assert "New event" not in listing.text
    assert ">Edit<" not in listing.text
    assert "/events/new" not in listing.text
    assert "/edit" not in listing.text


def test_event_editing_routes_no_longer_exist(client):
    _login_staff(client)
    event_id = _seed_event()

    assert client.get("/events/new").status_code == 404
    assert client.post("/events/new", data={"title": "x", "scheduled_at": "2027-01-01T18:00", "csrf_token": "x"}).status_code == 404
    assert client.get(f"/events/{event_id}/edit").status_code == 404
    assert client.post(f"/events/{event_id}/edit", data={"title": "x", "scheduled_at": "2027-01-01T18:00", "csrf_token": "x"}).status_code == 404
    assert client.post(f"/events/{event_id}/delete", data={"csrf_token": "x"}).status_code == 404


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


def test_nav_says_live_not_streamers(client):
    home = client.get("/")
    assert ">Live</a>" in home.text
    assert ">Streamers</a>" not in home.text


def test_featured_streamer_gets_embedded_player_on_live_page(client):
    _login_staff(client)
    token = _csrf(client, "/streamers")
    client.post("/streamers/add", data={
        "display_name": "n0v84", "twitch_login": "n0v84", "featured": "1", "csrf_token": token,
    }, follow_redirects=False)

    page = client.get("/streamers")
    assert "player.twitch.tv/?channel=n0v84" in page.text
    assert "Featured" in page.text


def test_feature_route_switches_the_featured_channel(client):
    _login_staff(client)
    for name in ["first_streamer", "second_streamer"]:
        token = _csrf(client, "/streamers")
        client.post("/streamers/add", data={
            "display_name": name, "twitch_login": name, "csrf_token": token,
        }, follow_redirects=False)

    with database.get_session() as session:
        second = next(s for s in services.list_streamers(session) if s.twitch_login == "second_streamer")
        second_id = second.id

    token = _csrf(client, "/streamers")
    r = client.post(f"/streamers/{second_id}/feature", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303

    page = client.get("/streamers")
    assert "player.twitch.tv/?channel=second_streamer" in page.text
    # And it's no longer offered as "Make Featured" now that it's the featured one.
    assert f'action="/streamers/{second_id}/feature"' not in page.text


def test_home_hides_the_entire_live_section_unless_someone_is_live(client, monkeypatch):
    _login_staff(client)
    token = _csrf(client, "/streamers")
    client.post("/streamers/add", data={
        "display_name": "n0v84", "twitch_login": "n0v84", "featured": "1", "csrf_token": token,
    }, follow_redirects=False)

    # Offline (the default in tests -- Twitch isn't configured): the whole
    # Live section is gone, not just the player -- no heading, no card.
    home = client.get("/")
    assert "player.twitch.tv/?channel=n0v84" not in home.text
    assert "featured-stream" not in home.text
    assert ">Live Now<" not in home.text

    # Live: the section reappears with the embedded player.
    monkeypatch.setattr(appmod.twitch_client, "live_streams", lambda logins: {
        "n0v84": {"title": "ranked grind", "viewer_count": 12, "thumbnail_url": "", "url": "https://twitch.tv/n0v84"},
    })
    home = client.get("/")
    assert "player.twitch.tv/?channel=n0v84" in home.text
    assert ">Live Now<" in home.text


def test_home_shows_live_section_for_a_non_featured_streamer_even_if_featured_is_offline(client, monkeypatch):
    _login_staff(client)
    for name in ["n0v84", "sidekick"]:
        token = _csrf(client, "/streamers")
        client.post("/streamers/add", data={
            "display_name": name, "twitch_login": name,
            "featured": "1" if name == "n0v84" else "", "csrf_token": token,
        }, follow_redirects=False)

    # Only the non-featured one is live -- featured stays offline (hidden),
    # but the section still shows for the one that is live.
    monkeypatch.setattr(appmod.twitch_client, "live_streams", lambda logins: {
        "sidekick": {"title": "grinding ranked", "viewer_count": 3, "thumbnail_url": "", "url": "https://twitch.tv/sidekick"},
    })
    home = client.get("/")
    assert ">Live Now<" in home.text
    assert "player.twitch.tv/?channel=n0v84" not in home.text  # featured is offline, no embed
    assert "Also live now" in home.text


def test_logout_clears_session(client):
    _login_staff(client)
    assert client.get("/news/new").status_code == 200
    client.get("/logout", follow_redirects=False)
    r = client.get("/news/new", follow_redirects=False)
    assert r.status_code == 303
