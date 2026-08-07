"""Tests for services.py -- article/event/streamer CRUD.

Run with: pytest proclubs/tests/test_services.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="proclubs-services-")
os.environ["SITE_DB_PATH"] = os.path.join(_TMP, "site.db")

import pytest  # noqa: E402

from sqlalchemy import select  # noqa: E402

import database  # noqa: E402
import discord_events as discord_events_mod  # noqa: E402
import services  # noqa: E402
from models import Article, Clip, Comment, Event, Like, Streamer  # noqa: E402, F401


@pytest.fixture(autouse=True)
def _fresh_db():
    database.Base.metadata.drop_all(database.engine)
    database.init_db()
    yield


AUTHOR = {"id": 1, "name": "Coach", "avatar": None}


def test_create_article_generates_slug_and_sanitizes_html():
    with database.get_session() as session:
        article = services.create_article(
            session, title="Big Win Tonight", summary="We won",
            body_html='<p><strong>GG</strong></p><script>alert(1)</script>',
            cover_image=None, published=True, author=AUTHOR,
        )
        assert article.slug == "big-win-tonight"
        assert "<strong>GG</strong>" in article.body_html
        assert "<script" not in article.body_html  # sanitized, not just stored raw
        assert article.published is True
        assert article.category == "News"  # default when not specified


def test_create_article_with_explicit_category():
    with database.get_session() as session:
        article = services.create_article(
            session, title="Big Signing", summary="", body_html="<p>x</p>", category="Transfer",
            cover_image=None, published=True, author=AUTHOR,
        )
        assert article.category == "Transfer"


def test_unrecognized_category_falls_back_to_default():
    with database.get_session() as session:
        article = services.create_article(
            session, title="Weird Category", summary="", body_html="<p>x</p>", category="Not A Real Category",
            cover_image=None, published=True, author=AUTHOR,
        )
        assert article.category == "News"


def test_list_articles_filters_by_category():
    with database.get_session() as session:
        services.create_article(session, title="Transfer News", summary="", body_html="<p>x</p>",
                                  category="Transfer", cover_image=None, published=True, author=AUTHOR)
        services.create_article(session, title="Match Recap", summary="", body_html="<p>x</p>",
                                  category="Match Highlight", cover_image=None, published=True, author=AUTHOR)
        transfers = services.list_articles(session, category="Transfer")
        assert [a.title for a in transfers] == ["Transfer News"]


def test_update_article_keeps_category_if_not_given():
    with database.get_session() as session:
        article = services.create_article(session, title="Original", summary="", body_html="<p>x</p>",
                                             category="Transfer", cover_image=None, published=True, author=AUTHOR)
        updated = services.update_article(session, article, title="Original", summary="",
                                            body_html="<p>y</p>", cover_image=None, published=True)
        assert updated.category == "Transfer"

        updated = services.update_article(session, article, title="Original", summary="",
                                            body_html="<p>z</p>", cover_image=None, published=True, category="News")
        assert updated.category == "News"


def test_create_article_defaults_focal_point_to_center():
    with database.get_session() as session:
        article = services.create_article(
            session, title="No Focal Given", summary="", body_html="<p>x</p>",
            cover_image=None, published=True, author=AUTHOR,
        )
        assert article.cover_focal_x == 50
        assert article.cover_focal_y == 50


def test_create_article_stores_given_focal_point():
    with database.get_session() as session:
        article = services.create_article(
            session, title="Focal Given", summary="", body_html="<p>x</p>",
            cover_image="data:image/png;base64,x", published=True, author=AUTHOR,
            cover_focal_x="18.5", cover_focal_y="82",
        )
        assert article.cover_focal_x == 18.5
        assert article.cover_focal_y == 82.0


def test_create_article_clamps_out_of_range_focal_point():
    with database.get_session() as session:
        article = services.create_article(
            session, title="Focal Out Of Range", summary="", body_html="<p>x</p>",
            cover_image=None, published=True, author=AUTHOR,
            cover_focal_x="150", cover_focal_y="-30",
        )
        assert article.cover_focal_x == 100
        assert article.cover_focal_y == 0


def test_create_article_falls_back_to_center_on_garbage_focal_point():
    with database.get_session() as session:
        article = services.create_article(
            session, title="Focal Garbage", summary="", body_html="<p>x</p>",
            cover_image=None, published=True, author=AUTHOR,
            cover_focal_x="not-a-number", cover_focal_y=None,
        )
        assert article.cover_focal_x == 50
        assert article.cover_focal_y == 50


def test_update_article_can_change_focal_point_without_a_new_image():
    with database.get_session() as session:
        article = services.create_article(
            session, title="Reposition Me", summary="", body_html="<p>x</p>",
            cover_image="data:image/png;base64,x", published=True, author=AUTHOR,
        )
        updated = services.update_article(
            session, article, title="Reposition Me", summary="", body_html="<p>x</p>",
            cover_image=None, published=True, cover_focal_x="10", cover_focal_y="90",
        )
        assert updated.cover_image == "data:image/png;base64,x"  # unchanged
        assert updated.cover_focal_x == 10
        assert updated.cover_focal_y == 90


def test_duplicate_titles_get_unique_slugs():
    with database.get_session() as session:
        first = services.create_article(
            session, title="Match Report", summary="", body_html="<p>x</p>",
            cover_image=None, published=True, author=AUTHOR,
        )
        second = services.create_article(
            session, title="Match Report", summary="", body_html="<p>y</p>",
            cover_image=None, published=True, author=AUTHOR,
        )
        assert first.slug == "match-report"
        assert second.slug == "match-report-2"


def test_create_article_requires_title_and_body():
    with database.get_session() as session:
        with pytest.raises(services.ServiceError):
            services.create_article(session, title="  ", summary="", body_html="<p>x</p>",
                                      cover_image=None, published=True, author=AUTHOR)
        with pytest.raises(services.ServiceError):
            # Quill's "empty" state is still markup, not an empty string.
            services.create_article(session, title="Title", summary="", body_html="<p><br></p>",
                                      cover_image=None, published=True, author=AUTHOR)


def test_create_article_allows_image_only_body_with_no_text():
    with database.get_session() as session:
        article = services.create_article(
            session, title="Photo Dump", summary="",
            body_html='<p><img src="data:image/png;base64,iVBORw0KGgo="></p>',
            cover_image=None, published=True, author=AUTHOR,
        )
        assert "<img" in article.body_html


def test_create_article_rejects_body_over_max_length(monkeypatch):
    import html_sanitize
    monkeypatch.setattr(html_sanitize, "MAX_BODY_LENGTH", 100)
    with database.get_session() as session:
        with pytest.raises(services.ServiceError, match="too long"):
            services.create_article(
                session, title="Huge", summary="", body_html="<p>" + ("x" * 200) + "</p>",
                cover_image=None, published=True, author=AUTHOR,
            )


def test_unpublished_articles_excluded_by_default():
    with database.get_session() as session:
        services.create_article(session, title="Draft One", summary="", body_html="<p>x</p>",
                                  cover_image=None, published=False, author=AUTHOR)
        services.create_article(session, title="Live One", summary="", body_html="<p>x</p>",
                                  cover_image=None, published=True, author=AUTHOR)

        public = services.list_articles(session)
        assert [a.title for a in public] == ["Live One"]

        everything = services.list_articles(session, include_drafts=True)
        assert len(everything) == 2


def test_update_article_reslugs_on_title_change():
    with database.get_session() as session:
        article = services.create_article(session, title="Old Title", summary="", body_html="<p>x</p>",
                                            cover_image=None, published=True, author=AUTHOR)
        updated = services.update_article(session, article, title="New Title", summary="",
                                            body_html="<p>y</p>", cover_image=None, published=True)
        assert updated.slug == "new-title"
        assert services.get_article(session, "old-title") is None
        assert services.get_article(session, "new-title") is not None


def test_delete_article():
    with database.get_session() as session:
        article = services.create_article(session, title="Gone Soon", summary="", body_html="<p>x</p>",
                                            cover_image=None, published=True, author=AUTHOR)
        slug = article.slug
        services.delete_article(session, article)
        assert services.get_article(session, slug) is None


MEMBER = {"id": 42, "name": "Fan", "avatar": None}


def test_add_comment_and_list_comments_ordered_oldest_first():
    with database.get_session() as session:
        article = services.create_article(session, title="Match Recap", summary="", body_html="<p>x</p>",
                                            cover_image=None, published=True, author=AUTHOR)
        services.add_comment(session, article, author=MEMBER, body="First!")
        services.add_comment(session, article, author={"id": 43, "name": "Other Fan", "avatar": None}, body="Second")

        comments = services.list_comments(session, article)
        assert [c.body for c in comments] == ["First!", "Second"]
        assert comments[0].author_name == "Fan"
        assert comments[0].author_discord_id == 42


def test_add_comment_rejects_empty_body():
    with database.get_session() as session:
        article = services.create_article(session, title="Empty Comment Test", summary="", body_html="<p>x</p>",
                                            cover_image=None, published=True, author=AUTHOR)
        with pytest.raises(services.ServiceError):
            services.add_comment(session, article, author=MEMBER, body="   ")


def test_add_comment_rejects_over_max_length():
    with database.get_session() as session:
        article = services.create_article(session, title="Long Comment Test", summary="", body_html="<p>x</p>",
                                            cover_image=None, published=True, author=AUTHOR)
        with pytest.raises(services.ServiceError):
            services.add_comment(session, article, author=MEMBER, body="x" * 2001)


def test_delete_comment():
    with database.get_session() as session:
        article = services.create_article(session, title="Delete Comment Test", summary="", body_html="<p>x</p>",
                                            cover_image=None, published=True, author=AUTHOR)
        comment = services.add_comment(session, article, author=MEMBER, body="temp")
        services.delete_comment(session, comment)
        assert services.list_comments(session, article) == []


def test_toggle_like_adds_then_removes():
    with database.get_session() as session:
        article = services.create_article(session, title="Like Test", summary="", body_html="<p>x</p>",
                                            cover_image=None, published=True, author=AUTHOR)
        assert services.has_liked(session, article, 42) is False
        assert services.count_likes(session, article) == 0

        liked = services.toggle_like(session, article, 42)
        assert liked is True
        assert services.has_liked(session, article, 42) is True
        assert services.count_likes(session, article) == 1

        liked = services.toggle_like(session, article, 42)
        assert liked is False
        assert services.has_liked(session, article, 42) is False
        assert services.count_likes(session, article) == 0


def test_toggle_like_is_per_user():
    with database.get_session() as session:
        article = services.create_article(session, title="Multi Like Test", summary="", body_html="<p>x</p>",
                                            cover_image=None, published=True, author=AUTHOR)
        services.toggle_like(session, article, 1)
        services.toggle_like(session, article, 2)
        assert services.count_likes(session, article) == 2
        assert services.has_liked(session, article, 1) is True
        assert services.has_liked(session, article, 3) is False


def test_delete_article_cascades_comments_and_likes():
    with database.get_session() as session:
        article = services.create_article(session, title="Cascade Test", summary="", body_html="<p>x</p>",
                                            cover_image=None, published=True, author=AUTHOR)
        services.add_comment(session, article, author=MEMBER, body="won't survive")
        services.toggle_like(session, article, 42)

        services.delete_article(session, article)

        assert session.execute(select(Comment)).first() is None
        assert session.execute(select(Like)).first() is None


def _make_event(session, **overrides):
    """Events have no create/update path left in services.py -- they're
    read-only from the site's own UI, populated only via
    services.sync_discord_events. Tests that need one on the board build
    the row directly instead."""
    fields = {
        "title": "Match", "event_type": "Match", "opponent": None,
        "description": None, "scheduled_at": datetime.utcnow(),
        "result": None, "created_by_name": "Coach", "discord_event_id": None,
    }
    fields.update(overrides)
    event = Event(**fields)
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def test_event_upcoming_only_filters_past():
    from datetime import datetime, timedelta

    with database.get_session() as session:
        _make_event(session, title="Yesterday's Match", scheduled_at=datetime.utcnow() - timedelta(days=1), result="W 2-0")
        _make_event(session, title="Tomorrow's Match", scheduled_at=datetime.utcnow() + timedelta(days=1))
        upcoming = services.list_events(session, upcoming_only=True)
        assert [e.title for e in upcoming] == ["Tomorrow's Match"]


def _discord_event(event_id, name="Scrim Night", status=1, start="2027-06-01T18:00:00+00:00",
                    description=None, image=None):
    return {
        "id": event_id, "name": name, "description": description,
        "scheduled_start_time": start, "status": status, "image": image,
    }


def test_sync_discord_events_creates_new_events():
    with database.get_session() as session:
        result = services.sync_discord_events(session, [_discord_event("d1", name="Scrim Night")])
        assert result == {"created": 1, "updated": 0, "removed": 0}

        events = services.list_events(session)
        assert len(events) == 1
        assert events[0].discord_event_id == "d1"
        assert events[0].title == "Scrim Night"
        assert events[0].event_type == "Match"  # sensible default, not from Discord


def test_sync_discord_events_sets_cover_image_when_discord_event_has_one():
    with database.get_session() as session:
        services.sync_discord_events(session, [_discord_event("d1", image="somehash")])
        event = services.list_events(session)[0]
        assert event.image == "https://cdn.discordapp.com/guild-events/d1/somehash.png"


def test_sync_discord_events_updates_cover_image_on_resync():
    with database.get_session() as session:
        services.sync_discord_events(session, [_discord_event("d1", image="oldhash")])
        services.sync_discord_events(session, [_discord_event("d1", image="newhash")])
        event = services.list_events(session)[0]
        assert event.image == "https://cdn.discordapp.com/guild-events/d1/newhash.png"

        # And clears it if the organizer removes the cover in Discord.
        services.sync_discord_events(session, [_discord_event("d1", image=None)])
        event = services.list_events(session)[0]
        assert event.image is None


def test_sync_discord_events_updates_existing_by_discord_id():
    with database.get_session() as session:
        services.sync_discord_events(session, [_discord_event("d1", name="Original Name")])
        event = services.list_events(session)[0]

        # Staff enriches fields Discord has no equivalent for (there's no
        # site UI for this anymore, but the sync itself must still leave
        # hand-set values alone -- simulate it by writing directly).
        event.event_type = "Tournament"
        event.opponent = "Rivals FC"
        session.commit()

        result = services.sync_discord_events(session, [_discord_event("d1", name="Renamed Event")])
        assert result == {"created": 0, "updated": 1, "removed": 0}

        events = services.list_events(session)
        assert len(events) == 1
        assert events[0].title == "Renamed Event"       # overwritten from Discord
        assert events[0].event_type == "Tournament"      # site-only field, untouched
        assert events[0].opponent == "Rivals FC"          # site-only field, untouched


def test_sync_discord_events_removes_canceled_upcoming_events():
    with database.get_session() as session:
        services.sync_discord_events(session, [_discord_event("d1")])
        assert len(services.list_events(session)) == 1

        # Discord no longer lists it at all (deleted) -- treated as canceled.
        result = services.sync_discord_events(session, [])
        assert result == {"created": 0, "updated": 0, "removed": 1}
        assert services.list_events(session) == []


def test_sync_discord_events_ignores_completed_and_canceled_statuses():
    with database.get_session() as session:
        result = services.sync_discord_events(session, [
            _discord_event("d1", status=discord_events_mod.STATUS_COMPLETED),
            _discord_event("d2", status=discord_events_mod.STATUS_CANCELED),
        ])
        assert result == {"created": 0, "updated": 0, "removed": 0}
        assert services.list_events(session) == []


def test_sync_discord_events_never_touches_manually_created_events():
    with database.get_session() as session:
        _make_event(session, title="Community Night", event_type="Community",
                    scheduled_at=datetime.utcnow() + timedelta(days=3))
        # Discord reports nothing at all -- a manually-created event (no
        # discord_event_id) must survive regardless.
        result = services.sync_discord_events(session, [])
        assert result == {"created": 0, "updated": 0, "removed": 0}
        assert [e.title for e in services.list_events(session)] == ["Community Night"]


def test_sync_discord_events_leaves_past_events_alone_even_if_discord_drops_them():
    with database.get_session() as session:
        services.sync_discord_events(session, [
            _discord_event("d1", start="2020-01-01T18:00:00+00:00"),
        ])
        assert len(services.list_events(session)) == 1

        # Discord's list no longer includes it (it's aged out on their side),
        # but it's in the past -- leave it as a historical record.
        result = services.sync_discord_events(session, [])
        assert result["removed"] == 0
        assert len(services.list_events(session)) == 1


def _discord_message(message_id, content="", video_url="https://cdn.discordapp.com/attachments/1/2/clip.mp4",
                      filename="clip.mp4", content_type="video/mp4",
                      timestamp="2027-06-01T18:00:00+00:00", author_name="Coach"):
    attachments = []
    if video_url is not None:
        attachments.append({"url": video_url, "filename": filename, "content_type": content_type})
    return {
        "id": message_id, "content": content, "timestamp": timestamp,
        "attachments": attachments, "author": {"username": author_name, "global_name": None},
    }


def test_sync_clips_creates_new_clip_from_video_attachment():
    with database.get_session() as session:
        result = services.sync_clips(session, "999", [_discord_message("m1", content="Nice goal")])
        assert result == {"created": 1, "updated": 0}

        clips = services.list_clips(session)
        assert len(clips) == 1
        assert clips[0].discord_message_id == "m1"
        assert clips[0].title == "Nice goal"
        assert clips[0].video_url == "https://cdn.discordapp.com/attachments/1/2/clip.mp4"
        assert clips[0].filename == "clip.mp4"
        assert clips[0].author_name == "Coach"
        assert "999/m1" in clips[0].jump_url


def test_sync_clips_ignores_messages_with_no_video_attachment():
    with database.get_session() as session:
        result = services.sync_clips(session, "999", [
            _discord_message("m1", video_url=None),
            {"id": "m2", "content": "just text", "timestamp": "2027-06-01T18:00:00+00:00",
             "attachments": [{"url": "https://cdn.discordapp.com/x.png", "filename": "screenshot.png",
                               "content_type": "image/png"}],
             "author": {"username": "Coach"}},
        ])
        assert result == {"created": 0, "updated": 0}
        assert services.list_clips(session) == []


def test_sync_clips_blank_content_yields_no_title():
    with database.get_session() as session:
        services.sync_clips(session, "999", [_discord_message("m1", content="   ")])
        assert services.list_clips(session)[0].title is None


def test_sync_clips_refreshes_video_url_on_resync_without_duplicating():
    with database.get_session() as session:
        services.sync_clips(session, "999", [
            _discord_message("m1", video_url="https://cdn.discordapp.com/old.mp4"),
        ])
        result = services.sync_clips(session, "999", [
            _discord_message("m1", video_url="https://cdn.discordapp.com/refreshed.mp4"),
        ])
        assert result == {"created": 0, "updated": 1}

        clips = services.list_clips(session)
        assert len(clips) == 1
        assert clips[0].video_url == "https://cdn.discordapp.com/refreshed.mp4"


def test_sync_clips_never_removes_clips_that_age_out_of_the_polled_window():
    with database.get_session() as session:
        services.sync_clips(session, "999", [_discord_message("m1")])
        # Discord's most-recent-N window no longer includes m1 -- unlike
        # events, that's not a deletion signal.
        result = services.sync_clips(session, "999", [])
        assert result == {"created": 0, "updated": 0}
        assert len(services.list_clips(session)) == 1


def test_list_clips_orders_newest_first():
    with database.get_session() as session:
        services.sync_clips(session, "999", [
            _discord_message("m1", timestamp="2027-01-01T00:00:00+00:00"),
            _discord_message("m2", timestamp="2027-06-01T00:00:00+00:00"),
        ])
        clips = services.list_clips(session)
        assert [c.discord_message_id for c in clips] == ["m2", "m1"]


def test_streamer_login_is_normalized_and_unique():
    with database.get_session() as session:
        streamer = services.create_streamer(
            session, display_name="Cap", twitch_login="@SomeStreamer",
            avatar=None, author_name="Coach",
        )
        assert streamer.twitch_login == "somestreamer"

        with pytest.raises(services.ServiceError):
            services.create_streamer(session, display_name="Cap Again",
                                       twitch_login="somestreamer", avatar=None,
                                       author_name="Coach")


def test_streamer_ordering_increments_position():
    with database.get_session() as session:
        first = services.create_streamer(session, display_name="A", twitch_login="a",
                                           avatar=None, author_name="Coach")
        second = services.create_streamer(session, display_name="B", twitch_login="b",
                                           avatar=None, author_name="Coach")
        assert second.position > first.position


def test_new_streamer_is_not_featured_by_default():
    with database.get_session() as session:
        streamer = services.create_streamer(session, display_name="A", twitch_login="a",
                                              avatar=None, author_name="Coach")
        assert streamer.featured is False
        assert services.get_featured_streamer(session) is None


def test_creating_a_featured_streamer_unfeatures_the_previous_one():
    with database.get_session() as session:
        first = services.create_streamer(session, display_name="A", twitch_login="a",
                                           avatar=None, author_name="Coach", featured=True)
        assert services.get_featured_streamer(session).id == first.id

        second = services.create_streamer(session, display_name="B", twitch_login="b",
                                            avatar=None, author_name="Coach", featured=True)
        session.refresh(first)
        assert first.featured is False
        assert services.get_featured_streamer(session).id == second.id


def test_set_featured_streamer_is_exclusive():
    with database.get_session() as session:
        first = services.create_streamer(session, display_name="A", twitch_login="a",
                                           avatar=None, author_name="Coach", featured=True)
        second = services.create_streamer(session, display_name="B", twitch_login="b",
                                            avatar=None, author_name="Coach")

        services.set_featured_streamer(session, second)
        session.refresh(first)
        assert first.featured is False
        assert second.featured is True
        assert services.get_featured_streamer(session).id == second.id
