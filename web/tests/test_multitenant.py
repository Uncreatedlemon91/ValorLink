"""Multi-tenant web tests (Phases 2 & 3).

Phase 2: subdomains serve their own unit database.
Phase 3: the apex serves a public directory; signed-in users apply to units;
admins edit their public listing.

Runs under pytest or directly:  python -m web.tests.test_multitenant
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="valorlink-mt-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/default.db"
os.environ["REGISTRY_DATABASE_URL"] = f"sqlite:///{_TMP}/registry.db"
os.environ["UNIT_DB_DIR"] = f"{_TMP}/units"
os.environ["PLATFORM_BASE_DOMAIN"] = "valorlink.co"
os.environ["PLATFORM_DEFAULT_SLUG"] = "hq"
os.environ["WEB_DEV_LOGIN"] = "1"
os.environ["WEB_SESSION_SECRET"] = "test-secret"
os.environ["PLATFORM_OPEN_REGISTRATION"] = "1"

import config  # noqa: E402
config.DATABASE_URL = os.environ["DATABASE_URL"]

from fastapi.testclient import TestClient  # noqa: E402

from db.base import Base, engine  # noqa: E402
from db.models import Candidacy  # noqa: E402
from tenancy.registry import RegistryBase, Tenant, registry_engine, registry_session  # noqa: E402
from tenancy.resolve import ensure_default_tenant, tenant_by_slug  # noqa: E402
from tenancy.units import (  # noqa: E402
    engine_for,
    provision_unit_db,
    sessionmaker_for,
    unit_db_url_for_slug,
)
from utils.settings import get_config  # noqa: E402

APEX = "valorlink.co"
UNITS = ("5thva", "2ndus")


def _set_name(db_url, name):
    with sessionmaker_for(db_url)() as s:
        get_config(s).regiment_name = name
        s.commit()


def _add_unit(slug, reg_name, cfg_name, recruiting=True):
    db_url = unit_db_url_for_slug(slug)
    provision_unit_db(db_url)
    with registry_session() as s:
        s.add(Tenant(slug=slug, name=reg_name, discord_guild_id=abs(hash(slug)) % 10**6,
                     db_url=db_url, recruiting_open=recruiting))
        s.commit()
    _set_name(db_url, cfg_name)


def _reset():
    """Fresh schemas + seed data before each test, for isolation."""
    Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
    RegistryBase.metadata.drop_all(registry_engine); RegistryBase.metadata.create_all(registry_engine)
    for slug in UNITS:
        Base.metadata.drop_all(engine_for(unit_db_url_for_slug(slug)))
    _set_name(os.environ["DATABASE_URL"], "Default Headquarters")
    with registry_session() as s:
        ensure_default_tenant(s, name="Default Headquarters")
        s.commit()
    _add_unit("5thva", "5th Virginia", "5th Virginia Volunteers", recruiting=True)
    _add_unit("2ndus", "2nd United States", "2nd U.S. Sharpshooters", recruiting=True)


def _host(sub):
    return {"host": f"{sub}.{APEX}"}


def test_apex_shows_the_directory():
    c = TestClient(app)
    html = c.get("/", headers={"host": APEX}).text
    assert "Units in the Field" in html
    assert "5th Virginia" in html and "2nd United States" in html


def test_subdomain_serves_its_own_unit():
    c = TestClient(app)
    html = c.get("/", headers=_host("5thva")).text
    assert "5th Virginia Volunteers" in html      # unit DB config name
    assert "2nd U.S. Sharpshooters" not in html


def test_unknown_subdomain_is_404():
    c = TestClient(app)
    r = c.get("/", headers=_host("ghost"))
    assert r.status_code == 404 and "No Such Unit" in r.text


def test_login_is_scoped_across_units():
    c = TestClient(app)
    c.post("/auth/dev", data={"discord_id": 1, "name": "Col. Test", "tier": "officer"},
           headers=_host("5thva"), follow_redirects=False)
    assert "Signed in as" in c.get("/", headers=_host("5thva")).text
    # a different unit sees a visitor
    assert "Viewing as a visitor" in c.get("/", headers=_host("2ndus")).text
    # and an officer action on the other unit is refused
    r = c.post("/members/1/service-log", data={"csrf": "x", "entry": "hi"},
               headers=_host("2ndus"), follow_redirects=False)
    assert r.status_code in (302, 303) and r.headers["location"] == "/login"


def test_apply_creates_candidacy_and_dedupes():
    c = TestClient(app)
    # sign in globally (identity) on the directory
    c.post("/auth/dev", data={"discord_id": 42, "name": "Recruit Rowe", "tier": "none"},
           headers={"host": APEX}, follow_redirects=False)
    token = _csrf(c, "/", {"host": APEX})
    c.post("/apply/5thva", data={"csrf": token}, headers={"host": APEX})
    with sessionmaker_for(unit_db_url_for_slug("5thva"))() as s:
        assert s.query(Candidacy).filter_by(discord_id=42).count() == 1
    # applying again does not duplicate
    c.post("/apply/5thva", data={"csrf": _csrf(c, "/", {"host": APEX})}, headers={"host": APEX})
    with sessionmaker_for(unit_db_url_for_slug("5thva"))() as s:
        assert s.query(Candidacy).filter_by(discord_id=42).count() == 1


def test_admin_edits_public_listing():
    c = TestClient(app)
    c.post("/auth/dev", data={"discord_id": 7, "name": "Gen. Test", "tier": "admin"},
           headers=_host("2ndus"), follow_redirects=False)
    token = _csrf(c, "/command-tent", _host("2ndus"))
    c.post("/admin/listing",
           data={"csrf": token, "name": "2nd U.S. (renamed)", "motto": "First and foremost",
                 "blurb": "Sharpshooters wanted.", "listed": "1"},  # recruiting_open omitted -> closed
           headers=_host("2ndus"))
    with registry_session() as s:
        row = tenant_by_slug(s, "2ndus")
        assert row.name == "2nd U.S. (renamed)"
        assert row.recruiting_open is False and row.listed is True


def test_register_flow_and_tls_allow():
    c = TestClient(app)
    c.post("/auth/dev", data={"discord_id": 9, "name": "Founder", "tier": "none"},
           headers={"host": APEX}, follow_redirects=False)
    assert c.get("/register", headers={"host": APEX}).status_code == 200
    token = _csrf(c, "/register", {"host": APEX})
    r = c.post("/register",
               data={"csrf": token, "slug": "newco", "name": "New Company", "guild_id": "999"},
               headers={"host": APEX})
    assert r.status_code == 200 and "Unit Raised" in r.text
    with registry_session() as s:
        row = tenant_by_slug(s, "newco")
        assert row is not None and row.discord_guild_id == 999

    # Caddy on-demand TLS gate
    assert c.get("/tls-allow", params={"domain": "newco.valorlink.co"}).status_code == 200
    assert c.get("/tls-allow", params={"domain": "valorlink.co"}).status_code == 200
    assert c.get("/tls-allow", params={"domain": "ghost.valorlink.co"}).status_code == 404


def test_register_check_reports_availability():
    c = TestClient(app)
    c.post("/auth/dev", data={"discord_id": 9, "name": "Founder", "tier": "none"},
           headers={"host": APEX}, follow_redirects=False)
    # a taken handle is unavailable
    d = c.get("/register/check", params={"slug": "5thva"}, headers={"host": APEX}).json()
    assert d["available"] is False
    # a free, valid handle is available
    d = c.get("/register/check", params={"slug": "freeco"}, headers={"host": APEX}).json()
    assert d["available"] is True
    # a malformed handle (underscore) is rejected with a reason
    d = c.get("/register/check", params={"slug": "bad_slug"}, headers={"host": APEX}).json()
    assert d["available"] is False and d["reason"]
    # a reserved handle is rejected
    d = c.get("/register/check", params={"slug": "admin"}, headers={"host": APEX}).json()
    assert d["available"] is False
    # unauthenticated callers are refused
    assert TestClient(app).get("/register/check", params={"slug": "freeco"},
                               headers={"host": APEX},
                               follow_redirects=False).status_code in (302, 303, 401)


def test_registration_closed_by_default():
    """With neither PLATFORM_ADMIN_IDS nor PLATFORM_OPEN_REGISTRATION set,
    registration is closed even to signed-in users."""
    import web.app as web_app
    old = os.environ.pop("PLATFORM_OPEN_REGISTRATION", None)
    try:
        c = TestClient(app)
        c.post("/auth/dev", data={"discord_id": 9, "name": "Founder", "tier": "none"},
               headers={"host": APEX}, follow_redirects=False)
        assert "Registration Closed" in c.get("/register", headers={"host": APEX}).text
        token_html = c.get("/register", headers={"host": APEX}).text
        # a POST is refused too
        r = c.post("/register",
                   data={"csrf": "x", "slug": "sneaky", "name": "Sneaky", "guild_id": "1"},
                   headers={"host": APEX}, follow_redirects=False)
        assert r.status_code in (302, 303, 403)
        with registry_session() as s:
            assert tenant_by_slug(s, "sneaky") is None
    finally:
        if old is not None:
            os.environ["PLATFORM_OPEN_REGISTRATION"] = old


import re  # noqa: E402


def _csrf(client, path, headers):
    html = client.get(path, headers=headers).text
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, f"no CSRF token on {path}"
    return m.group(1)


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        _reset()
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)} tests passed.")


from web.app import app  # noqa: E402

try:
    import pytest

    @pytest.fixture(autouse=True)
    def _fresh():
        _reset()
        yield
except ImportError:
    pass


if __name__ == "__main__":
    _run_all()
