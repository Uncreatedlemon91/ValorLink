"""The staged invite ladder: which tier is owed when, and what the poller
does about it.

Discord itself is never called here -- discord_rsvp is stubbed -- so these
are about the decision logic, which is the part that can silently ping the
wrong people or nobody at all.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="proclubs-invites-")
os.environ["SITE_DB_PATH"] = os.path.join(_TMP, "site.db")
os.environ["SESSION_SECRET"] = "test-secret"

import pytest  # noqa: E402

import config  # noqa: E402
import database  # noqa: E402
import event_invites_poll  # noqa: E402
import services  # noqa: E402
from models import Event  # noqa: E402

LADDER = [
    {"key": "create", "hours_before": None, "role_id": "111"},
    {"key": "48", "hours_before": 48.0, "role_id": "222"},
    {"key": "24", "hours_before": 24.0, "role_id": "333"},
]


@pytest.fixture
def session():
    database.Base.metadata.drop_all(database.engine)
    database.init_db()
    with database.get_session() as s:
        yield s


def _event(session, *, hours_away: float, announced: bool = True) -> Event:
    event = Event(
        title="Derby Day", event_type="Match",
        scheduled_at=datetime.utcnow() + timedelta(hours=hours_away),
        discord_channel_id="thread-1" if announced else None,
        discord_message_id="msg-1" if announced else None,
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


# --- which tiers are owed --------------------------------------------------- #
def test_only_the_create_tier_is_due_for_a_distant_event(session):
    event = _event(session, hours_away=200)
    due = services.due_invite_tiers(session, event, LADDER)
    assert [t["key"] for t in due] == ["create"]


def test_a_tier_comes_due_once_kickoff_is_close_enough(session):
    event = _event(session, hours_away=30)
    due = services.due_invite_tiers(session, event, LADDER)
    # 30h out: create and the 48h rung are owed, the 24h one is not yet.
    assert [t["key"] for t in due] == ["create", "48"]


def test_an_event_announced_late_owes_every_passed_tier_at_once(session):
    """A fixture added 12 hours before kick-off has missed the 48h and 24h
    moments. They're owed immediately rather than skipped -- otherwise a
    late-announced event would quietly never reach anyone past tier one."""
    event = _event(session, hours_away=12)
    due = services.due_invite_tiers(session, event, LADDER)
    assert [t["key"] for t in due] == ["create", "48", "24"]


def test_a_recorded_tier_is_not_owed_again(session):
    event = _event(session, hours_away=30)
    services.record_tier_invite(session, event, LADDER[0], member_count=4)
    due = services.due_invite_tiers(session, event, LADDER)
    assert [t["key"] for t in due] == ["48"]


def test_recording_the_same_tier_twice_is_refused(session):
    """The unique constraint is what makes a double-run a database error
    rather than a second ping to the same people."""
    event = _event(session, hours_away=30)
    services.record_tier_invite(session, event, LADDER[0], member_count=4)
    with pytest.raises(Exception):
        services.record_tier_invite(session, event, LADDER[0], member_count=4)


# --- which events the poller looks at --------------------------------------- #
def test_unannounced_events_are_skipped(session):
    _event(session, hours_away=5, announced=False)
    assert services.events_awaiting_invites(session) == []


def test_past_events_are_skipped(session):
    """A tier coming due after kick-off has missed its purpose; pinging
    people to a fixture already played is worse than staying quiet."""
    _event(session, hours_away=-3)
    assert services.events_awaiting_invites(session) == []


# --- the poller ------------------------------------------------------------- #
@pytest.fixture
def stub_discord(monkeypatch):
    calls = {"added": [], "pinged": []}

    def _invite(thread_id, role_id):
        calls["added"].append((thread_id, role_id))
        return 3

    def _ping(thread_id, role_id, event, site_url, first=False):
        calls["pinged"].append((thread_id, role_id, first))

    monkeypatch.setattr(event_invites_poll.discord_rsvp, "invite_role_to_thread", _invite)
    monkeypatch.setattr(event_invites_poll.discord_rsvp, "ping_tier", _ping)
    monkeypatch.setattr(config, "EVENT_STAGED_INVITES_ENABLED", True)
    monkeypatch.setattr(config, "EVENT_INVITE_TIERS", LADDER)
    monkeypatch.setattr(config, "SITE_BASE_URL", "https://example.test")
    return calls


def test_poller_invites_then_pings_and_records(session, stub_discord):
    event = _event(session, hours_away=30)
    event_invites_poll.main()

    # Members are added before the ping, so nobody is notified about a
    # thread they can't open yet.
    assert stub_discord["added"] == [("thread-1", "111"), ("thread-1", "222")]
    assert [p[1] for p in stub_discord["pinged"]] == ["111", "222"]
    assert services.invited_tier_keys(session, event.id) == {"create", "48"}


def test_poller_is_idempotent(session, stub_discord):
    event = _event(session, hours_away=30)
    event_invites_poll.main()
    stub_discord["added"].clear()
    event_invites_poll.main()
    assert stub_discord["added"] == []
    assert services.invited_tier_keys(session, event.id) == {"create", "48"}


def test_a_failing_tier_is_left_unrecorded_for_the_next_run(session, monkeypatch,
                                                            stub_discord):
    """Better to retry a tier than to record it as done having told nobody."""
    event = _event(session, hours_away=200)

    def _boom(thread_id, role_id):
        raise event_invites_poll.discord_rsvp.DiscordApiError("Discord is down")

    monkeypatch.setattr(event_invites_poll.discord_rsvp, "invite_role_to_thread", _boom)
    event_invites_poll.main()
    assert services.invited_tier_keys(session, event.id) == set()

    # Recovers on the next run once Discord is back.
    monkeypatch.undo()
    monkeypatch.setattr(config, "EVENT_STAGED_INVITES_ENABLED", True)
    monkeypatch.setattr(config, "EVENT_INVITE_TIERS", LADDER)
    monkeypatch.setattr(config, "SITE_BASE_URL", "https://example.test")
    monkeypatch.setattr(event_invites_poll.discord_rsvp, "invite_role_to_thread",
                        lambda t, r: 3)
    monkeypatch.setattr(event_invites_poll.discord_rsvp, "ping_tier",
                        lambda *a, **k: None)
    event_invites_poll.main()
    assert services.invited_tier_keys(session, event.id) == {"create"}


def test_poller_does_nothing_when_not_configured(session, monkeypatch, stub_discord):
    _event(session, hours_away=30)
    monkeypatch.setattr(config, "EVENT_STAGED_INVITES_ENABLED", False)
    event_invites_poll.main()
    assert stub_discord["added"] == []


# --- the config ladder ------------------------------------------------------ #
def test_invite_tier_parsing_orders_create_first_then_furthest_out():
    tiers = config._parse_invite_tiers("24:333,create:111,48:222")
    assert [t["key"] for t in tiers] == ["create", "48", "24"]
    assert [t["role_id"] for t in tiers] == ["111", "222", "333"]


def test_invite_tier_parsing_drops_malformed_entries():
    """A typo in one rung must not stop the app booting."""
    assert config._parse_invite_tiers("create:abc,,nonsense,-5:444,24:333") == [
        {"key": "24", "hours_before": 24.0, "role_id": "333"},
    ]
