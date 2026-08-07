"""Tests for discord_events.py -- the read-only Guild Scheduled Events
client. Network/retry behavior lives in discord_api.py and is tested in
test_discord_api.py; these tests just cover what discord_events does with
a response, mocking at the discord_api.get boundary.

Run with: pytest proclubs/tests/test_discord_events.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import config  # noqa: E402
import discord_events  # noqa: E402


class _FakeResponse:
    def __init__(self, json_data):
        self._json = json_data

    def json(self):
        return self._json


def test_list_scheduled_events_returns_parsed_json(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_GUILD_ID", 123)
    captured = {}

    def fake_get(path, params=None):
        captured["path"] = path
        return _FakeResponse([{"id": "1", "name": "Scrim Night"}])

    monkeypatch.setattr(discord_events.discord_api, "get", fake_get)

    events = discord_events.list_scheduled_events()
    assert events == [{"id": "1", "name": "Scrim Night"}]
    assert captured["path"] == "/guilds/123/scheduled-events"


def test_list_scheduled_events_propagates_api_error(monkeypatch):
    def fake_get(path, params=None):
        raise discord_events.DiscordApiError("boom")

    monkeypatch.setattr(discord_events.discord_api, "get", fake_get)
    with pytest.raises(discord_events.DiscordApiError):
        discord_events.list_scheduled_events()


def test_list_scheduled_events_raises_on_bad_json(monkeypatch):
    class _BadJsonResponse(_FakeResponse):
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(discord_events.discord_api, "get", lambda *a, **k: _BadJsonResponse(None))
    with pytest.raises(discord_events.DiscordApiError):
        discord_events.list_scheduled_events()


def test_is_upcoming():
    assert discord_events.is_upcoming({"status": discord_events.STATUS_SCHEDULED}) is True
    assert discord_events.is_upcoming({"status": discord_events.STATUS_ACTIVE}) is True
    assert discord_events.is_upcoming({"status": discord_events.STATUS_COMPLETED}) is False
    assert discord_events.is_upcoming({"status": discord_events.STATUS_CANCELED}) is False


def test_cover_image_url_builds_cdn_url_from_hash():
    url = discord_events.cover_image_url({"id": "123", "image": "abc123hash"})
    assert url == "https://cdn.discordapp.com/guild-events/123/abc123hash.png"


def test_cover_image_url_none_without_a_cover_set():
    assert discord_events.cover_image_url({"id": "123", "image": None}) is None
    assert discord_events.cover_image_url({"id": "123"}) is None
