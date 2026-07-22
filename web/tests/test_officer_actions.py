"""End-to-end tests for the officer-facing write features.

Exercises the web endpoints against a temporary database (data changes +
audit trail + queued Discord actions), permission gating, CSRF, and the
bot-side bridge dispatch with mocked Discord objects.

Runs under pytest or directly:  python -m web.tests.test_officer_actions
"""
import asyncio
import os
import re
import tempfile
from unittest.mock import AsyncMock, MagicMock

# A throwaway database, wired up before any app/db import reads DATABASE_URL.
_TMP = tempfile.mkdtemp(prefix="valorlink-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["REGISTRY_DATABASE_URL"] = f"sqlite:///{_TMP}/registry.db"
os.environ.pop("PLATFORM_BASE_DOMAIN", None)   # single-tenant: everything resolves to default
os.environ["WEB_DEV_LOGIN"] = "1"
os.environ["WEB_SESSION_SECRET"] = "test-secret"

import config  # noqa: E402
config.DATABASE_URL = os.environ["DATABASE_URL"]

from fastapi.testclient import TestClient  # noqa: E402

from db.base import Base, SessionLocal, engine  # noqa: E402
from db.models import (  # noqa: E402
    AttendanceRecord,
    AwardType,
    Candidacy,
    Company,
    DisciplinaryRecord,
    Event,
    Member,
    MemberAward,
    PendingAction,
    Rank,
    ServiceHistoryEntry,
)
from utils import queue  # noqa: E402
from utils.settings import get_config  # noqa: E402
from web.app import app  # noqa: E402

MEMBER_ID = 100
CANDIDATE_ID = 200


def _seed():
    with SessionLocal() as s:
        get_config(s)  # creates the singleton config row
        for i, (name, abbr) in enumerate([("Private", "Pvt"), ("Corporal", "Cpl"), ("Sergeant", "Sgt")]):
            s.add(Rank(name=name, abbreviation=abbr, position=i))
        s.add(Company(name="Alpha", is_default=True))
        s.add(Company(name="Bravo"))
        s.add(Member(discord_id=MEMBER_ID, callsign="Testman", rank="Private", company="Alpha", status="active"))
        s.add(Candidacy(discord_id=CANDIDATE_ID, callsign="Applicant"))
        s.add(AwardType(name="Marksman", created_by=1))
        s.commit()


def _reset():
    """Fresh schema + seed data before each test, so tests are independent."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    _seed()


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


def _csrf(client, path=f"/dossier/{MEMBER_ID}"):
    html = client.get(path).text
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no CSRF token found on page"
    return m.group(1)


def _actions(action=None):
    with SessionLocal() as s:
        q = s.query(PendingAction)
        if action:
            q = q.filter(PendingAction.action == action)
        return q.all()


def _member():
    with SessionLocal() as s:
        return s.get(Member, MEMBER_ID)


# --------------------------------------------------------------------------- #

def test_unauthenticated_write_is_rejected():
    client = TestClient(app)
    r = client.post(f"/members/{MEMBER_ID}/rank",
                    data={"csrf": "x", "rank": "Sergeant"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert _member().rank == "Private"  # unchanged
    assert _actions() == []


def test_promote_changes_data_and_queues_sync():
    client = TestClient(app)
    _login(client, "officer")
    token = _csrf(client)
    r = client.post(f"/members/{MEMBER_ID}/rank",
                    data={"csrf": token, "rank": "Sergeant", "citation": "For valor", "mode": "promote"})
    assert r.status_code == 200

    with SessionLocal() as s:
        m = s.get(Member, MEMBER_ID)
        assert m.rank == "Sergeant"
        entries = [e.entry for e in m.service_history]
        assert any("Promoted from Private to Sergeant" in e and "For valor" in e for e in entries)

    acts = _actions(queue.SYNC_RANK)
    assert len(acts) == 1
    import json
    payload = json.loads(acts[0].payload)
    assert payload["new_rank"] == "Sergeant"
    assert "promoted to" in payload["billboard"]
    assert acts[0].actor_id == 1


def test_bad_csrf_makes_no_change():
    client = TestClient(app)
    _login(client, "officer")
    before = _member().company
    r = client.post(f"/members/{MEMBER_ID}/company", data={"csrf": "wrong", "company": "Bravo"})
    assert r.status_code == 200
    assert "session expired" in r.text.lower()
    assert _member().company == before  # unchanged
    assert _actions(queue.SYNC_COMPANY) == []


def test_company_service_discipline_loa_lifecycle():
    client = TestClient(app)
    _login(client, "officer")

    # company transfer
    client.post(f"/members/{MEMBER_ID}/company", data={"csrf": _csrf(client), "company": "Bravo"})
    assert _member().company == "Bravo"
    assert len(_actions(queue.SYNC_COMPANY)) == 1

    # service log
    client.post(f"/members/{MEMBER_ID}/service-log",
                data={"csrf": _csrf(client), "entry": "Detailed to the color guard."})
    assert len(_actions(queue.REFRESH_PERSONNEL)) >= 1

    # discipline
    client.post(f"/members/{MEMBER_ID}/discipline",
                data={"csrf": _csrf(client), "record_type": "strike", "reason": "AWOL"})
    with SessionLocal() as s:
        strikes = s.query(DisciplinaryRecord).filter_by(member_id=MEMBER_ID, record_type="strike").count()
        assert strikes == 1
    assert len(_actions(queue.DISCIPLINE)) == 1

    # leave of absence, then end it
    client.post(f"/members/{MEMBER_ID}/loa", data={"csrf": _csrf(client), "days": "5", "reason": "travel"})
    assert _member().status == "loa"
    assert len(_actions(queue.LOA)) == 1
    client.post(f"/members/{MEMBER_ID}/loa-end", data={"csrf": _csrf(client)})
    assert _member().status == "active"
    assert len(_actions(queue.LOA_END)) == 1

    # discharge then reinstate
    client.post(f"/members/{MEMBER_ID}/discharge",
                data={"csrf": _csrf(client), "discharge_type": "honorable", "reason": "end of term"})
    assert _member().status == "discharged"
    assert len(_actions(queue.DISCHARGE)) == 1
    client.post(f"/members/{MEMBER_ID}/reinstate", data={"csrf": _csrf(client), "reason": "returned"})
    assert _member().status == "active"
    assert len(_actions(queue.REINSTATE)) == 1


def test_recruiter_can_approve_candidate():
    client = TestClient(app)
    _login(client, "recruiter")
    token = _csrf(client, "/recruits")
    r = client.post(f"/recruits/{CANDIDATE_ID}/approve", data={"csrf": token})
    assert r.status_code == 200
    with SessionLocal() as s:
        assert s.get(Candidacy, CANDIDATE_ID) is None
        m = s.get(Member, CANDIDATE_ID)
        assert m is not None and m.status == "active" and m.rank == "Private"
    assert len(_actions(queue.APPROVE_CANDIDATE)) == 1


def test_recruit_pipeline_stage_and_notes():
    client = TestClient(app)
    _login(client, "recruiter")
    # move the seeded applicant along the pipeline
    token = _csrf(client, "/recruits")
    client.post(f"/recruits/{CANDIDATE_ID}/stage",
                data={"csrf": token, "stage": "interviewing"})
    with SessionLocal() as s:
        assert s.get(Candidacy, CANDIDATE_ID).stage == "interviewing"
    # add recruiter notes
    token = _csrf(client, "/recruits")
    client.post(f"/recruits/{CANDIDATE_ID}/notes",
                data={"csrf": token, "notes": "Strong applicant, plays evenings."})
    with SessionLocal() as s:
        assert s.get(Candidacy, CANDIDATE_ID).notes == "Strong applicant, plays evenings."
    # a bad stage is rejected, leaving the value unchanged
    token = _csrf(client, "/recruits")
    client.post(f"/recruits/{CANDIDATE_ID}/stage", data={"csrf": token, "stage": "bogus"})
    with SessionLocal() as s:
        assert s.get(Candidacy, CANDIDATE_ID).stage == "interviewing"
    # the board renders the three columns
    html = client.get("/recruits").text
    assert "At the Gate" in html and "In Interview" in html and "Awaiting Decision" in html


def test_promotion_board_flags_eligible_and_promotes():
    from datetime import datetime, timedelta
    client = TestClient(app)
    _login(client, "officer")
    now = datetime.utcnow()
    with SessionLocal() as s:
        m = s.get(Member, MEMBER_ID)          # Private, eligible after time in rank
        m.rank_since = now - timedelta(days=40)
        s.add(Member(discord_id=600, callsign="Fresh", rank="Private", company="Alpha",
                     status="active", rank_since=now))   # too new to be ready
        s.commit()

    html = client.get("/promotions").text
    assert "Promotion Board" in html and "1 ready" in html  # only Testman is ready
    assert "Testman" in html and "Fresh" in html

    # promote Testman to the next rank from the board
    token = _csrf(client, "/promotions")
    r = client.post(f"/members/{MEMBER_ID}/rank",
                    data={"csrf": token, "rank": "Corporal", "mode": "promote"})
    assert r.status_code == 200
    with SessionLocal() as s:
        m = s.get(Member, MEMBER_ID)
        assert m.rank == "Corporal"
        assert m.rank_since >= now - timedelta(seconds=5)  # reset on promotion


def test_insufficient_tier_is_forbidden():
    client = TestClient(app)
    _login(client, "none")
    # The tier check runs before CSRF, so the token value is irrelevant here.
    r = client.post(f"/members/{MEMBER_ID}/rank",
                    data={"csrf": "x", "rank": "Corporal"}, follow_redirects=False)
    assert r.status_code == 403
    assert _member().rank == "Private"


def test_bridge_dispatch_applies_discord_side_effects():
    from cogs.bridge import Bridge

    bridge = object.__new__(Bridge)  # skip __init__ (which would start the loop)
    bridge.bot = MagicMock()

    guild = MagicMock()
    member = AsyncMock()
    member.mention = "<@100>"
    member.roles = []
    member.guild = guild
    guild.get_member.return_value = member
    guild.get_role.return_value = None
    guild.get_channel.return_value = None

    async def run():
        # a known action dispatches without error
        await bridge._dispatch(guild, queue.DISCIPLINE, {
            "discord_id": MEMBER_ID, "record_type": "note", "reason": "x",
            "issued_by": 1, "dm": "hello",
        })
        member.send.assert_awaited()  # DM attempted
        # an unknown action raises (so the queue marks it failed)
        raised = False
        try:
            await bridge._dispatch(guild, "no_such_action", {})
        except ValueError:
            raised = True
        assert raised

    asyncio.run(run())

    # _finish transitions: retries then FAILED
    with SessionLocal() as s:
        row = queue.enqueue(s, "bad", {})
        s.commit()
        rid = row.id
    for _ in range(3):
        bridge._finish(rid, None, error="boom")
    with SessionLocal() as s:
        assert s.get(PendingAction, rid).status == queue.FAILED


def test_officer_posts_announcement():
    client = TestClient(app)
    _login(client, "officer")
    # without an announcements channel, it's refused and nothing is queued
    token = _csrf(client, "/muster-calls")
    client.post("/announce", data={"csrf": token, "title": "Orders", "body": "Muster up."})
    assert len(_actions(queue.POST_ANNOUNCEMENT)) == 0
    # once a channel is configured, the announcement is queued
    with SessionLocal() as s:
        get_config(s).announcements_channel_id = 555
        s.commit()
    token = _csrf(client, "/muster-calls")
    r = client.post("/announce", data={"csrf": token, "title": "Orders", "body": "Muster Saturday."})
    assert r.status_code == 200
    assert len(_actions(queue.POST_ANNOUNCEMENT)) == 1


def test_bridge_posts_announcement():
    from cogs.bridge import Bridge
    bridge = object.__new__(Bridge)
    bridge.bot = MagicMock()
    guild = MagicMock()
    channel = AsyncMock()
    guild.get_channel.return_value = channel
    with SessionLocal() as s:
        get_config(s).announcements_channel_id = 555
        s.commit()

    async def run():
        await bridge._dispatch(guild, queue.POST_ANNOUNCEMENT,
                               {"title": "T", "body": "B", "actor_id": 1, "actor_name": "Off"})
    asyncio.run(run())
    channel.send.assert_awaited()


def test_admin_imports_roster_enqueues():
    client = TestClient(app)
    _login(client, "admin")
    token = _csrf(client, "/command-tent")
    r = client.post("/admin/import-roster", data={"csrf": token, "role_id": ""})
    assert r.status_code == 200
    assert len(_actions(queue.IMPORT_ROSTER)) == 1


def test_bridge_import_roster_creates_new_members_only():
    from cogs.bridge import Bridge
    bridge = object.__new__(Bridge)
    bridge.bot = MagicMock()

    def mk(uid, name, is_bot=False):
        m = MagicMock()
        m.id = uid; m.bot = is_bot; m.name = name; m.nick = None
        m.display_name = name; m.roles = []; m.avatar = None
        return m

    people = [mk(999, "BotGuy", is_bot=True), mk(MEMBER_ID, "Testman"), mk(500, "Newbie")]

    async def fake_fetch(limit=None):
        for m in people:
            yield m

    guild = MagicMock()
    guild.fetch_members = fake_fetch
    guild.get_role.return_value = None
    guild.get_channel.return_value = None

    async def run():
        await bridge._dispatch(guild, queue.IMPORT_ROSTER,
                               {"actor_id": 1, "role_id": None,
                                "default_rank": "Private", "default_company": "Alpha"})
    asyncio.run(run())

    with SessionLocal() as s:
        newcomer = s.get(Member, 500)
        assert newcomer is not None and newcomer.rank == "Private" and newcomer.company == "Alpha"
        assert s.query(Member).filter_by(discord_id=999).count() == 0  # bot skipped


def test_discord_name_change_updates_callsign():
    import cogs.roster as roster_mod
    from cogs.roster import Roster

    url = os.environ["DATABASE_URL"]
    saved = (roster_mod.db_url_for_guild, roster_mod.bind_guild, roster_mod.refresh_roster)
    roster_mod.db_url_for_guild = lambda gid: url
    roster_mod.bind_guild = lambda gid: roster_mod.set_current_db_url(url)
    roster_mod.refresh_roster = AsyncMock()
    try:
        cog = object.__new__(Roster)
        cog.bot = MagicMock()
        guild = MagicMock(); guild.id = 1

        # server nickname change → callsign follows (rank prefix stripped)
        before, after = MagicMock(), MagicMock()
        before.nick = "Pvt. Testman"; after.nick = "Pvt. Renamed"
        after.name = "testman_user"; after.id = MEMBER_ID; after.guild = guild
        asyncio.run(cog.on_member_update(before, after))
        with SessionLocal() as s:
            assert s.get(Member, MEMBER_ID).callsign == "Renamed"

        # global username change, no server nickname → callsign follows
        member = MagicMock(); member.nick = None
        guild.get_member.return_value = member
        cog.bot.guilds = [guild]
        ub, ua = MagicMock(), MagicMock()
        ub.name = "old"; ua.name = "NewUser"; ua.id = MEMBER_ID
        ub.avatar = ua.avatar = None
        asyncio.run(cog.on_user_update(ub, ua))
        with SessionLocal() as s:
            assert s.get(Member, MEMBER_ID).callsign == "NewUser"

        # username change is ignored when a server nickname is set (nick wins)
        member.nick = "Pvt. KeepThis"
        ub2, ua2 = MagicMock(), MagicMock()
        ub2.name = "NewUser"; ua2.name = "Ignored"; ua2.id = MEMBER_ID
        ub2.avatar = ua2.avatar = None
        asyncio.run(cog.on_user_update(ub2, ua2))
        with SessionLocal() as s:
            assert s.get(Member, MEMBER_ID).callsign == "NewUser"  # unchanged
    finally:
        (roster_mod.db_url_for_guild, roster_mod.bind_guild, roster_mod.refresh_roster) = saved


def test_member_profile_and_loa_self_service():
    client = TestClient(app)
    _login(client, "none", discord_id=MEMBER_ID, name="Testman")

    def mcsrf(path="/my-record"):
        m = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)
        return m.group(1)

    client.post("/my-record/profile",
                data={"csrf": mcsrf(), "ingame_name": "TM", "timezone": "UTC",
                      "availability": ["Fri", "Sat"], "bio": "Line infantry."})
    with SessionLocal() as s:
        m = s.get(Member, MEMBER_ID)
        assert m.ingame_name == "TM" and m.availability == "Fri,Sat" and m.bio == "Line infantry."

    client.post("/my-record/request-loa", data={"csrf": mcsrf(), "days": "10", "reason": "exams"})
    with SessionLocal() as s:
        assert s.get(Member, MEMBER_ID).loa_requested_until is not None

    # officer approves from the leave board
    _login(client, "officer")
    client.post(f"/members/{MEMBER_ID}/loa-request/approve", data={"csrf": _csrf(client, "/leave")})
    with SessionLocal() as s:
        m = s.get(Member, MEMBER_ID)
        assert m.status == "loa" and m.loa_until is not None and m.loa_requested_until is None
    assert len(_actions(queue.LOA)) >= 1


def test_identity_theme_switch():
    client = TestClient(app)
    _login(client, "admin")
    assert 'theme-parchment' in client.get("/").text
    client.post("/admin/identity",
                data={"csrf": _csrf(client, "/command-tent"), "regiment_name": "7th Rangers",
                      "brand_color": "#33aa55", "inactivity_days": "30", "theme": "modern"})
    with SessionLocal() as s:
        assert get_config(s).theme == "modern"
    assert 'theme-modern' in client.get("/").text


def test_headquarters_activity_feed():
    from datetime import datetime, timedelta
    client = TestClient(app)
    _login(client, "officer")
    with SessionLocal() as s:
        s.get(Member, MEMBER_ID).joined_date = datetime.utcnow() - timedelta(days=1)
        s.commit()
    # promote (Private -> Corporal)
    client.post(f"/members/{MEMBER_ID}/rank",
                data={"csrf": _csrf(client), "rank": "Corporal", "mode": "promote"})
    # grant the seeded award
    with SessionLocal() as s:
        award_id = s.query(AwardType).filter_by(name="Marksman").one().id
    client.post(f"/members/{MEMBER_ID}/award",
                data={"csrf": _csrf(client), "award_type_id": award_id})

    html = client.get("/").text
    assert "Recent Activity" in html
    assert "enlisted" in html
    assert "was promoted to Corporal" in html
    assert "earned Marksman" in html


def test_custom_terminology_overrides_and_reset():
    client = TestClient(app)
    _login(client, "admin")
    # override one term (and event kinds) on top of the default wor preset
    client.post("/admin/terminology",
                data={"csrf": _csrf(client, "/command-tent"), "events_nav": "War Room",
                      "event_types": "Muster, Siege"})
    with SessionLocal() as s:
        assert "War Room" in (get_config(s).terminology_custom or "")
    nav = client.get("/").text
    assert "War Room" in nav and "Muster Roll" in nav  # override applied, others intact
    assert ">Siege<" in client.get("/muster-calls").text
    # reset drops the overrides
    client.post("/admin/terminology/reset", data={"csrf": _csrf(client, "/command-tent")})
    with SessionLocal() as s:
        assert get_config(s).terminology_custom is None
    assert "War Room" not in client.get("/").text


def test_recurring_events_and_after_action():
    from datetime import datetime, timedelta
    client = TestClient(app)
    _login(client, "officer")
    client.post("/muster-calls/create",
                data={"csrf": _csrf(client, "/muster-calls"), "name": "Weekly Drill",
                      "event_type": "Drill", "date": "2099-02-01", "time": "19:00",
                      "tz_offset": "0", "repeat_weeks": "4"})
    with SessionLocal() as s:
        evs = s.query(Event).filter_by(name="Weekly Drill").order_by(Event.scheduled_at).all()
        assert len(evs) == 4
        # spaced one week apart
        assert (evs[1].scheduled_at - evs[0].scheduled_at) == timedelta(weeks=1)

    with SessionLocal() as s:
        pe = Event(name="Past Battle", event_type="Battle",
                   scheduled_at=datetime.utcnow() - timedelta(days=1), created_by=1)
        s.add(pe); s.commit(); peid = pe.id
    client.post(f"/muster-calls/{peid}/after-action",
                data={"csrf": _csrf(client, f"/muster-calls/{peid}"),
                      "outcome": "Victory", "notes": "Held the bridge."})
    with SessionLocal() as s:
        pe = s.get(Event, peid)
        assert pe.outcome == "Victory" and pe.after_action == "Held the bridge."


def test_member_can_rsvp_from_the_web():
    from datetime import datetime
    client = TestClient(app)
    with SessionLocal() as s:
        ev = Event(name="Evening Drill", event_type="Drill",
                   scheduled_at=datetime(2099, 1, 1), created_by=1)
        s.add(ev); s.commit(); eid = ev.id
    # the member (MEMBER_ID) signs in and answers the call
    _login(client, "none", discord_id=MEMBER_ID, name="Testman")
    client.post(f"/muster-calls/{eid}/rsvp",
                data={"csrf": _csrf(client, f"/muster-calls/{eid}"), "status": "accepted"})
    with SessionLocal() as s:
        rec = s.query(AttendanceRecord).filter_by(event_id=eid, member_id=MEMBER_ID).one()
        assert rec.status == "accepted"
    # changing the reply updates in place (no duplicate)
    client.post(f"/muster-calls/{eid}/rsvp",
                data={"csrf": _csrf(client, f"/muster-calls/{eid}"), "status": "declined"})
    with SessionLocal() as s:
        recs = s.query(AttendanceRecord).filter_by(event_id=eid, member_id=MEMBER_ID).all()
        assert len(recs) == 1 and recs[0].status == "declined"


def test_my_record_shows_own_dossier():
    client = TestClient(app)
    _login(client, "none", discord_id=MEMBER_ID, name="Testman")
    r = client.get("/my-record")
    assert r.status_code == 200 and "Testman" in r.text
    # someone without a record here gets a friendly 404
    _login(client, "none", discord_id=987654, name="Nobody")
    assert client.get("/my-record").status_code == 404


def test_officer_can_create_event_and_mark_attendance():
    client = TestClient(app)
    _login(client, "officer")
    token = _csrf(client, "/muster-calls")
    r = client.post("/muster-calls/create",
                    data={"csrf": token, "name": "Regimental Drill", "event_type": "Drill",
                          "date": "2099-01-02", "time": "19:00"})
    assert r.status_code == 200
    with SessionLocal() as s:
        ev = s.query(Event).filter_by(name="Regimental Drill").one()
        event_id = ev.id
    assert len(_actions(queue.ANNOUNCE_EVENT)) == 1

    # mark attendance for the member
    token = _csrf(client, f"/muster-calls/{event_id}")
    client.post(f"/muster-calls/{event_id}/attendance",
                data={"csrf": token, "member_id": MEMBER_ID, "status": "present"})
    with SessionLocal() as s:
        rec = s.query(AttendanceRecord).filter_by(event_id=event_id, member_id=MEMBER_ID).one()
        assert rec.status == "present"


def test_bulk_company_transfer_and_note():
    client = TestClient(app)
    _login(client, "officer")
    with SessionLocal() as s:
        s.add(Member(discord_id=400, callsign="Bulk1", rank="Private", company="Alpha", status="active"))
        s.add(Member(discord_id=401, callsign="Bulk2", rank="Private", company="Alpha", status="active"))
        s.commit()
    token = _csrf(client, "/muster")
    # bulk transfer both to Bravo
    r = client.post("/muster/bulk",
                    data={"csrf": token, "action": "company", "company": "Bravo",
                          "ids": ["400", "401"]})
    assert r.status_code == 200
    with SessionLocal() as s:
        assert s.get(Member, 400).company == "Bravo"
        assert s.get(Member, 401).company == "Bravo"
    assert len(_actions(queue.SYNC_COMPANY)) == 2

    # bulk service note
    token = _csrf(client, "/muster")
    client.post("/muster/bulk",
                data={"csrf": token, "action": "note", "entry": "Reviewed at muster.",
                      "ids": ["400", "401"]})
    with SessionLocal() as s:
        for mid in (400, 401):
            notes = s.query(ServiceHistoryEntry).filter_by(member_id=mid).count()
            assert notes >= 1


def test_bulk_action_requires_officer():
    client = TestClient(app)
    _login(client, "none", discord_id=9, name="Nobody")
    r = client.post("/muster/bulk",
                    data={"csrf": _csrf(client, "/muster"), "action": "company",
                          "company": "Bravo", "ids": ["400"]},
                    follow_redirects=False)
    assert r.status_code in (302, 303, 403)


def test_attendance_analytics_rate_and_at_risk():
    from datetime import datetime, timedelta
    client = TestClient(app)
    now = datetime.utcnow()
    with SessionLocal() as s:
        # member enrolled well before the calls so they're all eligible
        m = s.get(Member, MEMBER_ID)
        m.joined_date = now - timedelta(days=30)
        # a low-turnout second member to land in the at-risk list
        s.add(Member(discord_id=300, callsign="Slacker", rank="Private",
                     company="Alpha", status="active", joined_date=now - timedelta(days=30)))
        past = []
        for d in (20, 15, 10):
            e = Event(name=f"Past Drill {d}", event_type="Drill",
                      scheduled_at=now - timedelta(days=d), created_by=1)
            s.add(e); past.append(e)
        s.flush()
        for e in past:
            s.add(AttendanceRecord(event_id=e.id, member_id=MEMBER_ID, status="present"))
        # Slacker: present once, absent twice -> 33% -> at risk
        s.add(AttendanceRecord(event_id=past[0].id, member_id=300, status="present"))
        s.add(AttendanceRecord(event_id=past[1].id, member_id=300, status="absent"))
        s.add(AttendanceRecord(event_id=past[2].id, member_id=300, status="absent"))
        s.commit()

    html = client.get("/attendance").text
    assert "calls held" in html and "100%" in html          # Testman perfect turnout
    at_risk_block = html.split("By Member")[0]
    assert "Slacker" in at_risk_block                        # flagged at risk
    assert "Testman" not in at_risk_block                    # perfect turnout not flagged


def test_award_grant_and_revoke():
    client = TestClient(app)
    _login(client, "officer")
    with SessionLocal() as s:
        award_id = s.query(AwardType).filter_by(name="Marksman").one().id

    token = _csrf(client)
    client.post(f"/members/{MEMBER_ID}/award",
                data={"csrf": token, "award_type_id": award_id, "notes": "sharp eye"})
    with SessionLocal() as s:
        assert s.query(MemberAward).filter_by(member_id=MEMBER_ID, award_type_id=award_id).count() == 1
    assert len(_actions(queue.AWARD_GRANTED)) == 1

    token = _csrf(client)
    client.post(f"/members/{MEMBER_ID}/award/{award_id}/revoke", data={"csrf": token})
    with SessionLocal() as s:
        assert s.query(MemberAward).filter_by(member_id=MEMBER_ID, award_type_id=award_id).count() == 0
    assert len(_actions(queue.AWARD_REVOKED)) == 1


def test_officer_can_create_award_type():
    client = TestClient(app)
    _login(client, "officer")
    client.post("/honors/award-type",
                data={"csrf": _csrf(client, "/honors"), "name": "Valor Medal", "emoji": "🎖️",
                      "description": "For gallantry."})
    with SessionLocal() as s:
        assert s.query(AwardType).filter_by(name="Valor Medal").count() == 1


def test_admin_updates_identity_roles_and_ladder():
    client = TestClient(app)
    _login(client, "admin")

    # identity
    client.post("/admin/identity",
                data={"csrf": _csrf(client, "/command-tent"), "regiment_name": "1st Texas",
                      "motto": "Onward", "brand_color": "#123456", "inactivity_days": "45"})
    with SessionLocal() as s:
        cfg = get_config(s)
        assert cfg.regiment_name == "1st Texas" and cfg.brand_color == 0x123456
        assert cfg.inactivity_days_threshold == 45

    # roles
    client.post("/admin/roles",
                data={"csrf": _csrf(client, "/command-tent"), "admin": "111", "officer": "222"})
    with SessionLocal() as s:
        cfg = get_config(s)
        assert cfg.admin_role_id == 111 and cfg.officer_role_id == 222

    # add a rank, move it, then remove it
    client.post("/admin/ranks/add",
                data={"csrf": _csrf(client, "/command-tent"), "name": "Colonel", "abbreviation": "Col"})
    with SessionLocal() as s:
        rank = s.query(Rank).filter_by(name="Colonel").one()
        assert rank.position == 3  # top of a 3-rung ladder
        rid = rank.id
    client.post(f"/admin/ranks/{rid}/move", data={"csrf": _csrf(client, "/command-tent"), "direction": "down"})
    with SessionLocal() as s:
        assert s.query(Rank).filter_by(name="Colonel").one().position == 2
    client.post(f"/admin/ranks/{rid}/remove", data={"csrf": _csrf(client, "/command-tent")})
    with SessionLocal() as s:
        assert s.query(Rank).filter_by(name="Colonel").count() == 0


def test_admin_company_default_and_add():
    client = TestClient(app)
    _login(client, "admin")
    with SessionLocal() as s:
        bravo_id = s.query(Company).filter_by(name="Bravo").one().id
    client.post(f"/admin/companies/{bravo_id}/default", data={"csrf": _csrf(client, "/command-tent")})
    with SessionLocal() as s:
        defaults = [c.name for c in s.query(Company).filter_by(is_default=True).all()]
        assert defaults == ["Bravo"]
    client.post("/admin/companies/add",
                data={"csrf": _csrf(client, "/command-tent"), "name": "Skirmishers", "is_default": "1"})
    with SessionLocal() as s:
        assert [c.name for c in s.query(Company).filter_by(is_default=True).all()] == ["Skirmishers"]


def test_command_tent_is_admin_only():
    client = TestClient(app)
    _login(client, "officer")
    assert client.get("/command-tent", follow_redirects=False).status_code == 403
    r = client.post("/admin/identity",
                    data={"csrf": "x", "regiment_name": "X", "brand_color": "#000000", "inactivity_days": "30"},
                    follow_redirects=False)
    assert r.status_code == 403


def test_bridge_announces_event_to_channel():
    from cogs.bridge import Bridge

    with SessionLocal() as s:
        get_config(s).announcements_channel_id = 555
        ev = Event(name="Battle", event_type="Battle",
                   scheduled_at=__import__("datetime").datetime(2099, 1, 1), created_by=1)
        s.add(ev)
        s.commit()
        event_id = ev.id

    bridge = object.__new__(Bridge)
    bridge.bot = MagicMock()
    guild = MagicMock()
    channel = AsyncMock()
    channel.id = 555
    sent = MagicMock()
    sent.id = 987654321
    channel.send.return_value = sent
    guild.get_channel.return_value = channel

    asyncio.run(bridge._dispatch(guild, queue.ANNOUNCE_EVENT, {"event_id": event_id}))

    channel.send.assert_awaited()
    bridge.bot.add_view.assert_called()
    with SessionLocal() as s:
        assert s.get(Event, event_id).message_id == 987654321


def test_failed_action_shows_and_retries():
    from web import services
    client = TestClient(app)
    _login(client, "admin")
    # a failed action sits in the queue
    with SessionLocal() as s:
        s.add(PendingAction(action=queue.POST_ANNOUNCEMENT, payload="{}",
                            status=queue.FAILED, attempts=3, error="No channel"))
        s.commit()
    # it surfaces on the Command Tent with a retry control
    html = client.get("/command-tent").text
    assert "Bot Action Queue" in html and "Announcement" in html and "No channel" in html
    # admin retries it → back to pending, counters reset
    with SessionLocal() as s:
        aid = s.query(PendingAction).one().id
    r = client.post(f"/admin/actions/{aid}/retry", data={"csrf": _csrf(client, "/command-tent")})
    assert r.status_code == 200
    with SessionLocal() as s:
        row = s.get(PendingAction, aid)
        assert row.status == queue.PENDING and row.attempts == 0 and row.error is None


def test_retry_all_and_dismiss_require_admin():
    client = TestClient(app)
    _login(client, "officer")  # not admin: the require_admin gate fires before CSRF
    r = client.post("/admin/actions/retry-all", data={"csrf": "x"}, follow_redirects=False)
    assert r.status_code in (302, 303, 403)


def test_recruitment_metrics_counts_pipeline():
    from datetime import datetime, timedelta
    from web import services
    with SessionLocal() as s:
        # CANDIDATE_ID already seeded at 'applied'; add two more
        s.add(Candidacy(discord_id=201, callsign="B", stage="interviewing"))
        old = Candidacy(discord_id=202, callsign="C", stage="decision")
        old.created_at = datetime.utcnow() - timedelta(days=20)
        s.add(old)
        s.commit()
    with SessionLocal() as s:
        m = services.recruitment_metrics(s)
    assert m["total"] == 3
    assert m["stages"] == {"applied": 1, "interviewing": 1, "decision": 1}
    assert m["stale"] == 1  # the 20-day-old one
    assert m["oldest"] >= 20


def test_event_reminder_dms_rsvps_once():
    from datetime import datetime, timedelta
    from cogs.bridge import Bridge
    with SessionLocal() as s:
        ev = Event(name="Night Drill", event_type="Drill",
                   scheduled_at=datetime.utcnow() + timedelta(minutes=30), created_by=1)
        s.add(ev)
        s.commit()
        s.refresh(ev)
        eid = ev.id
        s.add(AttendanceRecord(event_id=eid, member_id=MEMBER_ID, status="accepted"))
        s.commit()

    bridge = object.__new__(Bridge)
    bridge.bot = MagicMock()
    bridge._dm = AsyncMock()
    guild = MagicMock()

    asyncio.run(bridge._remind_unit(guild))
    bridge._dm.assert_awaited_once()  # the accepted member got one DM
    with SessionLocal() as s:
        assert s.get(Event, eid).reminder_sent_at is not None

    # a second pass sends nothing (already reminded)
    bridge._dm.reset_mock()
    asyncio.run(bridge._remind_unit(guild))
    bridge._dm.assert_not_awaited()


def test_officer_action_writes_audit_entry():
    from db.models import AuditEntry
    client = TestClient(app)
    _login(client, "officer")
    client.post(f"/members/{MEMBER_ID}/rank",
                data={"csrf": _csrf(client), "rank": "Sergeant", "mode": "promote"})
    with SessionLocal() as s:
        entries = s.query(AuditEntry).all()
        assert any(e.category == "rank" and "Sergeant" in e.summary
                   and e.target_id == MEMBER_ID and e.source == "web" for e in entries)
    # it surfaces on the officer-gated Order Ledger, with a link to the member
    html = client.get("/audit").text
    assert "Order Ledger" in html and "Sergeant" in html and f"/dossier/{MEMBER_ID}" in html


def test_audit_ledger_requires_officer():
    client = TestClient(app)
    _login(client, "none")
    r = client.get("/audit", follow_redirects=False)
    assert r.status_code in (302, 303, 403)


def test_reminder_skips_opted_out_member():
    from datetime import datetime, timedelta
    from cogs.bridge import Bridge
    with SessionLocal() as s:
        s.get(Member, MEMBER_ID).reminders_opt_out = True
        ev = Event(name="Drill", event_type="Drill",
                   scheduled_at=datetime.utcnow() + timedelta(minutes=20), created_by=1)
        s.add(ev)
        s.commit()
        s.refresh(ev)
        eid = ev.id
        s.add(AttendanceRecord(event_id=eid, member_id=MEMBER_ID, status="accepted"))
        s.commit()

    bridge = object.__new__(Bridge)
    bridge.bot = MagicMock()
    bridge._dm = AsyncMock()
    asyncio.run(bridge._remind_unit(MagicMock()))
    bridge._dm.assert_not_awaited()  # opted out → no DM
    with SessionLocal() as s:
        assert s.get(Event, eid).reminder_sent_at is not None  # still marked, not re-scanned


def test_profile_toggle_sets_reminder_opt_out():
    client = TestClient(app)
    _login(client, "officer", discord_id=MEMBER_ID, name="Testman")
    client.post("/my-record/profile",
                data={"csrf": _csrf(client, "/my-record"), "reminders_opt_out": "1"})
    with SessionLocal() as s:
        assert s.get(Member, MEMBER_ID).reminders_opt_out is True
    # unchecking clears it (checkbox absent from the form submission)
    client.post("/my-record/profile", data={"csrf": _csrf(client, "/my-record")})
    with SessionLocal() as s:
        assert s.get(Member, MEMBER_ID).reminders_opt_out is False


def test_my_record_shows_muster_dashboard():
    from datetime import datetime, timedelta
    client = TestClient(app)
    _login(client, "officer", discord_id=MEMBER_ID, name="Testman")
    with SessionLocal() as s:
        s.add(Event(name="Saturday Line", event_type="Battle",
                    scheduled_at=datetime.utcnow() + timedelta(days=2), created_by=1))
        s.commit()
    html = client.get("/my-record").text
    assert "Your Events" in html and "Saturday Line" in html and "/rsvp" in html


def test_edit_event_updates_and_queues_refresh():
    from datetime import datetime
    client = TestClient(app)
    _login(client, "officer")
    with SessionLocal() as s:
        ev = Event(name="Old", event_type="Drill", scheduled_at=datetime(2099, 1, 1, 19, 0),
                   created_by=1, channel_id=5, message_id=9, reminder_sent_at=datetime.utcnow())
        s.add(ev)
        s.commit()
        s.refresh(ev)
        eid = ev.id
    token = _csrf(client, f"/muster-calls/{eid}")
    client.post(f"/muster-calls/{eid}/update",
                data={"csrf": token, "name": "New Name", "event_type": "Battle",
                      "date": "2099-02-02", "time": "20:00", "tz_offset": "0"})
    with SessionLocal() as s:
        ev = s.get(Event, eid)
        assert ev.name == "New Name" and ev.event_type == "Battle"
        assert ev.scheduled_at.strftime("%Y-%m-%d %H:%M") == "2099-02-02 20:00"
        assert ev.reminder_sent_at is None  # rescheduling re-arms the reminder
    assert len(_actions(queue.REFRESH_EVENT)) == 1


def test_delete_event_removes_and_queues_withdrawal():
    import json
    from datetime import datetime
    client = TestClient(app)
    _login(client, "officer")
    with SessionLocal() as s:
        ev = Event(name="Doomed", event_type="Drill", scheduled_at=datetime(2099, 1, 1),
                   created_by=1, channel_id=5, message_id=9)
        s.add(ev)
        s.commit()
        s.refresh(ev)
        eid = ev.id
        s.add(AttendanceRecord(event_id=eid, member_id=MEMBER_ID, status="accepted"))
        s.commit()
    token = _csrf(client, f"/muster-calls/{eid}")
    r = client.post(f"/muster-calls/{eid}/delete", data={"csrf": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/muster-calls"
    with SessionLocal() as s:
        assert s.get(Event, eid) is None
        assert s.query(AttendanceRecord).filter_by(event_id=eid).count() == 0  # cascaded
    acts = _actions(queue.DELETE_EVENT)
    assert len(acts) == 1
    p = json.loads(acts[0].payload)
    assert p["channel_id"] == 5 and p["message_id"] == 9


def test_bulk_attendance_marks_many_and_skips_blanks():
    from datetime import datetime
    client = TestClient(app)
    _login(client, "officer")
    with SessionLocal() as s:
        s.add(Member(discord_id=101, callsign="Two", rank="Private", company="Alpha", status="active"))
        ev = Event(name="Drill", event_type="Drill", scheduled_at=datetime(2099, 1, 1), created_by=1)
        s.add(ev)
        s.commit()
        s.refresh(ev)
        eid = ev.id
    token = _csrf(client, f"/muster-calls/{eid}")
    client.post(f"/muster-calls/{eid}/attendance/bulk",
                data={"csrf": token, "status_100": "present", "status_101": "absent",
                      f"status_{CANDIDATE_ID}": "", "status_999": "present"})
    with SessionLocal() as s:
        recs = {r.member_id: r.status for r in s.query(AttendanceRecord).filter_by(event_id=eid)}
    assert recs.get(100) == "present" and recs.get(101) == "absent"
    assert 999 not in recs  # not a member → skipped
    assert CANDIDATE_ID not in recs  # blank → untouched


def test_recurring_with_lead_defers_announcements():
    from web import services
    with SessionLocal() as s:
        # 3 weekly events, announce 2 days before each — far in the future so
        # none are due yet.
        first = services.create_event(
            s, {"id": 1, "name": "Off"}, "Recurring Drill", "Drill",
            "2099-06-01 19:00", tz_offset=0, repeat_weeks=3,
            lead_value="2", lead_unit="days")
    with SessionLocal() as s:
        evs = s.query(Event).filter(Event.name == "Recurring Drill").all()
        assert len(evs) == 3
        # every occurrence carries the lead and is not yet announced
        assert all(e.announce_lead_minutes == 2 * 1440 for e in evs)
        assert all(e.announced is False for e in evs)
    # nothing was posted to Discord up front
    assert _actions(queue.ANNOUNCE_EVENT) == []


def test_immediate_event_still_announces_now():
    from web import services
    with SessionLocal() as s:
        services.create_event(s, {"id": 1, "name": "Off"}, "One Off", "Drill",
                              "2099-06-01 19:00", tz_offset=0, repeat_weeks=1,
                              lead_value="0", lead_unit="days")
    with SessionLocal() as s:
        ev = s.query(Event).filter(Event.name == "One Off").one()
        assert ev.announce_lead_minutes is None and ev.announced is True
    assert len(_actions(queue.ANNOUNCE_EVENT)) == 1


def test_scheduler_posts_due_announcement_once():
    from datetime import datetime, timedelta
    from cogs.bridge import Bridge
    with SessionLocal() as s:
        # scheduled 1 hour out with a 2-day lead → the post window is already open
        ev = Event(name="Imminent", event_type="Drill",
                   scheduled_at=datetime.utcnow() + timedelta(hours=1),
                   created_by=1, announce_lead_minutes=2 * 1440, announced=False)
        s.add(ev)
        s.commit()
        s.refresh(ev)
        eid = ev.id

    bridge = object.__new__(Bridge)
    bridge.bot = MagicMock()
    bridge._do_announce_event = AsyncMock()
    asyncio.run(bridge._post_due_announcements(MagicMock()))
    bridge._do_announce_event.assert_awaited_once()
    with SessionLocal() as s:
        assert s.get(Event, eid).announced is True

    # a second pass does nothing (already announced)
    bridge._do_announce_event.reset_mock()
    asyncio.run(bridge._post_due_announcements(MagicMock()))
    bridge._do_announce_event.assert_not_awaited()


def test_scheduler_skips_not_yet_due_announcement():
    from datetime import datetime, timedelta
    from cogs.bridge import Bridge
    with SessionLocal() as s:
        # scheduled 30 days out with a 1-day lead → not due for ~29 days
        ev = Event(name="Far Off", event_type="Drill",
                   scheduled_at=datetime.utcnow() + timedelta(days=30),
                   created_by=1, announce_lead_minutes=1440, announced=False)
        s.add(ev)
        s.commit()
    bridge = object.__new__(Bridge)
    bridge.bot = MagicMock()
    bridge._do_announce_event = AsyncMock()
    asyncio.run(bridge._post_due_announcements(MagicMock()))
    bridge._do_announce_event.assert_not_awaited()


def test_assignment_add_assign_and_unassign():
    import json
    from db.models import Assignment, MemberAssignment
    client = TestClient(app)
    _login(client, "admin")
    # admin defines a leadership group with a Discord role
    client.post("/admin/assignments/add",
                data={"csrf": _csrf(client, "/command-tent"), "name": "High Command",
                      "role_id": "555", "description": "Senior leadership", "is_leadership": "1"})
    with SessionLocal() as s:
        a = s.query(Assignment).filter_by(name="High Command").one()
        assert a.is_leadership is True and a.role_id == 555
        aid = a.id
    # assign a member → link created + role add queued + audited
    client.post(f"/members/{MEMBER_ID}/assign",
                data={"csrf": _csrf(client), "assignment_id": aid})
    with SessionLocal() as s:
        assert s.query(MemberAssignment).filter_by(member_id=MEMBER_ID, assignment_id=aid).count() == 1
    acts = _actions(queue.ASSIGN_ROLE)
    assert len(acts) == 1 and json.loads(acts[0].payload)["role_id"] == 555
    # the roster's By-Assignment tab shows the member under the group
    staff = client.get("/roster?tab=assignments").text
    assert "High Command" in staff and "Testman" in staff
    # the old command-staff URL still lands there
    assert client.get("/command-staff").url.path == "/roster"
    # unassign → link gone + role removal queued
    client.post(f"/members/{MEMBER_ID}/unassign",
                data={"csrf": _csrf(client), "assignment_id": aid})
    with SessionLocal() as s:
        assert s.query(MemberAssignment).filter_by(member_id=MEMBER_ID, assignment_id=aid).count() == 0
    assert len(_actions(queue.UNASSIGN_ROLE)) == 1


def test_assignment_actions_require_officer():
    client = TestClient(app)
    _login(client, "none")
    r = client.post(f"/members/{MEMBER_ID}/assign",
                    data={"csrf": "x", "assignment_id": 1}, follow_redirects=False)
    assert r.status_code in (302, 303, 403)


def test_bridge_assigns_and_unassigns_role():
    from cogs.bridge import Bridge
    bridge = object.__new__(Bridge)
    bridge._refresh = AsyncMock()
    member = AsyncMock()
    role = MagicMock()
    guild = MagicMock()
    guild.get_member.return_value = member
    guild.get_role.return_value = role
    asyncio.run(bridge._do_assign_role(guild, {"discord_id": MEMBER_ID, "role_id": 555}))
    member.add_roles.assert_awaited_once()
    asyncio.run(bridge._do_unassign_role(guild, {"discord_id": MEMBER_ID, "role_id": 555}))
    member.remove_roles.assert_awaited_once()


def test_bridge_deletes_event_message():
    from cogs.bridge import Bridge
    bridge = object.__new__(Bridge)
    guild = MagicMock()
    channel = MagicMock()
    msg = AsyncMock()
    channel.fetch_message = AsyncMock(return_value=msg)
    guild.get_channel.return_value = channel
    asyncio.run(bridge._do_delete_event(guild, {"channel_id": 5, "message_id": 9}))
    msg.delete.assert_awaited()


def test_approve_with_chosen_rank_and_company():
    import json
    client = TestClient(app)
    _login(client, "recruiter")
    token = _csrf(client, "/recruits")
    r = client.post(f"/recruits/{CANDIDATE_ID}/approve",
                    data={"csrf": token, "rank": "Corporal", "company": "Bravo"})
    assert r.status_code == 200
    with SessionLocal() as s:
        m = s.get(Member, CANDIDATE_ID)
        assert m is not None and m.rank == "Corporal" and m.company == "Bravo"
    acts = _actions(queue.APPROVE_CANDIDATE)
    assert acts and json.loads(acts[0].payload)["default_rank"] == "Corporal"


def test_approve_rejects_unknown_rank():
    client = TestClient(app)
    _login(client, "recruiter")
    token = _csrf(client, "/recruits")
    client.post(f"/recruits/{CANDIDATE_ID}/approve",
                data={"csrf": token, "rank": "Field Marshal"})
    # the bogus rank is refused: applicant stays in the queue, nothing enlisted
    with SessionLocal() as s:
        assert s.get(Candidacy, CANDIDATE_ID) is not None
        assert s.get(Member, CANDIDATE_ID) is None
    assert _actions(queue.APPROVE_CANDIDATE) == []


def test_digest_composes_state_of_the_regiment():
    from datetime import datetime, timedelta
    from cogs.bridge import Bridge
    with SessionLocal() as s:
        # a fresh enlistment this week, an event held, and one on the books
        s.add(Member(discord_id=301, callsign="Newcomer", rank="Private",
                     company="Alpha", status="active", joined_date=datetime.utcnow()))
        held = Event(name="Saturday Drill", event_type="Drill",
                     scheduled_at=datetime.utcnow() - timedelta(days=2), created_by=1)
        s.add(held)
        s.add(Event(name="Assault", event_type="Battle",
                    scheduled_at=datetime.utcnow() + timedelta(days=3), created_by=1))
        s.commit()
        s.refresh(held)
        s.add(AttendanceRecord(event_id=held.id, member_id=MEMBER_ID, status="present"))
        s.commit()

        d = Bridge._compose_digest(s)
    assert d is not None
    assert "Newcomer" in d["new_enlistments"]
    assert d["recruits_waiting"] == 1  # the seeded applicant
    assert ("Saturday Drill", 1) in d["turnout"]
    assert any(name == "Assault" for name, _type, _when in d["upcoming"])


def test_digest_posts_weekly_and_snoozes():
    from cogs.bridge import Bridge
    with SessionLocal() as s:
        get_config(s).admin_log_channel_id = 999  # digest falls back to admin_log
        s.commit()

    bridge = object.__new__(Bridge)
    bridge.bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    guild = MagicMock()
    guild.get_channel.return_value = channel

    asyncio.run(bridge._send_due_digests(guild))
    channel.send.assert_awaited_once()
    with SessionLocal() as s:
        assert get_config(s).digest_last_sent_at is not None

    # a second pass within the week posts nothing
    channel.send.reset_mock()
    asyncio.run(bridge._send_due_digests(guild))
    channel.send.assert_not_awaited()


def test_digest_disabled_does_not_post():
    from cogs.bridge import Bridge
    with SessionLocal() as s:
        cfg = get_config(s)
        cfg.admin_log_channel_id = 999
        cfg.digest_enabled = False
        s.commit()

    bridge = object.__new__(Bridge)
    bridge.bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    guild = MagicMock()
    guild.get_channel.return_value = channel

    asyncio.run(bridge._send_due_digests(guild))
    channel.send.assert_not_awaited()


def test_build_nickname_composes_tags():
    from utils.sync import build_nickname
    assert build_nickname("5thVA", "A", "Cpl", "John") == "5thVA A Cpl. John"
    assert build_nickname("", "", "Cpl", "John") == "Cpl. John"        # historical format
    assert build_nickname("5thVA", "", "Pvt", "John") == "5thVA Pvt. John"
    assert build_nickname("", "B", "Sgt", "Jane") == "B Sgt. Jane"
    # over Discord's 32-char cap: prefix kept, name trimmed
    out = build_nickname("REGIMENT", "COMPANY", "Cpl", "A Very Long Callsign Indeed")
    assert len(out) <= 32 and out.startswith("REGIMENT COMPANY Cpl.")


def test_unit_tag_update_enqueues_resync():
    client = TestClient(app)
    _login(client, "admin")
    client.post("/admin/identity",
                data={"csrf": _csrf(client, "/command-tent"), "regiment_name": "5th Virginia",
                      "brand_color": "#123456", "inactivity_days": "30", "unit_tag": "5thVA"})
    with SessionLocal() as s:
        assert get_config(s).unit_tag == "5thVA"
    assert len(_actions(queue.RESYNC_NICKNAMES)) == 1
    # saving again with the same tag does not re-queue
    client.post("/admin/identity",
                data={"csrf": _csrf(client, "/command-tent"), "regiment_name": "5th Virginia",
                      "brand_color": "#123456", "inactivity_days": "30", "unit_tag": "5thVA"})
    assert len(_actions(queue.RESYNC_NICKNAMES)) == 1


def test_company_tag_update_enqueues_scoped_resync():
    import json
    client = TestClient(app)
    _login(client, "admin")
    with SessionLocal() as s:
        cid = s.query(Company).filter_by(name="Alpha").one().id
    client.post(f"/admin/companies/{cid}/update",
                data={"csrf": _csrf(client, "/command-tent"), "name": "Alpha",
                      "role_id": "", "tag": "A"})
    with SessionLocal() as s:
        assert s.get(Company, cid).tag == "A"
    acts = _actions(queue.RESYNC_NICKNAMES)
    assert acts and json.loads(acts[0].payload)["company"] == "Alpha"


def test_bridge_resync_rebuilds_member_nickname():
    from cogs.bridge import Bridge
    with SessionLocal() as s:
        get_config(s).unit_tag = "5thVA"
        s.query(Company).filter_by(name="Alpha").one().tag = "A"
        s.commit()
    bridge = object.__new__(Bridge)
    bridge.bot = MagicMock()
    member = MagicMock()
    member.id = MEMBER_ID
    member.edit = AsyncMock()
    guild = MagicMock()
    guild.get_member.return_value = member
    asyncio.run(bridge._do_resync_nicknames(guild, {}))
    member.edit.assert_awaited_once()
    assert member.edit.call_args.kwargs["nick"] == "5thVA A Pvt. Testman"


def test_bridge_alliance_announce_posts_to_announcements():
    from cogs.bridge import Bridge
    with SessionLocal() as s:
        get_config(s).announcements_channel_id = 555
        s.commit()
    bridge = object.__new__(Bridge)
    bridge.bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    guild = MagicMock()
    guild.get_channel.return_value = channel
    asyncio.run(bridge._do_alliance_announce(
        guild, {"alliance_name": "Army of NV", "title": "Muster", "body": "To the field."}))
    guild.get_channel.assert_called_with(555)
    channel.send.assert_awaited_once()


def test_bridge_posts_platform_broadcast_to_admin_log():
    from cogs.bridge import Bridge
    with SessionLocal() as s:
        get_config(s).admin_log_channel_id = 4242
        s.commit()
    bridge = object.__new__(Bridge)
    bridge.bot = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    guild = MagicMock()
    guild.get_channel.return_value = channel
    asyncio.run(bridge._do_platform_broadcast(
        guild, {"title": "Update", "body": "Site changes are live."}))
    guild.get_channel.assert_called_with(4242)
    channel.send.assert_awaited_once()


def test_bridge_platform_broadcast_skips_unit_without_admin_log():
    from cogs.bridge import Bridge
    # no admin_log channel configured on the seeded default config
    bridge = object.__new__(Bridge)
    bridge.bot = MagicMock()
    guild = MagicMock()
    guild.get_channel.return_value = None
    asyncio.run(bridge._do_platform_broadcast(guild, {"title": "", "body": "hi"}))
    # nothing to post to; the call is a no-op that doesn't raise


_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 48


def test_rank_add_with_insignia_image():
    client = TestClient(app)
    _login(client, "admin")
    token = _csrf(client, "/command-tent")
    r = client.post("/admin/ranks/add",
                    data={"csrf": token, "name": "Colonel", "abbreviation": "Col"},
                    files={"image": ("i.png", _PNG, "image/png")})
    assert r.status_code == 200
    with SessionLocal() as s:
        rank = s.query(Rank).filter_by(name="Colonel").one()
        assert rank.image and rank.image.startswith("data:image/png;base64,")


def test_roster_shows_rank_insignia():
    client = TestClient(app)
    _login(client, "admin")
    token = _csrf(client, "/command-tent")
    with SessionLocal() as s:
        rid = s.query(Rank).filter_by(name="Private").one().id
    client.post(f"/admin/ranks/{rid}/image",
                data={"csrf": token},
                files={"image": ("i.png", _PNG, "image/png")})
    r = client.get("/roster")
    assert r.status_code == 200
    assert "data:image/png;base64," in r.text


def test_dossier_muster_and_promotions_show_rank_insignia():
    client = TestClient(app)
    _login(client, "admin")
    token = _csrf(client, "/command-tent")
    with SessionLocal() as s:
        rid = s.query(Rank).filter_by(name="Private").one().id
    client.post(f"/admin/ranks/{rid}/image",
                data={"csrf": token},
                files={"image": ("i.png", _PNG, "image/png")})

    assert "data:image/png;base64," in client.get(f"/dossier/{MEMBER_ID}").text
    assert "data:image/png;base64," in client.get("/muster").text
    assert "data:image/png;base64," in client.get("/promotions").text


def test_rank_rename_updates_members_holding_it():
    import json
    client = TestClient(app)
    _login(client, "admin")
    token = _csrf(client, "/command-tent")
    with SessionLocal() as s:
        rid = s.query(Rank).filter_by(name="Private").one().id
    r = client.post(f"/admin/ranks/{rid}/update",
                    data={"csrf": token, "name": "Recruit", "abbreviation": "Rec", "tier": ""})
    assert r.status_code == 200
    with SessionLocal() as s:
        rank = s.get(Rank, rid)
        assert rank.name == "Recruit" and rank.abbreviation == "Rec"
        assert s.get(Member, MEMBER_ID).rank == "Recruit"
    acts = _actions(queue.RESYNC_NICKNAMES)
    assert acts and json.loads(acts[0].payload)["rank"] == "Recruit"


def test_company_rename_updates_members_holding_it():
    client = TestClient(app)
    _login(client, "admin")
    token = _csrf(client, "/command-tent")
    with SessionLocal() as s:
        cid = s.query(Company).filter_by(name="Alpha").one().id
    r = client.post(f"/admin/companies/{cid}/update",
                    data={"csrf": token, "name": "Alpha Company", "role_id": "", "tag": ""})
    assert r.status_code == 200
    with SessionLocal() as s:
        assert s.get(Company, cid).name == "Alpha Company"
        assert s.get(Member, MEMBER_ID).company == "Alpha Company"
    # a pure rename (tag unchanged) doesn't need a nickname rebuild
    assert not _actions(queue.RESYNC_NICKNAMES)


def test_rename_member_from_roster():
    client = TestClient(app)
    _login(client, "officer")
    token = _csrf(client, "/roster")
    html = client.get("/roster").text
    assert 'action="/members/%s/callsign"' % MEMBER_ID in html
    r = client.post(f"/members/{MEMBER_ID}/callsign", data={"csrf": token, "callsign": "Renamed"})
    assert r.status_code == 200
    with SessionLocal() as s:
        assert s.get(Member, MEMBER_ID).callsign == "Renamed"
    assert "Renamed" in client.get("/roster").text
    assert _actions(queue.RESYNC_NICKNAMES)


def test_rename_member_requires_officer():
    client = TestClient(app)
    _login(client, "none")
    html = client.get("/roster").text
    assert "/callsign" not in html
    r = client.post(f"/members/{MEMBER_ID}/callsign", data={"csrf": "x", "callsign": "Nope"},
                    follow_redirects=False)
    assert r.status_code == 403


def test_rank_abbreviation_only_change_enqueues_scoped_resync():
    import json
    client = TestClient(app)
    _login(client, "admin")
    token = _csrf(client, "/command-tent")
    with SessionLocal() as s:
        rid = s.query(Rank).filter_by(name="Private").one().id
    r = client.post(f"/admin/ranks/{rid}/update",
                    data={"csrf": token, "name": "Private", "abbreviation": "Rec", "tier": ""})
    assert r.status_code == 200
    acts = _actions(queue.RESYNC_NICKNAMES)
    assert acts and json.loads(acts[0].payload)["rank"] == "Private"

    # saving again unchanged does not re-queue
    token = _csrf(client, "/command-tent")
    client.post(f"/admin/ranks/{rid}/update",
                data={"csrf": token, "name": "Private", "abbreviation": "Rec", "tier": ""})
    assert len(_actions(queue.RESYNC_NICKNAMES)) == 1


def test_bridge_resync_scoped_to_rank():
    from cogs.bridge import Bridge
    with SessionLocal() as s:
        s.add(Member(discord_id=601, callsign="Other", rank="Corporal", company="Alpha",
                     status="active"))
        s.commit()
    bridge = object.__new__(Bridge)
    bridge.bot = MagicMock()
    private_member = MagicMock()
    private_member.id = MEMBER_ID
    private_member.edit = AsyncMock()
    corporal_member = MagicMock()
    corporal_member.id = 601
    corporal_member.edit = AsyncMock()
    guild = MagicMock()
    guild.get_member.side_effect = lambda did: (
        private_member if did == MEMBER_ID else corporal_member if did == 601 else None
    )
    asyncio.run(bridge._do_resync_nicknames(guild, {"rank": "Private"}))
    private_member.edit.assert_awaited_once()
    corporal_member.edit.assert_not_called()


def test_award_type_with_image_and_grant_carries_id():
    import json
    from web import services
    client = TestClient(app)
    _login(client, "officer")
    token = _csrf(client, "/honors")
    client.post("/honors/award-type",
                data={"csrf": token, "name": "Valor Star"},
                files={"image": ("m.png", _PNG, "image/png")})
    with SessionLocal() as s:
        award = s.query(AwardType).filter_by(name="Valor Star").one()
        assert award.image.startswith("data:image/png")
        atid = award.id
        services.grant_award(s, {"id": 1, "name": "O"}, MEMBER_ID, atid)
    acts = _actions(queue.AWARD_GRANTED)
    assert acts and json.loads(acts[0].payload)["award_type_id"] == atid


def test_bridge_promotion_with_insignia_posts_embed_and_file():
    from cogs.bridge import Bridge
    with SessionLocal() as s:
        get_config(s).billboard_channel_id = 777
        s.query(Rank).filter_by(name="Sergeant").one().image = "data:image/png;base64,QUJD"
        s.commit()
    bridge = object.__new__(Bridge)
    bridge.bot = MagicMock()
    bridge._refresh = AsyncMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    guild = MagicMock()
    guild.get_member.return_value = None
    guild.get_channel.return_value = channel
    asyncio.run(bridge._do_sync_rank(guild, {"discord_id": MEMBER_ID, "callsign": "Testman",
                                             "new_rank": "Sergeant", "billboard": "promoted"}))
    channel.send.assert_awaited_once()
    assert "file" in channel.send.call_args.kwargs and "embed" in channel.send.call_args.kwargs


def test_bridge_award_with_image_posts_embed_and_file():
    from cogs.bridge import Bridge
    with SessionLocal() as s:
        get_config(s).billboard_channel_id = 777
        award = s.query(AwardType).filter_by(name="Marksman").one()
        award.image = "data:image/png;base64,QUJD"
        s.commit()
        atid = award.id
    bridge = object.__new__(Bridge)
    bridge.bot = MagicMock()
    bridge._refresh = AsyncMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    guild = MagicMock()
    guild.get_channel.return_value = channel
    asyncio.run(bridge._do_award_granted(guild, {"discord_id": MEMBER_ID, "award_type_id": atid,
                                                 "award_name": "Marksman", "billboard": "awarded"}))
    channel.send.assert_awaited_once()
    assert "file" in channel.send.call_args.kwargs


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        _reset()
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)} tests passed.")


if __name__ == "__main__":
    _run_all()
