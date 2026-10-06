"""Recognition: milestones, Player of the Month and leaderboards.

Run with: pytest proclubs/tests/test_recognition.py
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import config  # noqa: E402
import database  # noqa: E402
import discord_notify  # noqa: E402
import matchweek as mw  # noqa: E402
import notify_poll  # noqa: E402
import recognition  # noqa: E402
import services  # noqa: E402
from models import Event  # noqa: E402


@pytest.fixture
def client():
    database.Base.metadata.drop_all(database.engine)
    with TestClient(appmod.app) as c:
        yield c


@pytest.fixture
def posts(monkeypatch):
    sent = []
    monkeypatch.setattr(config, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(config, "MATCHDAY_CHANNEL_ID", "777")
    monkeypatch.setattr(discord_notify, "post", lambda channel, **kw: sent.append(kw) or "m1")
    monkeypatch.setattr(discord_notify, "send_dm", lambda *a, **k: None)
    return sent


def _metrics(**values):
    base = {"apps": 0, "goals": 0, "assists": 0, "clean_sheets": 0, "squad_motm": 0}
    return {**base, **values}


def test_the_first_run_records_history_silently_then_announces(client):
    with database.get_session() as session:
        first = recognition.evaluate(session, {"1": _metrics(apps=12, goals=1)})
        assert {m.key for m in first} == {"apps-1", "apps-10", "goals-1"}
        assert recognition.unannounced(session) == []
        recognition.evaluate(session, {"1": _metrics(apps=25, goals=1)})
        assert [m.label for m in recognition.unannounced(session)] == ["25 appearances"]


def test_a_club_starting_from_nothing_still_hears_its_first_debut(client):
    with database.get_session() as session:
        assert recognition.evaluate(session, {"1": _metrics()}) == []
        recognition.evaluate(session, {"1": _metrics(apps=1)})
        assert [m.label for m in recognition.unannounced(session)] == ["Debut"]


def test_milestones_come_from_the_linked_gamertag_and_squad_votes():
    metrics = recognition.player_metrics(
        {"bo_gt": {"apps": 30, "goals": 11, "assists": 2, "mom": 1, "clean_sheets": 0}},
        {1: "Bo_GT"}, {"2": 5}, ["1", "2"])
    assert metrics["1"]["apps"] == 30 and metrics["1"]["squad_motm"] == 0
    assert metrics["2"] == _metrics(squad_motm=5)


def _voted_match(when, votes):
    """A match on `when` whose closed vote had these (voter, nominee) votes."""
    with database.get_session() as session:
        e = Event(title="Match", event_type="Match", scheduled_at=when,
                  vote_opened_at=when, vote_closes_at=when + timedelta(hours=1))
        session.add(e)
        session.commit()
        from models import MotmVote
        for voter, nominee in votes:
            session.add(MotmVote(event_id=e.id, voter_id=voter, nominee_id=nominee))
        session.commit()


def test_player_of_the_month_is_most_votes_with_wins_breaking_a_tie(client):
    _voted_match(datetime(2026, 9, 5, 20), [("a", "1")])
    _voted_match(datetime(2026, 9, 12, 20), [("a", "1")])
    _voted_match(datetime(2026, 9, 19, 20), [("a", "2"), ("b", "2")])
    _voted_match(datetime(2026, 10, 1, 20), [("a", "2"), ("b", "2")])      # October
    with database.get_session() as session:
        assert recognition.award_month(session, "2026-09", {}, now=datetime(2026, 9, 30)) is None
        award = recognition.award_month(session, "2026-09", {"1": "Ann", "2": "Ben"},
                                        now=datetime(2026, 10, 1, 9))
        # Two votes each in September; Ann won two matches, Ben one.
        assert (award.names, award.votes) == ("Ann", 2)
        assert recognition.months_won(session, "1")[0].month == "2026-09"


def test_the_bot_announces_milestones_and_the_month_once(client, posts, monkeypatch):
    with database.get_session() as session:
        services.ensure_player(session, discord_id="1", display_name="Ann")
        recognition.evaluate(session, {"1": _metrics()})      # initialise: nothing reached
    _voted_match(datetime(2026, 9, 5, 20), [("a", "1"), ("b", "1")])
    now = datetime(2026, 10, 1, 9)
    results = notify_poll.run(now)
    assert results["milestones"] == 1 and results["player_of_the_month"] == 1
    assert notify_poll.run(now)["milestones"] == 0
    assert notify_poll.run(now)["player_of_the_month"] == 0
    titles = [kw["embeds"][0]["title"] for kw in posts]
    assert "Milestones" in titles and "Player of the Month — September 2026" in titles
    milestone = next(kw for kw in posts if kw["embeds"][0]["title"] == "Milestones")
    assert "**Ann** — First Man of the Match" in milestone["embeds"][0]["description"]


def test_leaderboards_rank_and_skip_the_empty():
    people = [{"id": "1", "name": "Ann"}, {"id": "2", "name": "Ben"}, {"id": "3", "name": "Cal"}]
    usage = {"players": {"ann": {"form": 7.2, "goals": 3, "assists": 0},
                         "ben": {"form": 8.1, "goals": 5, "assists": 1}}}
    boards = {b["title"]: b["rows"] for b in recognition.leaderboards(
        people, usage, {1: "Ann", 2: "Ben"}, {"3": 2}, {1: {"rate": 90}, 2: {"rate": None}})}
    assert [r["name"] for r in boards["Form"]] == ["Ben", "Ann"]
    assert [r["name"] for r in boards["Assists"]] == ["Ben"]
    assert [r["name"] for r in boards["Man of the Match"]] == ["Cal"]
    assert [r["name"] for r in boards["Turns up"]] == ["Ann"]


def test_honours_show_on_the_player_file(client):
    with database.get_session() as session:
        services.ensure_player(session, discord_id="1", display_name="Ann")
        recognition.evaluate(session, {"1": _metrics(apps=10)})
    client.post("/auth/dev", data={"name": "Fan", "member": "1"})
    html = client.get("/players/1").text
    assert "Honors" in html and "10 appearances" in html and "Debut" in html


def test_month_helpers():
    assert recognition.previous_month(datetime(2027, 1, 3)) == "2026-12"
    assert recognition.month_bounds("2026-12") == (datetime(2026, 12, 1), datetime(2027, 1, 1))
    assert recognition.month_label("2026-09") == "September 2026"
