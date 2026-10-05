"""Database setup for the site's own content (articles, events, streamers).

A separate SQLite file from db.py's EA-stats history store (data/site.db vs
data/history.db) -- different shape, different lifecycle, no reason to share
a schema. Self-managed via create_all rather than Alembic: this is a small,
single-tenant app with no need for migration tooling.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

# SITE_DB_PATH lets tests (and any deployment that wants the DB elsewhere)
# point this at a scratch location without touching code.
DB_PATH = Path(os.getenv("SITE_DB_PATH") or Path(__file__).parent / "data" / "site.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


def init_db():
    import models  # noqa: F401  -- registers models on Base

    Base.metadata.create_all(engine)
    _add_missing_columns()
    _drop_legacy_columns()
    _rename_legacy_values()
    _repair_reused_event_ids()


def _add_missing_columns():
    """Additive-only schema sync: adds any column a model declares that an
    already-existing table is missing (e.g. after a code update adds a
    field), so a redeploy doesn't need the DB file wiped. Still no real
    migration tool -- this never drops, renames, or alters a column, only
    adds new ones, which is all a create_all-managed app like this needs."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            existing_columns = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                coltype = column.type.compile(engine.dialect)
                ddl = f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {coltype}'
                if column.server_default is not None:
                    ddl += f" DEFAULT '{column.server_default.arg}'"
                conn.execute(text(ddl))


# Columns a past model used to declare, since removed, that an
# already-deployed database may still be carrying. Additive-only sync
# above only ever adds columns, so these linger after the code that used
# them is gone -- harmless for a nullable leftover, but NOT NULL ones
# (like this) break every future insert into that table, since a new row
# just never supplies a value for a column its model no longer knows
# about. SQLite has supported DROP COLUMN since 3.35 (2021), so this is
# safe to run unconditionally on every startup -- a no-op once it's gone.
_LEGACY_COLUMNS = {
    # Replaced by Article.body_html when the article editor became
    # Quill-based rich text instead of Markdown -- see html_sanitize.py.
    "articles": ["body_md"],
}


def _drop_legacy_columns():
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table_name, columns in _LEGACY_COLUMNS.items():
            if table_name not in existing_tables:
                continue
            existing_columns = {c["name"] for c in inspector.get_columns(table_name)}
            for column_name in columns:
                if column_name in existing_columns:
                    conn.execute(text(f'ALTER TABLE {table_name} DROP COLUMN "{column_name}"'))


def _rename_legacy_values():
    """Stored values a later version renamed -- squad status "Reserve"
    became "Substitute". A no-op once nothing old is left."""
    import roles

    with engine.begin() as conn:
        for old, new in roles.LEGACY_SQUAD_STATUSES.items():
            for table in ("contracts", "roster_moves"):
                conn.execute(text(f"UPDATE {table} SET squad_status = :new WHERE squad_status = :old"),
                             {"new": new, "old": old})


# Tables that hang rows off an event by id (no foreign key, see
# services.delete_event), each with the column recording when the row was
# first written. Mirrors services.EVENT_CHILD_MODELS.
_EVENT_CHILD_STAMPS = {
    "event_signups": "responded_at",
    "event_tier_invites": "invited_at",
    "event_lineups": "updated_at",
    "match_ratings": "updated_at",
    "motm_votes": "updated_at",
}
_EVENT_ID_HIGH_WATER = "events:last_id"   # services._EVENT_ID_HIGH_WATER


def _repair_reused_event_ids():
    """Undo the damage of SQLite handing a deleted event's id to the next
    one. Deleting an event used to leave most of its rows behind (and the
    old Discord Scheduled Events mirror left even its sign-ups), so a new
    event that got the same id opened with someone else's sign-up sheet.

    First records the highest id anything has ever pointed at, so
    services._next_event_id never hands it out again -- the leftover rows
    are the only record of those ids, so this must run before they go.
    Then drops rows that can't belong to the event now holding their id:
    any written before that event existed, and any whose event is gone.
    Safe on every startup -- once clean, there's nothing for it to match."""
    with engine.begin() as conn:
        tables = set(inspect(conn).get_table_names())
        if "events" not in tables:
            return
        children = {t: c for t, c in _EVENT_CHILD_STAMPS.items() if t in tables}

        ids = [conn.execute(text("SELECT MAX(id) FROM events")).scalar()]
        ids += [conn.execute(text(f"SELECT MAX(event_id) FROM {t}")).scalar() for t in children]
        if "notifications" in tables:
            ids.append(conn.execute(text(
                "SELECT MAX(CAST(CASE kind WHEN 'vote' THEN key"
                " ELSE substr(key, 1, instr(key, ':') - 1) END AS INTEGER))"
                " FROM notifications WHERE kind IN ('remind', 'vote')")).scalar())
        stored = conn.execute(text("SELECT value FROM club_settings WHERE key = :k"),
                              {"k": _EVENT_ID_HIGH_WATER}).scalar()
        if stored and stored.isdigit():
            ids.append(int(stored))
        high = max((i for i in ids if i), default=0)
        if high:
            conn.execute(text("DELETE FROM club_settings WHERE key = :k"), {"k": _EVENT_ID_HIGH_WATER})
            conn.execute(text("INSERT INTO club_settings (key, value) VALUES (:k, :v)"),
                         {"k": _EVENT_ID_HIGH_WATER, "v": str(high)})

        for table, stamp in children.items():
            conn.execute(text(
                f"DELETE FROM {table} WHERE event_id NOT IN (SELECT id FROM events) "
                f"OR {stamp} < (SELECT created_at FROM events WHERE events.id = {table}.event_id)"))
        # "Already sent" marks keyed by event id (see notify_poll.py and
        # matchweek_routes.py): an inherited one would silently skip the
        # new event's reminders or its post-match vote.
        if "notifications" not in tables:
            return
        conn.execute(text(
            "DELETE FROM notifications WHERE kind IN ('remind', 'vote') AND EXISTS ("
            " SELECT 1 FROM events WHERE notifications.sent_at < events.created_at AND ("
            "  (notifications.kind = 'vote' AND notifications.key = CAST(events.id AS TEXT))"
            "  OR (notifications.kind = 'remind' AND notifications.key LIKE events.id || ':%')))"))


@contextmanager
def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
