"""Coach notes in Discord: each note is posted to the staff channel, nobody
is pinged, and deleting the note deletes the Discord copy.

Run with: pytest proclubs/tests/test_coach_notes.py
"""
import os
import re
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import coach_notes  # noqa: E402
import config  # noqa: E402
import database  # noqa: E402
import discord_api  # noqa: E402
import services  # noqa: E402

STAFF_CHANNEL = "1546267802791452772"


@pytest.fixture
def client():
    database.Base.metadata.drop_all(database.engine)
    with TestClient(appmod.app) as c:
        yield c


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


@pytest.fixture
def discord(monkeypatch):
    state = {"posts": [], "deletes": [], "fail": None}
    monkeypatch.setattr(config, "COACH_NOTES_DISCORD_ENABLED", True)
    monkeypatch.setattr(config, "COACH_NOTES_CHANNEL_ID", STAFF_CHANNEL)
    monkeypatch.setattr(config, "SITE_BASE_URL", "https://example.test")

    def post(path, json):
        if state["fail"]:
            raise discord_api.DiscordApiError(state["fail"])
        state["posts"].append((path, json))
        return _Resp({"id": f"m{len(state['posts'])}"})

    def delete(path):
        if state["fail"]:
            raise discord_api.DiscordApiError(state["fail"])
        state["deletes"].append(path)

    monkeypatch.setattr(discord_api, "post", post)
    monkeypatch.setattr(discord_api, "delete", delete)
    return state


def _id(name):
    return str(zlib.crc32(name.encode()))


def _coach(client, name="Coachy"):
    with database.get_session() as session:
        p = services.ensure_player(session, discord_id=_id(name), display_name=name)
        services.set_club_role(session, p, "Coach")
    client.post("/auth/dev", data={"name": name, "member": "1"}, follow_redirects=False)


def _player(uid="42", name="Cap"):
    with database.get_session() as session:
        services.create_contract(session, discord_id=uid, display_name=name, avatar_url=None,
                                 position="Striker", squad_status="Starter", weeks=8,
                                 source="recorded", created_by_name="Boss")


def _csrf(client, path):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text).group(1)


def _add_note(client, body, uid="42"):
    return client.post(f"/players/{uid}/notes", data={"body": body, "csrf_token": _csrf(client, f"/players/{uid}")})


def test_a_coach_note_is_posted_to_the_staff_channel(client, discord):
    _player()
    _coach(client)
    r = _add_note(client, "Drifts inside too early. @everyone")
    assert "posted to the staff channel" in r.text
    [(path, payload)] = discord["posts"]
    assert path == f"/channels/{STAFF_CHANNEL}/messages"
    assert payload["allowed_mentions"] == {"parse": []}          # pings nobody
    embed = payload["embeds"][0]
    assert embed["title"] == "Coach note — Cap"
    assert embed["description"].startswith("Drifts inside too early.")
    assert embed["url"] == "https://example.test/players/42#coach-notes"
    assert {"name": "Squad", "value": "Starter · Striker", "inline": True} in embed["fields"]
    assert embed["footer"]["text"] == "From Coachy · staff only"
    with database.get_session() as session:
        [note] = services.list_coach_notes(session, "42")
        assert (note.discord_channel_id, note.discord_message_id) == (STAFF_CHANNEL, "m1")


def test_deleting_the_note_deletes_the_discord_copy(client, discord):
    _player()
    _coach(client)
    _add_note(client, "Talk about his runs.")
    with database.get_session() as session:
        note_id = services.list_coach_notes(session, "42")[0].id
    r = client.post(f"/players/42/notes/{note_id}/delete", data={"csrf_token": _csrf(client, "/players/42")})
    assert "Note deleted." in r.text
    assert discord["deletes"] == [f"/channels/{STAFF_CHANNEL}/messages/m1"]


def test_a_discord_failure_still_saves_the_note(client, discord):
    _player()
    _coach(client)
    discord["fail"] = "403 Missing Access"
    r = _add_note(client, "Needs a rest.")
    assert "couldn" in r.text and "Missing Access" in r.text
    with database.get_session() as session:
        [note] = services.list_coach_notes(session, "42")
        assert note.discord_message_id is None


def test_a_copy_already_gone_from_discord_doesnt_block_deleting(client, discord):
    _player()
    _coach(client)
    _add_note(client, "x")
    discord["fail"] = "404 Unknown Message"
    with database.get_session() as session:
        note_id = services.list_coach_notes(session, "42")[0].id
    r = client.post(f"/players/42/notes/{note_id}/delete", data={"csrf_token": _csrf(client, "/players/42")})
    assert "Note deleted." in r.text and "couldn" not in r.text
    with database.get_session() as session:
        assert services.list_coach_notes(session, "42") == []


def test_turned_off_nothing_is_posted(client, discord, monkeypatch):
    monkeypatch.setattr(config, "COACH_NOTES_DISCORD_ENABLED", False)
    _player()
    _coach(client)
    r = _add_note(client, "Quiet one.")
    assert "Note added. Only staff can see it." in r.text
    assert discord["posts"] == []


def test_the_channel_defaults_to_recruitment_and_can_be_switched_off():
    assert config.coach_notes_channel("", "111") == "111"
    assert config.coach_notes_channel(" 222 ", "111") == "222"
    assert config.coach_notes_channel("off", "111") == ""


def test_the_embed_works_for_somebody_without_a_contract(client):
    with database.get_session() as session:
        services.ensure_player(session, discord_id="7", display_name="Gaffer")
        note = services.add_coach_note(session, discord_id="7", body="Good chat.", author={"id": 1, "name": "Boss"})
        embed = coach_notes.note_embed(session, note)
    assert embed["title"] == "Coach note — Gaffer"
    assert [f["name"] for f in embed["fields"]] == ["Discord"]
