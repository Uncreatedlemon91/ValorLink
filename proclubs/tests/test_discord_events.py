"""Tests for discord_events.py -- the read-only Guild Scheduled Events
REST client.

Run with: pytest proclubs/tests/test_discord_events.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import config  # noqa: E402
import discord_events  # noqa: E402


class _FakeResponse:
    def __init__(self, json_data, status_code=200, headers=None):
        self._json = json_data
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise discord_events.httpx.HTTPStatusError("bad status", request=None, response=self)

    def json(self):
        return self._json


def test_list_scheduled_events_returns_parsed_json(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_GUILD_ID", 123)
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "test-token")

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResponse([{"id": "1", "name": "Scrim Night"}])

    monkeypatch.setattr(discord_events.httpx, "get", fake_get)

    events = discord_events.list_scheduled_events()
    assert events == [{"id": "1", "name": "Scrim Night"}]
    assert "123" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bot test-token"


def test_list_scheduled_events_raises_on_http_failure(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        raise discord_events.httpx.ConnectError("boom")

    monkeypatch.setattr(discord_events.httpx, "get", fake_get)
    with pytest.raises(discord_events.DiscordApiError):
        discord_events.list_scheduled_events()


def test_list_scheduled_events_raises_on_bad_json(monkeypatch):
    class _BadJsonResponse(_FakeResponse):
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(discord_events.httpx, "get", lambda *a, **k: _BadJsonResponse(None))
    with pytest.raises(discord_events.DiscordApiError):
        discord_events.list_scheduled_events()


def test_list_scheduled_events_retries_once_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(discord_events.time, "sleep", lambda seconds: None)
    responses = [
        _FakeResponse({"retry_after": 0.5}, status_code=429, headers={"Retry-After": "0.5"}),
        _FakeResponse([{"id": "1", "name": "Scrim Night"}]),
    ]
    monkeypatch.setattr(discord_events.httpx, "get", lambda *a, **k: responses.pop(0))

    events = discord_events.list_scheduled_events()
    assert events == [{"id": "1", "name": "Scrim Night"}]
    assert responses == []  # both queued responses were consumed (one retry happened)


def test_list_scheduled_events_raises_clear_error_if_still_rate_limited(monkeypatch):
    sleeps = []
    monkeypatch.setattr(discord_events.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(discord_events.httpx, "get", lambda *a, **k: _FakeResponse(
        {"retry_after": 30}, status_code=429, headers={"Retry-After": "30"},
    ))

    with pytest.raises(discord_events.DiscordApiError, match="still rate-limited"):
        discord_events.list_scheduled_events()
    # Waited, but capped -- never the full 30s Discord asked for.
    assert sleeps == [discord_events._MAX_RETRY_WAIT]


def test_retry_after_seconds_prefers_header_over_body():
    resp = _FakeResponse({"retry_after": 99}, status_code=429, headers={"Retry-After": "2.5"})
    assert discord_events._retry_after_seconds(resp) == 2.5


def test_retry_after_seconds_falls_back_to_body_then_default():
    resp = _FakeResponse({"retry_after": 3}, status_code=429)
    assert discord_events._retry_after_seconds(resp) == 3.0

    resp_no_info = _FakeResponse({}, status_code=429)
    assert discord_events._retry_after_seconds(resp_no_info) == 1.0


def test_is_upcoming():
    assert discord_events.is_upcoming({"status": discord_events.STATUS_SCHEDULED}) is True
    assert discord_events.is_upcoming({"status": discord_events.STATUS_ACTIVE}) is True
    assert discord_events.is_upcoming({"status": discord_events.STATUS_COMPLETED}) is False
    assert discord_events.is_upcoming({"status": discord_events.STATUS_CANCELED}) is False
