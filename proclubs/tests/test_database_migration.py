"""Tests for database.py's additive-column sync -- the mechanism that lets a
redeploy add a new model field (like Article.category) without wiping an
already-populated site.db.

Run with: pytest proclubs/tests/test_database_migration.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="proclubs-migration-")
os.environ["SITE_DB_PATH"] = os.path.join(_TMP, "site.db")

from sqlalchemy import inspect, text  # noqa: E402

import database  # noqa: E402
import services  # noqa: E402


def test_init_db_adds_missing_column_to_existing_table_with_data():
    # Simulate a deployment from before `category` existed: create the
    # articles table by hand, without that column, and put a real row in it.
    database.Base.metadata.drop_all(database.engine)
    with database.engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE articles (
                id INTEGER PRIMARY KEY,
                title VARCHAR NOT NULL,
                slug VARCHAR NOT NULL UNIQUE,
                summary VARCHAR,
                body_md TEXT NOT NULL,
                body_html TEXT NOT NULL,
                cover_image TEXT,
                author_discord_id BIGINT,
                author_name VARCHAR NOT NULL,
                author_avatar VARCHAR,
                published BOOLEAN NOT NULL,
                published_at DATETIME,
                updated_at DATETIME
            )
        """))
        conn.execute(text("""
            INSERT INTO articles (title, slug, body_md, body_html, author_name, published)
            VALUES ('Pre-existing Post', 'pre-existing-post', 'x', 'x', 'Coach', 1)
        """))

    inspector = inspect(database.engine)
    assert "category" not in {c["name"] for c in inspector.get_columns("articles")}

    database.init_db()

    inspector = inspect(database.engine)
    assert "category" in {c["name"] for c in inspector.get_columns("articles")}

    with database.get_session() as session:
        article = services.get_article(session, "pre-existing-post")
        assert article is not None
        assert article.category == "News"  # backfilled via the column's DEFAULT


def test_init_db_drops_legacy_body_md_column_so_new_articles_can_be_created():
    # Simulate a database from before the rich-text editor migration
    # (bad5279): articles.body_md was NOT NULL, and the additive-only sync
    # above never drops a stale column on its own, so a real production
    # deployment would carry it forward across every later redeploy.
    # Before the fix, this made every *new* article insert fail with a
    # NOT NULL constraint violation, surfacing to staff as a raw 500 --
    # since a brand-new row simply never supplies a value for a column its
    # current model doesn't know about.
    database.Base.metadata.drop_all(database.engine)
    with database.engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE articles (
                id INTEGER PRIMARY KEY,
                title VARCHAR NOT NULL,
                slug VARCHAR NOT NULL UNIQUE,
                category VARCHAR NOT NULL DEFAULT 'News',
                summary VARCHAR,
                body_md TEXT NOT NULL,
                body_html TEXT NOT NULL,
                cover_image TEXT,
                author_discord_id BIGINT,
                author_name VARCHAR NOT NULL,
                author_avatar VARCHAR,
                published BOOLEAN NOT NULL,
                published_at DATETIME,
                updated_at DATETIME
            )
        """))
        conn.execute(text("""
            INSERT INTO articles (title, slug, body_md, body_html, author_name, published)
            VALUES ('Old Post', 'old-post', 'legacy markdown', '<p>legacy</p>', 'Coach', 1)
        """))

    database.init_db()

    inspector = inspect(database.engine)
    assert "body_md" not in {c["name"] for c in inspector.get_columns("articles")}

    with database.get_session() as session:
        # The pre-existing row (and its now-unused legacy body) survives.
        old = services.get_article(session, "old-post")
        assert old is not None

        # And -- the actual bug -- creating a brand-new article no longer
        # trips a NOT NULL constraint on the column its model doesn't
        # declare anymore.
        new = services.create_article(
            session, title="New Post", summary="", body_html="<p>fresh</p>",
            cover_image=None, published=False, author={"id": 1, "name": "Coach", "avatar": None},
        )
        assert new.slug == "new-post"


def test_init_db_is_idempotent_on_a_fresh_database():
    database.Base.metadata.drop_all(database.engine)
    database.init_db()
    database.init_db()  # should not raise on a schema that's already current
    with database.get_session() as session:
        assert services.list_articles(session) == []
