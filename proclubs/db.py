"""Local history store for the Pro Clubs Tracker.

EA's API only ever exposes a rolling window of recent matches (confirmed:
capped around 10 for this club regardless of what we ask for) and no
historical division/rating data at all. This module persists a copy of
what we see on each poll (see poll.py) into a small SQLite file so charts
can eventually show real season-long trends instead of just "the last
handful of matches EA still has lying around".

Plain stdlib sqlite3 -- no new dependency, and its own file and schema,
separate from the site's content database (see database.py).
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

-- Membership of the auto-built league table (see sync_league_roster) --
-- capped at LEAGUE_TABLE_MAX_TEAMS, populated from real opponents (see
-- matches.opp_club_id below), not manually curated. `pinned` protects our
-- own club's row from ever being evicted to make room for a new opponent.
CREATE TABLE IF NOT EXISTS league_clubs (
    platform TEXT NOT NULL,
    club_id TEXT NOT NULL,
    label TEXT,
    added_at INTEGER NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (platform, club_id)
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

-- EA's own season totals per player (members/stats), latest poll wins.
-- Complete where the matches table isn't: EA only ever lists the last ~10
-- matches, so any played before tracking began or while the poll was down
-- are missing there, but still counted here.
CREATE TABLE IF NOT EXISTS member_totals (
    platform TEXT NOT NULL,
    club_id TEXT NOT NULL,
    player_name TEXT NOT NULL,
    games_played INTEGER,
    goals INTEGER,
    assists INTEGER,
    mom INTEGER,
    captured_at INTEGER NOT NULL,
    PRIMARY KEY (platform, club_id, player_name)
);

-- Who's currently in the squad, per EA's member list (see record_squad) --
-- the baseline that squad_moves below is diffed against.
CREATE TABLE IF NOT EXISTS squad_members (
    platform TEXT NOT NULL,
    club_id TEXT NOT NULL,
    player_name TEXT NOT NULL,
    joined_at INTEGER NOT NULL,
    PRIMARY KEY (platform, club_id, player_name)
);

-- Signings and departures, one row per change noticed between two polls.
-- EA exposes no transfer history of its own, so this is the only record.
CREATE TABLE IF NOT EXISTS squad_moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    club_id TEXT NOT NULL,
    player_name TEXT NOT NULL,
    move TEXT NOT NULL,  -- 'joined' or 'left'
    moved_at INTEGER NOT NULL
);
"""


# Columns added after this file's tables first shipped -- `CREATE TABLE IF
# NOT EXISTS` doesn't touch a table that already exists, so an existing
# history.db needs these added in place (never dropped/recreated: this data
# isn't reconstructable, see the module docstring). (table, column, type).
_MIGRATIONS = [
    ("matches", "opp_club_id", "TEXT"),
    ("club_snapshots", "team_size", "INTEGER"),
]


def _migrate(conn):
    changed = False
    for table, column, coltype in _MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            changed = True
    if changed:
        conn.commit()  # DDL isn't auto-committed by executescript's own commit above


def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _num(v, cast=int):
    try:
        return cast(float(v))
    except (TypeError, ValueError):
        return None


def record_snapshot(platform, club_id, stats, division, team_size=None):
    stats = stats or {}
    division = division or {}
    conn = _connect()
    with conn:
        conn.execute(
            """INSERT INTO club_snapshots
               (platform, club_id, captured_at, division, best_division, points,
                skill_rating, wins, losses, ties, goals, goals_against, promotions,
                relegations, team_size)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                team_size,
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
                    opp_name, outcome, forfeit, captured_at, opp_club_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (match_id, platform, club_id, played_at, match_type, us_score, opp_score,
                 opp_name, outcome, forfeit, now, opp_id),
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


def rival_records(platform, club_id):
    """Head-to-head record against every opponent we've faced, aggregated
    from tracked match history -- this is the "Competition" report's
    answer to a question EA's API can't answer directly (see ea_client.py):
    our own actual results against each club, built from data we're
    already capturing on every poll. See league_table() below for the
    separate auto-built standings table across all tracked opponents.

    Forfeits are excluded from the tally the same way the rest of this
    module treats them -- a DNF isn't a real result to build a rivalry
    record on. match_history() already returns oldest-first, so a single
    pass naturally leaves each rival's last_outcome/last_played_at as its
    most recent meeting."""
    rivals: dict[str, dict] = {}
    for m in match_history(platform, club_id):
        if m["forfeit"]:
            continue
        name = m["opp_name"]
        r = rivals.setdefault(name, {
            "name": name, "played": 0, "wins": 0, "draws": 0, "losses": 0,
            "gf": 0, "ga": 0, "last_outcome": None, "last_played_at": None,
        })
        r["played"] += 1
        if m["outcome"] == "W":
            r["wins"] += 1
        elif m["outcome"] == "L":
            r["losses"] += 1
        else:
            r["draws"] += 1
        r["gf"] += m["us_score"] or 0
        r["ga"] += m["opp_score"] or 0
        r["last_outcome"] = m["outcome"]
        r["last_played_at"] = m["played_at"]
    return sorted(rivals.values(), key=lambda r: -r["played"])


def latest_snapshot(platform, club_id):
    """Most recent club_snapshots row for a club, or None if it's never
    been polled. Tie-broken by id, not just captured_at -- two snapshots
    recorded within the same second (e.g. two polls in quick succession,
    or just test setup) would otherwise be ambiguous."""
    conn = _connect()
    row = conn.execute(
        """SELECT captured_at, division, best_division, points, skill_rating,
                  wins, losses, ties, goals, goals_against, promotions,
                  relegations, team_size
           FROM club_snapshots WHERE platform=? AND club_id=?
           ORDER BY captured_at DESC, id DESC LIMIT 1""",
        (platform, club_id),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def known_opponents(platform, club_id):
    """Every club we've actually played, with a real EA club id -- the
    self-updating source for the league table (see sync_league_roster),
    as opposed to a manually maintained roster. Matches recorded before
    matches.opp_club_id existed won't have one and are excluded -- not
    backfillable, see the migration note above SCHEMA."""
    conn = _connect()
    rows = conn.execute(
        """SELECT DISTINCT opp_club_id AS club_id, opp_name AS label
           FROM matches WHERE platform=? AND club_id=? AND opp_club_id IS NOT NULL""",
        (platform, club_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def league_roster(platform):
    """Current membership of the auto-built league table -- see
    sync_league_roster(), which is what actually maintains this."""
    conn = _connect()
    rows = conn.execute(
        "SELECT club_id, label, added_at, pinned FROM league_clubs WHERE platform=? ORDER BY added_at",
        (platform,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def sync_league_roster(platform, our_club_id, our_label, max_teams=25):
    """Keeps league_clubs in sync with who we've actually played (see
    known_opponents), capped at max_teams -- no manual roster to maintain.
    Our own club is always present and pinned (protected from eviction).
    Once full, a newly-discovered opponent replaces whichever non-pinned
    member currently has the lowest skill rating in their latest snapshot
    (the same ranking league_table() displays by, so the club dropped is
    the one shown at the bottom), ties broken by whichever has been sitting
    in the table longest. A club with
    no snapshot yet counts as the lowest possible, so an unpolled/unproven
    member is the first to go if nothing else ranks lower -- except one
    just added in this same call, which won't have had a chance to be
    polled yet either; the added_at tiebreak protects it from immediately
    evicting itself.

    Members whose last known division has drifted away from ours are
    dropped outright before any of that, and a discovered opponent whose
    last known division doesn't match ours is never (re-)added in the
    first place -- off-division clubs don't belong in our table at all,
    so they shouldn't occupy one of the max_teams slots either (see
    league_table(), which also hides them from display, but that alone
    would leave them silently eating a roster slot forever). This uses
    club_snapshots, not league_clubs membership, as the source of truth
    for "do we know this club's division" -- snapshot history persists
    even after a club is dropped from league_clubs, so a club evicted for
    being off-division stays excluded on every future call instead of
    bouncing back in the next time it turns up in `matches`."""
    our_club_id = str(our_club_id)
    conn = _connect()
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO league_clubs (platform, club_id, label, added_at, pinned)
               VALUES (?,?,?,?,1)""",
            (platform, our_club_id, our_label, int(time.time())),
        )
        known_ids = {r["club_id"] for r in conn.execute(
            "SELECT club_id FROM league_clubs WHERE platform=?", (platform,)
        ).fetchall()}

        def _latest_snapshot(club_id):
            return conn.execute(
                """SELECT division, points, skill_rating FROM club_snapshots WHERE platform=? AND club_id=?
                   ORDER BY captured_at DESC, id DESC LIMIT 1""",
                (platform, club_id),
            ).fetchone()

        def _rating_for(club_id):
            """Ranks a member for eviction the same way league_table() ranks
            it for display -- otherwise the club we drop when the table is
            full isn't the one the table shows at the bottom."""
            row = _latest_snapshot(club_id)
            if not row or row["skill_rating"] is None:
                return -1
            try:
                return int(float(row["skill_rating"]))
            except (TypeError, ValueError):
                return -1

        def _off_division(club_id):
            # A club never polled yet (no snapshot) hasn't had the chance
            # to show it belongs or not -- treat it as fine until its own
            # division is known.
            if our_division is None:
                return False
            snap = _latest_snapshot(club_id)
            return bool(snap and snap["division"] is not None and snap["division"] != our_division)

        our_snap = _latest_snapshot(our_club_id)
        our_division = our_snap["division"] if our_snap else None
        if our_division is not None:
            members = conn.execute(
                "SELECT club_id FROM league_clubs WHERE platform=? AND pinned=0", (platform,)
            ).fetchall()
            for member in members:
                cid = member["club_id"]
                if _off_division(cid):
                    conn.execute(
                        "DELETE FROM league_clubs WHERE platform=? AND club_id=?", (platform, cid),
                    )
                    known_ids.discard(cid)

        opponents = conn.execute(
            """SELECT DISTINCT opp_club_id AS club_id, opp_name AS label
               FROM matches WHERE platform=? AND club_id=? AND opp_club_id IS NOT NULL""",
            (platform, our_club_id),
        ).fetchall()

        for opp in opponents:
            cid, label = opp["club_id"], opp["label"]
            if cid in known_ids:
                continue
            if _off_division(cid):
                continue

            count = conn.execute(
                "SELECT COUNT(*) AS n FROM league_clubs WHERE platform=?", (platform,)
            ).fetchone()["n"]

            if count >= max_teams:
                candidates = conn.execute(
                    "SELECT club_id, added_at FROM league_clubs WHERE platform=? AND pinned=0",
                    (platform,),
                ).fetchall()
                if not candidates:
                    continue  # everyone left is pinned -- no room to make
                evict = min(candidates, key=lambda r: (_rating_for(r["club_id"]), r["added_at"]))
                conn.execute(
                    "DELETE FROM league_clubs WHERE platform=? AND club_id=?",
                    (platform, evict["club_id"]),
                )
                known_ids.discard(evict["club_id"])

            conn.execute(
                "INSERT INTO league_clubs (platform, club_id, label, added_at, pinned) VALUES (?,?,?,?,0)",
                (platform, cid, label, int(time.time())),
            )
            known_ids.add(cid)
    conn.close()


def recent_form(platform, club_id, limit=5):
    """This club's last `limit` results, oldest of the batch first --
    'W'/'L'/'D' per match_history()'s outcome field. Forfeits are skipped,
    matching rival_records()'s treatment."""
    history = [m for m in match_history(platform, club_id) if not m["forfeit"]]
    return [m["outcome"] for m in history[-limit:]]


def league_table(platform, our_club_id):
    """The auto-built league table: every club in league_clubs with its
    current division/rating/record from its latest snapshot, filtered to
    clubs currently in the SAME division as us. Division numbers are a
    skill tier that moves independently per club (see ea_client.py) --
    not a real league/region grouping, EA doesn't expose one -- so "same
    division right now" is the closest available proxy for "who's
    actually in our bracket."

    Ranked by skill rating, highest first. Points measure how much a club
    has played as much as how well -- they only accumulate, so a club that
    grinds twice as many matches outranks a better one that played fewer.
    Skill rating is EA's own strength number and moves both ways, which
    makes it the fairer sort for a table whose members have wildly
    different match counts. Points are still recorded and still decide
    ties. A club that's never been polled sorts last (unknown strength,
    not zero).
    """
    our_club_id = str(our_club_id)
    rows = []
    our_division = None
    for entry in league_roster(platform):
        snap = latest_snapshot(platform, entry["club_id"]) or {}
        wins = _num(snap.get("wins")) or 0
        losses = _num(snap.get("losses")) or 0
        ties = _num(snap.get("ties")) or 0
        is_us = entry["club_id"] == our_club_id
        row = {
            "club_id": entry["club_id"],
            "label": entry["label"] or entry["club_id"],
            "is_us": is_us,
            "division": snap.get("division"),
            "skill_rating": _num(snap.get("skill_rating")),
            "points": _num(snap.get("points")),
            "played": wins + losses + ties,
            "team_size": _num(snap.get("team_size")),
            "form": recent_form(platform, entry["club_id"]),
            "has_data": bool(snap),
        }
        if is_us:
            our_division = row["division"]
        rows.append(row)

    if our_division is not None:
        rows = [r for r in rows if r["division"] == our_division]

    rows.sort(
        key=lambda r: (
            r["skill_rating"] if r["skill_rating"] is not None else -1,
            r["points"] if r["points"] is not None else -1,
        ),
        reverse=True,
    )
    return rows


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
        """SELECT mp.*, m.played_at, m.match_type, m.opp_name, m.us_score, m.opp_score,
                  m.outcome
           FROM match_players mp
           JOIN matches m ON m.match_id = mp.match_id AND m.club_id = mp.club_id
           WHERE m.platform=? AND m.club_id=? AND mp.player_name=?
           ORDER BY m.played_at""",
        (platform, club_id, player_name),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def career_totals(platform, club_id):
    """Career totals per player for our club, keyed by casefolded gamertag:
    {name, apps, goals, assists, mom, clean_sheets} -- what milestones are
    judged against."""
    conn = _connect()
    rows = conn.execute(
        """SELECT mp.player_name AS name, COUNT(*) AS apps,
                  COALESCE(SUM(mp.goals), 0) AS goals, COALESCE(SUM(mp.assists), 0) AS assists,
                  COALESCE(SUM(mp.mom), 0) AS mom, COALESCE(SUM(mp.clean_sheet), 0) AS clean_sheets
           FROM match_players mp
           JOIN matches m ON m.match_id = mp.match_id AND m.club_id = mp.club_id
           WHERE m.platform=? AND m.club_id=?
           GROUP BY mp.player_name""",
        (platform, club_id),
    ).fetchall()
    conn.close()
    totals = {}
    for r in rows:
        key = r["name"].casefold()
        if key in totals:  # same gamertag in different casing
            for k in ("apps", "goals", "assists", "mom", "clean_sheets"):
                totals[key][k] += r[k]
        else:
            totals[key] = dict(r)
    return totals


def match_player_ratings(club_id, match_ids):
    """[{match_id, player_name, rating, goals, assists, mom}] for the given
    matches of our club -- for a match report's "top rated"."""
    if not match_ids:
        return []
    conn = _connect()
    marks = ",".join("?" * len(match_ids))
    rows = conn.execute(
        f"""SELECT match_id, player_name, rating, goals, assists, mom FROM match_players
            WHERE club_id=? AND match_id IN ({marks})""",
        [club_id, *match_ids],
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def record_member_totals(platform, club_id, members):
    """Stores EA's season totals for each current member (members/stats).
    Someone who has left keeps their last totals."""
    now = int(time.time())
    conn = _connect()
    with conn:
        for m in members or []:
            name = (m.get("name") or "").strip()
            if not name:
                continue
            conn.execute(
                """INSERT INTO member_totals (platform, club_id, player_name, games_played,
                                              goals, assists, mom, captured_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT (platform, club_id, player_name) DO UPDATE SET
                     games_played=excluded.games_played, goals=excluded.goals,
                     assists=excluded.assists, mom=excluded.mom, captured_at=excluded.captured_at""",
                (platform, club_id, name, _num(m.get("gamesPlayed")), _num(m.get("goals")),
                 _num(m.get("assists")), _num(m.get("manOfTheMatch")), now),
            )
    conn.close()


def member_totals(platform, club_id):
    """gamertag (casefolded) -> {name, games_played, goals, assists, mom}."""
    conn = _connect()
    rows = conn.execute(
        """SELECT player_name, games_played, goals, assists, mom FROM member_totals
           WHERE platform=? AND club_id=?""", (platform, club_id)).fetchall()
    conn.close()
    return {r["player_name"].casefold(): {"name": r["player_name"], **{k: r[k] or 0 for k in (
        "games_played", "goals", "assists", "mom")}} for r in rows}


def ea_match_total(platform, club_id):
    """How many matches EA says the club has played (wins + draws + losses
    on the latest snapshot), or None before the first snapshot."""
    snap = latest_snapshot(platform, club_id)
    if not snap:
        return None
    parts = [_num(snap.get(k)) for k in ("wins", "ties", "losses")]
    if all(p is None for p in parts):
        return None
    return sum(p or 0 for p in parts)


def record_squad(platform, club_id, member_names):
    """Diffs EA's current member list against the last one we saw, logging
    each signing/departure into squad_moves. Returns (joined, left) as
    sorted name lists.

    The very first call for a club only records a baseline -- everyone
    already in the squad when tracking started isn't a "signing". An empty
    member list is ignored outright: that's EA failing to answer, not the
    whole squad walking out at once."""
    current = {n.strip() for n in member_names if n and n.strip()}
    if not current:
        return [], []
    now = int(time.time())
    conn = _connect()
    with conn:
        existing = {r["player_name"] for r in conn.execute(
            "SELECT player_name FROM squad_members WHERE platform=? AND club_id=?",
            (platform, club_id),
        ).fetchall()}
        baseline = not existing
        joined = sorted(current - existing)
        left = sorted(existing - current)
        for name in joined:
            conn.execute(
                "INSERT INTO squad_members (platform, club_id, player_name, joined_at) VALUES (?,?,?,?)",
                (platform, club_id, name, now),
            )
        for name in left:
            conn.execute(
                "DELETE FROM squad_members WHERE platform=? AND club_id=? AND player_name=?",
                (platform, club_id, name),
            )
        if not baseline:
            conn.executemany(
                "INSERT INTO squad_moves (platform, club_id, player_name, move, moved_at) VALUES (?,?,?,?,?)",
                [(platform, club_id, n, "joined", now) for n in joined]
                + [(platform, club_id, n, "left", now) for n in left],
            )
    conn.close()
    if baseline:
        return [], []
    return joined, left


def squad_moves(platform, club_id, since):
    """Signings/departures logged at or after `since` (unix seconds),
    oldest first."""
    conn = _connect()
    rows = conn.execute(
        """SELECT player_name, move, moved_at FROM squad_moves
           WHERE platform=? AND club_id=? AND moved_at>=? ORDER BY moved_at, id""",
        (platform, club_id, since),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def squad_usage(platform, club_id, window=10, form_games=5, min_form_apps=3):
    """Who has been playing, from the matches recorded for our club.

    Returns {"window": n, "players": {gamertag_casefolded: {...}}} where n
    is how many of the club's most recent matches the window actually
    covers (fewer than `window` early in a season; window=None means every
    recorded match), and each player has:

      name          gamertag as EA spells it
      apps_window   matches played among those n
      apps_total    every recorded appearance
      goals, assists, mom   totals across every recorded appearance
      form          average rating over their last `form_games`
                    appearances, or None with fewer than `min_form_apps`
      positions     {pos: count} as EA recorded where they played

    Keyed case-insensitively because gamertag links are matched that way
    (see services.set_player_link).
    """
    conn = _connect()
    recent = [r["match_id"] for r in conn.execute(
        """SELECT match_id FROM matches WHERE platform=? AND club_id=?
           ORDER BY played_at DESC, match_id DESC LIMIT ?""",
        (platform, club_id, -1 if window is None else window),
    ).fetchall()]
    rows = conn.execute(
        """SELECT mp.player_name, mp.match_id, mp.pos, mp.rating, mp.goals,
                  mp.assists, mp.mom
           FROM match_players mp
           JOIN matches m ON m.match_id = mp.match_id AND m.club_id = mp.club_id
           WHERE m.platform=? AND m.club_id=?
           ORDER BY m.played_at DESC, m.match_id DESC""",
        (platform, club_id),
    ).fetchall()
    conn.close()

    in_window = set(recent)
    players = {}
    for r in rows:
        key = r["player_name"].casefold()
        p = players.setdefault(key, {
            "name": r["player_name"], "apps_window": 0, "apps_total": 0,
            "goals": 0, "assists": 0, "mom": 0, "ratings": [], "positions": {},
        })
        p["apps_total"] += 1
        if r["match_id"] in in_window:
            p["apps_window"] += 1
        p["goals"] += r["goals"] or 0
        p["assists"] += r["assists"] or 0
        p["mom"] += r["mom"] or 0
        # Rows arrive newest first, so the first ratings seen are the
        # most recent ones.
        if r["rating"] is not None and len(p["ratings"]) < form_games:
            p["ratings"].append(r["rating"])
        if r["pos"]:
            p["positions"][r["pos"]] = p["positions"].get(r["pos"], 0) + 1
    for p in players.values():
        ratings = p.pop("ratings")
        p["form"] = (round(sum(ratings) / len(ratings), 2)
                     if len(ratings) >= min_form_apps else None)
    return {"window": len(recent), "players": players}


def snapshot_at(platform, club_id, ts):
    """The latest snapshot captured at or before `ts` -- "where did we
    stand at the start of the week" -- or None if tracking started later."""
    conn = _connect()
    row = conn.execute(
        """SELECT captured_at, division, points, skill_rating, wins, losses, ties
           FROM club_snapshots WHERE platform=? AND club_id=? AND captured_at<=?
           ORDER BY captured_at DESC, id DESC LIMIT 1""",
        (platform, club_id, ts),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def player_totals(platform, club_id, since):
    """Per-player totals across matches played at or after `since`, best
    performers first. Forfeits are excluded, as everywhere else here."""
    conn = _connect()
    rows = conn.execute(
        """SELECT mp.player_name,
                  COUNT(*) AS apps,
                  COALESCE(SUM(mp.goals), 0) AS goals,
                  COALESCE(SUM(mp.assists), 0) AS assists,
                  ROUND(AVG(mp.rating), 2) AS avg_rating,
                  COALESCE(SUM(mp.mom), 0) AS mom,
                  COALESCE(SUM(mp.clean_sheet), 0) AS clean_sheets,
                  COALESCE(SUM(mp.saves), 0) AS saves,
                  COALESCE(SUM(mp.tackles_made), 0) AS tackles,
                  COALESCE(SUM(mp.red_cards), 0) AS red_cards
           FROM match_players mp
           JOIN matches m ON m.match_id = mp.match_id AND m.club_id = mp.club_id
           WHERE m.platform=? AND m.club_id=? AND m.played_at>=? AND m.forfeit=0
           GROUP BY mp.player_name
           ORDER BY goals DESC, assists DESC, avg_rating DESC""",
        (platform, club_id, since),
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
