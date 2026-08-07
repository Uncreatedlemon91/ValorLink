"""Tests for discord_clips_poll.py's main() -- the standalone script run
by the systemd timer.

Run with: pytest proclubs/tests/test_discord_clips_poll.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="proclubs-clips-poll-")
os.environ["SITE_DB_PATH"] = os.path.join(_TMP, "site.db")

import config  # noqa: E402
import database  # noqa: E402
import discord_clips  # noqa: E402
import discord_clips_poll  # noqa: E402
import services  # noqa: E402


def test_main_does_nothing_when_sync_disabled(monkeypatch, capsys):
    monkeypatch.setattr(config, "CLIPS_SYNC_ENABLED", False)

    def fail(*a, **k):
        raise AssertionError("should not call Discord when sync is disabled")

    monkeypatch.setattr(discord_clips, "list_recent_messages", fail)
    discord_clips_poll.main()
    assert "nothing to sync" in capsys.readouterr().out


def test_main_syncs_when_enabled(monkeypatch, capsys):
    database.Base.metadata.drop_all(database.engine)
    monkeypatch.setattr(config, "CLIPS_SYNC_ENABLED", True)
    monkeypatch.setattr(config, "CLIPS_CHANNEL_ID", "555")
    monkeypatch.setattr(discord_clips, "list_recent_messages", lambda channel_id: [
        {"id": "m1", "content": "Nice goal", "timestamp": "2027-06-01T18:00:00+00:00",
         "attachments": [{"url": "https://cdn.discordapp.com/clip.mp4", "filename": "clip.mp4",
                           "content_type": "video/mp4"}],
         "author": {"username": "Coach"}},
    ])

    discord_clips_poll.main()

    with database.get_session() as session:
        clips = services.list_clips(session)
        assert len(clips) == 1
        assert clips[0].discord_message_id == "m1"

    assert "1 new clip" in capsys.readouterr().out


def test_main_handles_discord_api_failure_without_touching_db(monkeypatch, capsys):
    database.Base.metadata.drop_all(database.engine)
    monkeypatch.setattr(config, "CLIPS_SYNC_ENABLED", True)
    monkeypatch.setattr(config, "CLIPS_CHANNEL_ID", "555")

    def fail(channel_id):
        raise discord_clips.DiscordApiError("boom")

    monkeypatch.setattr(discord_clips, "list_recent_messages", fail)
    discord_clips_poll.main()

    with database.get_session() as session:
        assert services.list_clips(session) == []
    assert "could not fetch" in capsys.readouterr().out
