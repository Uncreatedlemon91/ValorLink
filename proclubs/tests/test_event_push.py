"""Push to Discord: one button on an event that posts it, or brings an
existing post back into view when it's archived, deleted, or stuck in a
private thread nobody can see.

Discord is faked at discord_api, so these check what the site asks
Discord to do and what it records.

Run with: pytest proclubs/tests/test_event_push.py
"""
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import config  # noqa: E402
import database  # noqa: E402
import discord_api  # noqa: E402
import services  # noqa: E402
from models import Event, EventTierInvite  # noqa: E402

PARENT = "500"


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
    """A fake Discord: channels by id ({"type", "archived", "messages"}),
    and every call made."""
    state = {"channels": {PARENT: {"type": 0, "archived": False, "messages": set()}},
             "calls": [], "next": 1000}
    monkeypatch.setattr(config, "EVENT_RSVP_ENABLED", True)
    monkeypatch.setattr(config, "EVENT_THREAD_CHANNEL_ID", PARENT)
    monkeypatch.setattr(config, "EVENT_STAGED_INVITES_ENABLED", False)
    monkeypatch.setattr(config, "DISCORD_GUILD_ID", 1)
    monkeypatch.setattr(config, "SITE_BASE_URL", "https://example.test")

    def new_id():
        state["next"] += 1
        return str(state["next"])

    def missing():
        raise discord_api.DiscordApiError("could not reach Discord's API: 404 Not Found")

    def get(path, params=None):
        state["calls"].append(("get", path))
        cid = path.rsplit("/", 1)[1]
        ch = state["channels"].get(cid)
        if ch is None:
            missing()
        return _Resp({"id": cid, "type": ch["type"], "thread_metadata": {"archived": ch["archived"]}})

    def post(path, json):
        state["calls"].append(("post", path, json))
        cid = path.split("/")[2]
        if path.endswith("/threads"):
            tid = new_id()
            state["channels"][tid] = {"type": json["type"], "archived": False, "messages": set()}
            return _Resp({"id": tid})
        if cid not in state["channels"]:
            missing()
        mid = new_id()
        state["channels"][cid]["messages"].add(mid)
        return _Resp({"id": mid})

    def patch(path, json):
        state["calls"].append(("patch", path, json))
        parts = path.split("/")
        ch = state["channels"].get(parts[2])
        if ch is None:
            missing()
        if len(parts) == 3:
            ch["archived"] = json.get("archived", ch["archived"])
            return _Resp({})
        if ch["archived"]:
            raise discord_api.DiscordApiError("400 Thread is archived")
        if parts[4] not in ch["messages"]:
            missing()
        return _Resp({})

    def delete(path):
        state["calls"].append(("delete", path))
        state["channels"].pop(path.split("/")[2], None)

    for name, fn in (("get", get), ("post", post), ("patch", patch), ("delete", delete)):
        monkeypatch.setattr(discord_api, name, fn)
    return state


def _login_staff(client):
    client.post("/auth/dev", data={"name": "Coach", "staff": "1"}, follow_redirects=False)


def _event(**kwargs) -> int:
    with database.get_session() as session:
        e = services.create_event(session, title="Derby", event_type="Match",
                                  scheduled_at=datetime.utcnow() + timedelta(days=3), opponent="",
                                  description="", image=None, staff_name="Coach")
        for k, v in kwargs.items():
            setattr(e, k, v)
        session.commit()
        return e.id


def _push(client, event_id):
    page = client.get(f"/events/{event_id}").text
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    return client.post(f"/events/{event_id}/announce", data={"csrf_token": token})


def _stored(event_id):
    with database.get_session() as session:
        e = session.get(Event, event_id)
        return e.discord_channel_id, e.discord_message_id


# --- The button ------------------------------------------------------------------ #
def test_staff_always_see_a_discord_button(client, discord):
    _login_staff(client)
    eid = _event()
    assert "Post to Discord" in client.get(f"/events/{eid}").text
    _push(client, eid)
    html = client.get(f"/events/{eid}").text
    assert "Push to Discord" in html
    channel, message = _stored(eid)
    assert f"https://discord.com/channels/1/{channel}/{message}" in html


def test_without_discord_set_up_the_button_says_what_is_missing(client, monkeypatch):
    monkeypatch.setattr(config, "EVENT_RSVP_ENABLED", False)
    monkeypatch.setattr(config, "event_rsvp_missing", lambda: ["EVENT_THREAD_CHANNEL_ID"])
    _login_staff(client)
    eid = _event()
    assert "Post to Discord" in client.get(f"/events/{eid}").text
    assert "set EVENT_THREAD_CHANNEL_ID" in _push(client, eid).text


# --- Threads people can see -------------------------------------------------------- #
def test_without_an_invite_ladder_the_thread_is_public(client, discord):
    _login_staff(client)
    eid = _event()
    _push(client, eid)
    thread = [c for c in discord["calls"] if c[0] == "post" and c[1].endswith("/threads")][0]
    assert thread[2]["type"] == 11


def test_with_an_invite_ladder_the_thread_stays_private(client, discord, monkeypatch):
    monkeypatch.setattr(config, "EVENT_STAGED_INVITES_ENABLED", True)
    monkeypatch.setattr(config, "EVENT_INVITE_TIERS", [])
    _login_staff(client)
    eid = _event()
    _push(client, eid)
    thread = [c for c in discord["calls"] if c[0] == "post" and c[1].endswith("/threads")][0]
    assert thread[2]["type"] == 12


# --- Pushing a post that already exists --------------------------------------------- #
def test_pushing_a_live_post_just_updates_it(client, discord):
    _login_staff(client)
    eid = _event()
    _push(client, eid)
    before = _stored(eid)
    r = _push(client, eid)
    assert "Discord post brought up to date." in r.text
    assert _stored(eid) == before


def test_pushing_reopens_an_archived_thread(client, discord):
    _login_staff(client)
    eid = _event()
    _push(client, eid)
    channel, _ = _stored(eid)
    discord["channels"][channel]["archived"] = True
    r = _push(client, eid)
    assert "Discord post brought up to date." in r.text
    assert discord["channels"][channel]["archived"] is False


def test_a_deleted_post_is_posted_again_in_its_thread(client, discord):
    _login_staff(client)
    eid = _event()
    _push(client, eid)
    channel, message = _stored(eid)
    discord["channels"][channel]["messages"].clear()
    r = _push(client, eid)
    assert "posted again in its thread" in r.text
    assert _stored(eid)[0] == channel and _stored(eid)[1] != message


def test_a_deleted_thread_is_replaced(client, discord):
    _login_staff(client)
    eid = _event()
    _push(client, eid)
    old_channel, _ = _stored(eid)
    del discord["channels"][old_channel]
    r = _push(client, eid)
    assert "posted again with sign-up buttons" in r.text
    new_channel, _ = _stored(eid)
    assert new_channel != old_channel and new_channel in discord["channels"]


def test_a_private_thread_nobody_can_see_is_replaced_by_a_public_one(client, discord):
    discord["channels"]["77"] = {"type": 12, "archived": False, "messages": {"78"}}
    with database.get_session() as session:
        session.add(Event(id=9, title="Old", event_type="Match",
                          scheduled_at=datetime.utcnow() + timedelta(days=1),
                          discord_channel_id="77", discord_message_id="78"))
        session.add(EventTierInvite(event_id=9, tier_key="create", role_id="111", member_count=0))
        session.commit()
    _login_staff(client)
    _push(client, 9)
    new_channel, _ = _stored(9)
    assert new_channel != "77" and discord["channels"][new_channel]["type"] == 11
    assert "77" not in discord["channels"]                       # the hidden one is tidied away
    with database.get_session() as session:
        assert services.invited_tier_keys(session, 9) == set()    # the ladder starts again


def test_a_discord_failure_is_flashed_and_nothing_is_lost(client, discord, monkeypatch):
    _login_staff(client)
    eid = _event()
    _push(client, eid)
    before = _stored(eid)

    def down(path, params=None):
        raise discord_api.DiscordApiError("could not reach Discord's API: 503 Service Unavailable")
    monkeypatch.setattr(discord_api, "get", down)
    r = _push(client, eid)
    assert "Couldn&#39;t push to Discord" in r.text or "Couldn't push to Discord" in r.text
    assert _stored(eid) == before
