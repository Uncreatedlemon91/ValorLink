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


def test_apex_home_is_informative_with_navigation():
    c = TestClient(app)
    html = c.get("/", headers={"host": APEX}).text
    # explains the platform and navigates to Find a Unit
    assert "A home for your gaming unit" in html
    assert 'href="/find"' in html and "Find a Unit" in html
    # signed-out visitors are pointed at sign-in for My Units
    assert "My Units" in html


def test_home_shows_my_units_nav_when_signed_in():
    c = TestClient(app)
    c.post("/auth/dev", data={"discord_id": 7, "name": "Gen", "tier": "admin"},
           headers={"host": APEX}, follow_redirects=False)
    html = c.get("/", headers={"host": APEX}).text
    assert 'href="/my-units"' in html


def test_my_units_page_lists_the_users_units():
    c = TestClient(app)
    # signing in on the apex resolves the default unit into the tier map
    c.post("/auth/dev", data={"discord_id": 7, "name": "Gen", "tier": "admin"},
           headers={"host": APEX}, follow_redirects=False)
    html = c.get("/my-units", headers={"host": APEX}).text
    assert "Your Units" in html and "Default Headquarters" in html
    # a signed-out visitor is prompted to sign in
    c2 = TestClient(app)
    out = c2.get("/my-units", headers={"host": APEX}).text
    assert "Sign In to See Your Units" in out


def test_my_units_redirects_from_a_subdomain():
    c = TestClient(app)
    r = c.get("/my-units", headers=_host("5thva"), follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"].endswith("/my-units")


def test_find_page_lists_and_prefills():
    c = TestClient(app)
    html = c.get("/find", headers={"host": APEX}).text
    assert "Find a Unit" in html and 'id="dir-search"' in html
    assert "5th Virginia" in html and "2nd United States" in html
    # a prefilled search term lands in the box
    assert 'value="sharp"' in c.get("/find?q=sharp", headers={"host": APEX}).text


def test_find_page_redirects_from_a_subdomain():
    c = TestClient(app)
    r = c.get("/find", headers=_host("5thva"), follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"].endswith("/find")


def test_directory_shows_platform_activity_feed():
    from datetime import datetime, timedelta

    from db.models import Member
    c = TestClient(app)
    with sessionmaker_for(unit_db_url_for_slug("5thva"))() as s:
        s.add(Member(discord_id=98765, callsign="Newbie", rank="Pvt", company="A",
                     status="active", joined_date=datetime.utcnow() - timedelta(days=1)))
        s.commit()
    html = c.get("/", headers={"host": APEX}).text
    assert "On the Field" in html
    assert "Newbie" in html and "enlisted" in html and "5th Virginia" in html


def test_subdomain_serves_its_own_unit():
    c = TestClient(app)
    html = c.get("/", headers=_host("5thva")).text
    assert "5th Virginia Volunteers" in html      # unit DB config name
    assert "2nd U.S. Sharpshooters" not in html


def test_unknown_subdomain_is_404():
    c = TestClient(app)
    r = c.get("/", headers=_host("ghost"))
    assert r.status_code == 404 and "No Such Unit" in r.text


def test_signed_in_name_prefers_server_nickname():
    from web.auth import _display_name
    # server nickname (WoR name) wins, with the bot's rank prefix stripped
    assert _display_name("Pvt. Smith", "SmithGaming", "smith_h") == "Smith"
    # no nickname → Discord account display name, then the handle
    assert _display_name(None, "SmithGaming", "smith_h") == "SmithGaming"
    assert _display_name(None, None, "smith_h") == "smith_h"


def test_login_is_scoped_across_units():
    c = TestClient(app)
    c.post("/auth/dev", data={"discord_id": 1, "name": "Col. Test", "tier": "officer"},
           headers=_host("5thva"), follow_redirects=False)
    assert "Signed in as" in c.get("/", headers=_host("5thva")).text
    # a different unit sees a visitor
    # on a unit they don't belong to they're a visitor (the top bar shows it)
    other = c.get("/", headers=_host("2ndus")).text
    assert "Officer sign-in" in other and "Signed in as" not in other
    # and an officer action on the other unit is refused
    r = c.post("/members/1/service-log", data={"csrf": "x", "entry": "hi"},
               headers=_host("2ndus"), follow_redirects=False)
    assert r.status_code in (302, 303) and r.headers["location"] == "/login"


def test_apply_creates_candidacy_and_dedupes():
    c = TestClient(app)
    # sign in on the unit and apply through its application form
    c.post("/auth/dev", data={"discord_id": 42, "name": "Recruit Rowe", "tier": "none"},
           headers=_host("5thva"), follow_redirects=False)
    token = _csrf(c, "/apply", _host("5thva"))  # session-stable token
    c.post("/apply", data={"csrf": token}, headers=_host("5thva"))
    with sessionmaker_for(unit_db_url_for_slug("5thva"))() as s:
        assert s.query(Candidacy).filter_by(discord_id=42).count() == 1
    # applying again does not duplicate (reuse the same session token)
    c.post("/apply", data={"csrf": token}, headers=_host("5thva"))
    with sessionmaker_for(unit_db_url_for_slug("5thva"))() as s:
        assert s.query(Candidacy).filter_by(discord_id=42).count() == 1


def test_recruitment_questions_collected_on_apply():
    c = TestClient(app)
    # admin adds a required question
    c.post("/auth/dev", data={"discord_id": 7, "name": "Gen", "tier": "admin"},
           headers=_host("5thva"), follow_redirects=False)
    c.post("/admin/questions/add",
           data={"csrf": _csrf(c, "/command-tent", _host("5thva")),
                 "prompt": "WoR experience?", "required": "1"}, headers=_host("5thva"))
    # a fresh applicant answers it
    c2 = TestClient(app)
    c2.post("/auth/dev", data={"discord_id": 55, "name": "Rec", "tier": "none"},
            headers=_host("5thva"), follow_redirects=False)
    form = c2.get("/apply", headers=_host("5thva")).text
    assert "WoR experience?" in form
    qid = re.search(r'name="q_(\d+)"', form).group(1)
    token = re.search(r'name="csrf" value="([^"]+)"', form).group(1)
    # missing a required answer is rejected
    c2.post("/apply", data={"csrf": token}, headers=_host("5thva"))
    with sessionmaker_for(unit_db_url_for_slug("5thva"))() as s:
        assert s.query(Candidacy).filter_by(discord_id=55).count() == 0
    # answering it records the answer
    c2.post("/apply", data={"csrf": token, f"q_{qid}": "Two years, line infantry."},
            headers=_host("5thva"))
    with sessionmaker_for(unit_db_url_for_slug("5thva"))() as s:
        cand = s.query(Candidacy).filter_by(discord_id=55).one()
        assert "Two years" in (cand.answers or "")


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


def test_command_tent_shows_setup_checklist():
    c = TestClient(app)
    c.post("/auth/dev", data={"discord_id": 7, "name": "Gen. Test", "tier": "admin"},
           headers=_host("2ndus"), follow_redirects=False)
    html = c.get("/command-tent", headers=_host("2ndus")).text
    assert "Getting Started" in html and "setup-card" in html
    assert "/ 8 done" in html
    # a fresh unit has its name set (via _add_unit) but no roles/channels yet
    assert "Set the Admin role" in html and "Build the rank ladder" in html


def test_admin_edits_discord_server_id():
    c = TestClient(app)
    c.post("/auth/dev", data={"discord_id": 7, "name": "Gen. Test", "tier": "admin"},
           headers=_host("2ndus"), follow_redirects=False)
    token = _csrf(c, "/command-tent", _host("2ndus"))
    # change to a fresh, unclaimed server id
    c.post("/admin/discord-link", data={"csrf": token, "guild_id": "777000111"},
           headers=_host("2ndus"))
    with registry_session() as s:
        assert tenant_by_slug(s, "2ndus").discord_guild_id == 777000111
    # a server already linked to another unit (5thva) is refused
    fifth = None
    with registry_session() as s:
        fifth = tenant_by_slug(s, "5thva").discord_guild_id
    token = _csrf(c, "/command-tent", _host("2ndus"))
    c.post("/admin/discord-link", data={"csrf": token, "guild_id": str(fifth)},
           headers=_host("2ndus"))
    with registry_session() as s:
        assert tenant_by_slug(s, "2ndus").discord_guild_id == 777000111  # unchanged
    # non-digits are rejected
    token = _csrf(c, "/command-tent", _host("2ndus"))
    c.post("/admin/discord-link", data={"csrf": token, "guild_id": "not-a-number"},
           headers=_host("2ndus"))
    with registry_session() as s:
        assert tenant_by_slug(s, "2ndus").discord_guild_id == 777000111  # unchanged
    # blank unlinks
    token = _csrf(c, "/command-tent", _host("2ndus"))
    c.post("/admin/discord-link", data={"csrf": token, "guild_id": ""},
           headers=_host("2ndus"))
    with registry_session() as s:
        assert tenant_by_slug(s, "2ndus").discord_guild_id is None


def test_discord_invite_replaces_web_apply():
    c = TestClient(app)
    c.post("/auth/dev", data={"discord_id": 7, "name": "Adm", "tier": "admin"},
           headers=_host("5thva"), follow_redirects=False)
    c.post("/admin/identity",
           data={"csrf": _csrf(c, "/command-tent", _host("5thva")), "regiment_name": "5th Virginia",
                 "brand_color": "#7c1f2b", "inactivity_days": "30",
                 "discord_invite": "https://discord.gg/xyz789"},
           headers=_host("5thva"))
    # a signed-out visitor gets a Discord invite button — no OAuth/apply needed
    v = TestClient(app)
    jp = v.get("/join", headers=_host("5thva")).text
    assert "Join our Discord" in jp and "discord.gg/xyz789" in jp
    # and the Find-a-Unit card links to the invite too
    d = v.get("/find", headers={"host": APEX}).text
    assert "discord.gg/xyz789" in d and "Join Discord" in d


def test_join_page_and_directory_counts():
    c = TestClient(app)
    html = c.get("/join", headers=_host("5thva")).text
    assert "Enlist with 5th Virginia" in html
    assert "recruiting" in html.lower()
    # the Find-a-Unit page shows a member count and links to each join page
    d = c.get("/find", headers={"host": APEX}).text
    assert "Learn more" in d and "member" in d


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


def test_find_page_groups_and_filters_by_game():
    with registry_session() as s:
        fifth = tenant_by_slug(s, "5thva")
        fifth.game = "War of Rights"; fifth.tags = "Milsim, EU"
        tenant_by_slug(s, "2ndus").game = "Squad"
        s.commit()
    c = TestClient(app)
    html = c.get("/find", headers={"host": APEX}).text
    # per-game section grouping + filter chips + the search tooling
    assert 'data-game="War of Rights"' in html and 'data-game="Squad"' in html
    assert 'id="dir-search"' in html and 'id="dir-filters"' in html
    # tags render on the card and feed search
    assert "Milsim" in html


def test_listing_editor_saves_game_and_tags():
    c = TestClient(app)
    c.post("/auth/dev", data={"discord_id": 7, "name": "Gen", "tier": "admin"},
           headers=_host("5thva"), follow_redirects=False)
    token = _csrf(c, "/command-tent", _host("5thva"))
    c.post("/admin/listing",
           data={"csrf": token, "name": "5th Virginia", "game": "War of Rights",
                 "tags": " Milsim ,, EU ", "listed": "1"}, headers=_host("5thva"))
    with registry_session() as s:
        row = tenant_by_slug(s, "5thva")
        assert row.game == "War of Rights"
        assert row.tags == "Milsim, EU"  # normalised: trimmed, blanks dropped


def test_open_registration_overrides_admin_allowlist():
    """PLATFORM_OPEN_REGISTRATION opens registration to any signed-in user even
    when a PLATFORM_ADMIN_IDS allowlist is set (admins keep their extra powers,
    the door is simply open)."""
    old_admin = os.environ.get("PLATFORM_ADMIN_IDS")
    os.environ["PLATFORM_ADMIN_IDS"] = "9999"  # a different user is the admin
    os.environ["PLATFORM_OPEN_REGISTRATION"] = "1"
    try:
        c = TestClient(app)
        # a non-admin signed-in user
        c.post("/auth/dev", data={"discord_id": 42, "name": "Newcomer", "tier": "none"},
               headers={"host": APEX}, follow_redirects=False)
        html = c.get("/register", headers={"host": APEX}).text
        assert "Registration Closed" not in html and "New Unit" in html
        # they can create a unit despite not being on the admin allowlist
        token = _csrf(c, "/register", {"host": APEX})
        c.post("/register",
               data={"csrf": token, "slug": "opened", "name": "Opened Up", "guild_id": ""},
               headers={"host": APEX}, follow_redirects=False)
        with registry_session() as s:
            assert tenant_by_slug(s, "opened") is not None
    finally:
        os.environ["PLATFORM_OPEN_REGISTRATION"] = "1"  # restore module default
        if old_admin is None:
            os.environ.pop("PLATFORM_ADMIN_IDS", None)
        else:
            os.environ["PLATFORM_ADMIN_IDS"] = old_admin


class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeDiscord:
    """Stands in for httpx during OAuth: knows which guilds the user is in and
    their roles in each, so we can exercise membership resolution offline."""

    def __init__(self, guild_roles: dict[int, list[str]], nicks: dict[int, str]):
        self.guild_roles = guild_roles  # guild_id -> role id strings
        self.nicks = nicks

    def get(self, url, headers=None):
        if url.endswith("/users/@me/guilds"):
            return _FakeResp(200, [{"id": str(g)} for g in self.guild_roles])
        # .../guilds/{gid}/member
        gid = int(url.rstrip("/").split("/")[-2])
        if gid not in self.guild_roles:
            return _FakeResp(404, {})
        return _FakeResp(200, {"roles": self.guild_roles[gid], "nick": self.nicks.get(gid)})


class _Req:
    def __init__(self, user):
        self.session = {"user": user}
        self.state = type("S", (), {})()


def test_one_signin_resolves_tier_across_all_units():
    """A single OAuth grant resolves the user's tier on every unit they belong
    to — admin on their own unit, plain member on another — so they never sign
    in twice."""
    from web.auth import _resolve_membership, effective_user

    # guild ids the fixture assigned each unit
    with registry_session() as s:
        g_5th = tenant_by_slug(s, "5thva").discord_guild_id
        g_2nd = tenant_by_slug(s, "2ndus").discord_guild_id
    # make role 111 the admin role in 5thva only
    with sessionmaker_for(unit_db_url_for_slug("5thva"))() as s:
        get_config(s).admin_role_id = 111
        s.commit()

    fake = _FakeDiscord(
        guild_roles={g_5th: ["111"], g_2nd: []},  # admin in 5thva, no role in 2ndus
        nicks={g_5th: "Col. Reb"},
    )
    me = {"id": 500, "global_name": "RebGaming", "username": "reb", "avatar": None}
    tiers, nick = _resolve_membership(fake, {}, me, "5thva")

    assert tiers == {"5thva": "admin", "2ndus": "none"}
    assert nick == "Col. Reb"

    # effective_user honors the map per-unit: admin where they hold the role,
    # a recognized member elsewhere, a visitor on units they don't belong to.
    req = _Req({"id": 500, "name": "Reb", "via": "discord", "tiers": tiers})
    assert effective_user(req, "5thva")["tier"] == "admin"
    assert effective_user(req, "2ndus")["tier"] == "none"
    assert effective_user(req, "hq") is None


def test_unit_switcher_lists_the_users_units():
    """A user who belongs to more than one unit gets a header switcher listing
    them, with the current unit flagged."""
    from web.app import _my_units

    req = _Req({"id": 9, "name": "X", "via": "discord",
                "tiers": {"5thva": "admin", "2ndus": "none"}})
    units = _my_units(req, "5thva")
    by_slug = {u["slug"]: u for u in units}
    assert set(by_slug) == {"5thva", "2ndus"}
    assert by_slug["5thva"]["current"] is True
    assert by_slug["2ndus"]["current"] is False
    assert by_slug["2ndus"]["url"].endswith("2ndus.valorlink.co/")

    # a user in only one unit gets no switcher
    solo = _Req({"id": 9, "tiers": {"5thva": "admin"}})
    assert _my_units(solo, "5thva") == []


def test_platform_dashboard_requires_platform_admin():
    """The cross-unit dashboard is gated to the PLATFORM_ADMIN_IDS allowlist."""
    c = TestClient(app)
    c.post("/auth/dev", data={"discord_id": 9, "name": "Nobody", "tier": "admin"},
           headers={"host": APEX}, follow_redirects=False)
    # no allowlist configured → not a platform admin → refused
    r = c.get("/admin/platform", headers={"host": APEX}, follow_redirects=False)
    assert r.status_code in (302, 303, 403)

    old = os.environ.get("PLATFORM_ADMIN_IDS")
    os.environ["PLATFORM_ADMIN_IDS"] = "9"
    try:
        html = c.get("/admin/platform", headers={"host": APEX}).text
        assert "Platform Dashboard" in html
        assert "5th Virginia" in html and "2nd United States" in html
    finally:
        if old is None:
            os.environ.pop("PLATFORM_ADMIN_IDS", None)
        else:
            os.environ["PLATFORM_ADMIN_IDS"] = old


def test_platform_broadcast_queues_update_to_every_unit():
    """A platform admin's update lands on every unit's queue exactly once, so
    the bot can post it to each admin-log channel."""
    import json
    from db.models import PendingAction
    from utils.queue import PLATFORM_BROADCAST
    c = TestClient(app)
    c.post("/auth/dev", data={"discord_id": 9, "name": "Owner", "tier": "admin"},
           headers={"host": APEX}, follow_redirects=False)
    old = os.environ.get("PLATFORM_ADMIN_IDS")
    os.environ["PLATFORM_ADMIN_IDS"] = "9"
    try:
        token = _csrf(c, "/admin/platform", {"host": APEX})
        r = c.post("/admin/platform/broadcast",
                   data={"csrf": token, "title": "Update", "body": "New features are live."},
                   headers={"host": APEX}, follow_redirects=False)
        assert r.status_code == 303
        all_dbs = [os.environ["DATABASE_URL"]] + [unit_db_url_for_slug(s) for s in UNITS]
        for db_url in all_dbs:
            with sessionmaker_for(db_url)() as s:
                rows = s.query(PendingAction).filter(
                    PendingAction.action == PLATFORM_BROADCAST).all()
                assert len(rows) == 1
                assert json.loads(rows[0].payload)["body"] == "New features are live."
    finally:
        if old is None:
            os.environ.pop("PLATFORM_ADMIN_IDS", None)
        else:
            os.environ["PLATFORM_ADMIN_IDS"] = old


def test_platform_broadcast_requires_platform_admin():
    """A signed-in non-admin cannot broadcast to every unit."""
    from db.models import PendingAction
    from utils.queue import PLATFORM_BROADCAST
    c = TestClient(app)
    c.post("/auth/dev", data={"discord_id": 42, "name": "Nobody", "tier": "admin"},
           headers={"host": APEX}, follow_redirects=False)
    # no allowlist → not a platform admin → refused, nothing queued
    r = c.post("/admin/platform/broadcast",
               data={"csrf": "x", "body": "sneaky"},
               headers={"host": APEX}, follow_redirects=False)
    assert r.status_code in (302, 303, 403)
    with sessionmaker_for(unit_db_url_for_slug("5thva"))() as s:
        assert s.query(PendingAction).filter(
            PendingAction.action == PLATFORM_BROADCAST).count() == 0


def _seed_soldier(uid):
    """A member serving (and promoted) in 5thva and dishonorably discharged
    from 2ndus."""
    from db.models import Member, ServiceHistoryEntry
    with sessionmaker_for(unit_db_url_for_slug("5thva"))() as s:
        s.add(Member(discord_id=uid, callsign="Trooper", rank="Sergeant",
                     company="Alpha", status="active"))
        s.add(ServiceHistoryEntry(
            member_id=uid, entry="Promoted from Corporal to Sergeant by Colonel Reed."))
        s.commit()
    with sessionmaker_for(unit_db_url_for_slug("2ndus"))() as s:
        s.add(Member(discord_id=uid, callsign="Trooper", rank="Corporal",
                     company="Bravo", status="discharged", discharge_type="dishonorable"))
        s.add(ServiceHistoryEntry(
            member_id=uid, entry="Dishonorably discharged. Reason: repeated misconduct."))
        s.commit()


def test_service_record_owner_sees_everything():
    uid = 7001
    _seed_soldier(uid)
    c = TestClient(app)
    c.post("/auth/dev", data={"discord_id": uid, "name": "Trooper", "tier": "none"},
           headers={"host": APEX}, follow_redirects=False)
    r = c.get("/me", headers={"host": APEX}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/u/{uid}"
    html = c.get(f"/u/{uid}", headers={"host": APEX}).text
    assert "5th Virginia" in html and "2nd United States" in html
    assert "Dishonorable" in html      # discharge type
    assert "misconduct" in html        # owner sees the written reason


def test_service_record_recruiter_sees_type_not_reason():
    uid = 7002
    _seed_soldier(uid)
    c = TestClient(app)
    c.post("/auth/dev", data={"discord_id": 9100, "name": "Rec", "tier": "recruiter"},
           headers={"host": APEX}, follow_redirects=False)
    html = c.get(f"/u/{uid}", headers={"host": APEX}).text
    assert "Dishonorable" in html       # vetting signal is shown
    assert "misconduct" not in html     # but never the reason


def test_service_record_is_public_positive_only():
    uid = 7003
    _seed_soldier(uid)
    c = TestClient(app)
    # a signed-out visitor: every record is public, no login needed
    r = c.get(f"/u/{uid}", headers={"host": APEX})
    assert r.status_code == 200
    assert "5th Virginia" in r.text            # positive record visible to anyone
    assert "Promoted to Sergeant" in r.text    # promotions shown to everyone
    assert "Dishonorable" not in r.text        # discharge type hidden from the public
    assert "misconduct" not in r.text          # reasons never shown


def test_service_record_not_found_is_404():
    c = TestClient(app)
    r = c.get("/u/999999", headers={"host": APEX})
    assert r.status_code == 404 and "No Record Found" in r.text


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
