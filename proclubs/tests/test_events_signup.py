"""Event sign-ups from both surfaces: the site's own forms and Discord's
interaction webhook, plus the attendance record they feed.

Run with: pytest proclubs/tests/test_events_signup.py
"""
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="proclubs-signup-")
os.environ["SITE_DB_PATH"] = os.path.join(_TMP, "site.db")
os.environ["DEV_LOGIN"] = "1"
os.environ["SESSION_SECRET"] = "test-secret"
os.environ["HTTPS_ONLY"] = ""

import json  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from nacl.signing import SigningKey  # noqa: E402

import app as appmod  # noqa: E402
import config  # noqa: E402
import database  # noqa: E402
import discord_rsvp  # noqa: E402
import services  # noqa: E402
from models import Event, EventSignup, PlayerLink, TacticsSlot  # noqa: E402


@pytest.fixture
def client():
    database.Base.metadata.drop_all(database.engine)
    with TestClient(appmod.app) as c:
        yield c


def _login_staff(client, name="Coach"):
    assert client.post("/auth/dev", data={"name": name, "staff": "1"},
                       follow_redirects=False).status_code == 303
    return client


def _login_fan(client, name="Winger"):
    assert client.post("/auth/dev", data={"name": name, "member": "1"},
                       follow_redirects=False).status_code == 303
    return client


def _csrf(client, path):
    m = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert m, f"no csrf token on {path}"
    return m.group(1)


def _seed_event(**kwargs) -> int:
    with database.get_session() as session:
        event = services.create_event(
            session,
            title=kwargs.get("title", "Derby Day"),
            event_type=kwargs.get("event_type", "Match"),
            scheduled_at=kwargs.get("scheduled_at", datetime.utcnow() + timedelta(days=2)),
            opponent=kwargs.get("opponent", "Rivals FC"),
            description=kwargs.get("description", ""),
            image=None, staff_name="Coach",
        )
        return event.id


# --- Site sign-ups ---------------------------------------------------------- #
def test_member_can_sign_up_and_change_answer(client):
    event_id = _seed_event()
    _login_fan(client)
    token = _csrf(client, f"/events/{event_id}")

    r = client.post(f"/events/{event_id}/signup",
                    data={"status": "going", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    with database.get_session() as session:
        assert services.signup_counts(session, event_id)["going"] == 1

    # Answering again replaces, never stacks -- this is what makes the two
    # surfaces safe to use interchangeably.
    client.post(f"/events/{event_id}/signup",
                data={"status": "maybe", "csrf_token": token}, follow_redirects=False)
    with database.get_session() as session:
        counts = services.signup_counts(session, event_id)
        assert counts["going"] == 0 and counts["maybe"] == 1
        assert len(services.list_signups(session, event_id)) == 1


def test_non_member_cannot_sign_up(client):
    event_id = _seed_event()
    client.post("/auth/dev", data={"name": "Outsider"}, follow_redirects=False)
    r = client.post(f"/events/{event_id}/signup",
                    data={"status": "going", "csrf_token": "x"}, follow_redirects=False)
    assert r.status_code in (302, 303, 401, 403)
    with database.get_session() as session:
        assert services.signup_counts(session, event_id)["going"] == 0


def test_closed_signups_are_refused(client):
    event_id = _seed_event()
    with database.get_session() as session:
        services.set_signups_open(session, services.get_event(session, event_id), open_=False)
    _login_fan(client)
    with database.get_session() as session:
        event = services.get_event(session, event_id)
        with pytest.raises(services.ServiceError):
            services.set_signup(session, event, discord_user_id=1, discord_name="X",
                                discord_avatar=None, status="going")


def test_only_staff_create_events(client):
    _login_fan(client)
    r = client.post("/events/new", data={
        "title": "Sneaky", "event_type": "Match", "scheduled_at": "2030-01-01T20:00",
        "csrf_token": "x",
    }, follow_redirects=False)
    assert r.status_code in (302, 303, 401, 403)


# --- Discord interactions --------------------------------------------------- #
def _signed_request(client, payload: dict, *, key: SigningKey):
    body = json.dumps(payload).encode()
    timestamp = "1700000000"
    signature = key.sign(timestamp.encode() + body).signature.hex()
    return client.post("/discord/interactions", content=body, headers={
        "X-Signature-Ed25519": signature,
        "X-Signature-Timestamp": timestamp,
        "Content-Type": "application/json",
    })


@pytest.fixture
def discord_key(monkeypatch):
    key = SigningKey.generate()
    monkeypatch.setattr(config, "DISCORD_PUBLIC_KEY", bytes(key.verify_key).hex())
    return key


def test_unsigned_interaction_is_rejected(client, discord_key):
    """Discord probes with deliberately-bad signatures when the endpoint is
    saved and refuses the URL unless they get a 401. More importantly, an
    unverified endpoint would let anyone forge sign-ups."""
    r = client.post("/discord/interactions", json={"type": 1})
    assert r.status_code == 401

    body = json.dumps({"type": 1}).encode()
    r = client.post("/discord/interactions", content=body, headers={
        "X-Signature-Ed25519": "00" * 64, "X-Signature-Timestamp": "1700000000",
    })
    assert r.status_code == 401


def test_ping_is_ponged(client, discord_key):
    r = _signed_request(client, {"type": discord_rsvp.INTERACTION_PING}, key=discord_key)
    assert r.status_code == 200
    assert r.json() == {"type": discord_rsvp.RESPONSE_PONG}


def test_button_press_records_signup_and_updates_message(client, discord_key):
    event_id = _seed_event()
    r = _signed_request(client, {
        "type": discord_rsvp.INTERACTION_MESSAGE_COMPONENT,
        "data": {"custom_id": f"rsvp:{event_id}:going"},
        "member": {"user": {"id": "4242", "global_name": "Striker", "avatar": "abc"}},
    }, key=discord_key)
    assert r.status_code == 200
    payload = r.json()
    # UPDATE_MESSAGE re-renders the announcement in the same round trip, so
    # no follow-up PATCH (and no extra rate-limit budget) is needed.
    assert payload["type"] == discord_rsvp.RESPONSE_UPDATE_MESSAGE
    assert "Striker" in json.dumps(payload["data"]["embeds"])

    with database.get_session() as session:
        signup = services.get_signup(session, event_id, 4242)
        assert signup.status == "going"
        assert signup.source == "discord"


def test_button_press_for_missing_event_is_ephemeral_not_500(client, discord_key):
    r = _signed_request(client, {
        "type": discord_rsvp.INTERACTION_MESSAGE_COMPONENT,
        "data": {"custom_id": "rsvp:99999:going"},
        "member": {"user": {"id": "1", "username": "ghost"}},
    }, key=discord_key)
    assert r.status_code == 200
    assert r.json()["data"]["flags"] == 64  # ephemeral, visible only to presser


def test_malformed_custom_id_is_rejected(client, discord_key):
    r = _signed_request(client, {
        "type": discord_rsvp.INTERACTION_MESSAGE_COMPONENT,
        "data": {"custom_id": "something:else"},
        "member": {"user": {"id": "1", "username": "x"}},
    }, key=discord_key)
    assert r.status_code == 400


def test_both_surfaces_share_one_row(client, discord_key):
    """The same person answering on the site and then in Discord must end up
    as one sign-up, not two."""
    event_id = _seed_event()
    _login_fan(client, name="Winger")
    token = _csrf(client, f"/events/{event_id}")
    client.post(f"/events/{event_id}/signup",
                data={"status": "going", "csrf_token": token}, follow_redirects=False)

    with database.get_session() as session:
        user_id = services.list_signups(session, event_id)[0].discord_user_id

    _signed_request(client, {
        "type": discord_rsvp.INTERACTION_MESSAGE_COMPONENT,
        "data": {"custom_id": f"rsvp:{event_id}:out"},
        "member": {"user": {"id": str(user_id), "global_name": "Winger"}},
    }, key=discord_key)

    with database.get_session() as session:
        signups = services.list_signups(session, event_id)
        assert len(signups) == 1
        assert signups[0].status == "out"
        assert signups[0].source == "discord"


# --- Attendance record ------------------------------------------------------ #
def _mark(session, event_id, user_id, attendance):
    signup = services.get_signup(session, event_id, user_id)
    services.mark_attendance(session, signup, attendance=attendance, staff_name="Coach")


def test_attendance_rate_needs_enough_history(client):
    with database.get_session() as session:
        for i in range(2):
            event_id = _seed_event(title=f"Past {i}",
                                   scheduled_at=datetime.utcnow() - timedelta(days=i + 1))
            event = services.get_event(session, event_id)
            services.set_signup(session, event, discord_user_id=7, discord_name="Sub",
                                discord_avatar=None, status="going")
            _mark(session, event_id, 7, "present")

        record = services.attendance_record(session, 7)
        # Two events is a 50% swing per event -- a percentage here would be
        # noise dressed up as data.
        assert record["rate"] is None
        assert record["has_history"] is True
        assert record["present"] == 2


def test_attendance_rate_excludes_excused(client):
    with database.get_session() as session:
        marks = ["present", "present", "absent", "excused"]
        for i, mark in enumerate(marks):
            event_id = _seed_event(title=f"Game {i}",
                                   scheduled_at=datetime.utcnow() - timedelta(days=i + 1))
            event = services.get_event(session, event_id)
            services.set_signup(session, event, discord_user_id=9, discord_name="Keeper",
                                discord_avatar=None, status="going")
            _mark(session, event_id, 9, mark)

        record = services.attendance_record(session, 9)
        # 2 present of 3 counted -- the excused event is in neither half,
        # so telling staff in advance never hurts the figure.
        assert record["counted"] == 3
        assert record["rate"] == 67
        assert record["excused"] == 1

        batch = services.attendance_records_for(session, [9])
        assert batch[9] == record


def test_unmarked_events_are_not_absences(client):
    with database.get_session() as session:
        for i in range(4):
            event_id = _seed_event(title=f"Unmarked {i}",
                                   scheduled_at=datetime.utcnow() - timedelta(days=i + 1))
            event = services.get_event(session, event_id)
            services.set_signup(session, event, discord_user_id=11, discord_name="Ghost",
                                discord_avatar=None, status="going")
        record = services.attendance_record(session, 11)
        assert record["counted"] == 0 and record["rate"] is None and not record["has_history"]


# --- Tactics role resolution ------------------------------------------------ #
def test_role_comes_from_tactics_via_gamertag_link(client):
    event_id = _seed_event()
    with database.get_session() as session:
        event = services.get_event(session, event_id)
        services.set_signup(session, event, discord_user_id=55, discord_name="Ace",
                            discord_avatar=None, status="going")
        # The Tactics board stores an EA gamertag; the sign-up knows only a
        # Discord user. PlayerLink is the hop between them.
        session.add(TacticsSlot(formation="4-3-3", slot_key="ST", player_name="AceStriker"))
        session.commit()

        assert services.tactics_roles_for(session, [55], {"ST": "ST"}) == {}

        services.set_player_link(session, discord_user_id=55, player_name="AceStriker")
        assert services.tactics_roles_for(session, [55], {"ST": "ST"}) == {55: "ST"}


def test_gamertag_link_is_exclusive(client):
    with database.get_session() as session:
        services.set_player_link(session, discord_user_id=1, player_name="Shared")
        with pytest.raises(services.ServiceError):
            services.set_player_link(session, discord_user_id=2, player_name="shared")


def test_role_matching_is_case_insensitive(client):
    with database.get_session() as session:
        session.add(TacticsSlot(formation="4-3-3", slot_key="CM1", player_name="MidGeneral"))
        session.commit()
        services.set_player_link(session, discord_user_id=3, player_name="midgeneral")
        assert services.tactics_roles_for(session, [3], {"CM1": "CM"}) == {3: "CM"}


# --- Deleting ---------------------------------------------------------------- #
def test_deleting_an_event_removes_its_signups(client):
    event_id = _seed_event()
    with database.get_session() as session:
        event = services.get_event(session, event_id)
        services.set_signup(session, event, discord_user_id=1, discord_name="A",
                            discord_avatar=None, status="going")
        services.delete_event(session, event)
        assert services.list_signups(session, event_id) == []
