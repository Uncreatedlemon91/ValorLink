"""Tests for discord_clips.py -- the read-only Discord channel-messages
client used to pull video clips onto the site's Clips page. Network/retry
behavior lives in discord_api.py and is tested in test_discord_api.py;
these tests mock at the discord_api.get boundary.

Run with: pytest proclubs/tests/test_discord_clips.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import config  # noqa: E402
import discord_clips  # noqa: E402


class _FakeResponse:
    def __init__(self, json_data):
        self._json = json_data

    def json(self):
        return self._json


def test_list_recent_messages_returns_parsed_json(monkeypatch):
    captured = {}

    def fake_get(path, params=None):
        captured["path"] = path
        captured["params"] = params
        return _FakeResponse([{"id": "1", "content": "clip"}])

    monkeypatch.setattr(discord_clips.discord_api, "get", fake_get)

    messages = discord_clips.list_recent_messages("555", limit=25)
    assert messages == [{"id": "1", "content": "clip"}]
    assert captured["path"] == "/channels/555/messages"
    assert captured["params"] == {"limit": 25}


def test_list_recent_messages_propagates_api_error(monkeypatch):
    def fake_get(path, params=None):
        raise discord_clips.DiscordApiError("boom")

    monkeypatch.setattr(discord_clips.discord_api, "get", fake_get)
    with pytest.raises(discord_clips.DiscordApiError):
        discord_clips.list_recent_messages("555")


def test_list_recent_messages_raises_on_bad_json(monkeypatch):
    class _BadJsonResponse(_FakeResponse):
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(discord_clips.discord_api, "get", lambda *a, **k: _BadJsonResponse(None))
    with pytest.raises(discord_clips.DiscordApiError):
        discord_clips.list_recent_messages("555")


def test_video_attachments_filters_to_video_content_type_only():
    message = {"attachments": [
        {"url": "https://cdn.discordapp.com/a.png", "content_type": "image/png"},
        {"url": "https://cdn.discordapp.com/b.mp4", "content_type": "video/mp4"},
        {"url": "https://cdn.discordapp.com/c.mov", "content_type": "video/quicktime"},
    ]}
    urls = [a["url"] for a in discord_clips.video_attachments(message)]
    assert urls == ["https://cdn.discordapp.com/b.mp4", "https://cdn.discordapp.com/c.mov"]


def test_video_attachments_empty_when_no_attachments():
    assert discord_clips.video_attachments({}) == []
    assert discord_clips.video_attachments({"attachments": []}) == []


def test_video_attachments_ignores_attachment_with_no_content_type():
    message = {"attachments": [{"url": "https://cdn.discordapp.com/mystery"}]}
    assert discord_clips.video_attachments(message) == []


def test_jump_url_includes_guild_channel_and_message(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_GUILD_ID", 111)
    url = discord_clips.jump_url("222", "333")
    assert url == "https://discord.com/channels/111/222/333"
