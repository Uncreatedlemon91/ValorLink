"""Tests for discord_events_poll.py's main() -- the standalone script run
by the systemd timer.

Run with: pytest proclubs/tests/test_discord_events_poll.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="proclubs-poll-")
os.environ["SITE_DB_PATH"] = os.path.join(_TMP, "site.db")

import config  # noqa: E402
import database  # noqa: E402
import discord_events  # noqa: E402
import discord_events_poll  # noqa: E402
import services  # noqa: E402


def test_main_does_nothing_when_sync_disabled(monkeypatch, capsys):
    monkeypatch.setattr(config, "DISCORD_EVENTS_SYNC_ENABLED", False)

    def fail(*a, **k):
        raise AssertionError("should not call Discord when sync is disabled")

    monkeypatch.setattr(discord_events, "list_scheduled_events", fail)
    discord_events_poll.main()
    assert "nothing to sync" in capsys.readouterr().out


def test_main_syncs_when_enabled(monkeypatch, capsys):
    database.Base.metadata.drop_all(database.engine)
    monkeypatch.setattr(config, "DISCORD_EVENTS_SYNC_ENABLED", True)
    monkeypatch.setattr(discord_events, "list_scheduled_events", lambda: [
        {"id": "d1", "name": "Scrim Night", "description": None,
         "scheduled_start_time": "2027-06-01T18:00:00+00:00", "status": 1},
    ])

    discord_events_poll.main()

    with database.get_session() as session:
        events = services.list_events(session)
        assert len(events) == 1
        assert events[0].discord_event_id == "d1"

    assert "1 created" in capsys.readouterr().out


def test_main_handles_discord_api_failure_without_touching_db(monkeypatch, capsys):
    database.Base.metadata.drop_all(database.engine)
    monkeypatch.setattr(config, "DISCORD_EVENTS_SYNC_ENABLED", True)

    def fail():
        raise discord_events.DiscordApiError("boom")

    monkeypatch.setattr(discord_events, "list_scheduled_events", fail)
    discord_events_poll.main()

    with database.get_session() as session:
        assert services.list_events(session) == []
    assert "could not fetch" in capsys.readouterr().out
