"""Tests for the Discord-role/channel dropdowns in Command Tent.

Exercises the fallback (no bot token / guild not linked -> plain ID text
box, already covered incidentally by test_officer_actions.py) and the live
picker path, with web.discord_meta mocked so no real Discord call is made.

Runs under pytest or directly:  python -m web.tests.test_role_pickers
"""
import os
import re
import tempfile
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="valorlink-roles-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["REGISTRY_DATABASE_URL"] = f"sqlite:///{_TMP}/registry.db"
os.environ.pop("PLATFORM_BASE_DOMAIN", None)   # single-tenant: everything resolves to default
os.environ["WEB_DEV_LOGIN"] = "1"
os.environ["WEB_SESSION_SECRET"] = "test-secret"

import config  # noqa: E402
config.DATABASE_URL = os.environ["DATABASE_URL"]
config.GUILD_ID = 900900900  # so the default tenant links to a guild

from fastapi.testclient import TestClient  # noqa: E402

from db.base import Base, engine  # noqa: E402
from tenancy.registry import RegistryBase, registry_engine  # noqa: E402
from utils.settings import get_config  # noqa: E402
from web.app import app  # noqa: E402

GUILD_ROLES = [
    {"id": 111, "name": "Colonel"},
    {"id": 222, "name": "Captain"},
]
GUILD_CHANNELS = [
    {"id": 333, "name": "roster"},
    {"id": 444, "name": "admin-log"},
]

RegistryBase.metadata.create_all(registry_engine)


def _reset():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    from db.base import SessionLocal
    with SessionLocal() as s:
        get_config(s)  # creates the singleton config row
        s.commit()


try:
    import pytest

    @pytest.fixture(autouse=True)
    def _fresh_db():
        _reset()
        yield
except ImportError:  # running without pytest
    pass


def _login(client, tier="admin", discord_id=1, name="Admin Test"):
    r = client.post("/auth/dev", data={"discord_id": discord_id, "name": name, "tier": tier},
                    follow_redirects=False)
    assert r.status_code == 303


def _csrf(client, path="/command-tent"):
    html = client.get(path).text
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, f"no CSRF token found on {path}"
    return m.group(1)


def test_falls_back_to_text_box_when_discord_data_unavailable():
    c = TestClient(app)
    _login(c)
    with patch("web.app.discord_meta.guild_roles", return_value=None), \
         patch("web.app.discord_meta.guild_channels", return_value=None):
        html = c.get("/command-tent").text
    assert 'placeholder="role ID"' in html
    assert '<select name="admin"' not in html


def test_renders_role_and_channel_dropdowns_when_available():
    c = TestClient(app)
    _login(c)
    with patch("web.app.discord_meta.guild_roles", return_value=GUILD_ROLES), \
         patch("web.app.discord_meta.guild_channels", return_value=GUILD_CHANNELS):
        html = c.get("/command-tent").text
    assert '<select name="admin"' in html
    assert '<option value="111" >Colonel</option>' in html
    assert '<select name="roster"' in html
    assert '<option value="333" >#roster</option>' in html


def test_currently_set_role_is_preselected():
    c = TestClient(app)
    _login(c)
    with patch("web.app.discord_meta.guild_roles", return_value=GUILD_ROLES), \
         patch("web.app.discord_meta.guild_channels", return_value=GUILD_CHANNELS):
        token = _csrf(c)
        c.post("/admin/roles", data={"csrf": token, "admin": "222", "officer": "", "recruiter": "",
                                     "member": "", "candidate": "", "visitor": "", "inactive": ""},
               follow_redirects=False)
        html = c.get("/command-tent").text
    assert '<option value="222" selected>Captain</option>' in html


def test_unknown_role_id_is_shown_rather_than_silently_dropped():
    """A role bound before it was renamed/deleted in Discord (or while the
    picker was briefly unavailable) must still show up as *something*, so
    saving the form again doesn't quietly clear it."""
    c = TestClient(app)
    _login(c)
    token = _csrf(c)
    c.post("/admin/roles", data={"csrf": token, "admin": "999999", "officer": "", "recruiter": "",
                                 "member": "", "candidate": "", "visitor": "", "inactive": ""},
           follow_redirects=False)
    with patch("web.app.discord_meta.guild_roles", return_value=GUILD_ROLES), \
         patch("web.app.discord_meta.guild_channels", return_value=GUILD_CHANNELS):
        html = c.get("/command-tent").text
    assert "Unknown role (999999)" in html


def test_rank_add_form_uses_role_picker_and_saves_correctly():
    c = TestClient(app)
    _login(c)
    with patch("web.app.discord_meta.guild_roles", return_value=GUILD_ROLES), \
         patch("web.app.discord_meta.guild_channels", return_value=GUILD_CHANNELS):
        token = _csrf(c)
        c.post("/admin/ranks/add", data={
            "csrf": token, "name": "Colonel", "abbreviation": "Col",
            "tier": "", "role_id": "111",
        }, follow_redirects=False)
        html = c.get("/command-tent").text
    assert "role <span class=\"role-id\">111</span>" in html


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
