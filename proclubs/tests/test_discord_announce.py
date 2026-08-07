"""Tests for discord_announce.py -- the rich-embed-on-publish feature.

Run with: pytest proclubs/tests/test_discord_announce.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import discord_announce  # noqa: E402


def test_build_embed_has_title_url_color_and_timestamp():
    embed = discord_announce.build_embed(
        title="Big Win Tonight", url="https://example.com/news/big-win-tonight",
        summary=None, category="News", author_name=None, cover_image_url=None,
        published_at=datetime(2026, 8, 7, 12, 0, 0),
    )
    assert embed["title"] == "Big Win Tonight"
    assert embed["url"] == "https://example.com/news/big-win-tonight"
    assert embed["color"] == discord_announce._CATEGORY_COLOR["News"]
    assert embed["author"] == {"name": "News"}
    assert embed["timestamp"] == "2026-08-07T12:00:00"


def test_build_embed_uses_distinct_colors_per_category():
    news = discord_announce.build_embed(
        title="x", url="https://x", summary=None, category="News", author_name=None,
        cover_image_url=None, published_at=datetime.utcnow(),
    )
    transfer = discord_announce.build_embed(
        title="x", url="https://x", summary=None, category="Transfer", author_name=None,
        cover_image_url=None, published_at=datetime.utcnow(),
    )
    assert news["color"] != transfer["color"]


def test_build_embed_falls_back_to_default_color_for_unknown_category():
    embed = discord_announce.build_embed(
        title="x", url="https://x", summary=None, category="Something New", author_name=None,
        cover_image_url=None, published_at=datetime.utcnow(),
    )
    assert embed["color"] == discord_announce._DEFAULT_COLOR


def test_build_embed_omits_optional_fields_when_absent():
    embed = discord_announce.build_embed(
        title="x", url="https://x", summary=None, category="News", author_name=None,
        cover_image_url=None, published_at=datetime.utcnow(),
    )
    assert "description" not in embed
    assert "footer" not in embed
    assert "image" not in embed


def test_build_embed_includes_optional_fields_when_present():
    embed = discord_announce.build_embed(
        title="x", url="https://x", summary="A great match.", category="News",
        author_name="Coach", cover_image_url="https://example.com/news/x/cover-image",
        published_at=datetime.utcnow(),
    )
    assert embed["description"] == "A great match."
    assert embed["footer"] == {"text": "Posted by Coach"}
    assert embed["image"] == {"url": "https://example.com/news/x/cover-image"}


def test_announce_posts_embed_to_the_given_channel(monkeypatch):
    captured = {}
    monkeypatch.setattr(discord_announce.discord_api, "post", lambda path, json: captured.update(path=path, json=json))

    discord_announce.announce("123456", {"title": "hi"})
    assert captured["path"] == "/channels/123456/messages"
    assert captured["json"] == {"embeds": [{"title": "hi"}]}


def test_announce_propagates_discord_api_errors(monkeypatch):
    def fake_post(path, json):
        raise discord_announce.DiscordApiError("boom")

    monkeypatch.setattr(discord_announce.discord_api, "post", fake_post)
    with pytest.raises(discord_announce.DiscordApiError):
        discord_announce.announce("123456", {"title": "hi"})
