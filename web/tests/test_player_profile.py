"""Tests for the platform-wide player profile.

The self-authored half (bio, timezone, availability, in-game names, links) is
one registry row per Discord identity; the service half is still assembled
per-unit. These tests cover the split, the one-time import of the old
per-unit columns, and the extra service data now surfaced on /u/.

Runs under pytest or directly:  python -m web.tests.test_player_profile
"""
import os
import re
import tempfile

_TMP = tempfile.mkdtemp(prefix="valorlink-pp-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/default.db"
os.environ["REGISTRY_DATABASE_URL"] = f"sqlite:///{_TMP}/registry.db"
os.environ["UNIT_DB_DIR"] = f"{_TMP}/units"
os.environ["PLATFORM_BASE_DOMAIN"] = "valorlink.co"
os.environ["PLATFORM_DEFAULT_SLUG"] = "hq"
os.environ["WEB_DEV_LOGIN"] = "1"
os.environ["WEB_SESSION_SECRET"] = "test-secret"

import config  # noqa: E402
config.DATABASE_URL = os.environ["DATABASE_URL"]

from fastapi.testclient import TestClient  # noqa: E402

from db.base import Base, engine  # noqa: E402
from db.models import Assignment, Member, MemberAssignment  # noqa: E402
from tenancy import player_profiles  # noqa: E402
from tenancy.registry import (  # noqa: E402
    PlayerProfile,
    RegistryBase,
    Tenant,
    registry_engine,
    registry_session,
)
from tenancy.resolve import ensure_default_tenant  # noqa: E402
from tenancy.units import (  # noqa: E402
    engine_for,
    provision_unit_db,
    sessionmaker_for,
    unit_db_url_for_slug,
)
from utils.settings import get_config  # noqa: E402
from web.app import app  # noqa: E402

APEX = "valorlink.co"
UNITS = ("5thva", "2ndus")
PLAYER = 4242


def _add_unit(slug, name, game):
    db_url = unit_db_url_for_slug(slug)
    provision_unit_db(db_url)
    with registry_session() as s:
        s.add(Tenant(slug=slug, name=name, discord_guild_id=abs(hash(slug)) % 10**6,
                     db_url=db_url, game=game))
        s.commit()
    with sessionmaker_for(db_url)() as s:
        get_config(s).regiment_name = name
        s.commit()
    return db_url


def _reset():
    Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
    RegistryBase.metadata.drop_all(registry_engine)
    RegistryBase.metadata.create_all(registry_engine)
    for slug in UNITS:
        Base.metadata.drop_all(engine_for(unit_db_url_for_slug(slug)))
    with sessionmaker_for(os.environ["DATABASE_URL"])() as s:
        get_config(s).regiment_name = "Default Headquarters"
        s.commit()
    with registry_session() as s:
        ensure_default_tenant(s, name="Default Headquarters")
        s.commit()
    _add_unit("5thva", "5th Virginia", "War of Rights")
    _add_unit("2ndus", "2nd United States", "Squad")


try:
    import pytest

    @pytest.fixture(autouse=True)
    def _fresh():
        _reset()
        yield
except ImportError:  # running without pytest
    pass


def _enlist(slug, **kwargs):
    with sessionmaker_for(unit_db_url_for_slug(slug))() as s:
        s.add(Member(**kwargs))
        s.commit()


def _login(client, discord_id=PLAYER, name="Reb", tier="none"):
    r = client.post("/auth/dev", data={"discord_id": discord_id, "name": name, "tier": tier},
                    headers={"host": APEX}, follow_redirects=False)
    assert r.status_code == 303


def _csrf(client, path):
    m = re.search(r'name="csrf" value="([^"]+)"', client.get(path, headers={"host": APEX}).text)
    assert m, f"no CSRF token on {path}"
    return m.group(1)


def test_profile_is_one_row_shared_by_every_unit():
    _enlist("5thva", discord_id=PLAYER, callsign="Reb", rank="Private",
            company="Alpha", status="active")
    _enlist("2ndus", discord_id=PLAYER, callsign="Reb", rank="Corporal",
            company="Bravo", status="active")
    c = TestClient(app)
    _login(c)
    c.post("/me/edit", headers={"host": APEX}, data={
        "csrf": _csrf(c, "/me/edit"), "bio": "Line infantry, mostly.",
        "timezone": "Europe/London", "availability": ["Fri", "Sat"],
    }, follow_redirects=False)

    # One registry row, not one per unit.
    with registry_session() as s:
        assert s.query(PlayerProfile).filter(PlayerProfile.discord_id == PLAYER).count() == 1

    html = c.get(f"/u/{PLAYER}", headers={"host": APEX}).text
    assert "Line infantry, mostly." in html
    assert "Europe/London" in html
    assert "Fri, Sat" in html


def test_ingame_names_are_offered_per_game_the_player_actually_plays():
    _enlist("5thva", discord_id=PLAYER, callsign="Reb", rank="Private",
            company="Alpha", status="active")
    c = TestClient(app)
    _login(c)
    form = c.get("/me/edit", headers={"host": APEX}).text
    # A game from a unit they serve in is offered; one they don't play isn't.
    assert 'name="ingame:War of Rights"' in form
    assert 'name="ingame:Squad"' not in form

    c.post("/me/edit", headers={"host": APEX}, data={
        "csrf": _csrf(c, "/me/edit"), "ingame:War of Rights": "RebYell",
    }, follow_redirects=False)
    assert player_profiles.get_profile(PLAYER)["ingame_names"] == {"War of Rights": "RebYell"}


def test_legacy_per_unit_profile_is_imported_once():
    """A bio written before the move -- on the unit they're active in -- is
    folded into the platform profile, and clearing it afterwards sticks."""
    _enlist("5thva", discord_id=PLAYER, callsign="Reb", rank="Private", company="Alpha",
            status="active", bio="Old per-unit bio.", timezone="UTC",
            availability="Wed", ingame_name="OldName")
    prof = player_profiles.get_profile(PLAYER)
    assert prof["bio"] == "Old per-unit bio."
    assert prof["timezone"] == "UTC"
    assert prof["availability"] == ["Wed"]
    assert prof["ingame_names"] == {"War of Rights": "OldName"}

    # Clearing it must not be undone by the importer on the next read.
    player_profiles.save_profile(PLAYER, bio="", timezone="", availability=[])
    assert player_profiles.get_profile(PLAYER)["bio"] is None


def test_javascript_links_are_rejected():
    c = TestClient(app)
    _login(c)
    c.post("/me/edit", headers={"host": APEX}, data={
        "csrf": _csrf(c, "/me/edit"),
        "link:steam": "javascript:alert(1)",
        "link:twitch": "twitch.tv/reb",
    }, follow_redirects=False)
    links = player_profiles.get_profile(PLAYER)["links"]
    assert "steam" not in links                       # dangerous scheme dropped
    assert links["twitch"] == "https://twitch.tv/reb"  # bare host gets https://


def test_profile_shows_assignments_and_furlough():
    from datetime import datetime, timedelta
    _enlist("5thva", discord_id=PLAYER, callsign="Reb", rank="Private", company="Alpha",
            status="loa", loa_until=datetime.utcnow() + timedelta(days=10))
    with sessionmaker_for(unit_db_url_for_slug("5thva"))() as s:
        s.add(Assignment(id=1, name="High Command", is_leadership=True))
        s.add(MemberAssignment(member_id=PLAYER, assignment_id=1))
        s.commit()
    html = TestClient(app).get(f"/u/{PLAYER}", headers={"host": APEX}).text
    assert "High Command" in html
    assert "on furlough" in html


def test_only_the_owner_is_offered_the_edit_link():
    _enlist("5thva", discord_id=PLAYER, callsign="Reb", rank="Private",
            company="Alpha", status="active")
    owner = TestClient(app)
    _login(owner, discord_id=PLAYER)
    assert '/me/edit' in owner.get(f"/u/{PLAYER}", headers={"host": APEX}).text

    visitor = TestClient(app)
    assert '/me/edit' not in visitor.get(f"/u/{PLAYER}", headers={"host": APEX}).text


def test_editing_requires_a_signed_in_user():
    r = TestClient(app).get("/me/edit", headers={"host": APEX}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
