"""One-shot poller: snapshot each club in tracked_clubs.json into
data/history.db. Run on a schedule via systemd (see
deploy/proclubs-poll.service + .timer) -- deliberately NOT part of the
Flask app process, so it's unaffected by how many gunicorn workers are
serving requests (no "which worker owns the timer" problem to solve).

Run it manually to test: python poll.py
"""

import json
from pathlib import Path

import db
import ea_client

TRACKED_CLUBS_PATH = Path(__file__).parent / "tracked_clubs.json"

# Friendlies are excluded -- they don't feed the "how is the team actually
# performing" trend charts this exists for.
MATCH_TYPES = ("leagueMatch", "playoffMatch")


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
    if stats or division:
        db.record_snapshot(platform, club_id, stats, division)
        print(f"[{label}] snapshot recorded")

    for match_type in MATCH_TYPES:
        try:
            matches = ea_client.matches_stats(platform, club_id, match_type, max_results=30)
        except ea_client.EAApiError as exc:
            print(f"[{label}] {match_type} fetch failed: {exc}")
            continue
        inserted = db.record_matches(platform, club_id, match_type, matches)
        print(f"[{label}] {match_type}: {inserted} new match(es) of {len(matches)} fetched")


def main():
    clubs = load_tracked_clubs()
    if not clubs:
        print("tracked_clubs.json is empty -- nothing to poll")
        return
    for c in clubs:
        poll_club(c["platform"], c["clubId"], c.get("label", c["clubId"]))


if __name__ == "__main__":
    main()
