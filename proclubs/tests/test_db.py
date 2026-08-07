"""Tests for db.py -- the local Pro Clubs history store (club_snapshots,
matches, match_players tables).

Run with: pytest proclubs/tests/test_db.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import db  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    # db.py has no SITE_DB_PATH-style override (unlike database.py) -- its
    # DB_PATH is a fixed module-level constant, so point it at a scratch
    # file per test instead.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "history.db")


def _raw_match(match_id, *, club_id="c1", opp_id="c2", opp_name="Rivals FC",
                us_goals=2, opp_goals=1, wins="1", losses="0", forfeit=False, timestamp=1000):
    return {
        "matchId": match_id,
        "timestamp": timestamp,
        "clubs": {
            club_id: {
                "goals": str(us_goals), "wins": wins, "losses": losses,
                "winnerByDnf": "1" if forfeit else "0", "date": str(timestamp),
            },
            opp_id: {"goals": str(opp_goals), "details": {"name": opp_name}, "winnerByDnf": "0"},
        },
        "players": {club_id: {}},
    }


def test_rival_records_aggregates_head_to_head_by_opponent():
    db.record_matches("common-gen5", "c1", "leagueMatch", [
        _raw_match("m1", opp_name="Rivals FC", us_goals=2, opp_goals=1, wins="1", losses="0", timestamp=1000),
        _raw_match("m2", opp_name="Rivals FC", us_goals=1, opp_goals=1, wins="0", losses="0", timestamp=2000),
        _raw_match("m3", opp_name="Steel City SC", us_goals=0, opp_goals=2, wins="0", losses="1", timestamp=3000),
    ])

    rivals = db.rival_records("common-gen5", "c1")
    by_name = {r["name"]: r for r in rivals}

    assert by_name["Rivals FC"]["played"] == 2
    assert by_name["Rivals FC"]["wins"] == 1
    assert by_name["Rivals FC"]["draws"] == 1
    assert by_name["Rivals FC"]["losses"] == 0
    assert by_name["Rivals FC"]["gf"] == 3
    assert by_name["Rivals FC"]["ga"] == 2
    assert by_name["Rivals FC"]["last_outcome"] == "D"  # the more recent of the two meetings

    assert by_name["Steel City SC"]["played"] == 1
    assert by_name["Steel City SC"]["losses"] == 1
    assert by_name["Steel City SC"]["last_outcome"] == "L"


def test_rival_records_excludes_forfeits():
    db.record_matches("common-gen5", "c1", "leagueMatch", [
        _raw_match("m1", opp_name="Rivals FC", forfeit=True, timestamp=1000),
        _raw_match("m2", opp_name="Rivals FC", forfeit=False, timestamp=2000),
    ])
    rivals = db.rival_records("common-gen5", "c1")
    assert rivals == [
        {"name": "Rivals FC", "played": 1, "wins": 1, "draws": 0, "losses": 0,
         "gf": 2, "ga": 1, "last_outcome": "W", "last_played_at": 2000},
    ]


def test_rival_records_sorted_by_matches_played_descending():
    db.record_matches("common-gen5", "c1", "leagueMatch", [
        _raw_match("m1", opp_name="Steel City SC", timestamp=1000),
        _raw_match("m2", opp_name="Rivals FC", timestamp=2000),
        _raw_match("m3", opp_name="Rivals FC", timestamp=3000),
    ])
    rivals = db.rival_records("common-gen5", "c1")
    assert [r["name"] for r in rivals] == ["Rivals FC", "Steel City SC"]


def test_rival_records_empty_when_no_tracked_history():
    assert db.rival_records("common-gen5", "c1") == []


def test_rival_records_scoped_to_platform_and_club():
    db.record_matches("common-gen5", "c1", "leagueMatch", [_raw_match("m1", opp_name="Rivals FC")])
    db.record_matches("common-gen4", "c1", "leagueMatch", [_raw_match("m2", opp_name="Other Platform Club")])
    db.record_matches("common-gen5", "c2", "leagueMatch", [_raw_match("m3", club_id="c2", opp_name="Other Club")])

    rivals = db.rival_records("common-gen5", "c1")
    assert [r["name"] for r in rivals] == ["Rivals FC"]
