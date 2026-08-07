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
import config  # noqa: E402
import database  # noqa: E402
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
    for path in ["/", "/news", "/events", "/streamers", "/stats", "/login"]:
        assert client.get(path).status_code == 200


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


def test_events_page_shows_events_but_has_no_editing_ui_even_for_staff(client):
    _login_staff(client)
    _seed_event(title="League Match", opponent="Rivals FC")

    listing = client.get("/events")
    assert "Rivals FC" in listing.text
    assert "New event" not in listing.text
    assert ">Edit<" not in listing.text
    assert "/events/new" not in listing.text
    assert "/edit" not in listing.text


def test_events_page_shows_the_event_cover_image_when_present(client):
    _seed_event(title="With A Cover", image="https://cdn.discordapp.com/guild-events/1/hash.png")
    _seed_event(title="No Cover", opponent="")

    listing = client.get("/events")
    assert '<img class="event-thumb" src="https://cdn.discordapp.com/guild-events/1/hash.png"' in listing.text
    assert listing.text.count('class="event-thumb"') == 1


def test_event_editing_routes_no_longer_exist(client):
    _login_staff(client)
    event_id = _seed_event()

    assert client.get("/events/new").status_code == 404
    assert client.post("/events/new", data={"title": "x", "scheduled_at": "2027-01-01T18:00", "csrf_token": "x"}).status_code == 404
    assert client.get(f"/events/{event_id}/edit").status_code == 404
    assert client.post(f"/events/{event_id}/edit", data={"title": "x", "scheduled_at": "2027-01-01T18:00", "csrf_token": "x"}).status_code == 404
    assert client.post(f"/events/{event_id}/delete", data={"csrf_token": "x"}).status_code == 404


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
