"""Tests for twitch_client.py -- the app-token cache and live-streams lookup,
against a mocked Twitch API (no real network calls).

Run with: pytest proclubs/tests/test_twitch_client.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import config  # noqa: E402
import twitch_client  # noqa: E402


@pytest.fixture(autouse=True)
def _configured_and_reset(monkeypatch):
    monkeypatch.setattr(config, "TWITCH_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(config, "TWITCH_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(config, "TWITCH_ENABLED", True)
    twitch_client._token_cache.update({"value": None, "expires_at": 0.0})
    twitch_client._streams_cache.update({"key": None, "value": None, "expires_at": 0.0})
    yield


class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._json


def test_disabled_returns_empty_without_network(monkeypatch):
    monkeypatch.setattr(config, "TWITCH_ENABLED", False)
    assert twitch_client.live_streams(["someone"]) == {}


def test_no_logins_returns_empty():
    assert twitch_client.live_streams([]) == {}


def test_live_streams_maps_by_login(monkeypatch):
    calls = []

    def fake_post(url, data=None, timeout=None):
        calls.append(("post", url))
        return _FakeResponse({"access_token": "tok123", "expires_in": 3600})

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(("get", url))
        return _FakeResponse({
            "data": [{
                "user_login": "shroud", "title": "ranked grind",
                "game_name": "EA Sports FC", "viewer_count": 42,
                "thumbnail_url": "https://x/{width}x{height}.jpg",
                "started_at": "2026-01-01T00:00:00Z",
            }],
        })

    monkeypatch.setattr(twitch_client.httpx, "post", fake_post)
    monkeypatch.setattr(twitch_client.httpx, "get", fake_get)

    result = twitch_client.live_streams(["shroud", "someone_offline"])
    assert "shroud" in result
    assert "someone_offline" not in result
    assert result["shroud"]["viewer_count"] == 42
    assert "{width}" not in result["shroud"]["thumbnail_url"]
    assert ("post", twitch_client._TOKEN_URL) in calls


def test_streams_are_cached_between_calls(monkeypatch):
    call_count = {"get": 0}

    def fake_post(url, data=None, timeout=None):
        return _FakeResponse({"access_token": "tok123", "expires_in": 3600})

    def fake_get(url, params=None, headers=None, timeout=None):
        call_count["get"] += 1
        return _FakeResponse({"data": []})

    monkeypatch.setattr(twitch_client.httpx, "post", fake_post)
    monkeypatch.setattr(twitch_client.httpx, "get", fake_get)

    twitch_client.live_streams(["shroud"])
    twitch_client.live_streams(["shroud"])
    assert call_count["get"] == 1  # second call served from cache


def test_api_failure_degrades_to_empty(monkeypatch):
    def fake_post(url, data=None, timeout=None):
        raise twitch_client.httpx.HTTPError("boom")

    monkeypatch.setattr(twitch_client.httpx, "post", fake_post)
    assert twitch_client.live_streams(["shroud"]) == {}
