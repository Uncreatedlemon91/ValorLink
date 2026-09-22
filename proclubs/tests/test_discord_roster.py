"""Tests for discord_roster.py -- the squad-move picker and its
announcements.

The rules worth pinning here are the ones a careless change would quietly
break: that announcing never touches a Discord role, that an announcement
can't be made about somebody who isn't in the server, that a mention
actually notifies the person it names, and that the avatar URL is right
for each of the four ways Discord represents one.

Run with: pytest proclubs/tests/test_discord_roster.py
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import discord_roster  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_cache():
    """One test's stubbed member list must not answer the next one's read."""
    discord_roster.invalidate_members_cache()
    yield
    discord_roster.invalidate_members_cache()


def _member(user_id="1", username="keeper", nick=None, global_name=None,
            avatar=None, guild_avatar=None, bot=False, discriminator="0",
            roles=None):
    return {
        "nick": nick,
        "avatar": guild_avatar,
        "roles": roles or [],
        "user": {
            "id": user_id, "username": username, "global_name": global_name,
            "avatar": avatar, "discriminator": discriminator, "bot": bot,
        },
    }


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


# --- Naming and avatars ----------------------------------------------------- #
def test_display_name_prefers_nickname_then_global_name_then_username():
    assert discord_roster.display_name(
        _member(nick="Cap", global_name="Jordan", username="jordan_x")) == "Cap"
    assert discord_roster.display_name(
        _member(global_name="Jordan", username="jordan_x")) == "Jordan"
    assert discord_roster.display_name(_member(username="jordan_x")) == "jordan_x"


def test_guild_avatar_wins_over_account_avatar():
    """Somebody who set a server-specific avatar should look on the site
    the way they look in Discord's own member list."""
    url = discord_roster.avatar_url(_member(user_id="77", avatar="acct", guild_avatar="srv"))
    assert "/guilds/" in url and "srv" in url
    assert "acct" not in url


def test_account_avatar_used_when_there_is_no_guild_avatar():
    url = discord_roster.avatar_url(_member(user_id="77", avatar="acct"))
    assert url.startswith("https://cdn.discordapp.com/avatars/77/acct.png")


def test_animated_avatars_are_requested_as_a_still_png():
    url = discord_roster.avatar_url(_member(user_id="77", avatar="a_animated"))
    assert url.endswith(".png?size=128") or ".png?" in url
    assert ".gif" not in url


def test_default_art_for_a_new_style_account_indexes_off_the_snowflake():
    url = discord_roster.avatar_url(_member(user_id=str(6 << 22), discriminator="0"))
    assert url == "https://cdn.discordapp.com/embed/avatars/0.png"


def test_default_art_for_a_legacy_account_indexes_off_the_discriminator():
    url = discord_roster.avatar_url(_member(user_id="77", discriminator="1234"))
    assert url == "https://cdn.discordapp.com/embed/avatars/4.png"


# --- The picker ------------------------------------------------------------- #
def test_roster_choices_drops_bots_and_sorts_by_name(monkeypatch):
    monkeypatch.setattr(discord_roster, "fetch_guild_members", lambda: [
        _member(user_id="3", username="zoe"),
        _member(user_id="1", username="MatchBot", bot=True),
        _member(user_id="2", username="alex"),
    ])
    names = [c["name"] for c in discord_roster.roster_choices()]
    assert names == ["alex", "zoe"], "a bot can't be offered a position"


def test_roster_choices_sorts_case_insensitively(monkeypatch):
    """Otherwise every lowercase handle sorts below every capitalised one
    and the list reads as unsorted."""
    monkeypatch.setattr(discord_roster, "fetch_guild_members", lambda: [
        _member(user_id="1", username="alex"),
        _member(user_id="2", username="Bo"),
        _member(user_id="3", username="chris"),
    ])
    assert [c["name"] for c in discord_roster.roster_choices()] == ["alex", "Bo", "chris"]


def test_the_member_list_is_cached_between_reads(monkeypatch):
    calls = []

    def fetch():
        calls.append(1)
        return [_member(user_id="1", username="alex")]

    monkeypatch.setattr(discord_roster, "fetch_guild_members", fetch)
    discord_roster.roster_choices()
    discord_roster.roster_choices()
    assert len(calls) == 1


def test_invalidating_the_cache_forces_a_fresh_read(monkeypatch):
    calls = []

    def fetch():
        calls.append(1)
        return [_member(user_id="1", username="alex")]

    monkeypatch.setattr(discord_roster, "fetch_guild_members", fetch)
    discord_roster.roster_choices()
    discord_roster.invalidate_members_cache()
    discord_roster.roster_choices()
    assert len(calls) == 2


def test_fetch_guild_members_pages_until_discord_runs_out(monkeypatch):
    pages = [
        [_member(user_id=str(i)) for i in range(1, 1001)],
        [_member(user_id="1001")],
    ]
    seen_after = []

    def fake_get(path, params=None):
        seen_after.append(params["after"])
        return _FakeResponse(pages.pop(0) if pages else [])

    monkeypatch.setattr(discord_roster.discord_api, "get", fake_get)
    members = discord_roster.fetch_guild_members()
    assert len(members) == 1001
    assert seen_after == ["0", "1000"], "the second page must start after the first's last id"


def test_find_member_resolves_an_id_and_refuses_an_unknown_one():
    members = [{"id": "42", "name": "Alex", "username": "alex", "avatar_url": "x"}]
    assert discord_roster.find_member(members, "42")["name"] == "Alex"
    assert discord_roster.find_member(members, "99") is None


# --- The announcement ------------------------------------------------------- #
def _choice(name="Alex", member_id="42"):
    return {"id": member_id, "name": name, "username": "alex",
            "avatar_url": "https://cdn.discordapp.com/avatars/42/x.png"}


def test_an_offer_reads_as_a_welcome_and_names_the_position():
    embed = discord_roster.build_move_embed(
        kind=discord_roster.MOVE_OFFER, member=_choice(), position="Striker",
        note=None, announced_by="Coach",
        announced_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    assert "Offer Extended" in embed["title"]
    assert "<@42>" in embed["description"]
    assert "Striker" in embed["description"]
    assert embed["color"] == discord_roster._OFFER_COLOR
    assert {"name": "Position", "value": "Striker", "inline": True} in embed["fields"]
    assert embed["footer"]["text"].startswith("Announced by Coach")


def test_a_departure_is_respectful_and_visually_distinct_from_an_offer():
    """The wording is part of the feature -- "a nice professional
    message" is the whole ask, and a departure is the one most easily got
    wrong."""
    embed = discord_roster.build_move_embed(
        kind=discord_roster.MOVE_RELEASE, member=_choice(), position=None,
        note=None, announced_by=None,
    )
    assert "Departure" in embed["title"]
    assert "thank them" in embed["description"]
    assert embed["color"] == discord_roster._RELEASE_COLOR
    assert embed["color"] != discord_roster._OFFER_COLOR
    assert "footer" not in embed


def test_the_embed_carries_the_members_avatar():
    embed = discord_roster.build_move_embed(
        kind=discord_roster.MOVE_OFFER, member=_choice(), position=None,
        note=None, announced_by=None,
    )
    assert embed["thumbnail"]["url"] == _choice()["avatar_url"]


def test_a_staff_note_is_added_as_its_own_field():
    embed = discord_roster.build_move_embed(
        kind=discord_roster.MOVE_OFFER, member=_choice(), position="Striker",
        note="Joining us from Rivals FC.", announced_by=None,
    )
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["From the staff"] == "Joining us from Rivals FC."
    assert fields["Position"] == "Striker"


def test_a_note_alone_still_gets_a_fields_list():
    """The position field is what creates `fields`; a note with no
    position must not fall through the crack that leaves."""
    embed = discord_roster.build_move_embed(
        kind=discord_roster.MOVE_RELEASE, member=_choice(), position=None,
        note="Mutual decision.", announced_by=None,
    )
    assert embed["fields"] == [
        {"name": "From the staff", "value": "Mutual decision.", "inline": False}
    ]


def test_an_over_long_note_is_truncated_to_discords_field_limit():
    embed = discord_roster.build_move_embed(
        kind=discord_roster.MOVE_OFFER, member=_choice(), position=None,
        note="x" * 5000, announced_by=None,
    )
    assert len(embed["fields"][0]["value"]) == 1024


def test_an_unknown_kind_is_rejected_rather_than_guessed():
    with pytest.raises(ValueError):
        discord_roster.build_move_embed(
            kind="promote", member=_choice(), position=None, note=None,
            announced_by=None,
        )


def test_announcing_mentions_the_player_so_they_are_actually_notified(monkeypatch):
    sent = {}

    def fake_post(path, json):
        sent["path"], sent["body"] = path, json
        return _FakeResponse({"id": "999"})

    monkeypatch.setattr(discord_roster.discord_api, "post", fake_post)
    message_id = discord_roster.announce_move(
        "555", {"title": "x"}, mention_id="42",
    )
    assert message_id == "999"
    assert sent["path"] == "/channels/555/messages"
    assert sent["body"]["content"] == "<@42>"
    assert sent["body"]["allowed_mentions"] == {"users": ["42"]}


def test_an_announcement_can_never_become_an_everyone_ping(monkeypatch):
    """allowed_mentions must be explicit: leave it off and any @everyone
    that ends up in a staff note would go out for real."""
    sent = {}
    monkeypatch.setattr(discord_roster.discord_api, "post",
                        lambda path, json: (sent.update(body=json), _FakeResponse({"id": "1"}))[1])
    discord_roster.announce_move("555", {"title": "x"}, mention_id=None)
    assert sent["body"]["allowed_mentions"] == {"users": []}
    assert "content" not in sent["body"]


def test_nothing_in_this_module_writes_a_discord_role():
    """The guardrail the feature rests on, checked against the source
    rather than trusted: this module reads members and posts messages,
    and must never PUT or DELETE a role. A change that adds one should
    have to come here and argue for it."""
    source = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "discord_roster.py")).read()
    assert "/roles/" not in source
    assert "discord_api.put" not in source
    assert "discord_api.delete" not in source


def test_an_over_long_position_is_capped_so_the_post_cannot_be_rejected():
    """maxlength on the input is a suggestion to a browser; Discord's
    4096-character description limit is not."""
    embed = discord_roster.build_move_embed(
        kind=discord_roster.MOVE_OFFER, member=_choice(), position="x" * 5000,
        note=None, announced_by=None,
    )
    assert len(embed["fields"][0]["value"]) == 80
    assert len(embed["description"]) < 4096
