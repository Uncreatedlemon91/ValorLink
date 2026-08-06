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


def test_init_db_is_idempotent_on_a_fresh_database():
    database.Base.metadata.drop_all(database.engine)
    database.init_db()
    database.init_db()  # should not raise on a schema that's already current
    with database.get_session() as session:
        assert services.list_articles(session) == []
