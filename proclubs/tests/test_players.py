"""Player files, club roles, coach notes, the login gate and the public
splash page.

Run with: pytest proclubs/tests/test_players.py
"""
import os
import re
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import app as appmod  # noqa: E402
import database  # noqa: E402
import roles  # noqa: E402
import services  # noqa: E402
from models import Contract  # noqa: E402


@pytest.fixture
def client():
    database.Base.metadata.drop_all(database.engine)
    with TestClient(appmod.app) as c:
        yield c


def _id(name):
    """The Discord ID the dev login gives a display name."""
    return str(zlib.crc32(name.encode()))


def _login(client, name, *, member=True, staff=False):
    data = {"name": name}
    if member:
        data["member"] = "1"
    if staff:
        data["staff"] = "1"
    client.post("/auth/dev", data=data, follow_redirects=False)
    return _id(name)


def _csrf(client, path):
    m = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert m, path
    return m.group(1)


def _player(discord_id, name, *, club_role=None):
    with database.get_session() as session:
        p = services.ensure_player(session, discord_id=discord_id, display_name=name)
        if club_role:
            services.set_club_role(session, p, club_role)


def _contract(discord_id, name, *, status="Starter", position="Striker"):
    with database.get_session() as session:
        services.create_contract(
            session, discord_id=discord_id, display_name=name, avatar_url=None,
            position=position, squad_status=status, weeks=8, source="recorded",
            created_by_name="Boss")


# --- The login gate ---------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/news", "/events", "/tactics", "/stats", "/players",
                                  "/clips", "/league", "/players/me"])
def test_members_pages_send_guests_to_sign_in(client, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_the_data_api_refuses_guests(client):
    r = client.get("/api/history/rivals")
    assert r.status_code == 401


@pytest.mark.parametrize("path", ["/", "/welcome", "/login"])
def test_the_public_pages_stay_public(client, path):
    assert client.get(path, follow_redirects=False).status_code == 200


def test_discord_can_still_fetch_article_covers(client):
    """Announcements point Discord at the cover image; Discord can't sign in."""
    r = client.get("/news/whatever/cover-image", follow_redirects=False)
    assert r.status_code != 303


def test_discord_can_still_deliver_button_presses(client):
    # Unsigned, so refused -- but by the signature check, not the gate.
    r = client.post("/discord/interactions", content=b"{}", follow_redirects=False)
    assert r.status_code == 401 and "location" not in r.headers


def test_the_next_page_is_only_ever_on_this_site(client):
    client.get("/login?next=//evil.example/x")
    r = client.post("/auth/dev", data={"name": "Fan", "member": "1"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_members_get_the_dashboard_not_the_splash(client):
    _login(client, "Fan")
    html = client.get("/").text
    assert "home-dash" in html and "welcome-hero" not in html
    # ...and can still preview the public page.
    assert "welcome-hero" in client.get("/welcome").text


# --- Access levels ----------------------------------------------------------- #
@pytest.mark.parametrize("kw, level", [
    (dict(signed_in=False, is_member=False, discord_staff=False, club_role=None), roles.GUEST),
    (dict(signed_in=True, is_member=False, discord_staff=False, club_role=None), roles.GUEST),
    (dict(signed_in=True, is_member=True, discord_staff=False, club_role=None), roles.MEMBER),
    (dict(signed_in=True, is_member=True, discord_staff=False, club_role="Coach"), roles.STAFF),
    (dict(signed_in=True, is_member=True, discord_staff=False, club_role="Head Coach"), roles.MANAGEMENT),
    (dict(signed_in=True, is_member=True, discord_staff=False, club_role="Club President"), roles.MANAGEMENT),
    (dict(signed_in=True, is_member=True, discord_staff=True, club_role=None), roles.MANAGEMENT),
])
def test_access_levels(kw, level):
    assert roles.access_level(**kw) == level


def test_a_coach_runs_matchday_but_not_contracts(client):
    uid = _login(client, "Coachy")
    _player(uid, "Coachy", club_role="Coach")
    assert client.get("/squad").status_code == 200
    assert client.get("/events/new").status_code == 200
    r = client.get("/roster")
    assert r.status_code == 403 and "club management" in r.text
    assert client.get("/club-profile").status_code == 403


def test_a_role_change_applies_on_the_next_page_load(client):
    """Read from the database per request, not frozen into the session at
    sign-in."""
    uid = _login(client, "Riser")
    assert client.get("/squad").status_code == 403
    _player(uid, "Riser", club_role="Head Coach")
    assert client.get("/roster", follow_redirects=False).status_code != 403


def test_signing_in_as_a_member_creates_a_player_file(client):
    uid = _login(client, "Newbie")
    client.get("/news")
    with database.get_session() as session:
        assert services.get_player(session, uid).display_name == "Newbie"


# --- Club roles -------------------------------------------------------------- #
def test_management_can_give_a_club_role(client):
    _player("42", "Cap")
    _login(client, "Boss", staff=True)
    token = _csrf(client, "/players/42")
    r = client.post("/players/42/role", data={"club_role": "Coach", "csrf_token": token})
    assert r.status_code == 200 and "Cap is now Coach." in r.text
    with database.get_session() as session:
        assert services.get_player(session, "42").club_role == "Coach"


def test_nobody_changes_their_own_club_role(client):
    uid = _login(client, "Prez")
    _player(uid, "Prez", club_role="Club President")
    token = _csrf(client, "/players/me")
    r = client.post(f"/players/{uid}/role", data={"club_role": "", "csrf_token": token})
    assert r.status_code == 400
    with database.get_session() as session:
        assert services.get_player(session, uid).club_role == "Club President"


def test_a_coach_cannot_hand_out_roles(client):
    _player("42", "Cap")
    uid = _login(client, "Coachy")
    _player(uid, "Coachy", club_role="Coach")
    token = _csrf(client, "/players/42")
    r = client.post("/players/42/role", data={"club_role": "Head Coach", "csrf_token": token})
    assert r.status_code == 403


def test_only_real_club_roles(client):
    _player("42", "Cap")
    _login(client, "Boss", staff=True)
    token = _csrf(client, "/players/42")
    r = client.post("/players/42/role", data={"club_role": "Kit Man", "csrf_token": token})
    assert r.status_code == 400


# --- Player files -------------------------------------------------------------- #
def test_the_squad_list_groups_players_by_status(client):
    _contract("1", "Ann", status="Starter")
    _contract("2", "Ben", status="Substitute")
    _player("3", "Cal", club_role="Head Coach")
    _login(client, "Fan")
    html = client.get("/players").text
    assert "Starting Players" in html and "Substitute Players" in html
    assert "Rotation Players" not in html       # nobody at that status
    assert "Cal" in html and "Head Coach" in html
    assert html.index("Ann") < html.index("Ben")


def test_members_see_each_others_stats_but_not_contracts(client):
    _contract("42", "Cap")
    _login(client, "Fan")
    html = client.get("/players/42").text
    assert "Season" in html                       # stats panel
    assert "Contract</h2>" not in html and "Attendance</h2>" not in html


def test_a_player_sees_their_own_contract(client):
    uid = _login(client, "Self")
    _contract(uid, "Self")
    html = client.get("/players/me", follow_redirects=True).text
    assert "Contract</h2>" in html and "8 weeks" in html and "Attendance</h2>" in html
    assert "Edit your profile" in html


def test_an_unknown_player_is_a_404(client):
    _login(client, "Fan")
    assert client.get("/players/999").status_code == 404
    assert client.get("/players/not-a-number").status_code == 404


def test_a_player_edits_only_their_own_profile(client):
    uid = _login(client, "Self")
    _player("42", "Cap")
    token = _csrf(client, "/players/me")
    r = client.post(f"/players/{uid}/profile", data={
        "preferred_foot": "Left", "archetype": "Box-to-box", "bio": "Engine room.",
        "csrf_token": token})
    assert "Engine room." in r.text and "Box-to-box" in r.text
    r = client.post("/players/42/profile", data={"bio": "hacked", "csrf_token": token})
    assert r.status_code == 400
    with database.get_session() as session:
        assert services.get_player(session, "42").bio is None


# --- Coach notes ------------------------------------------------------------- #
def _add_note(client, discord_id, body):
    token = _csrf(client, f"/players/{discord_id}")
    return client.post(f"/players/{discord_id}/notes", data={"body": body, "csrf_token": token})


def test_coach_notes_are_staff_only_even_from_the_player(client):
    uid = _login(client, "Self")
    _contract(uid, "Self")
    _login(client, "Coachy")
    _player(_id("Coachy"), "Coachy", club_role="Coach")
    assert _add_note(client, uid, "Drifts inside too early.").status_code == 200
    assert "Drifts inside too early." in client.get(f"/players/{uid}").text

    _login(client, "Self")
    html = client.get(f"/players/{uid}").text
    assert "Drifts inside too early." not in html and "Coach notes" not in html

    _login(client, "Fan")
    assert "Drifts inside too early." not in client.get(f"/players/{uid}").text


def test_staff_dont_see_notes_on_their_own_file(client):
    uid = _login(client, "PlayerCoach")
    _player(uid, "PlayerCoach", club_role="Coach")
    with database.get_session() as session:
        services.add_coach_note(session, discord_id=uid, body="About them.",
                                author={"id": 1, "name": "Gaffer"})
    html = client.get(f"/players/{uid}").text
    assert "About them." not in html and "Coach notes" not in html


def test_members_cannot_write_coach_notes(client):
    _player("42", "Cap")
    _login(client, "Fan")
    token = _csrf(client, "/players/me")
    r = client.post("/players/42/notes", data={"body": "sneaky", "csrf_token": token})
    assert r.status_code == 403


def test_a_note_is_deleted_by_its_author_or_management(client):
    _player("42", "Cap")
    for name in ("CoachA", "CoachB"):
        _player(_id(name), name, club_role="Coach")
    _login(client, "CoachA")
    _add_note(client, "42", "Note A")
    with database.get_session() as session:
        note_id = services.list_coach_notes(session, "42")[0].id

    _login(client, "CoachB")
    token = _csrf(client, "/players/42")
    assert client.post(f"/players/42/notes/{note_id}/delete",
                       data={"csrf_token": token}).status_code == 400
    _login(client, "Boss", staff=True)
    token = _csrf(client, "/players/42")
    client.post(f"/players/42/notes/{note_id}/delete", data={"csrf_token": token})
    with database.get_session() as session:
        assert services.list_coach_notes(session, "42") == []


# --- Contracts carry over -------------------------------------------------------- #
def test_existing_contracts_get_player_files_at_startup(client):
    with database.get_session() as session:
        session.add(Contract(discord_id="77", display_name="Old Guard", squad_status="Starter",
                             weeks=4, expires_at=services.datetime.utcnow()))
        session.commit()
        assert services.get_player(session, "77") is None
        assert services.backfill_players(session) == 1
        assert services.get_player(session, "77").display_name == "Old Guard"
        assert services.backfill_players(session) == 0


def test_reserve_contracts_become_substitutes(client):
    _contract("42", "Cap")
    with database.engine.begin() as conn:
        conn.execute(text("UPDATE contracts SET squad_status = 'Reserve'"))
    database.init_db()
    with database.get_session() as session:
        assert services.live_contract_for(session, "42").squad_status == "Substitute"


# --- The splash page and its profile ---------------------------------------- #
def test_the_splash_page_shows_the_club_and_what_it_needs(client):
    _contract("1", "Keeper", position="Goalkeeper")
    _player("2", "Gaffer", club_role="Head Coach")
    html = client.get("/").text
    assert "Players signed" in html and ">1<" in html
    assert "Gaffer" in html and "Head Coach" in html
    # Short of strikers for the default 4-3-3, not of keepers.
    looking = html[html.index("position-chips"):]
    looking = looking[:looking.index("</ul>")]
    assert "Striker" in looking and "Goalkeeper" not in looking


def test_management_edits_what_the_splash_page_says(client):
    _login(client, "Boss", staff=True)
    token = _csrf(client, "/club-profile")
    data = {k: "" for k in services.CLUB_PROFILE_FIELDS}
    data.update(headline="Tiki-taka on a Tuesday", match_nights="Tue & Thu 8pm ET",
                csrf_token=token)
    assert client.post("/club-profile", data=data).status_code == 200
    client.get("/logout")
    html = client.get("/").text
    assert "Tiki-taka on a Tuesday" in html and "Tue &amp; Thu 8pm ET" in html


def test_a_departure_ends_a_club_role(client, monkeypatch):
    """Leaving the club takes the role's access with it."""
    monkeypatch.setattr(appmod.config, "ROSTER_MOVES_ENABLED", True)
    monkeypatch.setattr(appmod.config, "ROSTER_ANNOUNCE_CHANNEL_ID", "555")
    monkeypatch.setattr(appmod.discord_roster, "fetch_guild_members", lambda: [
        {"nick": "Cap", "avatar": None, "roles": [],
         "user": {"id": "42", "username": "cap", "global_name": None, "avatar": None,
                  "discriminator": "0", "bot": False}},
    ])
    appmod.discord_roster.invalidate_members_cache()

    class _Posted:
        @staticmethod
        def json():
            return {"id": "msg-1"}
    monkeypatch.setattr(appmod.discord_roster.discord_api, "post", lambda path, json: _Posted())
    _player("42", "Cap", club_role="Coach")
    _login(client, "Boss", staff=True)
    token = _csrf(client, "/roster")
    client.post("/roster/announce", data={"discord_id": "42", "kind": "release",
                                          "csrf_token": token})
    with database.get_session() as session:
        assert services.get_player(session, "42").club_role is None
