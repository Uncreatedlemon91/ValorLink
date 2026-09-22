"""End-to-end tests for app.py's routes: auth gating, CSRF, and the article/
event/streamer flows through the actual HTTP layer.

Run with: pytest proclubs/tests/test_app.py
"""
import json
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
from nacl.signing import SigningKey  # noqa: E402

import app as appmod  # noqa: E402
import config  # noqa: E402
import database  # noqa: E402
import discord_rsvp  # noqa: E402
import services  # noqa: E402
from models import Clip, Event  # noqa: E402


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
    """A regular signed-in guild member -- can comment/like, not staff."""
    r = client.post("/auth/dev", data={"name": name, "member": "1"}, follow_redirects=False)
    assert r.status_code == 303
    return client


def _login_non_member(client, name="Outsider"):
    """Signed in with Discord, but not in our guild -- can't comment/like."""
    r = client.post("/auth/dev", data={"name": name}, follow_redirects=False)
    assert r.status_code == 303
    return client


def _seed_article(*, title="Recap", body_html="<p>Great win.</p>", published=True,
                   cover_image=None, cover_focal_x=50, cover_focal_y=50) -> str:
    with database.get_session() as session:
        article = services.create_article(
            session, title=title, summary="", body_html=body_html, cover_image=cover_image,
            published=published, author={"id": 999, "name": "Coach", "avatar": None},
            cover_focal_x=cover_focal_x, cover_focal_y=cover_focal_y,
        )
        return article.slug


def _csrf(client, path):
    html = client.get(path).text
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, f"no csrf token found on {path}"
    return m.group(1)


def _seed_event(*, title="League Match", opponent="Rivals FC", scheduled_at=None,
                 event_type="Match", discord_event_id=None, image=None) -> int:
    """Events are read-only from the site now (Discord-sync only, see
    services.sync_discord_events) -- tests that need one on the page seed
    it directly rather than going through a since-removed /events/new."""
    with database.get_session() as session:
        event = Event(
            title=title, event_type=event_type, opponent=opponent,
            scheduled_at=scheduled_at or (datetime.utcnow() + timedelta(days=7)),
            discord_event_id=discord_event_id, image=image,
        )
        session.add(event)
        session.commit()
        session.refresh(event)
        return event.id


def test_public_pages_load_signed_out(client):
    for path in ["/", "/news", "/events", "/streamers", "/stats", "/league", "/tactics", "/login"]:
        assert client.get(path).status_code == 200


def test_tactics_page_hides_editing_ui_from_non_staff(client):
    anon = client.get("/tactics")
    assert "Save Lineup" not in anon.text
    assert "tactics-roster" not in anon.text
    assert "Substitutes Bench" in anon.text  # read-only for everyone, like the pitch

    _login_fan(client)
    fan = client.get("/tactics")
    assert "Save Lineup" not in fan.text


def test_tactics_page_shows_editing_ui_to_staff(client):
    _login_staff(client)
    r = client.get("/tactics")
    assert "Save Lineup" in r.text
    assert "tactics-roster" in r.text


def test_api_tactics_save_requires_staff(client):
    r = client.post("/api/tactics", data={
        "formation": "4-3-3", "slots_json": "{}", "csrf_token": "x",
    }, follow_redirects=False)
    assert r.status_code in (303, 401, 403)

    _login_fan(client)
    r = client.post("/api/tactics", data={
        "formation": "4-3-3", "slots_json": "{}", "csrf_token": "x",
    })
    assert r.status_code == 403


def test_api_tactics_save_requires_csrf(client):
    _login_staff(client)
    r = client.post("/api/tactics", data={
        "formation": "4-3-3", "slots_json": "{}", "csrf_token": "wrong-token",
    })
    assert r.status_code == 400


def test_api_tactics_save_persists_lineup(client):
    _login_staff(client)
    token = _csrf(client, "/tactics")
    r = client.post("/api/tactics", data={
        "formation": "4-3-3", "slots_json": '{"GK": "Rusty", "ST": "Grey"}', "csrf_token": token,
    })
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    page = client.get("/tactics").text
    assert "Rusty" in page
    assert "Grey" in page


def test_api_tactics_save_persists_bench_slots(client):
    _login_staff(client)
    token = _csrf(client, "/tactics")
    r = client.post("/api/tactics", data={
        "formation": "4-3-3", "slots_json": '{"GK": "Rusty", "SUB1": "Benchwarmer"}', "csrf_token": token,
    })
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    page = client.get("/tactics").text
    assert "Rusty" in page
    assert "Benchwarmer" in page


def test_api_tactics_save_rejects_unknown_formation(client):
    _login_staff(client)
    token = _csrf(client, "/tactics")
    r = client.post("/api/tactics", data={
        "formation": "not-a-real-formation", "slots_json": "{}", "csrf_token": token,
    })
    assert r.status_code == 400


def test_api_tactics_save_rejects_malformed_json(client):
    _login_staff(client)
    token = _csrf(client, "/tactics")
    r = client.post("/api/tactics", data={
        "formation": "4-3-3", "slots_json": "not json", "csrf_token": token,
    })
    assert r.status_code == 400


def test_api_tactics_save_rejects_unknown_slot_key(client):
    _login_staff(client)
    token = _csrf(client, "/tactics")
    r = client.post("/api/tactics", data={
        "formation": "4-3-3", "slots_json": '{"NOT_A_REAL_SLOT": "Grey"}', "csrf_token": token,
    })
    assert r.status_code == 400
    assert "Unknown slot" in r.json()["error"]


def test_formations_cover_all_fc26_shapes():
    expected_names = {
        "4-3-3", "4-3-2-1", "4-2-3-1", "4-2-2-2", "4-4-2", "4-4-1-1",
        "4-1-2-1-2", "4-1-3-2", "4-1-4-1", "4-5-1",
        "3-4-3", "3-4-2-1", "3-4-1-2", "3-5-2", "3-5-1-1", "3-1-4-2",
        "5-2-1-2", "5-2-3", "5-3-2", "5-4-1",
    }
    assert set(appmod.FORMATIONS.keys()) == expected_names
    assert len(appmod.FORMATIONS) == 20

    for name, layout in appmod.FORMATIONS.items():
        expected_outfield = sum(int(n) for n in name.split("-"))
        assert len(layout) == expected_outfield + 1, (
            f"{name}: expected {expected_outfield + 1} slots (incl. GK), got {len(layout)}"
        )
        assert "GK" in layout
        for slot_key, slot in layout.items():
            assert 0 <= slot["top"] <= 100, f"{name}.{slot_key}: top out of range"
            assert 0 <= slot["left"] <= 100, f"{name}.{slot_key}: left out of range"
            assert slot["label"]


def test_bench_slots_are_formation_independent():
    assert set(appmod.BENCH_SLOTS.keys()) == {f"SUB{i}" for i in range(1, 8)}
    for slot_key, slot in appmod.BENCH_SLOTS.items():
        assert slot["label"]
        # Bench slots render in a static row, not positioned on the pitch.
        assert "top" not in slot and "left" not in slot
    # No pitch slot key collides with a bench slot key in any formation.
    for name, layout in appmod.FORMATIONS.items():
        assert not (set(layout) & set(appmod.BENCH_SLOTS)), f"{name} has a slot key colliding with BENCH_SLOTS"


def test_league_page_shows_not_configured_when_club_id_unset(client, monkeypatch):
    monkeypatch.setattr(config, "CLUB_ID", "")
    r = client.get("/league")
    assert r.status_code == 200
    assert "isn't configured" in r.text


def test_league_page_shows_empty_state_when_no_data_yet(client, monkeypatch):
    monkeypatch.setattr(config, "CLUB_ID", "8481799")
    monkeypatch.setattr(appmod.db, "league_table", lambda platform, club_id: [])
    monkeypatch.setattr(appmod.db, "latest_snapshot", lambda platform, club_id: None)
    monkeypatch.setattr(appmod.db, "league_roster", lambda platform: [])
    r = client.get("/league")
    assert r.status_code == 200
    assert "No league data yet" in r.text


def test_league_page_renders_table_rows(client, monkeypatch):
    monkeypatch.setattr(config, "CLUB_ID", "8481799")
    monkeypatch.setattr(appmod.db, "league_table", lambda platform, club_id: [
        {"club_id": "c2", "label": "Rivals FC", "is_us": False, "division": "3", "points": 15,
         "played": 3, "team_size": 6, "form": ["W", "W", "D"], "has_data": True},
        {"club_id": "8481799", "label": "YeeHaw FC", "is_us": True, "division": "3", "points": 10,
         "played": 2, "team_size": 5, "form": ["L", "W"], "has_data": True},
    ])
    monkeypatch.setattr(appmod.db, "latest_snapshot", lambda platform, club_id: {"division": "3"})
    monkeypatch.setattr(appmod.db, "league_roster", lambda platform: [
        {"club_id": "c2", "label": "Rivals FC", "added_at": 1, "pinned": 0},
        {"club_id": "8481799", "label": "YeeHaw FC", "added_at": 1, "pinned": 1},
    ])

    r = client.get("/league")
    assert r.status_code == 200
    assert "Rivals FC" in r.text
    assert "YeeHaw FC" in r.text
    # The lede describes the grouping without printing the tier number:
    # it comes from EA's all-time leaderboard and lags live play badly.
    assert "groups in the same tier as us" in r.text
    assert "2 of" in r.text  # roster_size footnote
    # Sorted by points, highest first -- match the specific table-row spans,
    # not just any mention of "YeeHaw FC" (which is also the site's own brand
    # name, shown in the nav/footer well before the table itself).
    assert r.text.index('lt-name">Rivals FC') < r.text.index('lt-name">YeeHaw FC')
    assert 'class="lt-us-pill"' in r.text


def test_league_page_explains_roster_members_hidden_by_division(client, monkeypatch):
    # A club can be in the roster (we've played them) without appearing in
    # the main table (different division right now) -- the page must say
    # so explicitly rather than the club just silently not being there.
    monkeypatch.setattr(config, "CLUB_ID", "8481799")
    monkeypatch.setattr(appmod.db, "league_table", lambda platform, club_id: [
        {"club_id": "8481799", "label": "YeeHaw FC", "is_us": True, "division": "8", "points": 10,
         "played": 2, "team_size": 5, "form": ["L", "W"], "has_data": True},
    ])

    def fake_latest_snapshot(platform, club_id):
        if club_id == "8481799":
            return {"division": "8"}
        return {"division": "5"}  # the other tracked club -- a different division

    monkeypatch.setattr(appmod.db, "latest_snapshot", fake_latest_snapshot)
    monkeypatch.setattr(appmod.db, "league_roster", lambda platform: [
        {"club_id": "8481799", "label": "YeeHaw FC", "added_at": 1, "pinned": 1},
        {"club_id": "c2", "label": "Rivals FC", "added_at": 2, "pinned": 0},
    ])

    r = client.get("/league")
    assert r.status_code == 200
    assert "1 more tracked club" in r.text
    assert "not shown above" in r.text
    assert "Rivals FC" in r.text
    # Named as a tier, not a division number: the value behind it is EA's
    # stale leaderboard tier, and no division is shown anywhere on the site.
    assert "different tier" in r.text
    assert "Division 5" not in r.text


def test_league_page_explains_roster_members_not_polled_yet(client, monkeypatch):
    monkeypatch.setattr(config, "CLUB_ID", "8481799")
    monkeypatch.setattr(appmod.db, "league_table", lambda platform, club_id: [
        {"club_id": "8481799", "label": "YeeHaw FC", "is_us": True, "division": "8", "points": 10,
         "played": 2, "team_size": 5, "form": ["L", "W"], "has_data": True},
    ])

    def fake_latest_snapshot(platform, club_id):
        if club_id == "8481799":
            return {"division": "8"}
        return None  # just added this run, not polled yet

    monkeypatch.setattr(appmod.db, "latest_snapshot", fake_latest_snapshot)
    monkeypatch.setattr(appmod.db, "league_roster", lambda platform: [
        {"club_id": "8481799", "label": "YeeHaw FC", "added_at": 1, "pinned": 1},
        {"club_id": "c2", "label": "Rivals FC", "added_at": 2, "pinned": 0},
    ])

    r = client.get("/league")
    assert r.status_code == 200
    assert "not polled yet" in r.text
    assert "Rivals FC" in r.text


def test_api_history_rivals_returns_tracked_since_and_records(client, monkeypatch):
    monkeypatch.setattr(appmod.db, "tracked_since", lambda platform, club_id: 1700000000)
    monkeypatch.setattr(appmod.db, "rival_records", lambda platform, club_id: [
        {"name": "Rivals FC", "played": 3, "wins": 2, "draws": 1, "losses": 0,
         "gf": 7, "ga": 4, "last_outcome": "W", "last_played_at": 1700000500},
    ])
    r = client.get("/api/history/rivals")
    assert r.status_code == 200
    body = r.json()
    assert body["trackedSince"] == 1700000000
    assert body["rivals"][0]["name"] == "Rivals FC"
    assert body["rivals"][0]["played"] == 3


def test_api_history_rivals_empty_when_untracked(client, monkeypatch):
    monkeypatch.setattr(appmod.db, "tracked_since", lambda platform, club_id: None)
    monkeypatch.setattr(appmod.db, "rival_records", lambda platform, club_id: [])
    r = client.get("/api/history/rivals")
    assert r.status_code == 200
    assert r.json() == {"trackedSince": None, "rivals": []}


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
        "body_html": "<h1>Big news</h1><p>Here we go.</p>", "published": "1", "csrf_token": token,
    }, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/news/season-opener"

    detail = client.get("/news/season-opener")
    assert detail.status_code == 200
    assert "<h1>Big news</h1>" in detail.text

    listing = client.get("/news")
    assert "Season Opener" in listing.text


def test_staff_can_set_focal_point_when_publishing_an_article(client):
    _login_staff(client)
    token = _csrf(client, "/news/new")
    r = client.post("/news/new", data={
        "title": "Focal Point Feature", "summary": "", "body_html": "<p>x</p>",
        "published": "1", "csrf_token": token,
        "cover_focal_x": "22.5", "cover_focal_y": "75",
    }, follow_redirects=False)
    assert r.status_code == 303

    edit = client.get("/news/focal-point-feature/edit")
    assert 'id="cover_focal_x" value="22.5"' in edit.text
    assert 'id="cover_focal_y" value="75.0"' in edit.text


def test_focal_point_out_of_range_is_clamped_on_save(client):
    _login_staff(client)
    token = _csrf(client, "/news/new")
    client.post("/news/new", data={
        "title": "Clamp Test", "summary": "", "body_html": "<p>x</p>",
        "published": "1", "csrf_token": token,
        "cover_focal_x": "500", "cover_focal_y": "-40",
    }, follow_redirects=False)

    edit = client.get("/news/clamp-test/edit")
    assert 'id="cover_focal_x" value="100.0"' in edit.text
    assert 'id="cover_focal_y" value="0.0"' in edit.text


def test_article_cover_image_renders_with_its_focal_position(client):
    slug = _seed_article(cover_image="data:image/png;base64,x", cover_focal_x=30, cover_focal_y=70)
    detail = client.get(f"/news/{slug}")
    assert 'style="object-position: 30.0% 70.0%;"' in detail.text


def test_article_without_cover_image_has_no_focal_picker_on_edit(client):
    slug = _seed_article(cover_image=None)
    _login_staff(client)
    edit = client.get(f"/news/{slug}/edit")
    assert 'id="focal-picker" hidden' in edit.text


def test_article_with_cover_image_shows_focal_picker_on_edit(client):
    slug = _seed_article(cover_image="data:image/png;base64,x", cover_focal_x=15, cover_focal_y=85)
    _login_staff(client)
    edit = client.get(f"/news/{slug}/edit")
    assert 'id="focal-picker" hidden' not in edit.text
    assert "left: 15.0%" in edit.text
    assert "top: 85.0%" in edit.text


def test_cover_image_route_serves_decoded_bytes(client):
    import base64
    raw = b"fake-png-bytes"
    encoded = base64.b64encode(raw).decode("ascii")
    slug = _seed_article(cover_image=f"data:image/png;base64,{encoded}")

    r = client.get(f"/news/{slug}/cover-image")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == raw


def test_cover_image_route_404_when_no_cover_image(client):
    slug = _seed_article(cover_image=None)
    assert client.get(f"/news/{slug}/cover-image").status_code == 404


def test_cover_image_route_404_for_missing_article(client):
    assert client.get("/news/does-not-exist/cover-image").status_code == 404


def _enable_announcements(monkeypatch, base_url="https://example.com"):
    monkeypatch.setattr(config, "NEWS_ANNOUNCE_ENABLED", True)
    monkeypatch.setattr(config, "NEWS_ANNOUNCE_CHANNEL_ID", "555")
    monkeypatch.setattr(config, "SITE_BASE_URL", base_url)


def test_publishing_a_new_article_announces_to_discord(client, monkeypatch):
    _enable_announcements(monkeypatch)
    captured = {}
    monkeypatch.setattr(appmod.discord_announce, "announce", lambda channel_id, embed: captured.update(channel_id=channel_id, embed=embed))

    _login_staff(client)
    token = _csrf(client, "/news/new")
    client.post("/news/new", data={
        "title": "Season Opener Win", "summary": "Great start.", "body_html": "<p>x</p>",
        "published": "1", "csrf_token": token,
    }, follow_redirects=False)

    assert captured["channel_id"] == "555"
    assert captured["embed"]["title"] == "Season Opener Win"
    assert captured["embed"]["url"] == "https://example.com/news/season-opener-win"


def test_publishing_saves_the_announcement_message_id(client, monkeypatch):
    _enable_announcements(monkeypatch)
    monkeypatch.setattr(appmod.discord_announce, "announce", lambda channel_id, embed: "999888777")

    _login_staff(client)
    token = _csrf(client, "/news/new")
    client.post("/news/new", data={
        "title": "Saved Message Id", "summary": "", "body_html": "<p>x</p>",
        "published": "1", "csrf_token": token,
    }, follow_redirects=False)

    with database.get_session() as session:
        article = services.get_article(session, "saved-message-id")
        assert article.discord_message_id == "999888777"


def test_failed_announcement_does_not_save_a_message_id(client, monkeypatch):
    _enable_announcements(monkeypatch)

    def fail(*a, **k):
        raise appmod.discord_announce.DiscordApiError("boom")

    monkeypatch.setattr(appmod.discord_announce, "announce", fail)

    _login_staff(client)
    token = _csrf(client, "/news/new")
    client.post("/news/new", data={
        "title": "Failed Announce", "summary": "", "body_html": "<p>x</p>",
        "published": "1", "csrf_token": token,
    }, follow_redirects=False)

    with database.get_session() as session:
        article = services.get_article(session, "failed-announce")
        assert article.discord_message_id is None


def test_article_page_folds_discord_reactions_into_the_like_count(client):
    """One figure, not two: the Discord announcement's reactions are added
    to the site's own likes rather than shown as a separate badge."""
    slug = _seed_article(title="Popular Post")
    with database.get_session() as session:
        article = services.get_article(session, slug)
        article.discord_message_id = "999"
        article.discord_reaction_count = 12
        session.commit()

    detail = client.get(f"/news/{slug}")
    assert "12 Likes" in detail.text
    assert "on Discord" not in detail.text

    # A site like adds to the same total rather than starting a second one.
    _login_fan(client)
    token = _csrf(client, f"/news/{slug}")
    client.post(f"/news/{slug}/like", data={"csrf_token": token})
    detail = client.get(f"/news/{slug}")
    assert "13 Likes" in detail.text


def test_article_like_count_is_site_only_when_no_discord_reactions(client):
    slug = _seed_article(title="Quiet Post")
    detail = client.get(f"/news/{slug}")
    assert "0 Likes" in detail.text
    assert "on Discord" not in detail.text

    with database.get_session() as session:
        article = services.get_article(session, slug)
        article.discord_message_id = "999"
        article.discord_reaction_count = 0
        session.commit()

    detail = client.get(f"/news/{slug}")
    assert "0 Likes" in detail.text
    assert "on Discord" not in detail.text


def test_saving_a_draft_does_not_announce(client, monkeypatch):
    _enable_announcements(monkeypatch)
    called = []
    monkeypatch.setattr(appmod.discord_announce, "announce", lambda *a, **k: called.append(1))

    _login_staff(client)
    token = _csrf(client, "/news/new")
    client.post("/news/new", data={
        "title": "Draft Only", "summary": "", "body_html": "<p>x</p>",
        "published": "", "csrf_token": token,
    }, follow_redirects=False)

    assert called == []


def test_editing_an_already_published_article_does_not_reannounce(client, monkeypatch):
    _enable_announcements(monkeypatch)
    called = []
    monkeypatch.setattr(appmod.discord_announce, "announce", lambda *a, **k: called.append(1))
    slug = _seed_article(title="Already Live", published=True)

    _login_staff(client)
    token = _csrf(client, f"/news/{slug}/edit")
    client.post(f"/news/{slug}/edit", data={
        "title": "Already Live", "summary": "typo fix", "body_html": "<p>x</p>",
        "published": "1", "csrf_token": token,
    }, follow_redirects=False)

    assert called == []


def test_publishing_a_draft_via_edit_announces(client, monkeypatch):
    _enable_announcements(monkeypatch)
    called = []
    monkeypatch.setattr(appmod.discord_announce, "announce", lambda *a, **k: called.append(1))
    slug = _seed_article(title="Coming Soon", published=False)

    _login_staff(client)
    token = _csrf(client, f"/news/{slug}/edit")
    client.post(f"/news/{slug}/edit", data={
        "title": "Coming Soon", "summary": "", "body_html": "<p>x</p>",
        "published": "1", "csrf_token": token,
    }, follow_redirects=False)

    assert called == [1]


def test_announce_skipped_when_not_configured(client, monkeypatch):
    monkeypatch.setattr(config, "NEWS_ANNOUNCE_ENABLED", False)
    called = []
    monkeypatch.setattr(appmod.discord_announce, "announce", lambda *a, **k: called.append(1))

    _login_staff(client)
    token = _csrf(client, "/news/new")
    client.post("/news/new", data={
        "title": "No Announce Config", "summary": "", "body_html": "<p>x</p>",
        "published": "1", "csrf_token": token,
    }, follow_redirects=False)

    assert called == []


def test_announce_skipped_and_flashed_when_site_base_url_missing(client, monkeypatch):
    monkeypatch.setattr(config, "NEWS_ANNOUNCE_ENABLED", True)
    monkeypatch.setattr(config, "NEWS_ANNOUNCE_CHANNEL_ID", "555")
    monkeypatch.setattr(config, "SITE_BASE_URL", "")
    called = []
    monkeypatch.setattr(appmod.discord_announce, "announce", lambda *a, **k: called.append(1))

    _login_staff(client)
    token = _csrf(client, "/news/new")
    r = client.post("/news/new", data={
        "title": "No Base URL", "summary": "", "body_html": "<p>x</p>",
        "published": "1", "csrf_token": token,
    }, follow_redirects=True)

    assert called == []
    assert "SITE_BASE_URL" in r.text


def test_publish_still_succeeds_when_discord_announce_fails(client, monkeypatch):
    _enable_announcements(monkeypatch)

    def fail(*a, **k):
        raise appmod.discord_announce.DiscordApiError("discord is down")

    monkeypatch.setattr(appmod.discord_announce, "announce", fail)

    _login_staff(client)
    token = _csrf(client, "/news/new")
    r = client.post("/news/new", data={
        "title": "Resilient Publish", "summary": "", "body_html": "<p>x</p>",
        "published": "1", "csrf_token": token,
    }, follow_redirects=True)

    assert r.status_code == 200
    assert "Resilient Publish" in r.text
    assert "announcement failed" in r.text


def test_draft_article_hidden_from_fans_visible_to_staff(client):
    _login_staff(client)
    token = _csrf(client, "/news/new")
    client.post("/news/new", data={
        "title": "Unfinished Draft", "summary": "", "body_html": "<p>wip</p>",
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
            "title": title, "summary": "", "body_html": "<p>x</p>",
            "published": "1", "csrf_token": token,
        }, follow_redirects=False)

    home = client.get("/")
    # The most recently published article leads as the featured story...
    assert 'href="/news/second-post"' in home.text
    assert home.text.index("second-post") < home.text.index("first-post")
    # ...linked twice within the hero itself (headline + CTA button), but
    # not a third time from the "Latest news" rail below it.
    assert home.text.count('href="/news/second-post"') == 2


def test_home_shows_engagement_badge_with_like_and_comment_counts(client):
    # Must not be the single most-recent article -- that one is the hero
    # "featured" story, which doesn't render through the news-rail badge.
    slug = _seed_article(title="Big Win", cover_image="/static/img/cover.jpg")
    _seed_article(title="Newer Post", cover_image="/static/img/cover2.jpg")
    with database.get_session() as session:
        article = services.get_article(session, slug)
        services.toggle_like(session, article, 1)
        services.toggle_like(session, article, 2)
        services.add_comment(session, article, author={"id": 3, "name": "Fan", "avatar": None}, body="Nice!")

    home = client.get("/").text
    assert "engagement-badge" in home
    badge = home[home.index("engagement-badge"):home.index("engagement-badge") + 600]
    assert "2" in badge
    assert "1" in badge


def test_home_hides_engagement_badge_when_no_engagement(client):
    _seed_article(title="Quiet Post", cover_image="/static/img/cover.jpg")
    _seed_article(title="Newer Post", cover_image="/static/img/cover2.jpg")
    home = client.get("/").text
    assert "engagement-badge" not in home


def test_home_hides_engagement_badge_without_cover_image(client):
    slug = _seed_article(title="No Cover", cover_image=None)
    _seed_article(title="Newer Post", cover_image="/static/img/cover2.jpg")
    with database.get_session() as session:
        article = services.get_article(session, slug)
        services.toggle_like(session, article, 1)

    home = client.get("/").text
    assert "engagement-badge" not in home


def test_home_uses_real_crest_color_when_ea_data_available(client, monkeypatch):
    _seed_event()

    monkeypatch.setattr(appmod.config, "CLUB_ID", "8481799")
    # overall_stats deliberately fails here: the crest is fetched
    # independently, so a stats outage must not blank club identity.
    def _boom(platform, club_id, **kw):
        raise appmod.ea_client.EAApiError("stats down", 503)
    monkeypatch.setattr(appmod.ea_client, "overall_stats", _boom)
    monkeypatch.setattr(appmod.ea_client, "crest_colors", lambda platform, club_id, **kw: {
        "crest": "#C91B1B", "kit1": "#F2F2F2", "kit2": "#DB1812",
    })
    home = client.get("/")
    assert 'style="background:#C91B1B;"' in home.text
    assert "crest-branded" in home.text


def test_home_standing_band_shows_countup_rating_and_live_record(client, monkeypatch):
    monkeypatch.setattr(appmod.config, "CLUB_ID", "8481799")
    monkeypatch.setattr(appmod.ea_client, "overall_stats", lambda platform, club_id, **kw: {
        "skillRating": "1450", "wins": "111", "ties": "16", "losses": "58",
        "bestDivision": "9",
    })
    monkeypatch.setattr(appmod.ea_client, "crest_colors", lambda platform, club_id, **kw: {
        "crest": "#C91B1B", "kit1": "#F2F2F2", "kit2": "#DB1812",
        "accent": "#6CACDE", "accent_trim": "#F2F2F2",
    })
    home = client.get("/")
    assert 'class="standing-band"' in home.text
    # Every figure in the band comes from live overallStats.
    assert 'data-countup="1450"' in home.text
    assert "111W 16D 58L" in home.text
    assert 'data-countup="60"' in home.text  # 111 of 185 won
    # EA's bestDivision is a stale legacy field; no division is shown at all.
    assert "Division" not in home.text
    assert ">9<" not in home.text
    # The band's glow is tinted with the third-kit accent, not the crest
    # red. The trim half of that duo went with the best-division marker it
    # used to outline -- there's no division on the page any more.
    assert '#6CACDE' in home.text
    assert '#C91B1B' not in home.text


def test_home_standing_band_handles_missing_rating_gracefully(client, monkeypatch):
    monkeypatch.setattr(appmod.config, "CLUB_ID", "8481799")
    monkeypatch.setattr(appmod.ea_client, "overall_stats", lambda platform, club_id, **kw: {})
    monkeypatch.setattr(appmod.ea_client, "crest_colors", lambda platform, club_id, **kw: None)
    home = client.get("/")
    assert "data-countup" not in home.text
    assert 'class="standing-band"' in home.text


def test_site_never_shows_a_division_anywhere(client, monkeypatch):
    """EA's allTimeLeaderboard record carries a currentDivision that ran a
    hundred matches behind (10 while the club was really in 2), EA exposes
    no live one, and it can't be derived from skill rating. So the site
    reports no division at all rather than a wrong or hand-maintained one
    -- see ea_client.division_stats and app._standing_teaser."""
    monkeypatch.setattr(appmod.config, "CLUB_ID", "8481799")
    monkeypatch.setattr(appmod.ea_client, "division_stats", lambda platform, club_id, **kw: {
        "currentDivision": "10", "bestDivision": "4", "points": "54",
    })
    monkeypatch.setattr(appmod.ea_client, "overall_stats", lambda platform, club_id, **kw: {
        "skillRating": "2054", "wins": "111", "ties": "16", "losses": "58",
    })
    monkeypatch.setattr(appmod.ea_client, "crest_colors", lambda platform, club_id, **kw: None)
    home = client.get("/")
    assert 'data-countup="2054"' in home.text
    assert "Division" not in home.text
    assert ">10<" not in home.text

    standings = client.get("/api/standings").json()
    assert "currentDivision" not in standings
    assert "bestDivision" not in standings
    assert "points" not in standings  # same stale record


def test_home_falls_back_to_neutral_crest_without_ea_data(client, monkeypatch):
    monkeypatch.setattr(appmod.config, "CLUB_ID", "")
    home = client.get("/")
    assert "crest-branded" not in home.text


def test_home_shows_connect_with_us_button_to_the_discord_invite(client, monkeypatch):
    monkeypatch.setattr(appmod.config, "DISCORD_INVITE_URL", "https://discord.gg/J4d7D5kDX8")
    home = client.get("/")
    assert "Connect with us" in home.text
    assert 'href="https://discord.gg/J4d7D5kDX8"' in home.text


def test_home_hides_connect_band_when_invite_not_configured(client, monkeypatch):
    monkeypatch.setattr(appmod.config, "DISCORD_INVITE_URL", "")
    home = client.get("/")
    assert "Connect with us" not in home.text


def test_discord_banner_shows_for_signed_out_visitors(client, monkeypatch):
    monkeypatch.setattr(appmod.config, "DISCORD_INVITE_URL", "https://discord.gg/J4d7D5kDX8")
    home = client.get("/")
    assert "discord-banner" in home.text
    assert 'href="https://discord.gg/J4d7D5kDX8"' in home.text
    assert "Sign in with Discord" in home.text


def test_discord_banner_shows_for_signed_in_non_members(client, monkeypatch):
    monkeypatch.setattr(appmod.config, "DISCORD_INVITE_URL", "https://discord.gg/J4d7D5kDX8")
    _login_non_member(client)
    home = client.get("/")
    assert "discord-banner" in home.text
    assert "not in our Discord server" in home.text


def test_discord_banner_hidden_for_guild_members(client, monkeypatch):
    monkeypatch.setattr(appmod.config, "DISCORD_INVITE_URL", "https://discord.gg/J4d7D5kDX8")
    _login_fan(client)
    home = client.get("/")
    assert "discord-banner" not in home.text


def test_discord_banner_hidden_when_invite_not_configured(client, monkeypatch):
    monkeypatch.setattr(appmod.config, "DISCORD_INVITE_URL", "")
    home = client.get("/")
    assert "discord-banner" not in home.text


def test_news_detail_comment_prompt_links_invite_for_signed_out_and_non_members(client, monkeypatch):
    monkeypatch.setattr(appmod.config, "DISCORD_INVITE_URL", "https://discord.gg/J4d7D5kDX8")
    slug = _seed_article()

    signed_out = client.get(f"/news/{slug}")
    assert 'href="https://discord.gg/J4d7D5kDX8"' in signed_out.text

    _login_non_member(client)
    non_member = client.get(f"/news/{slug}")
    assert 'href="https://discord.gg/J4d7D5kDX8"' in non_member.text


def test_editing_article_with_no_summary_does_not_prefill_the_literal_word_none(client):
    # Regression: news_form.html used to prefill the summary input with
    # `article.summary if article else ''`, which for an article that has
    # a real summary of None (not "no article at all") rendered Jinja's
    # str(None) into the value attribute -- editing and re-saving without
    # touching that field then overwrote the actual NULL with the literal
    # text "None", which went on to display everywhere the summary shows.
    slug = _seed_article()  # default summary is unset -> None
    _login_staff(client)
    edit = client.get(f"/news/{slug}/edit")
    assert 'id="summary"' in edit.text
    assert 'value="None"' not in edit.text

    token = _csrf(client, f"/news/{slug}/edit")
    client.post(f"/news/{slug}/edit", data={
        "title": "Recap", "summary": "", "body_html": "<p>Great win.</p>",
        "published": "1", "csrf_token": token,
    }, follow_redirects=False)

    listing = client.get("/news")
    assert "None" not in listing.text


def test_article_category_defaults_and_can_be_set(client):
    _login_staff(client)
    token = _csrf(client, "/news/new")
    r = client.post("/news/new", data={
        "title": "Transfer Window Update", "category": "Transfer", "summary": "",
        "body_html": "<p>x</p>", "published": "1", "csrf_token": token,
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
            "body_html": "<p>x</p>", "published": "1", "csrf_token": token,
        }, follow_redirects=False)

    filtered = client.get("/news?category=Transfer")
    assert "Transfer Item" in filtered.text
    assert "News Item" not in filtered.text


def test_news_list_shows_engagement_badge_with_counts(client):
    slug = _seed_article(title="Popular Post", cover_image="/static/img/cover.jpg")
    with database.get_session() as session:
        article = services.get_article(session, slug)
        services.toggle_like(session, article, 1)
        services.add_comment(session, article, author={"id": 3, "name": "Fan", "avatar": None}, body="Nice!")

    listing = client.get("/news").text
    assert "engagement-badge" in listing
    badge = listing[listing.index("engagement-badge"):listing.index("engagement-badge") + 600]
    assert "1" in badge


def test_news_list_hides_engagement_badge_when_no_engagement(client):
    _seed_article(title="Quiet Post", cover_image="/static/img/cover.jpg")
    listing = client.get("/news").text
    assert "engagement-badge" not in listing


def test_comments_section_prompts_sign_in_when_signed_out(client):
    slug = _seed_article()
    detail = client.get(f"/news/{slug}")
    assert "Sign in with Discord" in detail.text
    assert 'like-btn static' in detail.text


def test_comments_section_explains_membership_requirement_when_not_in_guild(client):
    slug = _seed_article()
    _login_non_member(client)
    detail = client.get(f"/news/{slug}")
    assert "need to be a member of our Discord server to comment" in detail.text
    assert 'like-btn static' in detail.text


def test_comment_route_rejects_signed_out_visitor(client):
    slug = _seed_article()
    r = client.post(f"/news/{slug}/comments", data={"body": "hi", "csrf_token": "x"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_comment_route_rejects_non_guild_member(client):
    slug = _seed_article()
    _login_non_member(client)
    r = client.post(f"/news/{slug}/comments", data={"body": "hi", "csrf_token": "x"})
    assert r.status_code == 403


def test_like_route_rejects_non_guild_member(client):
    slug = _seed_article()
    _login_non_member(client)
    r = client.post(f"/news/{slug}/like", data={"csrf_token": "x"})
    assert r.status_code == 403


def test_signed_in_guild_member_can_comment(client):
    slug = _seed_article()
    _login_fan(client)
    token = _csrf(client, f"/news/{slug}")
    r = client.post(f"/news/{slug}/comments", data={"body": "Nice win!", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/news/{slug}#comments"

    detail = client.get(f"/news/{slug}")
    assert "Nice win!" in detail.text
    assert "1 Comment" in detail.text


def test_comment_body_is_escaped_not_rendered_as_html(client):
    slug = _seed_article()
    _login_fan(client)
    token = _csrf(client, f"/news/{slug}")
    client.post(f"/news/{slug}/comments", data={"body": "<script>alert(1)</script>", "csrf_token": token})

    detail = client.get(f"/news/{slug}")
    assert "<script>alert(1)</script>" not in detail.text
    assert "&lt;script&gt;" in detail.text


def test_empty_comment_is_rejected(client):
    slug = _seed_article()
    _login_fan(client)
    token = _csrf(client, f"/news/{slug}")
    r = client.post(f"/news/{slug}/comments", data={"body": "   ", "csrf_token": token})
    assert r.status_code == 400


def test_comment_author_can_delete_their_own_comment(client):
    slug = _seed_article()
    _login_fan(client, name="Commenter")
    token = _csrf(client, f"/news/{slug}")
    client.post(f"/news/{slug}/comments", data={"body": "delete me", "csrf_token": token})

    detail = client.get(f"/news/{slug}")
    comment_id = re.search(r"/comments/(\d+)/delete", detail.text).group(1)

    token = _csrf(client, f"/news/{slug}")
    r = client.post(f"/news/{slug}/comments/{comment_id}/delete", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    assert "delete me" not in client.get(f"/news/{slug}").text


def test_other_fan_cannot_delete_someone_elses_comment(client):
    slug = _seed_article()
    _login_fan(client, name="Commenter")
    token = _csrf(client, f"/news/{slug}")
    client.post(f"/news/{slug}/comments", data={"body": "not yours", "csrf_token": token})
    comment_id = re.search(r"/comments/(\d+)/delete", client.get(f"/news/{slug}").text).group(1)

    other = TestClient(appmod.app)
    with other:
        _login_fan(other, name="Someone Else")
        token2 = _csrf(other, f"/news/{slug}")
        other.post(f"/news/{slug}/comments/{comment_id}/delete", data={"csrf_token": token2}, follow_redirects=False)

    assert "not yours" in client.get(f"/news/{slug}").text


def test_staff_can_delete_any_comment(client):
    slug = _seed_article()
    fan = TestClient(appmod.app)
    with fan:
        _login_fan(fan, name="Commenter")
        token = _csrf(fan, f"/news/{slug}")
        fan.post(f"/news/{slug}/comments", data={"body": "moderate me", "csrf_token": token})
        comment_id = re.search(r"/comments/(\d+)/delete", fan.get(f"/news/{slug}").text).group(1)

    _login_staff(client)
    token = _csrf(client, f"/news/{slug}")
    client.post(f"/news/{slug}/comments/{comment_id}/delete", data={"csrf_token": token})
    assert "moderate me" not in client.get(f"/news/{slug}").text


def test_like_toggles_and_shows_count(client):
    slug = _seed_article()
    _login_fan(client)
    token = _csrf(client, f"/news/{slug}")
    client.post(f"/news/{slug}/like", data={"csrf_token": token})

    detail = client.get(f"/news/{slug}")
    assert "1 Like" in detail.text
    assert 'class="like-btn liked"' in detail.text

    token = _csrf(client, f"/news/{slug}")
    client.post(f"/news/{slug}/like", data={"csrf_token": token})

    detail = client.get(f"/news/{slug}")
    assert "0 Likes" in detail.text
    assert 'class="like-btn liked"' not in detail.text


def test_csrf_token_is_required_on_writes(client):
    _login_staff(client)
    r = client.post("/streamers/add", data={
        "display_name": "Bad", "twitch_login": "bad", "csrf_token": "not-the-real-token",
    })
    assert r.status_code == 400


def test_events_page_offers_editing_to_staff_only(client):
    """Events are staff-editable on the site now -- the old Discord-only
    rule is gone (see README's Events section). Everyone else still just
    reads the schedule."""
    _seed_event(title="League Match", opponent="Rivals FC")

    _login_fan(client)
    listing = client.get("/events")
    assert "Rivals FC" in listing.text
    assert "/events/new" not in listing.text

    _login_staff(client)
    listing = client.get("/events")
    assert "/events/new" in listing.text


def test_events_page_shows_the_event_cover_image_when_present(client):
    _seed_event(title="With A Cover", image="https://cdn.discordapp.com/guild-events/1/hash.png")
    _seed_event(title="No Cover", opponent="")

    listing = client.get("/events")
    assert '<img class="event-thumb" src="https://cdn.discordapp.com/guild-events/1/hash.png"' in listing.text
    assert listing.text.count('class="event-thumb"') == 1


def test_event_editing_routes_are_staff_gated(client):
    """The routes exist again, but a signed-in non-staff member must not
    reach any of the writing ones."""
    event_id = _seed_event()
    _login_fan(client)

    assert client.get("/events/new", follow_redirects=False).status_code in (302, 303, 401, 403)
    assert client.get(f"/events/{event_id}/edit", follow_redirects=False).status_code in (302, 303, 401, 403)
    for path in (f"/events/{event_id}/edit", f"/events/{event_id}/delete",
                 f"/events/{event_id}/announce", f"/events/{event_id}/signups-open"):
        r = client.post(path, data={"title": "x", "scheduled_at": "2027-01-01T18:00",
                                    "csrf_token": "x"}, follow_redirects=False)
        assert r.status_code in (302, 303, 401, 403), path


def test_staff_can_create_and_edit_an_event(client):
    _login_staff(client)
    token = _csrf(client, "/events/new")
    r = client.post("/events/new", data={
        "title": "Cup Final", "event_type": "Match", "scheduled_at": "2030-05-01T19:00",
        "opponent": "Rivals FC", "description": "Bring your boots.", "csrf_token": token,
    }, follow_redirects=False)
    assert r.status_code == 303

    with database.get_session() as session:
        event = services.list_events(session)[0]
        assert event.title == "Cup Final" and event.opponent == "Rivals FC"
        event_id = event.id

    detail = client.get(f"/events/{event_id}")
    assert "Cup Final" in detail.text and "Bring your boots." in detail.text

    token = _csrf(client, f"/events/{event_id}/edit")
    client.post(f"/events/{event_id}/edit", data={
        "title": "Cup Final", "event_type": "Match", "scheduled_at": "2030-05-01T19:00",
        "opponent": "Rivals FC", "description": "", "result": "W 3-0", "csrf_token": token,
    }, follow_redirects=False)
    with database.get_session() as session:
        assert services.get_event(session, event_id).result == "W 3-0"


def _seed_clip(*, discord_message_id="m1", title="Nice goal",
                video_url="https://cdn.discordapp.com/attachments/1/2/clip.mp4",
                jump_url="https://discord.com/channels/1/2/m1",
                author_name="Coach", posted_at=None) -> int:
    with database.get_session() as session:
        clip = Clip(
            discord_message_id=discord_message_id, title=title, video_url=video_url,
            filename="clip.mp4", author_name=author_name, jump_url=jump_url,
            posted_at=posted_at or datetime.utcnow(),
        )
        session.add(clip)
        session.commit()
        return clip.id


def test_clips_page_shows_not_configured_message_when_sync_disabled(client, monkeypatch):
    monkeypatch.setattr(config, "CLIPS_SYNC_ENABLED", False)
    r = client.get("/clips")
    assert r.status_code == 200
    assert "isn't configured" in r.text


def test_clips_page_shows_empty_state_when_enabled_but_no_clips(client, monkeypatch):
    monkeypatch.setattr(config, "CLIPS_SYNC_ENABLED", True)
    r = client.get("/clips")
    assert r.status_code == 200
    assert "No clips yet" in r.text


def test_clips_page_lists_synced_clips(client, monkeypatch):
    monkeypatch.setattr(config, "CLIPS_SYNC_ENABLED", True)
    _seed_clip(title="Nice goal", video_url="https://cdn.discordapp.com/attachments/1/2/clip.mp4",
               jump_url="https://discord.com/channels/1/2/m1")

    r = client.get("/clips")
    assert "Nice goal" in r.text
    assert 'src="https://cdn.discordapp.com/attachments/1/2/clip.mp4"' in r.text
    assert 'href="https://discord.com/channels/1/2/m1"' in r.text
    assert "View in Discord" in r.text


def test_clips_page_has_no_upload_or_editing_ui(client, monkeypatch):
    monkeypatch.setattr(config, "CLIPS_SYNC_ENABLED", True)
    _login_staff(client)
    _seed_clip()

    r = client.get("/clips")
    assert "New clip" not in r.text
    assert ">Edit<" not in r.text
    assert "/clips/new" not in r.text


def test_api_clips_requires_staff(client):
    r = client.get("/api/clips", follow_redirects=False)
    assert r.status_code in (303, 401, 403)


def test_api_clips_lists_synced_clips_without_video_url(client):
    _login_staff(client)
    _seed_clip(title="Great save", author_name="Coach")

    r = client.get("/api/clips")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["title"] == "Great save"
    assert body[0]["authorName"] == "Coach"
    assert "video_url" not in body[0] and "videoUrl" not in body[0]


def test_article_resolves_clip_embed_to_live_video(client):
    clip_id = _seed_clip(title="Golazo", video_url="https://cdn.discordapp.com/attachments/1/2/golazo.mp4",
                          jump_url="https://discord.com/channels/1/2/m1")
    slug = _seed_article(body_html=f'<p>Check this out:</p><clip-embed data-clip-id="{clip_id}"></clip-embed>')

    detail = client.get(f"/news/{slug}")
    assert detail.status_code == 200
    assert 'src="https://cdn.discordapp.com/attachments/1/2/golazo.mp4"' in detail.text
    assert 'href="https://discord.com/channels/1/2/m1"' in detail.text
    assert "<clip-embed" not in detail.text


def test_article_clip_embed_falls_back_when_clip_gone(client):
    slug = _seed_article(body_html='<p>Old clip:</p><clip-embed data-clip-id="99999"></clip-embed>')

    detail = client.get(f"/news/{slug}")
    assert detail.status_code == 200
    assert "no longer available" in detail.text
    assert "<clip-embed" not in detail.text


def test_viewing_article_does_not_persist_resolved_clip_html(client):
    """render_clip_embeds must not mutate the ORM object in place -- doing
    so would get flushed back to the DB, permanently baking in whatever
    video_url happened to be live at that moment (see services.py)."""
    clip_id = _seed_clip(video_url="https://cdn.discordapp.com/attachments/1/2/clip.mp4")
    slug = _seed_article(body_html=f'<p>Clip:</p><clip-embed data-clip-id="{clip_id}"></clip-embed>')

    client.get(f"/news/{slug}")

    with database.get_session() as session:
        article = services.get_article(session, slug)
        assert "<clip-embed" in article.body_html
        assert "cdn.discordapp.com" not in article.body_html


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


# --------------------------------------------------------------------------- #
# Squad Moves (/roster)
# --------------------------------------------------------------------------- #
@pytest.fixture
def roster_ready(monkeypatch):
    """A configured, reachable Discord with two members in it.

    ROSTER_MOVES_ENABLED is computed at import, so the gate on the routes
    is patched alongside the channel it reads -- monkeypatching the env
    var alone would leave the flag stale and every test here blocked.
    """
    monkeypatch.setattr(config, "ROSTER_MOVES_ENABLED", True)
    monkeypatch.setattr(config, "ROSTER_ANNOUNCE_CHANNEL_ID", "555")
    monkeypatch.setattr(appmod.discord_roster, "fetch_guild_members", lambda: [
        {"nick": "Cap", "avatar": None, "roles": [],
         "user": {"id": "42", "username": "alex", "global_name": None,
                  "avatar": "abc", "discriminator": "0", "bot": False}},
        {"nick": None, "avatar": None, "roles": [],
         "user": {"id": "43", "username": "sam", "global_name": None,
                  "avatar": None, "discriminator": "0", "bot": False}},
    ])
    appmod.discord_roster.invalidate_members_cache()
    posted = []

    def fake_post(path, json):
        posted.append((path, json))

        class _R:
            @staticmethod
            def json():
                return {"id": "msg-1"}
        return _R()

    monkeypatch.setattr(appmod.discord_roster.discord_api, "post", fake_post)
    yield posted
    appmod.discord_roster.invalidate_members_cache()


def test_roster_page_is_staff_only(client, roster_ready):
    # Signed out -> sent to sign in; signed in but not staff -> refused.
    assert client.get("/roster", follow_redirects=False).status_code == 303
    _login_fan(client)
    assert client.get("/roster", follow_redirects=False).status_code == 403
    _login_staff(client)
    assert client.get("/roster").status_code == 200


def test_roster_page_lists_members_with_their_discord_avatars(client, roster_ready):
    _login_staff(client)
    html = client.get("/roster").text
    assert "Cap" in html and "sam" in html
    assert "cdn.discordapp.com/avatars/42/abc.png" in html
    # Nobody without an avatar set should render a broken image.
    assert "cdn.discordapp.com/embed/avatars/" in html


def test_roster_link_is_only_in_the_nav_for_staff(client, roster_ready):
    _login_fan(client)
    assert 'href="/roster"' not in client.get("/news").text
    _login_staff(client)
    assert 'href="/roster"' in client.get("/news").text


def test_offering_a_position_posts_the_announcement_and_records_it(client, roster_ready):
    _login_staff(client, name="Coach")
    token = _csrf(client, "/roster")
    r = client.post("/roster/announce", data={
        "discord_id": "42", "kind": "offer", "position": "Striker",
        "note": "Joining from Rivals FC.", "csrf_token": token,
    }, follow_redirects=False)
    assert r.status_code == 303

    assert len(roster_ready) == 1
    path, body = roster_ready[0]
    assert path == "/channels/555/messages"
    embed = body["embeds"][0]
    assert "Cap" in embed["title"] and "Offer" in embed["title"]
    assert "Striker" in embed["description"]
    assert body["allowed_mentions"] == {"users": ["42"]}

    with database.get_session() as session:
        moves = services.recent_roster_moves(session)
    assert len(moves) == 1
    assert (moves[0].kind, moves[0].display_name) == ("offer", "Cap")
    assert moves[0].discord_message_id == "msg-1"
    assert moves[0].announced_by_name == "Coach"


def test_letting_someone_go_posts_the_other_announcement(client, roster_ready):
    _login_staff(client)
    token = _csrf(client, "/roster")
    client.post("/roster/announce", data={
        "discord_id": "43", "kind": "release", "position": "", "note": "",
        "csrf_token": token,
    }, follow_redirects=False)
    embed = roster_ready[0][1]["embeds"][0]
    assert "Departure" in embed["title"]
    assert "sam" in embed["title"]


def test_announcing_never_touches_a_discord_role(client, roster_ready):
    """The one thing this feature must not do: a squad announcement is a
    message, not a permission change."""
    _login_staff(client)
    token = _csrf(client, "/roster")
    client.post("/roster/announce", data={
        "discord_id": "42", "kind": "release", "csrf_token": token,
    }, follow_redirects=False)
    assert [path for path, _ in roster_ready] == ["/channels/555/messages"]


def test_the_history_shows_what_was_announced(client, roster_ready):
    _login_staff(client)
    token = _csrf(client, "/roster")
    client.post("/roster/announce", data={
        "discord_id": "42", "kind": "offer", "position": "Striker", "csrf_token": token,
    }, follow_redirects=False)
    html = client.get("/roster").text
    assert "Recently announced" in html
    assert "roster-move-offer" in html
    assert "Striker" in html


def test_an_id_that_is_not_in_the_server_is_refused(client, roster_ready):
    """The form posts an id back; a stale tab or a hand-edited one must
    not be able to announce a position for a stranger."""
    _login_staff(client)
    token = _csrf(client, "/roster")
    r = client.post("/roster/announce", data={
        "discord_id": "99999", "kind": "offer", "csrf_token": token,
    }, follow_redirects=False)
    assert r.status_code == 400
    assert roster_ready == []
    with database.get_session() as session:
        assert services.recent_roster_moves(session) == []


def test_an_unknown_kind_is_refused(client, roster_ready):
    _login_staff(client)
    token = _csrf(client, "/roster")
    r = client.post("/roster/announce", data={
        "discord_id": "42", "kind": "promote", "csrf_token": token,
    }, follow_redirects=False)
    assert r.status_code == 400
    assert roster_ready == []


def test_announcing_requires_a_valid_csrf_token(client, roster_ready):
    _login_staff(client)
    r = client.post("/roster/announce", data={
        "discord_id": "42", "kind": "offer", "csrf_token": "forged",
    }, follow_redirects=False)
    assert r.status_code == 400
    assert roster_ready == []


def test_a_failed_post_is_recorded_and_reported_not_silently_dropped(client, roster_ready, monkeypatch):
    """Otherwise staff see a success redirect, assume the club announced
    something, and never find out it didn't."""
    def boom(path, json):
        raise appmod.discord_roster.DiscordApiError("Missing Access, code 50001")

    monkeypatch.setattr(appmod.discord_roster.discord_api, "post", boom)
    _login_staff(client)
    token = _csrf(client, "/roster")
    r = client.post("/roster/announce", data={
        "discord_id": "42", "kind": "offer", "csrf_token": token,
    }, follow_redirects=True)
    assert "Missing Access" in r.text
    with database.get_session() as session:
        moves = services.recent_roster_moves(session)
    assert len(moves) == 1 and moves[0].discord_message_id is None
    assert "not delivered to Discord" in r.text


def test_the_page_explains_a_missing_server_members_intent(client, roster_ready, monkeypatch):
    """A 403 here is a checkbox in Discord's Developer Portal, not a bug
    in this app -- the page has to say so or nobody will find it."""
    def forbidden():
        raise appmod.discord_roster.DiscordApiError("403 Forbidden (Missing Access, code 50001)")

    monkeypatch.setattr(appmod.discord_roster, "fetch_guild_members", forbidden)
    appmod.discord_roster.invalidate_members_cache()
    _login_staff(client)
    html = client.get("/roster").text
    assert "Server Members Intent" in html
    assert "403 Forbidden" in html


def test_the_page_names_the_settings_it_is_missing_when_unconfigured(client, monkeypatch):
    monkeypatch.setattr(config, "ROSTER_MOVES_ENABLED", False)
    monkeypatch.setattr(config, "roster_moves_missing", lambda: ["DISCORD_BOT_TOKEN"])
    _login_staff(client)
    html = client.get("/roster").text
    assert "DISCORD_BOT_TOKEN" in html


def test_the_page_says_out_loud_that_let_go_never_removes_a_role(client, roster_ready):
    """Staff have to know revoking access is still theirs to do, or
    somebody will be 'let go' and keep their access for a week."""
    _login_staff(client)
    assert "never removes" in client.get("/roster").text


def test_the_page_says_whether_accepting_will_set_the_role(client, roster_ready, monkeypatch):
    """The one automatic role write in the app -- staff should know from
    the page whether it's actually switched on, not from the .env."""
    monkeypatch.setattr(config, "ROSTER_ROLE_GRANT_ENABLED", True)
    _login_staff(client)
    assert "adds them to the squad role automatically" in client.get("/roster").text

    monkeypatch.setattr(config, "ROSTER_ROLE_GRANT_ENABLED", False)
    assert "ROSTER_SQUAD_ROLE_ID" in client.get("/roster").text


# --------------------------------------------------------------------------- #
# Offers: the player answers, then staff confirm the signing
# --------------------------------------------------------------------------- #
def _offer(client, roster_ready, *, discord_id="42", position="Striker"):
    """Publishes an offer through the real route and returns its row id."""
    token = _csrf(client, "/roster")
    r = client.post("/roster/announce", data={
        "discord_id": discord_id, "kind": "offer", "position": position,
        "csrf_token": token,
    }, follow_redirects=False)
    assert r.status_code == 303
    with database.get_session() as session:
        return services.recent_roster_moves(session)[0].id


def _press(client, key, *, custom_id, user_id, name="Cap"):
    """One button press, signed the way Discord signs it."""
    payload = {
        "type": discord_rsvp.INTERACTION_MESSAGE_COMPONENT,
        "data": {"custom_id": custom_id},
        "member": {"user": {"id": str(user_id), "username": name, "avatar": None}},
    }
    body = json.dumps(payload).encode()
    timestamp = "1700000000"
    signature = key.sign(timestamp.encode() + body).signature.hex()
    return client.post("/discord/interactions", content=body, headers={
        "X-Signature-Ed25519": signature,
        "X-Signature-Timestamp": timestamp,
        "Content-Type": "application/json",
    })


@pytest.fixture
def discord_key(monkeypatch):
    key = SigningKey.generate()
    monkeypatch.setattr(config, "DISCORD_PUBLIC_KEY", bytes(key.verify_key).hex())
    return key


@pytest.fixture
def role_grant(monkeypatch):
    """Role granting switched on, with the PUTs captured rather than sent."""
    monkeypatch.setattr(config, "ROSTER_ROLE_GRANT_ENABLED", True)
    monkeypatch.setattr(config, "ROSTER_SQUAD_ROLE_ID", "777")
    monkeypatch.setattr(config, "DISCORD_GUILD_ID", 999)
    puts = []
    monkeypatch.setattr(appmod.discord_roster.discord_api, "put", lambda p: puts.append(p))
    return puts


def test_an_offer_is_posted_with_accept_and_decline_buttons(client, roster_ready):
    _login_staff(client)
    move_id = _offer(client, roster_ready)
    body = roster_ready[0][1]
    ids = [c["custom_id"] for c in body["components"][0]["components"]]
    assert ids == [f"roster:accepted:{move_id}", f"roster:declined:{move_id}"]


def test_a_departure_gets_no_buttons(client, roster_ready):
    """Nobody declines being let go."""
    _login_staff(client)
    token = _csrf(client, "/roster")
    client.post("/roster/announce", data={
        "discord_id": "43", "kind": "release", "csrf_token": token,
    }, follow_redirects=False)
    assert "components" not in roster_ready[0][1]


def test_accepting_grants_the_squad_role_and_edits_the_offer_in_place(
        client, roster_ready, discord_key, role_grant):
    _login_staff(client)
    move_id = _offer(client, roster_ready)

    r = _press(client, discord_key, custom_id=f"roster:accepted:{move_id}", user_id=42)
    assert r.status_code == 200
    payload = r.json()
    assert payload["type"] == discord_rsvp.RESPONSE_UPDATE_MESSAGE
    assert "Offer Accepted" in payload["data"]["embeds"][0]["title"]
    # Buttons cleared: a settled offer with live buttons invites presses
    # that can't be honoured.
    assert payload["data"]["components"] == []

    assert role_grant == ["/guilds/999/members/42/roles/777"]
    with database.get_session() as session:
        move = services.get_roster_move(session, move_id)
        assert move.response == "accepted"
        assert move.role_granted is True
        assert move.role_error is None
        assert move.responded_at is not None


def test_declining_records_the_answer_and_touches_no_role(
        client, roster_ready, discord_key, role_grant):
    _login_staff(client)
    move_id = _offer(client, roster_ready)

    r = _press(client, discord_key, custom_id=f"roster:declined:{move_id}", user_id=42)
    assert "Offer Declined" in r.json()["data"]["embeds"][0]["title"]
    assert role_grant == [], "declining must never write a role"
    with database.get_session() as session:
        move = services.get_roster_move(session, move_id)
        assert move.response == "declined"
        assert move.role_granted is False


def test_only_the_player_the_offer_names_can_answer_it(
        client, roster_ready, discord_key, role_grant):
    """Otherwise anyone who can see the channel could accept on somebody
    else's behalf -- and, with role granting on, give themselves a role."""
    _login_staff(client)
    move_id = _offer(client, roster_ready)

    r = _press(client, discord_key, custom_id=f"roster:accepted:{move_id}", user_id=43)
    assert r.status_code == 200
    # Ephemeral refusal (type 4, flag 64), not an edit of the post.
    assert r.json()["type"] == 4
    assert r.json()["data"]["flags"] == 64
    assert "isn't yours" in r.json()["data"]["content"]

    assert role_grant == []
    with database.get_session() as session:
        assert services.get_roster_move(session, move_id).response is None


def test_an_offer_can_only_be_answered_once(client, roster_ready, discord_key, role_grant):
    _login_staff(client)
    move_id = _offer(client, roster_ready)
    _press(client, discord_key, custom_id=f"roster:accepted:{move_id}", user_id=42)

    r = _press(client, discord_key, custom_id=f"roster:declined:{move_id}", user_id=42)
    assert r.json()["type"] == 4
    assert "already been accepted" in r.json()["data"]["content"]
    with database.get_session() as session:
        assert services.get_roster_move(session, move_id).response == "accepted"
    assert len(role_grant) == 1, "a second press must not re-grant"


def test_an_acceptance_stands_even_if_the_role_write_fails(
        client, roster_ready, discord_key, monkeypatch):
    """The press is theirs. A permissions problem on our side is not a
    reason to pretend they didn't answer -- it's a thing to go and fix."""
    monkeypatch.setattr(config, "ROSTER_ROLE_GRANT_ENABLED", True)
    monkeypatch.setattr(config, "ROSTER_SQUAD_ROLE_ID", "777")

    def forbidden(path):
        raise appmod.discord_roster.DiscordApiError("403 Forbidden (Missing Permissions, code 50013)")

    monkeypatch.setattr(appmod.discord_roster.discord_api, "put", forbidden)
    _login_staff(client)
    move_id = _offer(client, roster_ready)

    r = _press(client, discord_key, custom_id=f"roster:accepted:{move_id}", user_id=42)
    assert "Offer Accepted" in r.json()["data"]["embeds"][0]["title"]
    with database.get_session() as session:
        move = services.get_roster_move(session, move_id)
        assert move.response == "accepted"
        assert move.role_granted is False
        assert "Missing Permissions" in move.role_error

    # And staff are told, rather than seeing an acceptance that silently
    # granted nothing.
    html = client.get("/roster").text
    assert "the squad role wasn't added" in html
    assert "Missing Permissions" in html


def test_accepting_records_the_answer_when_role_granting_is_off(
        client, roster_ready, discord_key, monkeypatch):
    """The offer flow has to work without ROSTER_SQUAD_ROLE_ID set."""
    monkeypatch.setattr(config, "ROSTER_ROLE_GRANT_ENABLED", False)
    puts = []
    monkeypatch.setattr(appmod.discord_roster.discord_api, "put", lambda p: puts.append(p))
    _login_staff(client)
    move_id = _offer(client, roster_ready)

    _press(client, discord_key, custom_id=f"roster:accepted:{move_id}", user_id=42)
    assert puts == []
    with database.get_session() as session:
        move = services.get_roster_move(session, move_id)
        assert move.response == "accepted" and move.role_error is None


def test_a_press_on_an_offer_that_no_longer_exists_is_answered_not_crashed(
        client, roster_ready, discord_key):
    _login_staff(client)
    r = _press(client, discord_key, custom_id="roster:accepted:9999", user_id=42)
    assert r.json()["type"] == 4
    assert "no longer exists" in r.json()["data"]["content"]


def test_a_malformed_roster_button_is_rejected(client, roster_ready, discord_key):
    r = _press(client, discord_key, custom_id="roster:maybe:1", user_id=42)
    assert r.status_code == 400


def test_event_signups_still_route_past_the_roster_branch(client, roster_ready, discord_key):
    """Both features share the interactions URL; adding offers must not
    have swallowed event sign-ups. Checked with a real, well-formed
    sign-up press and a real event, not a string that would have failed
    to parse anyway."""
    event_id = _seed_event()
    r = _press(client, discord_key, custom_id=f"rsvp:{event_id}:going", user_id=42)
    assert r.status_code == 200
    assert r.json()["type"] == discord_rsvp.RESPONSE_UPDATE_MESSAGE
    with database.get_session() as session:
        assert services.signup_counts(session, event_id)["going"] == 1


# --- Staff confirmation ----------------------------------------------------- #
def test_an_accepted_offer_offers_a_confirm_signing_button(
        client, roster_ready, discord_key, role_grant):
    _login_staff(client)
    move_id = _offer(client, roster_ready)
    assert "Confirm signing" not in client.get("/roster").text, "not before they answer"

    _press(client, discord_key, custom_id=f"roster:accepted:{move_id}", user_id=42)
    html = client.get("/roster").text
    assert "Confirm signing" in html
    assert f"/roster/{move_id}/confirm" in html


def test_confirming_publishes_the_celebration(client, roster_ready, discord_key, role_grant):
    _login_staff(client, name="Coach")
    move_id = _offer(client, roster_ready)
    _press(client, discord_key, custom_id=f"roster:accepted:{move_id}", user_id=42)

    roster_ready.clear()
    token = _csrf(client, "/roster")
    r = client.post(f"/roster/{move_id}/confirm", data={"csrf_token": token},
                    follow_redirects=False)
    assert r.status_code == 303

    assert len(roster_ready) == 1
    embed = roster_ready[0][1]["embeds"][0]
    assert embed["title"] == "Cap has signed for YeeHaw FC"
    assert "Welcome to the squad" in embed["description"]
    assert embed["footer"]["text"].startswith("Confirmed by Coach")

    with database.get_session() as session:
        move = services.get_roster_move(session, move_id)
        assert move.confirmed_at is not None
        assert move.confirm_message_id == "msg-1"
        assert move.confirmed_by_name == "Coach"


def test_the_celebration_goes_to_the_channel_the_offer_went_to(
        client, roster_ready, discord_key, role_grant, monkeypatch):
    """The setting can change between the offer and the signing; the
    conversation shouldn't split across two channels because of it."""
    _login_staff(client)
    move_id = _offer(client, roster_ready)
    _press(client, discord_key, custom_id=f"roster:accepted:{move_id}", user_id=42)

    monkeypatch.setattr(config, "ROSTER_ANNOUNCE_CHANNEL_ID", "different-channel")
    roster_ready.clear()
    token = _csrf(client, "/roster")
    client.post(f"/roster/{move_id}/confirm", data={"csrf_token": token},
                follow_redirects=False)
    assert roster_ready[0][0] == "/channels/555/messages"


def test_an_unanswered_offer_cannot_be_confirmed(client, roster_ready):
    _login_staff(client)
    move_id = _offer(client, roster_ready)
    roster_ready.clear()
    token = _csrf(client, "/roster")
    r = client.post(f"/roster/{move_id}/confirm", data={"csrf_token": token},
                    follow_redirects=False)
    assert r.status_code == 400
    assert roster_ready == []


def test_a_declined_offer_cannot_be_confirmed(client, roster_ready, discord_key, role_grant):
    _login_staff(client)
    move_id = _offer(client, roster_ready)
    _press(client, discord_key, custom_id=f"roster:declined:{move_id}", user_id=42)
    roster_ready.clear()
    token = _csrf(client, "/roster")
    r = client.post(f"/roster/{move_id}/confirm", data={"csrf_token": token},
                    follow_redirects=False)
    assert r.status_code == 400
    assert roster_ready == []


def test_a_signing_cannot_be_announced_twice(client, roster_ready, discord_key, role_grant):
    _login_staff(client)
    move_id = _offer(client, roster_ready)
    _press(client, discord_key, custom_id=f"roster:accepted:{move_id}", user_id=42)
    token = _csrf(client, "/roster")
    client.post(f"/roster/{move_id}/confirm", data={"csrf_token": token},
                follow_redirects=False)
    roster_ready.clear()

    token = _csrf(client, "/roster")
    r = client.post(f"/roster/{move_id}/confirm", data={"csrf_token": token},
                    follow_redirects=False)
    assert r.status_code == 400
    assert roster_ready == []


def test_confirming_is_staff_only_and_csrf_protected(client, roster_ready, discord_key, role_grant):
    _login_staff(client)
    move_id = _offer(client, roster_ready)
    _press(client, discord_key, custom_id=f"roster:accepted:{move_id}", user_id=42)
    roster_ready.clear()

    r = client.post(f"/roster/{move_id}/confirm", data={"csrf_token": "forged"},
                    follow_redirects=False)
    assert r.status_code == 400

    _login_fan(client)
    r = client.post(f"/roster/{move_id}/confirm", data={"csrf_token": "x"},
                    follow_redirects=False)
    assert r.status_code in (303, 403)
    assert roster_ready == []


def test_the_page_shows_the_offer_moving_through_its_states(
        client, roster_ready, discord_key, role_grant):
    _login_staff(client)
    move_id = _offer(client, roster_ready)
    assert "Awaiting answer" in client.get("/roster").text

    _press(client, discord_key, custom_id=f"roster:accepted:{move_id}", user_id=42)
    assert "Accepted" in client.get("/roster").text

    token = _csrf(client, "/roster")
    client.post(f"/roster/{move_id}/confirm", data={"csrf_token": token},
                follow_redirects=False)
    html = client.get("/roster").text
    assert "Signed" in html
    assert "Confirm signing" not in html
