"""Tests for discord_api.py -- the shared GET-with-429-retry helper used by
discord_events.py and discord_clips.py.

Run with: pytest proclubs/tests/test_discord_api.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import config  # noqa: E402
import discord_api  # noqa: E402


class _FakeResponse:
    def __init__(self, json_data, status_code=200, headers=None):
        self._json = json_data
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise discord_api.httpx.HTTPStatusError("bad status", request=None, response=self)

    def json(self):
        return self._json


def test_get_returns_response_and_sends_bot_auth(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "test-token")
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["params"] = params
        return _FakeResponse([{"id": "1"}])

    monkeypatch.setattr(discord_api.httpx, "get", fake_get)

    resp = discord_api.get("/channels/999/messages", params={"limit": 50})
    assert resp.json() == [{"id": "1"}]
    assert captured["url"] == "https://discord.com/api/v10/channels/999/messages"
    assert captured["headers"]["Authorization"] == "Bot test-token"
    assert captured["params"] == {"limit": 50}


def test_get_raises_on_network_failure(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        raise discord_api.httpx.ConnectError("boom")

    monkeypatch.setattr(discord_api.httpx, "get", fake_get)
    with pytest.raises(discord_api.DiscordApiError):
        discord_api.get("/channels/999/messages")


def test_get_retries_once_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(discord_api.time, "sleep", lambda seconds: None)
    responses = [
        _FakeResponse({"retry_after": 0.5}, status_code=429, headers={"Retry-After": "0.5"}),
        _FakeResponse([{"id": "1"}]),
    ]
    monkeypatch.setattr(discord_api.httpx, "get", lambda *a, **k: responses.pop(0))

    resp = discord_api.get("/channels/999/messages")
    assert resp.json() == [{"id": "1"}]
    assert responses == []  # both queued responses were consumed (one retry happened)


def test_get_raises_clear_error_if_still_rate_limited(monkeypatch):
    sleeps = []
    monkeypatch.setattr(discord_api.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(discord_api.httpx, "get", lambda *a, **k: _FakeResponse(
        {"retry_after": 30}, status_code=429, headers={"Retry-After": "30"},
    ))

    with pytest.raises(discord_api.DiscordApiError, match="still rate-limited"):
        discord_api.get("/channels/999/messages")
    # Waited, but capped -- never the full 30s Discord asked for.
    assert sleeps == [discord_api._MAX_RETRY_WAIT]


def test_get_raises_on_other_http_error(monkeypatch):
    monkeypatch.setattr(discord_api.httpx, "get", lambda *a, **k: _FakeResponse(
        {"message": "not found"}, status_code=404,
    ))
    with pytest.raises(discord_api.DiscordApiError):
        discord_api.get("/channels/999/messages")


def test_retry_after_seconds_prefers_header_over_body():
    resp = _FakeResponse({"retry_after": 99}, status_code=429, headers={"Retry-After": "2.5"})
    assert discord_api._retry_after_seconds(resp) == 2.5


def test_retry_after_seconds_falls_back_to_body_then_default():
    resp = _FakeResponse({"retry_after": 3}, status_code=429)
    assert discord_api._retry_after_seconds(resp) == 3.0

    resp_no_info = _FakeResponse({}, status_code=429)
    assert discord_api._retry_after_seconds(resp_no_info) == 1.0
