"""Phase 2 tests: the web app resolves a unit per request (by subdomain) and
serves that unit's own database, while the apex serves the default unit.

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

import config  # noqa: E402
config.DATABASE_URL = os.environ["DATABASE_URL"]

from fastapi.testclient import TestClient  # noqa: E402

from db.base import Base, engine  # noqa: E402
from tenancy.registry import Tenant, init_registry, registry_session  # noqa: E402
from tenancy.resolve import ensure_default_tenant  # noqa: E402
from tenancy.units import provision_unit_db, sessionmaker_for, unit_db_url_for_slug  # noqa: E402
from utils.settings import get_config  # noqa: E402

APEX = "valorlink.co"
UNIT_HOST = "5thva.valorlink.co"


def _set_name(db_url, name):
    with sessionmaker_for(db_url)() as s:
        cfg = get_config(s)
        cfg.regiment_name = name
        s.commit()


def _setup():
    # default unit (apex) database
    Base.metadata.create_all(engine)
    _set_name(os.environ["DATABASE_URL"], "Default Headquarters")

    init_registry()
    with registry_session() as s:
        ensure_default_tenant(s, name="Default Headquarters")
        # a second unit at 5thva.valorlink.co with its own database
        db_url = unit_db_url_for_slug("5thva")
        provision_unit_db(db_url)
        s.add(Tenant(slug="5thva", name="5th Virginia", discord_guild_id=555, db_url=db_url))
        s.commit()
    _set_name(unit_db_url_for_slug("5thva"), "5th Virginia Volunteers")


def test_apex_serves_default_unit():
    c = TestClient(app)
    html = c.get("/", headers={"host": APEX}).text
    assert "Default Headquarters" in html
    assert "5th Virginia Volunteers" not in html


def test_subdomain_serves_its_own_unit():
    c = TestClient(app)
    html = c.get("/", headers={"host": UNIT_HOST}).text
    assert "5th Virginia Volunteers" in html
    assert "Default Headquarters" not in html


def test_unknown_subdomain_is_404():
    c = TestClient(app)
    r = c.get("/", headers={"host": "ghost.valorlink.co"})
    assert r.status_code == 404
    assert "No Such Unit" in r.text


def test_login_is_scoped_to_the_unit_signed_into():
    c = TestClient(app)
    # sign in on the 5thva subdomain as an officer
    c.post("/auth/dev", data={"discord_id": 1, "name": "Col. Test", "tier": "officer"},
           headers={"host": UNIT_HOST}, follow_redirects=False)

    # recognised on 5thva …
    on_unit = c.get("/", headers={"host": UNIT_HOST}).text
    assert "Signed in as" in on_unit and "Col. Test" in on_unit

    # … but a visitor on the apex (different unit)
    on_apex = c.get("/", headers={"host": APEX}).text
    assert "Viewing as a visitor" in on_apex

    # and an officer-only action on the apex is refused (sent to sign-in)
    r = c.post("/members/1/service-log", data={"csrf": "x", "entry": "hi"},
               headers={"host": APEX}, follow_redirects=False)
    assert r.status_code in (302, 303) and r.headers["location"] == "/login"


def _run_all():
    _setup()
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)} tests passed.")


# import the app after env + (for direct runs) after setup wiring is defined
from web.app import app  # noqa: E402

try:
    import pytest

    @pytest.fixture(scope="session", autouse=True)
    def _seed_once():
        _setup()
        yield
except ImportError:
    pass


if __name__ == "__main__":
    _run_all()
