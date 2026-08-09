"""Tests for discord_reactions_poll.py's main() -- the standalone script
run by the systemd timer to refresh cached Discord reaction counts.

Run with: pytest proclubs/tests/test_discord_reactions_poll.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="proclubs-reactions-poll-")
os.environ["SITE_DB_PATH"] = os.path.join(_TMP, "site.db")

import config  # noqa: E402
import database  # noqa: E402
import discord_announce  # noqa: E402
import discord_reactions_poll  # noqa: E402
import services  # noqa: E402

AUTHOR = {"id": 1, "name": "Coach", "avatar": None}


def _seed_announced_article(title="Big Win", message_id="999"):
    with database.get_session() as session:
        article = services.create_article(
            session, title=title, summary="", body_html="<p>x</p>",
            cover_image=None, published=True, author=AUTHOR,
        )
        article.discord_message_id = message_id
        session.commit()
        return article.slug


def test_main_does_nothing_when_announcements_disabled(monkeypatch, capsys):
    monkeypatch.setattr(config, "NEWS_ANNOUNCE_ENABLED", False)

    def fail(*a, **k):
        raise AssertionError("should not call Discord when announcements are disabled")

    monkeypatch.setattr(discord_announce, "fetch_reaction_count", fail)
    discord_reactions_poll.main()
    assert "nothing to poll" in capsys.readouterr().out


def test_main_updates_reaction_count(monkeypatch, capsys):
    database.Base.metadata.drop_all(database.engine)
    database.init_db()
    monkeypatch.setattr(config, "NEWS_ANNOUNCE_ENABLED", True)
    monkeypatch.setattr(config, "NEWS_ANNOUNCE_CHANNEL_ID", "555")
    slug = _seed_announced_article(message_id="999")

    monkeypatch.setattr(discord_announce, "fetch_reaction_count", lambda channel_id, message_id: 7)
    discord_reactions_poll.main()

    with database.get_session() as session:
        article = services.get_article(session, slug)
        assert article.discord_reaction_count == 7

    out = capsys.readouterr().out
    assert "checked 1 article" in out
    assert "1 reaction count" in out


def test_main_skips_articles_with_no_discord_message(monkeypatch):
    database.Base.metadata.drop_all(database.engine)
    database.init_db()
    monkeypatch.setattr(config, "NEWS_ANNOUNCE_ENABLED", True)
    monkeypatch.setattr(config, "NEWS_ANNOUNCE_CHANNEL_ID", "555")
    with database.get_session() as session:
        services.create_article(session, title="Never Announced", summary="", body_html="<p>x</p>",
                                 cover_image=None, published=True, author=AUTHOR)

    called = []
    monkeypatch.setattr(discord_announce, "fetch_reaction_count", lambda *a, **k: called.append(1) or 0)
    discord_reactions_poll.main()
    assert called == []


def test_main_continues_past_a_single_article_failure(monkeypatch, capsys):
    database.Base.metadata.drop_all(database.engine)
    database.init_db()
    monkeypatch.setattr(config, "NEWS_ANNOUNCE_ENABLED", True)
    monkeypatch.setattr(config, "NEWS_ANNOUNCE_CHANNEL_ID", "555")
    slug_ok = _seed_announced_article(title="Still Works", message_id="1")
    slug_fail = _seed_announced_article(title="Message Gone", message_id="2")

    def fake_fetch(channel_id, message_id):
        if message_id == "2":
            raise discord_announce.DiscordApiError("unknown message")
        return 3

    monkeypatch.setattr(discord_announce, "fetch_reaction_count", fake_fetch)
    discord_reactions_poll.main()

    with database.get_session() as session:
        assert services.get_article(session, slug_ok).discord_reaction_count == 3
        assert services.get_article(session, slug_fail).discord_reaction_count is None

    out = capsys.readouterr().out
    assert "could not fetch reactions" in out
    assert "1 failed" in out


def test_main_respects_poll_limit(monkeypatch):
    database.Base.metadata.drop_all(database.engine)
    database.init_db()
    monkeypatch.setattr(config, "NEWS_ANNOUNCE_ENABLED", True)
    monkeypatch.setattr(config, "NEWS_ANNOUNCE_CHANNEL_ID", "555")
    monkeypatch.setattr(config, "DISCORD_REACTIONS_POLL_LIMIT", 1)
    _seed_announced_article(title="One", message_id="1")
    _seed_announced_article(title="Two", message_id="2")

    calls = []
    monkeypatch.setattr(discord_announce, "fetch_reaction_count", lambda channel_id, message_id: calls.append(message_id) or 0)
    discord_reactions_poll.main()
    assert len(calls) == 1
