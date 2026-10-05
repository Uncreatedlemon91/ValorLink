"""Managed Discord roles: the site decides who holds them, and nothing
outside the configured list is ever touched.

Run with: pytest proclubs/tests/test_role_sync.py
"""
import os
import re
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import config  # noqa: E402
import database  # noqa: E402
import discord_roster  # noqa: E402
import recruitment  # noqa: E402
import role_settings  # noqa: E402
import role_sync  # noqa: E402
import services  # noqa: E402

ROLES = {"Starter": "1548912106928087091", "Rotation": "222", "Substitute": "333",
         "Coach": "444", "Head Coach": "555", "Trialist": "1535705667925446706", "Squad": "777"}
UNRELATED = "999"
BOT_ID = "8000"


def _server_roles():
    """The guild's roles as Discord lists them. The bot's own role (8) sits
    at position 50; "Owners" is above it, "BotRole" belongs to an integration."""
    named = {"Starter": "Starters", "Rotation": "Rotation", "Substitute": "Subs", "Coach": "Coaches",
             "Head Coach": "Head Coach", "Trialist": "Trialists", "Squad": "Squad"}
    roles = [{"id": ROLES[k], "name": n, "position": 10 + i, "managed": False, "color": 0}
             for i, (k, n) in enumerate(named.items())]
    return roles + [{"id": "1", "name": "@everyone", "position": 0, "managed": False, "color": 0},
                    {"id": "8", "name": "YeeHaw Bot", "position": 50, "managed": True, "color": 0},
                    {"id": "60", "name": "Owners", "position": 60, "managed": False, "color": 0},
                    {"id": "61", "name": "Fresh Role", "position": 5, "managed": False, "color": 0},
                    {"id": UNRELATED, "name": "Gamers", "position": 3, "managed": False, "color": 0}]


@pytest.fixture
def client():
    database.Base.metadata.drop_all(database.engine)
    with TestClient(appmod.app) as c:
        yield c


@pytest.fixture
def discord(monkeypatch):
    """A fake guild: member roles by user id, and the calls made."""
    state = {"members": {BOT_ID: {"8"}}, "calls": [], "server_roles": _server_roles()}
    monkeypatch.setattr(role_settings, "_bot_user_id", None)
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "bot-token")
    monkeypatch.setattr(config, "DISCORD_GUILD_ID", 1)
    monkeypatch.setattr(config, "DISCORD_STAFF_ROLE_ID", 0)
    monkeypatch.setattr(config, "MANAGED_ROLE_SETTINGS", {k: v for k, v in ROLES.items() if k != "Squad"})
    monkeypatch.setattr(config, "ROSTER_SQUAD_ROLE_ID", ROLES["Squad"])

    class _Resp:
        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    def get(path, params=None):
        if path.endswith("/roles"):
            return _Resp(state["server_roles"])
        if path == "/users/@me":
            return _Resp({"id": BOT_ID})
        uid = path.rsplit("/", 1)[1]
        if uid not in state["members"]:
            raise role_sync.DiscordApiError("404 Not Found")
        return _Resp({"roles": sorted(state["members"][uid])})

    def put(path):
        uid, role = path.split("/members/")[1].split("/roles/")
        state["calls"].append(("add", uid, role))
        state["members"].setdefault(uid, set()).add(role)

    def delete(path):
        uid, role = path.split("/members/")[1].split("/roles/")
        state["calls"].append(("remove", uid, role))
        state["members"].get(uid, set()).discard(role)

    monkeypatch.setattr(role_sync.discord_api, "get", get)
    monkeypatch.setattr(role_sync.discord_api, "put", put)
    monkeypatch.setattr(role_sync.discord_api, "delete", delete)
    monkeypatch.setattr(discord_roster, "invalidate_members_cache", lambda: None)
    monkeypatch.setattr(discord_roster, "fetch_guild_members", lambda: [])
    return state


def _contract(uid, status="Starter"):
    with database.get_session() as session:
        services.create_contract(session, discord_id=uid, display_name=f"P{uid}", avatar_url=None,
                                 position="Striker", squad_status=status, weeks=8,
                                 source="recorded", created_by_name="Boss")


def _id(name):
    return str(zlib.crc32(name.encode()))


def _csrf(client, path):
    m = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert m, path
    return m.group(1)


# --- What the site says --------------------------------------------------------- #
def test_the_staff_role_is_never_managed_whatever_is_configured():
    ids = config.managed_role_ids({"Starter": "10", "Coach": "20", "Junk": "abc", "Blank": ""}, 20)
    assert ids == {"Starter": "10"}


def test_desired_roles_follow_the_contract_club_role_and_trial(client, discord):
    _contract("1", "Rotation")
    with database.get_session() as session:
        assert role_sync.desired_keys(session, "1") == {"Squad", "Rotation"}
        p = services.ensure_player(session, discord_id="2", display_name="Gaffer")
        services.set_club_role(session, p, "Coach")
        assert role_sync.desired_keys(session, "2") == {"Coach"}
        prospect = recruitment.add_prospect(session, name="Dee", discord_id="3", gamertag="",
                                            position="", secondary="", source="", added_by="x")
        assert role_sync.desired_keys(session, "3") == set()      # a prospect isn't a trialist yet
        recruitment.set_stage(session, prospect, "trial")
        assert role_sync.desired_keys(session, "3") == {"Trialist"}
    _contract("3", "Starter")                                       # signed: no longer a trialist
    with database.get_session() as session:
        assert role_sync.desired_keys(session, "3") == {"Squad", "Starter"}


# --- Syncing a member ----------------------------------------------------------- #
def test_a_status_change_swaps_the_role_and_leaves_others_alone(client, discord):
    discord["members"]["1"] = {ROLES["Starter"], ROLES["Squad"], UNRELATED}
    _contract("1", "Rotation")
    with database.get_session() as session:
        result = role_sync.sync_member(session, "1")
    assert result == {"added": ["Rotation"], "removed": ["Starter"], "in_server": True}
    assert discord["members"]["1"] == {ROLES["Rotation"], ROLES["Squad"], UNRELATED}


def test_somebody_not_in_the_server_is_left_alone(client, discord):
    _contract("1")
    with database.get_session() as session:
        assert role_sync.sync_member(session, "1")["in_server"] is False
    assert discord["calls"] == []


def test_nothing_happens_when_sync_is_off(client, discord, monkeypatch):
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "")
    discord["members"]["1"] = {ROLES["Starter"]}
    with database.get_session() as session:
        role_sync.sync_member(session, "1")
    assert discord["calls"] == []


def test_a_discord_failure_is_reported_not_raised(client, discord, monkeypatch):
    discord["members"]["1"] = set()
    _contract("1")

    def refuse(path):
        raise role_sync.DiscordApiError("403 Missing Permissions")
    monkeypatch.setattr(role_sync.discord_api, "put", refuse)
    problem = role_sync.sync_quietly("1")
    assert "couldn't be updated" in problem and "Manage Roles" in problem


# --- From the site's own actions ------------------------------------------------------ #
def test_giving_a_club_role_gives_the_discord_role(client, discord):
    discord["members"]["42"] = set()
    with database.get_session() as session:
        services.ensure_player(session, discord_id="42", display_name="Cap")
    client.post("/auth/dev", data={"name": "Boss", "member": "1", "staff": "1"})
    token = _csrf(client, "/players/42")
    client.post("/players/42/role", data={"club_role": "Head Coach", "csrf_token": token})
    assert discord["members"]["42"] == {ROLES["Head Coach"]}
    token = _csrf(client, "/players/42")
    client.post("/players/42/role", data={"club_role": "", "csrf_token": token})
    assert discord["members"]["42"] == set()


def test_moving_a_prospect_onto_trial_gives_the_trialist_role(client, discord):
    discord["members"]["55"] = set()
    with database.get_session() as session:
        p = recruitment.add_prospect(session, name="Dee", discord_id="55", gamertag="",
                                     position="", secondary="", source="", added_by="x")
        pid = p.id
    client.post("/auth/dev", data={"name": "Coach", "member": "1", "staff": "1"})
    token = _csrf(client, f"/recruitment/{pid}")
    client.post(f"/recruitment/{pid}/stage", data={"stage": "trial", "csrf_token": token})
    assert discord["members"]["55"] == {ROLES["Trialist"]}
    token = _csrf(client, f"/recruitment/{pid}")
    client.post(f"/recruitment/{pid}/stage", data={"stage": "rejected", "csrf_token": token})
    assert discord["members"]["55"] == set()


# --- The whole server ------------------------------------------------------------------ #
def _member(uid, name, roles):
    return {"nick": name, "avatar": None, "roles": list(roles),
            "user": {"id": uid, "username": name.lower(), "global_name": None, "avatar": None,
                     "discriminator": "0", "bot": False}}


def test_the_server_page_previews_then_applies(client, discord, monkeypatch):
    _contract("1", "Starter")                                          # should hold Starter + Squad
    discord["members"] = {"1": set(), "2": {ROLES["Starter"], UNRELATED}, "3": {UNRELATED}}
    monkeypatch.setattr(discord_roster, "fetch_guild_members", lambda: [
        _member(uid, f"Member{uid}x", roles) for uid, roles in discord["members"].items()])
    client.post("/auth/dev", data={"name": "Boss", "member": "1", "staff": "1"})
    html = client.get("/discord-roles").text
    assert "Member1x" in html and "Member2x" in html and "Member3x" not in html         # 3 holds nothing managed
    assert discord["calls"] == []                                      # previewing changes nothing
    token = _csrf(client, "/discord-roles")
    r = client.post("/discord-roles/apply", data={"csrf_token": token})
    assert "brought in line for 2 member(s)" in r.text
    assert discord["members"]["1"] == {ROLES["Starter"], ROLES["Squad"]}
    assert discord["members"]["2"] == {UNRELATED}                      # unrelated role untouched


def test_only_management_sees_the_server_page(client, discord):
    with database.get_session() as session:
        p = services.ensure_player(session, discord_id=_id("Coachy"), display_name="Coachy")
        services.set_club_role(session, p, "Coach")
    client.post("/auth/dev", data={"name": "Coachy", "member": "1"})
    assert client.get("/discord-roles").status_code == 403


# --- Which role is which, set on the site ---------------------------------------- #
def _settings_form(**overrides):
    values = {f"role__{k}": v for k, v in ROLES.items()}
    values.update({f"role__{k}": v for k, v in overrides.items()})
    return values


def test_the_page_offers_the_servers_roles(client, discord):
    client.post("/auth/dev", data={"name": "Boss", "member": "1", "staff": "1"})
    html = client.get("/discord-roles").text
    assert '<select id="role__Starter" name="role__Starter">' in html
    assert "Fresh Role" in html and "@everyone" not in html
    assert "belongs to an integration" in html                       # shown, but can't be picked
    assert "read from .env" in html                                  # nothing saved on the site yet


def test_a_saved_role_beats_the_env_and_blank_turns_it_off(client, discord):
    with database.get_session() as session:
        role_settings.save(session, {**ROLES, "Starter": "61", "Trialist": ""}, by_name="Boss")
        assert role_settings.current(session)["Starter"] == {"value": "61", "source": "site"}
        ids = role_sync.managed(session)
    assert ids["Starter"] == "61" and "Trialist" not in ids
    discord["members"]["1"] = set()
    _contract("1", "Starter")
    with database.get_session() as session:
        role_sync.sync_member(session, "1")
    assert discord["members"]["1"] == {"61", ROLES["Squad"]}


def test_saving_through_the_page(client, discord):
    client.post("/auth/dev", data={"name": "Boss", "member": "1", "staff": "1"})
    token = _csrf(client, "/discord-roles")
    r = client.post("/discord-roles/settings", data={**_settings_form(Starter="61"), "csrf_token": token})
    assert "Discord roles saved." in r.text
    assert "no longer manages the old Starter role" in r.text          # the old one isn't stripped
    with database.get_session() as session:
        assert role_sync.managed(session)["Starter"] == "61"


@pytest.mark.parametrize("overrides, message", [
    ({"Starter": "123"}, "a role in the server"),
    ({"Starter": "8"}, "belongs to a bot or integration"),
    ({"Starter": "abc"}, "should be a Discord role ID"),
    ({"Starter": "1"}, "be @everyone"),
    ({"Rotation": ROLES["Starter"]}, "set to the same role"),
    ({"Coach": "4242"}, "Discord staff role"),
])
def test_the_page_refuses_roles_that_cant_work(client, discord, monkeypatch, overrides, message):
    monkeypatch.setattr(config, "DISCORD_STAFF_ROLE_ID", 4242)
    client.post("/auth/dev", data={"name": "Boss", "member": "1", "staff": "1"})
    token = _csrf(client, "/discord-roles")
    r = client.post("/discord-roles/settings", data={**_settings_form(**overrides), "csrf_token": token})
    assert "Not saved" in r.text and message in r.text
    with database.get_session() as session:
        assert role_settings.current(session)["Starter"]["source"] == "env"   # nothing written


def test_a_role_above_the_bot_is_flagged(client, discord):
    with database.get_session() as session:
        role_settings.save(session, {**ROLES, "Starter": "60"}, by_name="Boss")
    client.post("/auth/dev", data={"name": "Boss", "member": "1", "staff": "1"})
    assert "own role sits below this one" in client.get("/discord-roles").text


def test_without_the_bot_the_roles_are_typed(client, discord, monkeypatch):
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "")
    client.post("/auth/dev", data={"name": "Boss", "member": "1", "staff": "1"})
    html = client.get("/discord-roles").text
    assert 'name="role__Starter" value="1548912106928087091"' in html
    token = _csrf(client, "/discord-roles")
    client.post("/discord-roles/settings", data={**_settings_form(Starter="5555"), "csrf_token": token})
    with database.get_session() as session:
        assert role_settings.current(session)["Starter"]["value"] == "5555"


def test_only_management_saves_roles(client, discord):
    with database.get_session() as session:
        p = services.ensure_player(session, discord_id=_id("Coachy"), display_name="Coachy")
        services.set_club_role(session, p, "Coach")
    client.post("/auth/dev", data={"name": "Coachy", "member": "1"})
    token = _csrf(client, "/set-pieces")
    r = client.post("/discord-roles/settings", data={**_settings_form(Starter="61"), "csrf_token": token})
    assert r.status_code == 403
