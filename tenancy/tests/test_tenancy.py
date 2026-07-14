"""Phase 1 tenancy tests: provisioning, per-unit isolation, and resolution.

Runs under pytest or directly:  python -m tenancy.tests.test_tenancy
"""
import os
import tempfile

# Point the registry and unit databases at a throwaway directory before import.
_TMP = tempfile.mkdtemp(prefix="valorlink-tenancy-")
os.environ["REGISTRY_DATABASE_URL"] = f"sqlite:///{_TMP}/registry.db"
os.environ["UNIT_DB_DIR"] = f"{_TMP}/units"
os.environ["PLATFORM_BASE_DOMAIN"] = "valorlink.co"
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP}/legacy.db")

from db.models import Member  # noqa: E402
from tenancy.registry import Tenant, init_registry, registry_session  # noqa: E402
from tenancy.resolve import (  # noqa: E402
    ensure_default_tenant,
    slug_from_host,
    tenant_by_guild,
    tenant_by_slug,
)
from tenancy.units import provision_unit_db, session_for, unit_db_url_for_slug  # noqa: E402


def _make(slug, name, guild):
    with registry_session() as s:
        db_url = unit_db_url_for_slug(slug)
        provision_unit_db(db_url)
        t = Tenant(slug=slug, name=name, discord_guild_id=guild, db_url=db_url)
        s.add(t)
        s.commit()
        s.refresh(t)
        s.expunge(t)
        return t


def test_slug_from_host():
    assert slug_from_host("5thva.valorlink.co") == "5thva"
    assert slug_from_host("5thva.valorlink.co:443") == "5thva"
    assert slug_from_host("valorlink.co") is None          # apex = public site
    assert slug_from_host("www.valorlink.co") is None
    assert slug_from_host("a.b.valorlink.co") is None       # only one label is a unit
    assert slug_from_host("someone-elses-domain.com") is None
    assert slug_from_host(None) is None


def test_provision_creates_isolated_schema():
    t = _make("alpha", "Alpha Unit", 111)
    # the unit database has the ValorLink schema and is stamped at head
    with session_for(t) as s:
        assert s.query(Member).count() == 0
    engine = __import__("tenancy.units", fromlist=["engine_for"]).engine_for(t.db_url)
    from sqlalchemy import inspect
    tables = inspect(engine).get_table_names()
    assert "members" in tables and "alembic_version" in tables
    with engine.connect() as conn:
        from sqlalchemy import text
        ver = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert ver  # stamped


def test_units_are_isolated():
    a = _make("bravo", "Bravo Unit", 222)
    b = _make("charlie", "Charlie Unit", 333)

    with session_for(a) as s:
        s.add(Member(discord_id=1, callsign="A-one", rank="Pvt", company="X"))
        s.commit()
    with session_for(b) as s:
        s.add(Member(discord_id=1, callsign="B-one", rank="Pvt", company="Y"))
        s.commit()

    # same primary key in each, different data — proving separate databases
    with session_for(a) as s:
        assert s.get(Member, 1).callsign == "A-one"
        assert s.query(Member).count() == 1
    with session_for(b) as s:
        assert s.get(Member, 1).callsign == "B-one"


def test_resolve_by_slug_and_guild():
    _make("delta", "Delta Unit", 444)
    with registry_session() as s:
        assert tenant_by_slug(s, "delta").name == "Delta Unit"
        assert tenant_by_guild(s, 444).slug == "delta"
        assert tenant_by_slug(s, "nope") is None
        assert tenant_by_guild(s, 999999) is None


def test_default_tenant_seeding_is_idempotent():
    with registry_session() as s:
        first = ensure_default_tenant(s, name="Legacy HQ")
        assert first.is_default and first.db_url == os.environ["DATABASE_URL"]
        again = ensure_default_tenant(s, name="Different")
        assert again.id == first.id  # not duplicated


def _run_all():
    init_registry()
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)} tests passed.")


if __name__ == "__main__":
    _run_all()
