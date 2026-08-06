"""Tests for services.py -- article/event/streamer CRUD.

Run with: pytest proclubs/tests/test_services.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="proclubs-services-")
os.environ["SITE_DB_PATH"] = os.path.join(_TMP, "site.db")

import pytest  # noqa: E402

import database  # noqa: E402
import services  # noqa: E402
from models import Article, Event, Streamer  # noqa: E402, F401


@pytest.fixture(autouse=True)
def _fresh_db():
    database.Base.metadata.drop_all(database.engine)
    database.init_db()
    yield


AUTHOR = {"id": 1, "name": "Coach", "avatar": None}


def test_create_article_generates_slug_and_html():
    with database.get_session() as session:
        article = services.create_article(
            session, title="Big Win Tonight", summary="We won",
            body_md="**GG**", cover_image=None, published=True, author=AUTHOR,
        )
        assert article.slug == "big-win-tonight"
        assert "<strong>GG</strong>" in article.body_html
        assert article.published is True
        assert article.category == "News"  # default when not specified


def test_create_article_with_explicit_category():
    with database.get_session() as session:
        article = services.create_article(
            session, title="Big Signing", summary="", body_md="x", category="Transfer",
            cover_image=None, published=True, author=AUTHOR,
        )
        assert article.category == "Transfer"


def test_unrecognized_category_falls_back_to_default():
    with database.get_session() as session:
        article = services.create_article(
            session, title="Weird Category", summary="", body_md="x", category="Not A Real Category",
            cover_image=None, published=True, author=AUTHOR,
        )
        assert article.category == "News"


def test_list_articles_filters_by_category():
    with database.get_session() as session:
        services.create_article(session, title="Transfer News", summary="", body_md="x",
                                  category="Transfer", cover_image=None, published=True, author=AUTHOR)
        services.create_article(session, title="Match Recap", summary="", body_md="x",
                                  category="Match Highlight", cover_image=None, published=True, author=AUTHOR)
        transfers = services.list_articles(session, category="Transfer")
        assert [a.title for a in transfers] == ["Transfer News"]


def test_update_article_keeps_category_if_not_given():
    with database.get_session() as session:
        article = services.create_article(session, title="Original", summary="", body_md="x",
                                             category="Transfer", cover_image=None, published=True, author=AUTHOR)
        updated = services.update_article(session, article, title="Original", summary="",
                                            body_md="y", cover_image=None, published=True)
        assert updated.category == "Transfer"

        updated = services.update_article(session, article, title="Original", summary="",
                                            body_md="z", cover_image=None, published=True, category="News")
        assert updated.category == "News"


def test_duplicate_titles_get_unique_slugs():
    with database.get_session() as session:
        first = services.create_article(
            session, title="Match Report", summary="", body_md="x",
            cover_image=None, published=True, author=AUTHOR,
        )
        second = services.create_article(
            session, title="Match Report", summary="", body_md="y",
            cover_image=None, published=True, author=AUTHOR,
        )
        assert first.slug == "match-report"
        assert second.slug == "match-report-2"


def test_create_article_requires_title_and_body():
    with database.get_session() as session:
        with pytest.raises(services.ServiceError):
            services.create_article(session, title="  ", summary="", body_md="x",
                                      cover_image=None, published=True, author=AUTHOR)
        with pytest.raises(services.ServiceError):
            services.create_article(session, title="Title", summary="", body_md="   ",
                                      cover_image=None, published=True, author=AUTHOR)


def test_unpublished_articles_excluded_by_default():
    with database.get_session() as session:
        services.create_article(session, title="Draft One", summary="", body_md="x",
                                  cover_image=None, published=False, author=AUTHOR)
        services.create_article(session, title="Live One", summary="", body_md="x",
                                  cover_image=None, published=True, author=AUTHOR)

        public = services.list_articles(session)
        assert [a.title for a in public] == ["Live One"]

        everything = services.list_articles(session, include_drafts=True)
        assert len(everything) == 2


def test_update_article_reslugs_on_title_change():
    with database.get_session() as session:
        article = services.create_article(session, title="Old Title", summary="", body_md="x",
                                            cover_image=None, published=True, author=AUTHOR)
        updated = services.update_article(session, article, title="New Title", summary="",
                                            body_md="y", cover_image=None, published=True)
        assert updated.slug == "new-title"
        assert services.get_article(session, "old-title") is None
        assert services.get_article(session, "new-title") is not None


def test_delete_article():
    with database.get_session() as session:
        article = services.create_article(session, title="Gone Soon", summary="", body_md="x",
                                            cover_image=None, published=True, author=AUTHOR)
        slug = article.slug
        services.delete_article(session, article)
        assert services.get_article(session, slug) is None


def test_event_upcoming_only_filters_past():
    from datetime import datetime, timedelta

    with database.get_session() as session:
        services.create_event(
            session, title="Yesterday's Match", event_type="Match", opponent="",
            description="", scheduled_at=datetime.utcnow() - timedelta(days=1),
            image=None, result="W 2-0", author_name="Coach",
        )
        services.create_event(
            session, title="Tomorrow's Match", event_type="Match", opponent="",
            description="", scheduled_at=datetime.utcnow() + timedelta(days=1),
            image=None, result="", author_name="Coach",
        )
        upcoming = services.list_events(session, upcoming_only=True)
        assert [e.title for e in upcoming] == ["Tomorrow's Match"]


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
