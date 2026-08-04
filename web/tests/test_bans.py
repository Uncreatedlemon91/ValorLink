"""Tests for the platform-wide Discord-identity ban system.

Bans are registry-backed (cross-unit by design) and enforced through
auth.current_user()/effective_user(), so these tests don't need a unit
database -- just the registry schema, dev-login sessions, and a platform-admin
allowlist for the admin routes.

Runs under pytest or directly:  python -m web.tests.test_bans
"""
import os
import re
import tempfile

_TMP = tempfile.mkdtemp(prefix="valorlink-bans-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["REGISTRY_DATABASE_URL"] = f"sqlite:///{_TMP}/registry.db"
os.environ["PLATFORM_BASE_DOMAIN"] = "valorlink.co"
os.environ["PLATFORM_ADMIN_IDS"] = "1"
os.environ["WEB_DEV_LOGIN"] = "1"
os.environ["WEB_SESSION_SECRET"] = "test-secret"

import config  # noqa: E402
config.DATABASE_URL = os.environ["DATABASE_URL"]

from fastapi.testclient import TestClient  # noqa: E402

from db.base import Base, engine  # noqa: E402
from tenancy.registry import PlatformBan, RegistryBase, registry_engine, registry_session  # noqa: E402
from web.app import app  # noqa: E402

APEX = "valorlink.co"
ADMIN_ID = 1
MEMBER_A = 101

RegistryBase.metadata.create_all(registry_engine)
Base.metadata.create_all(engine)


def _reset():
    with registry_session() as s:
        s.query(PlatformBan).delete()
        s.commit()


def _login(client, discord_id, name, tier="none"):
    client.post("/auth/dev", data={"discord_id": discord_id, "name": name, "tier": tier},
                headers={"host": APEX}, follow_redirects=False)
    return client


def _csrf(client, path="/admin/platform"):
    html = client.get(path, headers={"host": APEX}).text
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, f"no CSRF token on {path}"
    return m.group(1)


def test_admin_can_ban_and_unban_a_user():
    _reset()
    admin = _login(TestClient(app), ADMIN_ID, "Admin")
    token = _csrf(admin)
    r = admin.post("/admin/platform/ban",
                    data={"csrf": token, "discord_id": str(MEMBER_A), "reason": "spam"},
                    headers={"host": APEX}, follow_redirects=False)
    assert r.status_code == 303
    html = admin.get("/admin/platform", headers={"host": APEX}).text
    assert str(MEMBER_A) in html and "spam" in html

    token = _csrf(admin)
    admin.post("/admin/platform/unban", data={"csrf": token, "discord_id": str(MEMBER_A)},
               headers={"host": APEX}, follow_redirects=False)
    html = admin.get("/admin/platform", headers={"host": APEX}).text
    assert str(MEMBER_A) not in html.split("Units <span")[0].split("Banned Users")[1]


def test_non_admin_cannot_ban():
    _reset()
    member = _login(TestClient(app), MEMBER_A, "Alice")
    r = member.post("/admin/platform/ban",
                     data={"csrf": "x", "discord_id": "999", "reason": "test"},
                     headers={"host": APEX}, follow_redirects=False)
    assert r.status_code in (401, 403)


def test_banned_user_cannot_sign_in():
    _reset()
    with registry_session() as s:
        s.add(PlatformBan(discord_id=MEMBER_A, reason="abuse"))
        s.commit()
    c = TestClient(app)
    r = c.post("/auth/dev", data={"discord_id": MEMBER_A, "name": "Alice", "tier": "none"},
               headers={"host": APEX}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    html = c.get("/", headers={"host": APEX}).text
    assert "My Units" in html
    assert "Alice" not in html


def test_banning_revokes_an_existing_live_session():
    _reset()
    c = _login(TestClient(app), MEMBER_A, "Alice")
    html = c.get("/", headers={"host": APEX}).text
    assert "Alice" in html

    with registry_session() as s:
        s.add(PlatformBan(discord_id=MEMBER_A, reason="misconduct"))
        s.commit()

    html = c.get("/", headers={"host": APEX}).text
    assert "Alice" not in html
    assert "banned" in html.lower()


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
