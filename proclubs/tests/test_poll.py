"""Tests for poll.py -- the standalone EA-stats poller run by
proclubs-poll.timer, including the auto-built league table it now
also maintains (see db.sync_league_roster).

Run with: pytest proclubs/tests/test_poll.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402
import ea_client  # noqa: E402
import poll  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "history.db")


def _match(match_id, *, club_id="c1", opp_id="c2", opp_name="Rivals FC", timestamp=1000):
    return {
        "matchId": match_id,
        "timestamp": timestamp,
        "clubs": {
            club_id: {"goals": "2", "wins": "1", "losses": "0", "winnerByDnf": "0", "date": str(timestamp)},
            opp_id: {"goals": "1", "details": {"name": opp_name}, "winnerByDnf": "0"},
        },
        "players": {club_id: {}},
    }


def test_poll_club_records_snapshot_with_team_size(monkeypatch):
    monkeypatch.setattr(ea_client, "overall_stats", lambda p, c: {"wins": "5"})
    monkeypatch.setattr(ea_client, "division_stats", lambda p, c: {"currentDivision": "3", "points": "20"})
    monkeypatch.setattr(ea_client, "member_stats", lambda p, c: {"members": [{"name": "A"}, {"name": "B"}]})
    monkeypatch.setattr(ea_client, "matches_stats", lambda p, c, mt, max_results=30: [])

    poll.poll_club("common-gen5", "c1", "Our Club")

    snap = db.latest_snapshot("common-gen5", "c1")
    assert snap["points"] == "20"
    assert snap["team_size"] == 2


def test_poll_club_handles_member_stats_failure_gracefully(monkeypatch, capsys):
    monkeypatch.setattr(ea_client, "overall_stats", lambda p, c: {"wins": "5"})
    monkeypatch.setattr(ea_client, "division_stats", lambda p, c: {"currentDivision": "3", "points": "20"})

    def fail(p, c):
        raise ea_client.EAApiError("boom")

    monkeypatch.setattr(ea_client, "member_stats", fail)
    monkeypatch.setattr(ea_client, "matches_stats", lambda p, c, mt, max_results=30: [])

    poll.poll_club("common-gen5", "c1", "Our Club")  # must not raise

    snap = db.latest_snapshot("common-gen5", "c1")
    assert snap["points"] == "20"
    assert snap["team_size"] is None
    assert "member stats failed" in capsys.readouterr().out


def test_poll_club_records_matches_for_both_match_types(monkeypatch):
    monkeypatch.setattr(ea_client, "overall_stats", lambda p, c: None)
    monkeypatch.setattr(ea_client, "division_stats", lambda p, c: None)
    monkeypatch.setattr(ea_client, "member_stats", lambda p, c: {"members": []})

    calls = []

    def fake_matches(p, c, match_type, max_results=30):
        calls.append(match_type)
        return [_match("m-" + match_type)]

    monkeypatch.setattr(ea_client, "matches_stats", fake_matches)
    poll.poll_club("common-gen5", "c1", "Our Club")

    assert set(calls) == {"leagueMatch", "playoffMatch"}
    assert len(db.match_history("common-gen5", "c1")) == 2


def test_sync_and_poll_league_table_polls_newly_discovered_opponents(monkeypatch):
    monkeypatch.setattr(config, "CLUB_ID", "c1")
    monkeypatch.setattr(config, "CLUB_PLATFORM", "common-gen5")
    monkeypatch.setattr(poll, "LEAGUE_TABLE_MAX_TEAMS", 25)

    db.record_matches("common-gen5", "c1", "leagueMatch", [_match("m1", opp_id="c2", opp_name="Rivals FC")])

    polled_clubs = []
    monkeypatch.setattr(poll, "poll_club", lambda platform, club_id, label: polled_clubs.append(club_id))

    poll.sync_and_poll_league_table([{"platform": "common-gen5", "clubId": "c1", "label": "Our Club"}], set())

    assert "c2" in polled_clubs  # the newly-discovered opponent got polled
    assert "c1" in polled_clubs  # so did our own club, via the league roster


def test_sync_and_poll_league_table_does_not_repoll_already_polled_clubs(monkeypatch):
    monkeypatch.setattr(config, "CLUB_ID", "c1")
    monkeypatch.setattr(config, "CLUB_PLATFORM", "common-gen5")

    db.record_matches("common-gen5", "c1", "leagueMatch", [_match("m1", opp_id="c2", opp_name="Rivals FC")])

    polled_calls = []
    monkeypatch.setattr(poll, "poll_club", lambda platform, club_id, label: polled_calls.append(club_id))

    already_polled = {("common-gen5", "c1")}  # simulates the tracked-clubs loop already covering us
    poll.sync_and_poll_league_table([{"platform": "common-gen5", "clubId": "c1", "label": "Our Club"}], already_polled)

    assert polled_calls == ["c2"]  # c1 skipped -- already covered this run


def test_sync_and_poll_league_table_does_nothing_without_club_id(monkeypatch):
    monkeypatch.setattr(config, "CLUB_ID", "")

    def fail(*a, **k):
        raise AssertionError("should not poll when CLUB_ID is unset")

    monkeypatch.setattr(poll, "poll_club", fail)
    poll.sync_and_poll_league_table([], set())  # must not raise


def test_main_builds_league_table_end_to_end(monkeypatch):
    monkeypatch.setattr(poll, "load_tracked_clubs", lambda: [
        {"platform": "common-gen5", "clubId": "c1", "label": "Our Club"},
    ])
    monkeypatch.setattr(config, "CLUB_ID", "c1")
    monkeypatch.setattr(config, "CLUB_PLATFORM", "common-gen5")

    monkeypatch.setattr(ea_client, "overall_stats", lambda p, c: {"wins": "1"})
    monkeypatch.setattr(ea_client, "division_stats", lambda p, c: {"currentDivision": "3", "points": "10"})
    monkeypatch.setattr(ea_client, "member_stats", lambda p, c: {"members": []})
    monkeypatch.setattr(ea_client, "matches_stats", lambda p, c, mt, max_results=30: (
        [_match(f"m-{c}-{mt}", opp_id="c2", opp_name="Rivals FC")] if mt == "leagueMatch" and c == "c1" else []
    ))

    poll.main()

    table = db.league_table("common-gen5", "c1")
    assert {r["label"] for r in table} >= {"Our Club"}
    ids = {r["club_id"] for r in db.league_roster("common-gen5")}
    assert "c2" in ids  # the opponent surfaced from our own match got pulled into the league table
