"""One-shot poller: snapshot each club in tracked_clubs.json into
data/history.db. Run on a schedule via systemd (see
deploy/proclubs-poll.service + .timer) -- deliberately NOT part of the
Flask app process, so it's unaffected by how many gunicorn workers are
serving requests (no "which worker owns the timer" problem to solve).

Also maintains the auto-built league table (see db.sync_league_roster):
after polling our own club and recording its matches, whichever real
opponents that turned up get folded into the league roster (capped at
LEAGUE_TABLE_MAX_TEAMS, evicting the lowest-ranked member once full --
see db.py), and every club currently in that roster gets polled the same
way our own club does, so it has its own division/points/matches to show.

Run it manually to test: python poll.py
"""

import json
from pathlib import Path

import config
import db
import ea_client

TRACKED_CLUBS_PATH = Path(__file__).parent / "tracked_clubs.json"

# Friendlies are excluded -- they don't feed the "how is the team actually
# performing" trend charts this exists for.
MATCH_TYPES = ("leagueMatch", "playoffMatch")

LEAGUE_TABLE_MAX_TEAMS = config.LEAGUE_TABLE_MAX_TEAMS


def load_tracked_clubs():
    if not TRACKED_CLUBS_PATH.exists():
        print(f"{TRACKED_CLUBS_PATH} doesn't exist -- nothing to poll")
        return []
    return json.loads(TRACKED_CLUBS_PATH.read_text())


def poll_club(platform, club_id, label):
    print(f"[{label}] polling {platform}/{club_id}...")

    try:
        stats = ea_client.overall_stats(platform, club_id)
        division = ea_client.division_stats(platform, club_id)
    except ea_client.EAApiError as exc:
        print(f"[{label}] overall/division stats failed: {exc}")
        stats = division = None

    team_size = None
    try:
        members = (ea_client.member_stats(platform, club_id) or {}).get("members", [])
        team_size = len(members)
    except ea_client.EAApiError as exc:
        print(f"[{label}] member stats failed: {exc}")

    if stats or division:
        db.record_snapshot(platform, club_id, stats, division, team_size=team_size)
        print(f"[{label}] snapshot recorded")

    for match_type in MATCH_TYPES:
        try:
            matches = ea_client.matches_stats(platform, club_id, match_type, max_results=30)
        except ea_client.EAApiError as exc:
            print(f"[{label}] {match_type} fetch failed: {exc}")
            continue
        inserted = db.record_matches(platform, club_id, match_type, matches)
        print(f"[{label}] {match_type}: {inserted} new match(es) of {len(matches)} fetched")


def sync_and_poll_league_table(clubs, polled):
    """clubs: tracked_clubs.json entries already polled this run. polled:
    the set of (platform, club_id) already covered, so a club that's both
    "our own tracked club" and "in the league roster" isn't polled twice
    in the same run."""
    if not config.CLUB_ID:
        return
    platform, our_id = config.CLUB_PLATFORM, str(config.CLUB_ID)
    our_label = next(
        (c.get("label", our_id) for c in clubs if str(c["clubId"]) == our_id), our_id,
    )

    db.sync_league_roster(platform, our_id, our_label, max_teams=LEAGUE_TABLE_MAX_TEAMS)

    for entry in db.league_roster(platform):
        key = (platform, entry["club_id"])
        if key in polled:
            continue
        poll_club(platform, entry["club_id"], entry["label"] or entry["club_id"])
        polled.add(key)


def main():
    clubs = load_tracked_clubs()
    if not clubs:
        print("tracked_clubs.json is empty -- nothing to poll")
        return

    polled = set()
    for c in clubs:
        poll_club(c["platform"], c["clubId"], c.get("label", c["clubId"]))
        polled.add((c["platform"], str(c["clubId"])))

    sync_and_poll_league_table(clubs, polled)


if __name__ == "__main__":
    main()
