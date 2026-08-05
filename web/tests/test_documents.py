"""Tests for unit documents (Markdown handbooks/SOPs).

Runs under pytest or directly:  python -m web.tests.test_documents
"""
import os
import re
import tempfile

_TMP = tempfile.mkdtemp(prefix="valorlink-docs-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["REGISTRY_DATABASE_URL"] = f"sqlite:///{_TMP}/registry.db"
os.environ.pop("PLATFORM_BASE_DOMAIN", None)   # single-tenant: everything resolves to default
os.environ["WEB_DEV_LOGIN"] = "1"
os.environ["WEB_SESSION_SECRET"] = "test-secret"

import config  # noqa: E402
config.DATABASE_URL = os.environ["DATABASE_URL"]

from fastapi.testclient import TestClient  # noqa: E402

from db.base import Base, engine  # noqa: E402
from tenancy.registry import RegistryBase, registry_engine  # noqa: E402
from utils.settings import get_config  # noqa: E402
from web.app import app  # noqa: E402

# The dev-login route checks the platform ban list before resolving the
# tenant, so the registry schema needs to exist before the very first
# request -- create_all is idempotent and safe to run up front.
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


def _login(client, tier="officer", discord_id=1, name="Officer Test"):
    r = client.post("/auth/dev", data={"discord_id": discord_id, "name": name, "tier": tier},
                    follow_redirects=False)
    assert r.status_code == 303


def _csrf(client, path="/documents"):
    html = client.get(path).text
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, f"no CSRF token found on {path}"
    return m.group(1)


def test_signed_out_visitor_is_redirected_to_login():
    c = TestClient(app)
    r = c.get("/documents", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_signed_in_member_of_any_tier_can_view_the_list():
    c = TestClient(app)
    _login(c, tier="none", discord_id=50, name="Rank and File")
    html = c.get("/documents").text
    assert "No Documents Yet" in html


def test_officer_can_publish_a_document_and_it_renders_markdown():
    c = TestClient(app)
    _login(c, tier="officer")
    token = _csrf(c, "/documents/new")
    r = c.post("/documents/new", data={
        "csrf": token, "title": "Regiment Handbook",
        "body": "# Welcome\n\nThis is **bold** text.\n\n<script>alert(1)</script>",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/documents/regiment-handbook"

    html = c.get("/documents/regiment-handbook").text
    assert "<h1>Welcome</h1>" in html
    assert "<strong>bold</strong>" in html
    assert "<script>alert(1)</script>" not in html  # sanitized out of the document body

    listing = c.get("/documents").text
    assert "Regiment Handbook" in listing


def test_recruiter_cannot_publish_a_document():
    c = TestClient(app)
    _login(c, tier="recruiter")
    r = c.post("/documents/new", data={
        "csrf": "x", "title": "Sneaky", "body": "hi",
    }, follow_redirects=False)
    assert r.status_code == 403


def test_two_documents_with_the_same_title_get_distinct_slugs():
    c = TestClient(app)
    _login(c, tier="officer")
    token = _csrf(c, "/documents/new")
    c.post("/documents/new", data={"csrf": token, "title": "SOP", "body": "one"},
           follow_redirects=False)
    token = _csrf(c, "/documents/new")
    r = c.post("/documents/new", data={"csrf": token, "title": "SOP", "body": "two"},
               follow_redirects=False)
    assert r.headers["location"] == "/documents/sop-2"


def test_officer_can_edit_and_delete_an_unlocked_document():
    c = TestClient(app)
    _login(c, tier="officer")
    token = _csrf(c, "/documents/new")
    c.post("/documents/new", data={"csrf": token, "title": "Orders", "body": "v1"},
           follow_redirects=False)

    token = _csrf(c, "/documents/orders/edit")
    c.post("/documents/orders/edit", data={"csrf": token, "title": "Orders", "body": "v2"},
           follow_redirects=False)
    assert "v2" in c.get("/documents/orders").text

    token = _csrf(c, "/documents/orders")
    r = c.post("/documents/orders/delete", data={"csrf": token}, follow_redirects=False)
    assert r.status_code == 303
    assert c.get("/documents/orders").status_code == 404


def test_admin_can_lock_a_document_to_admin_only_editing():
    admin = TestClient(app)
    _login(admin, tier="admin", discord_id=1, name="Admin")
    token = _csrf(admin, "/documents/new")
    admin.post("/documents/new", data={
        "csrf": token, "title": "Command Directive", "body": "secret plans",
        "admin_only": "1",
    }, follow_redirects=False)

    officer = TestClient(app)
    _login(officer, tier="officer", discord_id=2, name="Officer")
    # An officer can still read a locked document...
    assert "secret plans" in officer.get("/documents/command-directive").text
    # ...but not edit or delete it.
    r = officer.get("/documents/command-directive/edit", follow_redirects=False)
    assert r.status_code == 403
    # The document page renders no delete form for a locked doc the officer
    # can't touch, so grab a valid CSRF token from a page they can reach.
    token = _csrf(officer, "/documents/new")
    r = officer.post("/documents/command-directive/delete", data={"csrf": token},
                     follow_redirects=False)
    assert r.status_code == 403

    # The admin who locked it can still edit it.
    token = _csrf(admin, "/documents/command-directive/edit")
    r = admin.post("/documents/command-directive/edit", data={
        "csrf": token, "title": "Command Directive", "body": "updated plans",
    }, follow_redirects=False)
    assert r.status_code == 303


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
