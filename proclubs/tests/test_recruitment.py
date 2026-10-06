"""Recruitment: joining the server -> a file -> trial feedback (posted to
the recruitment channel) -> an offer from the file -> the signing ->
settling in.

Run with: pytest proclubs/tests/test_recruitment.py
"""
import json
import os
import re
import sys
import zlib
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from nacl.signing import SigningKey  # noqa: E402

import app as appmod  # noqa: E402
import config  # noqa: E402
import database  # noqa: E402
import discord_rsvp  # noqa: E402
import recruitment  # noqa: E402
import services  # noqa: E402
import staff_tools  # noqa: E402
from models import AvailabilityPattern, Event, Player  # noqa: E402

NOW = datetime(2026, 10, 6, 12, 0)


@pytest.fixture
def client():
    database.Base.metadata.drop_all(database.engine)
    with TestClient(appmod.app) as c:
        yield c


def _login(client, name="Coach", *, management=True):
    data = {"name": name, "member": "1", "staff": "1"} if management else {"name": name, "member": "1"}
    client.post("/auth/dev", data=data, follow_redirects=False)


def _csrf(client, path):
    m = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert m, path
    return m.group(1)


def _member(uid, name, joined, *, bot=False):
    return {"nick": name, "avatar": None, "roles": [], "joined_at": joined.isoformat() + "+00:00",
            "user": {"id": uid, "username": name.lower(), "global_name": None, "avatar": None,
                     "discriminator": "0", "bot": bot}}


class _R:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


@pytest.fixture
def discord(monkeypatch):
    """A configured bot, a server with a few members, and every post
    captured instead of sent."""
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "x")
    monkeypatch.setattr(config, "DISCORD_GUILD_ID", 999)
    monkeypatch.setattr(config, "ROSTER_MOVES_ENABLED", True)
    monkeypatch.setattr(config, "ROSTER_ANNOUNCE_CHANNEL_ID", "555")
    monkeypatch.setattr(config, "RECRUITMENT_CHANNEL_ID", "1546267802791452772")
    monkeypatch.setattr(config, "RECRUITMENT_FEEDBACK_ENABLED", True)
    monkeypatch.setattr(config, "NOTIFY_ENABLED", True)
    now = datetime.utcnow()
    members = [
        _member("42", "Cap", now - timedelta(days=2)),
        _member("43", "Sam", now - timedelta(days=60)),       # been here ages
        _member("44", "Bot", now - timedelta(days=1), bot=True),
        _member("45", "Dee", now - timedelta(days=5)),
    ]
    monkeypatch.setattr(appmod.discord_roster, "fetch_guild_members", lambda: members)
    appmod.discord_roster.invalidate_members_cache()
    posted = []

    def fake_post(path, json):
        posted.append((path, json))
        if path == "/users/@me/channels":
            return _R({"id": "dm-1"})
        return _R({"id": f"msg-{len(posted)}"})

    monkeypatch.setattr(appmod.discord_roster.discord_api, "post", fake_post)
    monkeypatch.setattr(appmod.discord_roster.discord_api, "put", lambda p: None)
    monkeypatch.setattr(appmod.discord_roster.discord_api, "get", lambda *a, **k: _R({"roles": []}))
    yield posted
    appmod.discord_roster.invalidate_members_cache()


# --- 1. New in the server ------------------------------------------------------- #
def test_new_arrivals_are_recent_humans_nobody_has_dealt_with():
    database.Base.metadata.drop_all(database.engine)
    database.init_db()
    members = [_member("1", "New", NOW - timedelta(days=1)), _member("2", "Old", NOW - timedelta(days=45)),
               _member("3", "Robot", NOW, bot=True), _member("4", "Filed", NOW - timedelta(days=3)),
               _member("5", "Signed", NOW - timedelta(days=3)), _member("6", "Gone", NOW - timedelta(days=3)),
               _member("7", "Newer", NOW - timedelta(hours=2))]
    with database.get_session() as session:
        recruitment.add_prospect(session, name="Filed", discord_id="4", gamertag="", position="",
                                 secondary="", source="", added_by="Coach")
        services.create_contract(session, discord_id="5", display_name="Signed", avatar_url=None,
                                 position="Striker", squad_status="Starter", weeks=8, source="recorded",
                                 created_by_name="Boss")
        recruitment.dismiss_arrival(session, "6", "Coach")
        arrivals = recruitment.new_arrivals(session, members, now=NOW)
    assert [a["name"] for a in arrivals] == ["Newer", "New"]


def test_staff_start_a_file_from_the_arrivals_list(client, discord):
    _login(client)
    html = client.get("/recruitment").text
    assert "New in the server" in html and "Cap" in html and "Dee" in html
    assert "Sam" not in html.split("New in the server")[1].split("pipeline")[0]
    token = _csrf(client, "/recruitment")
    r = client.post("/recruitment/arrivals/42/start", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    with database.get_session() as session:
        p = recruitment.find_by_discord_id(session, "42")
        assert p.name == "Cap" and p.stage == "prospect" and p.source == "Joined the Discord server"
    assert r.headers["location"] == f"/recruitment/{p.id}"

    client.post("/recruitment/arrivals/45/dismiss", data={"csrf_token": token})
    html = client.get("/recruitment").text
    assert "Nobody new to deal with" in html


def test_one_file_per_discord_account(client):
    database.init_db()
    with database.get_session() as session:
        recruitment.add_prospect(session, name="Cap", discord_id="42", gamertag="", position="",
                                 secondary="", source="", added_by="Coach")
        with pytest.raises(services.ServiceError, match="already has a file"):
            recruitment.add_prospect(session, name="Cap again", discord_id="42", gamertag="", position="",
                                     secondary="", source="", added_by="Coach")


# --- 3. Trial feedback goes to the recruitment channel --------------------------- #
def test_a_trial_note_is_posted_to_the_recruitment_channel(client, discord):
    _login(client, "Coach Bo")
    with database.get_session() as session:
        e = Event(title="Friendly vs Rovers", event_type="Match", scheduled_at=datetime.utcnow() - timedelta(hours=3))
        session.add(e)
        session.commit()
        p = recruitment.add_prospect(session, name="Cap", discord_id="42", gamertag="Cap_GT",
                                     position="Striker", secondary="", source="", added_by="Coach")
        pid, eid = p.id, e.id
    token = _csrf(client, f"/recruitment/{pid}")
    r = client.post(f"/recruitment/{pid}/notes", data={
        "body": "Sharp movement, @everyone should see this", "rating": "8", "event_id": str(eid),
        "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303

    path, body = discord[-1]
    assert path == "/channels/1546267802791452772/messages"
    assert body["allowed_mentions"] == {"parse": []}, "a note can never ping anybody"
    embed = body["embeds"][0]
    assert embed["title"] == "Trial feedback — Cap"
    assert "Sharp movement" in embed["description"] and "Striker" in embed["description"]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Rating"] == "**8/10**"
    assert fields["Stage"] == "On trial", "a note from a match moves them onto trial"
    assert "Friendly vs Rovers" in fields["From"]
    assert embed["footer"]["text"] == "From Coach Bo"
    assert embed["url"].endswith(f"/recruitment/{pid}")
    with database.get_session() as session:
        note = recruitment.notes_for(session, pid)[0][0]
        assert note.discord_message_id == f"msg-{len(discord)}"
    assert "Posted to Discord" in client.get(f"/recruitment/{pid}").text


def test_a_failed_post_still_saves_the_note(client, discord, monkeypatch):
    _login(client)
    with database.get_session() as session:
        pid = recruitment.add_prospect(session, name="Cap", discord_id="", gamertag="", position="",
                                       secondary="", source="", added_by="Coach").id

    def boom(path, json):
        raise appmod.discord_roster.DiscordApiError("403 Missing Access")
    monkeypatch.setattr(appmod.discord_roster.discord_api, "post", boom)
    token = _csrf(client, f"/recruitment/{pid}")
    client.post(f"/recruitment/{pid}/notes", data={"body": "Decent", "csrf_token": token})
    html = client.get(f"/recruitment/{pid}").text
    assert "Decent" in html
    with database.get_session() as session:
        assert recruitment.notes_for(session, pid)[0][0].discord_message_id is None


def test_no_channel_means_no_post(client, discord, monkeypatch):
    monkeypatch.setattr(config, "RECRUITMENT_FEEDBACK_ENABLED", False)
    _login(client)
    with database.get_session() as session:
        pid = recruitment.add_prospect(session, name="Cap", discord_id="", gamertag="", position="",
                                       secondary="", source="", added_by="Coach").id
    token = _csrf(client, f"/recruitment/{pid}")
    before = len(discord)
    client.post(f"/recruitment/{pid}/notes", data={"body": "Fine", "csrf_token": token})
    assert not [p for p, _ in discord[before:] if "1546267802791452772" in p]


# --- 4-6. Offer from the file, signing, settling in ----------------------------- #
@pytest.fixture
def discord_key(monkeypatch):
    key = SigningKey.generate()
    monkeypatch.setattr(config, "DISCORD_PUBLIC_KEY", bytes(key.verify_key).hex())
    return key


def _press(client, key, custom_id, user_id):
    body = json.dumps({"type": discord_rsvp.INTERACTION_MESSAGE_COMPONENT, "data": {"custom_id": custom_id},
                       "member": {"user": {"id": str(user_id), "username": "cap", "avatar": None}}}).encode()
    ts = "1700000000"
    return client.post("/discord/interactions", content=body, headers={
        "X-Signature-Ed25519": key.sign(ts.encode() + body).signature.hex(),
        "X-Signature-Timestamp": ts, "Content-Type": "application/json"})


def test_from_offer_to_settled_in(client, discord, discord_key):
    _login(client)
    with database.get_session() as session:
        p = recruitment.add_prospect(session, name="Cap", discord_id="42", gamertag="Cap_GT",
                                     position="Striker", secondary="", source="", added_by="Coach")
        recruitment.set_stage(session, p, "trial")
        pid = p.id
    token = _csrf(client, f"/recruitment/{pid}")
    assert "Offer a contract" in client.get(f"/recruitment/{pid}").text

    r = client.post(f"/recruitment/{pid}/offer", data={
        "position": "Striker", "contract_weeks": "8", "squad_status": "Rotation", "csrf_token": token},
        follow_redirects=False)
    assert r.headers["location"] == f"/recruitment/{pid}"
    offer_path, offer_body = [x for x in discord if x[0] == "/channels/555/messages"][-1]
    assert offer_body["components"], "answerable in Discord"
    with database.get_session() as session:
        p = recruitment.get_prospect(session, pid)
        assert p.stage == "offered"
        move = recruitment.latest_offer(session, p)
        assert recruitment.offer_state(move) == "waiting"
        move_id = move.id
    assert "Waiting on their answer" in client.get(f"/recruitment/{pid}").text
    # A second offer while one is open is refused.
    r = client.post(f"/recruitment/{pid}/offer", data={
        "position": "Striker", "contract_weeks": "8", "squad_status": "Rotation", "csrf_token": token})
    assert "already has an offer" in r.text

    _press(client, discord_key, f"roster:accepted:{move_id}", 42)
    html = client.get(f"/recruitment/{pid}").text
    assert "Confirm signing" in html and 'value="Cap_GT"' in html

    r = client.post(f"/roster/{move_id}/confirm", data={
        "csrf_token": token, "player_name": "Cap_GT", "next": f"/recruitment/{pid}"}, follow_redirects=False)
    assert r.headers["location"] == f"/recruitment/{pid}"
    with database.get_session() as session:
        p = recruitment.get_prospect(session, pid)
        assert p.stage == "signed" and p.signed_at is not None
        steps = {s["key"]: s["done"] for s in recruitment.onboarding(session, p)}
    assert steps == {"contract": True, "gamertag": True, "ea_club": False, "welcome": False,
                     "nights": False, "goal": False}

    # The welcome DM ticks itself off.
    client.post(f"/recruitment/{pid}/welcome", data={"csrf_token": token})
    dm = [b for path, b in discord if path == "/channels/dm-1/messages"][-1]
    assert "Welcome to" in dm["content"] and "/availability" in dm["content"]
    client.post(f"/recruitment/{pid}/onboarding", data={"step": "ea_club", "done": "1", "csrf_token": token})
    with pytest.raises(services.ServiceError):
        with database.get_session() as session:
            recruitment.tick(session, recruitment.get_prospect(session, pid), "contract", True)
    with database.get_session() as session:
        session.add(AvailabilityPattern(discord_id="42", days="1010100"))
        session.commit()
        p = recruitment.get_prospect(session, pid)
        assert recruitment.progress(recruitment.onboarding(session, p)) == (5, 6)
        assert [x.name for x, _ in recruitment.settling_in(session)] == ["Cap"]
        items = staff_tools.action_items(session, management=True)
    assert any("Still settling in: Cap" in i["text"] for i in items)


def test_an_offer_from_roster_moves_the_file_along(client, discord):
    _login(client)
    with database.get_session() as session:
        pid = recruitment.add_prospect(session, name="Cap", discord_id="42", gamertag="", position="",
                                       secondary="", source="", added_by="Coach").id
    token = _csrf(client, "/roster")
    client.post("/roster/announce", data={"discord_id": "42", "kind": "offer", "contract_weeks": "8",
                                          "squad_status": "Starter", "position": "Striker", "csrf_token": token})
    with database.get_session() as session:
        assert recruitment.get_prospect(session, pid).stage == "offered"


def test_confirm_only_redirects_back_to_a_recruitment_file(client, discord, discord_key):
    _login(client)
    token = _csrf(client, "/roster")
    client.post("/roster/announce", data={"discord_id": "42", "kind": "offer", "contract_weeks": "8",
                                          "squad_status": "Starter", "position": "Striker", "csrf_token": token})
    with database.get_session() as session:
        move_id = services.recent_roster_moves(session)[0].id
    _press(client, discord_key, f"roster:accepted:{move_id}", 42)
    r = client.post(f"/roster/{move_id}/confirm", data={"csrf_token": token, "next": "https://evil.example"},
                    follow_redirects=False)
    assert r.headers["location"] == "/roster"


def test_a_coach_can_note_but_only_management_can_offer(client, discord):
    with database.get_session() as session:
        session.add(Player(discord_id=str(zlib.crc32(b"Kay")), display_name="Kay", club_role="Coach"))
        session.commit()
        pid = recruitment.add_prospect(session, name="Cap", discord_id="42", gamertag="", position="Striker",
                                       secondary="", source="", added_by="Coach").id
    _login(client, "Kay", management=False)
    html = client.get(f"/recruitment/{pid}").text
    assert "Management send contract offers" in html and "Offer a contract" not in html
    token = _csrf(client, f"/recruitment/{pid}")
    client.post(f"/recruitment/{pid}/notes", data={"body": "Good feet", "csrf_token": token})
    assert any(path.endswith("1546267802791452772/messages") for path, _ in discord)
    r = client.post(f"/recruitment/{pid}/offer", data={
        "position": "Striker", "contract_weeks": "8", "squad_status": "Rotation", "csrf_token": token},
        follow_redirects=False)
    assert r.status_code == 403


def test_members_cannot_see_recruitment(client):
    _login(client, "Fan", management=False)
    assert client.get("/recruitment", follow_redirects=False).status_code == 403
