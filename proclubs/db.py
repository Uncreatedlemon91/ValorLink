"""Local history store for the Pro Clubs Tracker.

EA's API only ever exposes a rolling window of recent matches (confirmed:
capped around 10 for this club regardless of what we ask for) and no
historical division/rating data at all. This module persists a copy of
what we see on each poll (see poll.py) into a small SQLite file so charts
can eventually show real season-long trends instead of just "the last
handful of matches EA still has lying around".

Plain stdlib sqlite3 -- no new dependency, and isolated from ValorLink's
own databases (its own file, its own schema, never touched by anything
else in this repo).
"""

import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "history.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS club_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    club_id TEXT NOT NULL,
    captured_at INTEGER NOT NULL,
    division TEXT,
    best_division TEXT,
    points TEXT,
    skill_rating TEXT,
    wins TEXT,
    losses TEXT,
    ties TEXT,
    goals TEXT,
    goals_against TEXT,
    promotions TEXT,
    relegations TEXT
);

CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    club_id TEXT NOT NULL,
    played_at INTEGER,
    match_type TEXT NOT NULL,
    us_score INTEGER,
    opp_score INTEGER,
    opp_name TEXT,
    outcome TEXT,
    forfeit INTEGER,
    captured_at INTEGER NOT NULL,
    PRIMARY KEY (match_id, club_id)
);

CREATE TABLE IF NOT EXISTS match_players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL,
    club_id TEXT NOT NULL,
    player_name TEXT NOT NULL,
    pos TEXT,
    rating REAL,
    goals INTEGER,
    assists INTEGER,
    shots INTEGER,
    passes_made INTEGER,
    pass_attempts INTEGER,
    tackles_made INTEGER,
    tackle_attempts INTEGER,
    saves INTEGER,
    mom INTEGER,
    red_cards INTEGER,
    clean_sheet INTEGER,
    minutes_played INTEGER,
    UNIQUE(match_id, club_id, player_name)
);
"""


def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _num(v, cast=int):
    try:
        return cast(float(v))
    except (TypeError, ValueError):
        return None


def record_snapshot(platform, club_id, stats, division):
    stats = stats or {}
    division = division or {}
    conn = _connect()
    with conn:
        conn.execute(
            """INSERT INTO club_snapshots
               (platform, club_id, captured_at, division, best_division, points,
                skill_rating, wins, losses, ties, goals, goals_against, promotions, relegations)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                platform,
                club_id,
                int(time.time()),
                division.get("currentDivision"),
                division.get("bestDivision") or stats.get("bestDivision"),
                division.get("points"),
                stats.get("skillRating"),
                stats.get("wins"),
                stats.get("losses"),
                stats.get("ties"),
                stats.get("goals"),
                stats.get("goalsAgainst"),
                stats.get("promotions") or division.get("promotions"),
                stats.get("relegations") or division.get("relegations"),
            ),
        )
    conn.close()


def record_matches(platform, club_id, match_type, raw_matches):
    """raw_matches: the raw list from ea_client.matches_stats. Returns how
    many NEW matches were inserted (already-seen matchIds are skipped)."""
    conn = _connect()
    inserted = 0
    now = int(time.time())
    with conn:
        for m in raw_matches:
            clubs = m.get("clubs") or {}
            us = clubs.get(club_id)
            if not us:
                continue
            opp_id = next((cid for cid in clubs if cid != club_id), None)
            opp = clubs.get(opp_id) or {}
            match_id = str(m.get("matchId") or "").strip()
            if not match_id:
                continue

            us_score = _num(us.get("goals"))
            opp_score = _num(opp.get("goals"))
            outcome = "W" if us.get("wins") == "1" else "L" if us.get("losses") == "1" else "D"
            forfeit = 1 if (us.get("winnerByDnf") == "1" or opp.get("winnerByDnf") == "1") else 0
            played_at = _num(us.get("date")) or _num(m.get("timestamp"))
            opp_name = (opp.get("details") or {}).get("name") or "Opponent"

            cur = conn.execute(
                """INSERT OR IGNORE INTO matches
                   (match_id, platform, club_id, played_at, match_type, us_score, opp_score,
                    opp_name, outcome, forfeit, captured_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (match_id, platform, club_id, played_at, match_type, us_score, opp_score,
                 opp_name, outcome, forfeit, now),
            )
            if not cur.rowcount:
                continue  # already had this match from a previous poll
            inserted += 1

            roster = (m.get("players") or {}).get(club_id) or {}
            for p in roster.values():
                is_gk = p.get("pos") == "goalkeeper"
                clean_sheet = 1 if (_num(p.get("cleansheetsgk")) == 1 or _num(p.get("cleansheetsdef")) == 1) else 0
                minutes = _num(p.get("secondsPlayed") or p.get("gameTime"))
                conn.execute(
                    """INSERT OR IGNORE INTO match_players
                       (match_id, club_id, player_name, pos, rating, goals, assists, shots,
                        passes_made, pass_attempts, tackles_made, tackle_attempts, saves,
                        mom, red_cards, clean_sheet, minutes_played)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        match_id,
                        club_id,
                        p.get("playername") or "Unknown",
                        p.get("pos"),
                        _num(p.get("rating"), float),
                        _num(p.get("goals")),
                        _num(p.get("assists")),
                        _num(p.get("shots")),
                        _num(p.get("passesmade")),
                        _num(p.get("passattempts")),
                        _num(p.get("tacklesmade")),
                        _num(p.get("tackleattempts")),
                        _num(p.get("saves")) if is_gk else None,
                        1 if _num(p.get("mom")) == 1 else 0,
                        _num(p.get("redcards")),
                        clean_sheet,
                        round(minutes / 60) if minutes else None,
                    ),
                )
    conn.close()
    return inserted


def division_history(platform, club_id):
    conn = _connect()
    rows = conn.execute(
        """SELECT captured_at, division, best_division, points, skill_rating,
                  wins, losses, ties, goals, goals_against, promotions, relegations
           FROM club_snapshots WHERE platform=? AND club_id=? ORDER BY captured_at""",
        (platform, club_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def match_history(platform, club_id, match_type=None):
    conn = _connect()
    q = """SELECT match_id, played_at, match_type, us_score, opp_score, opp_name,
                  outcome, forfeit, captured_at
           FROM matches WHERE platform=? AND club_id=?"""
    params = [platform, club_id]
    if match_type:
        q += " AND match_type=?"
        params.append(match_type)
    q += " ORDER BY played_at"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def player_names(platform, club_id):
    """Distinct players we've captured for this club, for a picker."""
    conn = _connect()
    rows = conn.execute(
        """SELECT DISTINCT mp.player_name FROM match_players mp
           JOIN matches m ON m.match_id = mp.match_id AND m.club_id = mp.club_id
           WHERE m.platform=? AND m.club_id=? ORDER BY mp.player_name COLLATE NOCASE""",
        (platform, club_id),
    ).fetchall()
    conn.close()
    return [r["player_name"] for r in rows]


def player_trend(platform, club_id, player_name):
    conn = _connect()
    rows = conn.execute(
        """SELECT mp.*, m.played_at, m.match_type, m.opp_name
           FROM match_players mp
           JOIN matches m ON m.match_id = mp.match_id AND m.club_id = mp.club_id
           WHERE m.platform=? AND m.club_id=? AND mp.player_name=?
           ORDER BY m.played_at""",
        (platform, club_id, player_name),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def tracked_since(platform, club_id):
    """Earliest timestamp we have anything captured for this club, or None
    if we've never successfully polled it (used by the frontend to show an
    honest "not tracked yet" state instead of an empty chart)."""
    conn = _connect()
    row = conn.execute(
        """SELECT MIN(t) AS earliest FROM (
             SELECT MIN(captured_at) AS t FROM club_snapshots WHERE platform=? AND club_id=?
             UNION ALL
             SELECT MIN(captured_at) AS t FROM matches WHERE platform=? AND club_id=?
           )""",
        (platform, club_id, platform, club_id),
    ).fetchone()
    conn.close()
    return row["earliest"] if row and row["earliest"] is not None else None
