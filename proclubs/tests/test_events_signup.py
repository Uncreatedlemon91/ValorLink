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


# --- Formations and position claims ----------------------------------------- #
def _seed_formation_event(formation="4-3-3") -> int:
    with database.get_session() as session:
        event = services.create_event(
            session, title="Cup Tie", event_type="Match",
            scheduled_at=datetime.utcnow() + timedelta(days=1), opponent="Rivals FC",
            description="", image=None, staff_name="Coach", formation=formation,
        )
        return event.id


def test_claiming_a_position_signs_you_up(client):
    event_id = _seed_formation_event()
    with database.get_session() as session:
        event = services.get_event(session, event_id)
        signup = services.claim_slot(session, event, discord_user_id=1, discord_name="Keeper",
                                     discord_avatar=None, slot_key="GK")
        # Picking where you'll play and saying you'll be there are the same
        # statement -- there is no separate "going" step to forget.
        assert signup.status == "going" and signup.slot_key == "GK"
        assert services.signup_counts(session, event_id)["going"] == 1


def test_one_player_per_position(client):
    event_id = _seed_formation_event()
    with database.get_session() as session:
        event = services.get_event(session, event_id)
        services.claim_slot(session, event, discord_user_id=1, discord_name="Keeper",
                            discord_avatar=None, slot_key="GK")
        with pytest.raises(services.ServiceError) as exc:
            services.claim_slot(session, event, discord_user_id=2, discord_name="Other",
                                discord_avatar=None, slot_key="GK")
        assert "Keeper" in str(exc.value)


def test_moving_position_releases_the_old_one(client):
    event_id = _seed_formation_event()
    with database.get_session() as session:
        event = services.get_event(session, event_id)
        services.claim_slot(session, event, discord_user_id=1, discord_name="Utility",
                            discord_avatar=None, slot_key="LB")
        services.claim_slot(session, event, discord_user_id=1, discord_name="Utility",
                            discord_avatar=None, slot_key="ST")
        claimed = services.claimed_slots(session, event_id)
        assert "LB" not in claimed and claimed["ST"].discord_user_id == 1
        assert len(services.list_signups(session, event_id)) == 1


def test_answering_out_frees_the_shirt(client):
    event_id = _seed_formation_event()
    with database.get_session() as session:
        event = services.get_event(session, event_id)
        services.claim_slot(session, event, discord_user_id=1, discord_name="Winger",
                            discord_avatar=None, slot_key="LW")
        services.set_signup(session, event, discord_user_id=1, discord_name="Winger",
                            discord_avatar=None, status="out")
        # Holding a position while saying you can't make it would block a
        # slot nobody can see is free.
        assert services.claimed_slots(session, event_id) == {}
        assert services.get_signup(session, event_id, 1).slot_key is None


def test_unknown_slot_is_refused(client):
    event_id = _seed_formation_event("4-3-3")
    with database.get_session() as session:
        event = services.get_event(session, event_id)
        # CDM1 exists in 4-2-3-1 but not in 4-3-3.
        with pytest.raises(services.ServiceError):
            services.claim_slot(session, event, discord_user_id=1, discord_name="X",
                                discord_avatar=None, slot_key="CDM1")


def test_changing_formation_releases_claims(client):
    event_id = _seed_formation_event("4-3-3")
    with database.get_session() as session:
        event = services.get_event(session, event_id)
        services.claim_slot(session, event, discord_user_id=1, discord_name="Mid",
                            discord_avatar=None, slot_key="CM1")
        services.update_event(session, event, title=event.title, event_type=event.event_type,
                              scheduled_at=event.scheduled_at, opponent=event.opponent,
                              description=None, image=None, result=None, formation="4-2-3-1")
        # Slot keys are formation-specific, so the claim can't survive -- but
        # the player stays signed up rather than being silently dropped.
        assert services.claimed_slots(session, event_id) == {}
        assert services.get_signup(session, event_id, 1).status == "going"


def test_event_without_formation_keeps_plain_rsvp(client):
    event_id = _seed_event()
    with database.get_session() as session:
        event = services.get_event(session, event_id)
        assert services.event_slots(event) == {}
        with pytest.raises(services.ServiceError):
            services.claim_slot(session, event, discord_user_id=1, discord_name="X",
                                discord_avatar=None, slot_key="GK")


def test_discord_select_claims_a_position(client, discord_key):
    event_id = _seed_formation_event()
    r = _signed_request(client, {
        "type": discord_rsvp.INTERACTION_MESSAGE_COMPONENT,
        "data": {"custom_id": f"rsvpslot:{event_id}", "component_type": 3, "values": ["ST"]},
        "member": {"user": {"id": "77", "global_name": "Poacher"}},
    }, key=discord_key)
    assert r.status_code == 200
    assert r.json()["type"] == discord_rsvp.RESPONSE_UPDATE_MESSAGE
    with database.get_session() as session:
        signup = services.get_signup(session, event_id, 77)
        assert signup.slot_key == "ST" and signup.status == "going"
        assert signup.source == "discord"


def test_discord_select_on_taken_position_is_ephemeral(client, discord_key):
    event_id = _seed_formation_event()
    with database.get_session() as session:
        event = services.get_event(session, event_id)
        services.claim_slot(session, event, discord_user_id=1, discord_name="First",
                            discord_avatar=None, slot_key="GK")
    r = _signed_request(client, {
        "type": discord_rsvp.INTERACTION_MESSAGE_COMPONENT,
        "data": {"custom_id": f"rsvpslot:{event_id}", "component_type": 3, "values": ["GK"]},
        "member": {"user": {"id": "88", "global_name": "TooSlow"}},
    }, key=discord_key)
    assert r.status_code == 200
    # Ephemeral, so only the loser of the race sees it and the post is left
    # showing the state that actually won.
    assert r.json()["data"]["flags"] == 64
    assert "First" in r.json()["data"]["content"]


def test_taken_positions_drop_out_of_the_discord_picker(client):
    event_id = _seed_formation_event()
    with database.get_session() as session:
        event = services.get_event(session, event_id)
        services.claim_slot(session, event, discord_user_id=1, discord_name="Keeper",
                            discord_avatar=None, slot_key="GK")
        signups = services.list_signups(session, event_id)
        slots = services.event_slots(event)
        components = discord_rsvp.build_components(event, signups, slots)

    select = components[0]["components"][0]
    values = [o["value"] for o in select["options"]]
    assert "GK" not in values and "ST" in values
    # No "Going" button alongside the picker -- picking a shirt is the sign-up.
    buttons = [c["custom_id"] for c in components[1]["components"]]
    assert not any(b.endswith(":going") for b in buttons)


def test_full_squad_drops_the_picker_entirely(client):
    """Discord rejects a select with zero options, so a fully-claimed squad
    must lose the picker rather than render an empty one."""
    event_id = _seed_formation_event()
    with database.get_session() as session:
        event = services.get_event(session, event_id)
        slots = services.event_slots(event)
        for i, key in enumerate(slots):
            services.claim_slot(session, event, discord_user_id=100 + i,
                                discord_name=f"P{i}", discord_avatar=None, slot_key=key)
        signups = services.list_signups(session, event_id)
        components = discord_rsvp.build_components(event, signups, slots)

    assert all(c["components"][0]["type"] != 3 for c in components)
    assert len(components) == 1  # just the maybe/out row


def test_claimed_shirt_beats_tactics_board_on_the_page(client):
    """The Tactics board is the default lineup; a claim is what they signed
    up to play in this specific match, so the claim wins."""
    event_id = _seed_formation_event()
    with database.get_session() as session:
        event = services.get_event(session, event_id)
        session.add(TacticsSlot(formation="4-3-3", slot_key="GK", player_name="Utility"))
        session.commit()
        services.set_player_link(session, discord_user_id=5, player_name="Utility")
        services.claim_slot(session, event, discord_user_id=5, discord_name="Utility",
                            discord_avatar=None, slot_key="ST")

    _login_staff(client)
    page = client.get(f"/events/{event_id}").text
    assert "Cup Tie" in page
    with database.get_session() as session:
        event = services.get_event(session, event_id)
        view = appmod._event_view(session, event, None)
    assert view["roles"][5] == "ST"          # claimed shirt
    assert view["tactics_roles"][5] == "GK"  # board default, still available
