"""Phase 4 tests: the bot's tenant-context DB routing, the multi-tenant
bridge, and unit provisioning.

Runs under pytest or directly:  python -m tenancy.tests.test_bot_routing
"""
import asyncio
import os
import tempfile
from unittest.mock import MagicMock

_TMP = tempfile.mkdtemp(prefix="valorlink-bot-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/default.db"
os.environ["REGISTRY_DATABASE_URL"] = f"sqlite:///{_TMP}/registry.db"
os.environ["UNIT_DB_DIR"] = f"{_TMP}/units"

from db.base import SessionLocal, db_session  # noqa: E402
from db.context import reset_current_db_url, set_current_db_url  # noqa: E402
from db.models import Base, Member, PendingAction  # noqa: E402
from tenancy import provision  # noqa: E402
from tenancy.registry import init_registry, registry_session  # noqa: E402
from tenancy.resolve import tenant_by_slug  # noqa: E402
from tenancy.routing import db_url_for_guild, invalidate  # noqa: E402
from tenancy.units import sessionmaker_for, unit_db_url_for_slug  # noqa: E402
from utils import queue  # noqa: E402

# default DB schema (for the unbound fallback case)
from db.base import engine as _default_engine  # noqa: E402
Base.metadata.create_all(_default_engine)


init_registry()


def test_db_session_follows_the_bound_unit():
    a = provision.create_unit("alpha", "Alpha", guild_id=111)
    b = provision.create_unit("bravo", "Bravo", guild_id=222)

    tok = set_current_db_url(a)
    try:
        with db_session() as s:
            s.add(Member(discord_id=1, callsign="alpha-one", rank="Pvt", company="X")); s.commit()
    finally:
        reset_current_db_url(tok)

    tok = set_current_db_url(b)
    try:
        with db_session() as s:
            s.add(Member(discord_id=1, callsign="bravo-one", rank="Pvt", company="Y")); s.commit()
    finally:
        reset_current_db_url(tok)

    # each unit kept its own row under the same id
    with sessionmaker_for(a)() as s:
        assert s.get(Member, 1).callsign == "alpha-one"
    with sessionmaker_for(b)() as s:
        assert s.get(Member, 1).callsign == "bravo-one"

    # unbound falls back to the default database (a third, separate store)
    with db_session() as s:
        assert s.get(Member, 1) is None
    assert isinstance(SessionLocal(), object)


def test_routing_resolves_and_caches_guilds():
    provision.create_unit("charlie", "Charlie", guild_id=333)
    assert db_url_for_guild(333) == unit_db_url_for_slug("charlie")
    # unknown guild falls back to the default unit
    with registry_session() as s:
        assert tenant_by_slug(s, "charlie") is not None


def test_bridge_drains_each_units_queue_against_its_guild():
    from cogs.bridge import Bridge

    url_a = provision.create_unit("delta", "Delta", guild_id=444)
    url_b = provision.create_unit("echo", "Echo", guild_id=555)

    # queue one action in each unit's own database
    for url in (url_a, url_b):
        tok = set_current_db_url(url)
        try:
            with db_session() as s:
                queue.enqueue(s, queue.REFRESH_PERSONNEL, {"discord_id": 1}); s.commit()
        finally:
            reset_current_db_url(tok)

    bridge = object.__new__(Bridge)
    bridge.bot = MagicMock()
    guilds = {444: MagicMock(), 555: MagicMock()}
    bridge.bot.get_guild.side_effect = lambda gid: guilds.get(gid)

    asyncio.run(Bridge.drain_queue.coro(bridge))

    # both units' actions were processed against their own database
    for url in (url_a, url_b):
        with sessionmaker_for(url)() as s:
            row = s.query(PendingAction).one()
            assert row.status == queue.DONE


def test_delete_unit_removes_from_registry_and_protects_default():
    provision.create_unit("golf", "Golf", guild_id=777)
    with registry_session() as s:
        assert tenant_by_slug(s, "golf") is not None
    result = provision.delete_unit("golf")   # archives db by default
    assert result["name"] == "Golf" and result["purged"] is False
    with registry_session() as s:
        assert tenant_by_slug(s, "golf") is None
    # deleting a non-existent unit errors
    try:
        provision.delete_unit("golf")
        assert False
    except provision.ProvisionError:
        pass
    # the default unit is protected
    from tenancy.resolve import ensure_default_tenant
    with registry_session() as s:
        ensure_default_tenant(s, name="HQ")
        s.commit()
        default_slug = __import__("tenancy.resolve", fromlist=["default_tenant"]).default_tenant(s).slug
    try:
        provision.delete_unit(default_slug)
        assert False
    except provision.ProvisionError:
        pass


def test_create_unit_rejects_bad_and_duplicate_slugs():
    provision.create_unit("foxtrot", "Foxtrot", guild_id=666)
    for bad in ("foxtrot", "WWW", "a b", "-x", "www"):
        try:
            provision.create_unit(bad, "X")
            assert False, f"expected rejection for {bad!r}"
        except provision.ProvisionError:
            pass


def test_command_sync_survives_a_forbidden_guild():
    """One guild the bot lacks command access in must not crash startup — it's
    skipped so every other unit still comes online."""
    import discord

    import bot as bot_module
    from bot import ValorLink

    inst = object.__new__(ValorLink)
    tree = MagicMock()
    tree.copy_global_to = MagicMock()

    resp = MagicMock(); resp.status = 403; resp.reason = "Forbidden"

    async def fake_sync(*, guild):
        if guild.id == 222:                       # the misconfigured unit
            raise discord.Forbidden(resp, {"code": 50001, "message": "Missing Access"})
    tree.sync = fake_sync
    inst._BotBase__tree = tree                    # commands.Bot.tree is read-only

    saved = bot_module.registered_guild_ids
    bot_module.registered_guild_ids = lambda: [111, 222]
    old_guild = bot_module.config.GUILD_ID
    bot_module.config.GUILD_ID = None
    try:
        asyncio.run(inst._sync_commands())        # must not raise
    finally:
        bot_module.registered_guild_ids = saved
        bot_module.config.GUILD_ID = old_guild


def test_bot_removal_retires_unit_but_spares_default():
    from bot import ValorLink
    from tenancy.registry import Tenant

    provision.create_unit("kickme", "Kick Me", guild_id=909)
    # a protected default unit bound to its own guild
    with registry_session() as s:
        s.add(Tenant(slug="hqdefault", name="HQ", discord_guild_id=800,
                     db_url="sqlite:///:memory:", is_default=True))
        s.commit()

    inst = object.__new__(ValorLink)

    # kicked from a normal unit's server → it's retired
    g1 = MagicMock(); g1.id = 909; g1.name = "Kick Me"
    asyncio.run(inst.on_guild_remove(g1))
    invalidate()
    with registry_session() as s:
        assert tenant_by_slug(s, "kickme") is None
    assert db_url_for_guild(909) is None

    # kicked from the default unit's server → left registered
    g2 = MagicMock(); g2.id = 800; g2.name = "HQ"
    asyncio.run(inst.on_guild_remove(g2))
    with registry_session() as s:
        assert tenant_by_slug(s, "hqdefault") is not None

    # an unregistered guild is a no-op
    g3 = MagicMock(); g3.id = 404404; g3.name = "Stranger"
    asyncio.run(inst.on_guild_remove(g3))


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)} tests passed.")


if __name__ == "__main__":
    _run_all()
