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


# --- opp_club_id / known_opponents / league table -------------------------- #

def test_record_matches_persists_opponent_club_id():
    db.record_matches("common-gen5", "c1", "leagueMatch", [
        _raw_match("m1", opp_id="c2", opp_name="Rivals FC"),
    ])
    opponents = db.known_opponents("common-gen5", "c1")
    assert opponents == [{"club_id": "c2", "label": "Rivals FC"}]


def test_known_opponents_excludes_matches_recorded_before_opp_club_id_existed():
    # Simulates a pre-migration row: opp_club_id is NULL even though the
    # match itself is real -- not backfillable, see db.py's migration note.
    conn = db._connect()
    with conn:
        conn.execute(
            """INSERT INTO matches (match_id, platform, club_id, played_at, match_type,
                                     us_score, opp_score, opp_name, outcome, forfeit, captured_at, opp_club_id)
               VALUES ('old1','common-gen5','c1',1000,'leagueMatch',2,1,'Legacy FC','W',0,1000,NULL)"""
        )
    conn.close()
    assert db.known_opponents("common-gen5", "c1") == []


def test_latest_snapshot_returns_most_recent_and_none_when_unpolled():
    assert db.latest_snapshot("common-gen5", "c1") is None
    db.record_snapshot("common-gen5", "c1", {"wins": "1"}, {"currentDivision": "3", "points": "10"})
    db.record_snapshot("common-gen5", "c1", {"wins": "2"}, {"currentDivision": "3", "points": "13"})
    snap = db.latest_snapshot("common-gen5", "c1")
    assert snap["points"] == "13"


def test_sync_league_roster_adds_our_own_club_pinned():
    db.sync_league_roster("common-gen5", "c1", "Our Club", max_teams=25)
    roster = db.league_roster("common-gen5")
    assert len(roster) == 1
    assert roster[0]["club_id"] == "c1"
    assert roster[0]["pinned"] == 1


def test_sync_league_roster_adds_new_opponents():
    db.record_matches("common-gen5", "c1", "leagueMatch", [
        _raw_match("m1", opp_id="c2", opp_name="Rivals FC"),
        _raw_match("m2", opp_id="c3", opp_name="Steel City SC"),
    ])
    db.sync_league_roster("common-gen5", "c1", "Our Club", max_teams=25)
    ids = {r["club_id"] for r in db.league_roster("common-gen5")}
    assert ids == {"c1", "c2", "c3"}


def test_sync_league_roster_does_not_duplicate_known_opponents():
    db.record_matches("common-gen5", "c1", "leagueMatch", [_raw_match("m1", opp_id="c2", opp_name="Rivals FC")])
    db.sync_league_roster("common-gen5", "c1", "Our Club", max_teams=25)
    db.record_matches("common-gen5", "c1", "leagueMatch", [_raw_match("m2", opp_id="c2", opp_name="Rivals FC", timestamp=2000)])
    db.sync_league_roster("common-gen5", "c1", "Our Club", max_teams=25)
    ids = [r["club_id"] for r in db.league_roster("common-gen5")]
    assert ids.count("c2") == 1


def test_sync_league_roster_evicts_lowest_points_once_full():
    db.record_matches("common-gen5", "c1", "leagueMatch", [
        _raw_match("m1", opp_id="c2", opp_name="Low Club"),
        _raw_match("m2", opp_id="c3", opp_name="High Club"),
    ])
    db.sync_league_roster("common-gen5", "c1", "Our Club", max_teams=2)  # cap: us + one opponent
    db.record_snapshot("common-gen5", "c2", {}, {"currentDivision": "3", "points": "5"})

    db.record_matches("common-gen5", "c1", "leagueMatch", [
        _raw_match("m3", opp_id="c3", opp_name="High Club", timestamp=3000),
    ])
    db.sync_league_roster("common-gen5", "c1", "Our Club", max_teams=2)

    ids = {r["club_id"] for r in db.league_roster("common-gen5")}
    # c2 had the fewest points of the non-pinned members, so it's the one
    # replaced by the newly-discovered c3 once the table (cap 2) was full.
    assert ids == {"c1", "c3"}


def test_sync_league_roster_never_evicts_pinned_club():
    db.record_matches("common-gen5", "c1", "leagueMatch", [_raw_match("m1", opp_id="c2", opp_name="Rivals FC")])
    db.sync_league_roster("common-gen5", "c1", "Our Club", max_teams=1)  # cap smaller than "us" alone
    ids = {r["club_id"] for r in db.league_roster("common-gen5")}
    assert "c1" in ids  # never evicted even though the roster is already "full" before it's considered


def test_sync_league_roster_drops_members_whose_division_has_drifted():
    db.record_matches("common-gen5", "c1", "leagueMatch", [
        _raw_match("m1", opp_id="c2", opp_name="Same Division Club"),
        _raw_match("m2", opp_id="c3", opp_name="Different Division Club"),
    ])
    db.sync_league_roster("common-gen5", "c1", "Our Club", max_teams=25)
    ids = {r["club_id"] for r in db.league_roster("common-gen5")}
    assert ids == {"c1", "c2", "c3"}  # both discovered -- division unknown yet, nothing to drop

    db.record_snapshot("common-gen5", "c1", {}, {"currentDivision": "3", "points": "10"})
    db.record_snapshot("common-gen5", "c2", {}, {"currentDivision": "3", "points": "8"})
    db.record_snapshot("common-gen5", "c3", {}, {"currentDivision": "5", "points": "20"})

    db.sync_league_roster("common-gen5", "c1", "Our Club", max_teams=25)
    ids = {r["club_id"] for r in db.league_roster("common-gen5")}
    assert ids == {"c1", "c2"}  # c3's division no longer matches ours -- dropped outright


def test_sync_league_roster_off_division_drop_frees_a_slot_for_new_opponents():
    db.record_matches("common-gen5", "c1", "leagueMatch", [
        _raw_match("m1", opp_id="c2", opp_name="Off Division Club"),
    ])
    db.sync_league_roster("common-gen5", "c1", "Our Club", max_teams=2)  # cap: us + one opponent
    db.record_snapshot("common-gen5", "c1", {}, {"currentDivision": "3", "points": "10"})
    # High points would normally protect c2 from the points-based eviction below,
    # but its division no longer matches ours.
    db.record_snapshot("common-gen5", "c2", {}, {"currentDivision": "5", "points": "999"})

    db.record_matches("common-gen5", "c1", "leagueMatch", [
        _raw_match("m2", opp_id="c3", opp_name="Same Division Club", timestamp=2000),
    ])
    db.sync_league_roster("common-gen5", "c1", "Our Club", max_teams=2)

    ids = {r["club_id"] for r in db.league_roster("common-gen5")}
    # c2 is dropped for being off-division (not evicted for low points -- it had
    # the highest), freeing the slot c3 takes instead of being turned away.
    assert ids == {"c1", "c3"}


def test_sync_league_roster_off_division_club_stays_excluded_across_repeated_calls():
    # Regression: an evicted-for-division club is still in `matches` (we've
    # played it before), so a naive eviction that only removes it from
    # league_clubs gets it immediately re-added on the very next call, once
    # the opponent-discovery loop rediscovers it as "not currently known".
    db.record_matches("common-gen5", "c1", "leagueMatch", [
        _raw_match("m1", opp_id="c2", opp_name="Different Division Club"),
    ])
    db.sync_league_roster("common-gen5", "c1", "Our Club", max_teams=25)
    db.record_snapshot("common-gen5", "c1", {}, {"currentDivision": "3", "points": "10"})
    db.record_snapshot("common-gen5", "c2", {}, {"currentDivision": "5", "points": "20"})

    for _ in range(3):
        db.sync_league_roster("common-gen5", "c1", "Our Club", max_teams=25)
        ids = {r["club_id"] for r in db.league_roster("common-gen5")}
        assert ids == {"c1"}  # c2 must not bounce back in on a later call


def test_sync_league_roster_leaves_unpolled_members_alone():
    db.record_matches("common-gen5", "c1", "leagueMatch", [
        _raw_match("m1", opp_id="c2", opp_name="Unpolled Club"),
    ])
    db.sync_league_roster("common-gen5", "c1", "Our Club", max_teams=25)
    db.record_snapshot("common-gen5", "c1", {}, {"currentDivision": "3", "points": "10"})
    # c2 never got a snapshot -- its division is unknown, not proven wrong, so a
    # second sync (now that our own division is known) must leave it alone.
    db.sync_league_roster("common-gen5", "c1", "Our Club", max_teams=25)
    ids = {r["club_id"] for r in db.league_roster("common-gen5")}
    assert "c2" in ids


def test_connect_migrates_pre_existing_db_missing_new_columns(tmp_path, monkeypatch):
    # Simulate a history.db from before opp_club_id/team_size existed --
    # ALTER TABLE must add them in place without touching existing rows,
    # since this data (a mirror of EA's own rolling window) can't be
    # reconstructed if lost. See db.py's module docstring.
    import sqlite3
    old_db = tmp_path / "old_history.db"
    conn = sqlite3.connect(old_db)
    conn.execute("""CREATE TABLE matches (
        match_id TEXT NOT NULL, platform TEXT NOT NULL, club_id TEXT NOT NULL,
        played_at INTEGER, match_type TEXT NOT NULL, us_score INTEGER, opp_score INTEGER,
        opp_name TEXT, outcome TEXT, forfeit INTEGER, captured_at INTEGER NOT NULL,
        PRIMARY KEY (match_id, club_id)
    )""")
    conn.execute("""CREATE TABLE club_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, club_id TEXT NOT NULL,
        captured_at INTEGER NOT NULL, division TEXT, best_division TEXT, points TEXT,
        skill_rating TEXT, wins TEXT, losses TEXT, ties TEXT, goals TEXT, goals_against TEXT,
        promotions TEXT, relegations TEXT
    )""")
    conn.execute("""INSERT INTO matches VALUES
        ('old1','common-gen5','c1',1000,'leagueMatch',2,1,'Legacy FC','W',0,1000)""")
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", old_db)
    history = db.match_history("common-gen5", "c1")
    assert len(history) == 1  # pre-existing row survived the migration
    assert history[0]["match_id"] == "old1"

    # New columns exist and are usable now.
    db.record_matches("common-gen5", "c1", "leagueMatch", [_raw_match("new1", opp_id="c2", opp_name="New Club")])
    assert db.known_opponents("common-gen5", "c1") == [{"club_id": "c2", "label": "New Club"}]


def test_recent_form_returns_last_n_oldest_first_excluding_forfeits():
    db.record_matches("common-gen5", "c1", "leagueMatch", [
        _raw_match("m1", wins="1", losses="0", timestamp=1000),
        _raw_match("m2", wins="0", losses="1", timestamp=2000),
        _raw_match("m3", forfeit=True, timestamp=2500),
        _raw_match("m4", wins="0", losses="0", timestamp=3000),  # draw
    ])
    assert db.recent_form("common-gen5", "c1", limit=5) == ["W", "L", "D"]


def test_recent_form_caps_at_limit():
    db.record_matches("common-gen5", "c1", "leagueMatch", [
        _raw_match(f"m{i}", wins="1", losses="0", timestamp=i) for i in range(1, 8)
    ])
    assert db.recent_form("common-gen5", "c1", limit=5) == ["W"] * 5


def test_league_table_filters_to_our_current_division_and_sorts_by_skill_rating():
    db.record_matches("common-gen5", "c1", "leagueMatch", [
        _raw_match("m1", opp_id="c2", opp_name="Same Division Club"),
        _raw_match("m2", opp_id="c3", opp_name="Different Division Club"),
    ])
    db.sync_league_roster("common-gen5", "c1", "Our Club", max_teams=25)
    db.record_snapshot("common-gen5", "c1", {"wins": "1", "losses": "0", "ties": "0", "skillRating": "1900"},
                        {"currentDivision": "3", "points": "10"})
    db.record_snapshot("common-gen5", "c2", {"wins": "3", "losses": "0", "ties": "0", "skillRating": "1700"},
                        {"currentDivision": "3", "points": "15"})
    db.record_snapshot("common-gen5", "c3", {"wins": "0", "losses": "3", "ties": "0", "skillRating": "2500"},
                        {"currentDivision": "5", "points": "2"})

    table = db.league_table("common-gen5", "c1")
    labels = [r["label"] for r in table]
    assert "Different Division Club" not in labels  # different division -- filtered out
    # Ranked by skill rating, not points: the club with MORE points (15 vs 10)
    # sits below us because its rating is lower. Points reward volume as much
    # as quality, which is the whole reason for the change.
    assert labels == ["Our Club", "Same Division Club"]
    assert table[0]["skill_rating"] == 1900
    assert table[0]["is_us"] is True


def test_league_table_breaks_rating_ties_on_points():
    db.record_matches("common-gen5", "c1", "leagueMatch", [
        _raw_match("m1", opp_id="c2", opp_name="Level Club"),
    ])
    db.sync_league_roster("common-gen5", "c1", "Our Club", max_teams=25)
    db.record_snapshot("common-gen5", "c1", {"wins": "1", "losses": "0", "ties": "0", "skillRating": "1800"},
                        {"currentDivision": "3", "points": "20"})
    db.record_snapshot("common-gen5", "c2", {"wins": "1", "losses": "0", "ties": "0", "skillRating": "1800"},
                        {"currentDivision": "3", "points": "44"})

    table = db.league_table("common-gen5", "c1")
    assert [r["label"] for r in table] == ["Level Club", "Our Club"]


def test_league_table_sorts_unrated_clubs_last():
    """A club whose snapshot carries no skill rating has unknown strength,
    not zero -- it belongs at the bottom rather than jumbled among rated
    clubs. (A club with no snapshot at all never reaches the sort: the
    division filter drops it first, and the page lists it as excluded.)"""
    db.record_matches("common-gen5", "c1", "leagueMatch", [
        _raw_match("m1", opp_id="c2", opp_name="Unrated Club"),
    ])
    db.sync_league_roster("common-gen5", "c1", "Our Club", max_teams=25)
    db.record_snapshot("common-gen5", "c1", {"wins": "1", "losses": "0", "ties": "0", "skillRating": "1200"},
                        {"currentDivision": "3", "points": "5"})
    db.record_snapshot("common-gen5", "c2", {"wins": "9", "losses": "0", "ties": "0"},
                        {"currentDivision": "3", "points": "99"})

    table = db.league_table("common-gen5", "c1")
    # 99 points and a 9-0 record still can't outrank a real rating.
    assert [r["label"] for r in table] == ["Our Club", "Unrated Club"]
    assert table[-1]["skill_rating"] is None


def test_league_table_shows_all_when_our_own_club_has_no_snapshot_yet():
    db.record_matches("common-gen5", "c1", "leagueMatch", [_raw_match("m1", opp_id="c2", opp_name="Rivals FC")])
    db.sync_league_roster("common-gen5", "c1", "Our Club", max_teams=25)
    # No snapshots recorded at all -- our_division is None, so nothing to filter by.
    table = db.league_table("common-gen5", "c1")
    assert {r["label"] for r in table} == {"Our Club", "Rivals FC"}
    assert all(r["has_data"] is False for r in table)
