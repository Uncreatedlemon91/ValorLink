"""Switch the site to a new season's club, and erase the old season's stats.

EA issues a brand-new club ID every title: the FC 26 club and the FC 27 club
are different clubs to the API even under the same name, and nothing carries
over. So a new season is three steps, all done here:

  1. find the new club's ID on EA's API,
  2. erase the stats history accumulated for the old one (data/history.db:
     snapshots, matches, per-player lines, and the league table built from
     that season's opponents),
  3. point CLUB_ID in .env at the new club.

Usage (on the droplet, from /opt/valorlink/proclubs, as the app user):

  python season.py find                      # search EA for CLUB_NAME
  python season.py switch                    # use the one exact match found
  python season.py switch --club-id 1234567  # or name the club outright
  python season.py reset                     # step 2 only

Then restart the site and run one poll (the script prints the commands).

WHAT IT ERASES, AND WHAT IT DOESN'T. Only data/history.db -- the EA-derived
stats. The site's own content (news, events and sign-ups, squad moves,
clips, streamers, gamertag links, the tactics board) is the club's, not the
game's, and is left alone; gamertags in particular carry over, since they
are EA account names rather than anything per-title.

The old history.db is MOVED into the backups directory rather than deleted
outright (pass --no-archive to delete it). That data can't be re-fetched --
EA evicts old matches from its own window -- and the step that decides
which club is "ours" is the one most likely to go wrong when several clubs
share a name. The command to delete the archive is printed alongside it.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import config
import db
import ea_client

ENV_PATH = Path(__file__).parent / ".env"
# Next to the daily backups (deploy/backup.sh), so everything recoverable
# lives in one place.
ARCHIVE_DIR = Path(os.getenv("BACKUP_DIR") or Path(__file__).parent.parent / "backups")

# Searched when no --platform is given: the crossplay pool first, where a
# current club almost certainly is, then last-gen. "nx" is a legacy code
# from older titles and not worth a request.
SEARCH_PLATFORMS = ("common-gen5", "common-gen4")


class SeasonError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Finding the club
# --------------------------------------------------------------------------- #
def find_clubs(name: str, platforms=SEARCH_PLATFORMS) -> list[dict]:
    """Every club EA returns for `name`, across the given platforms.

    EA's search is a substring match over its all-time leaderboard, so it
    returns lookalikes ("Yeehaw FC 2", "Yeehaw FC Reserves") as well as
    the club itself -- each result carries `exact` so callers can tell.
    """
    results = []
    errors = []
    for platform in platforms:
        try:
            clubs = ea_client.search_club(platform, name)
        except ea_client.EAApiError as exc:
            errors.append(f"{platform}: {exc}")
            continue
        for club in clubs:
            results.append({**club, "platform": platform,
                            "exact": club["name"].strip().casefold() == name.strip().casefold()})
    if not results and errors:
        # Every platform failed -- that's an outage, not "no such club".
        raise SeasonError("couldn't search EA: " + "; ".join(errors))
    return results


def resolve_club(name: str, club_id: str | None, platform: str | None) -> dict:
    """The club to switch to, verified against EA.

    With --club-id, that club is looked up directly (clubs/info works for
    any club, including one too new for the leaderboard search). Without
    it, the search must produce exactly one exact-name match -- two clubs
    called "Yeehaw FC" is a question for a human, not a guess for a script.
    """
    if club_id:
        plat = platform or config.CLUB_PLATFORM
        try:
            info = ea_client.club_info(plat, club_id)
        except ea_client.EAApiError as exc:
            raise SeasonError(f"couldn't look up club {club_id} on {plat}: {exc}") from exc
        if not info:
            raise SeasonError(f"EA has no club {club_id} on {plat}.")
        return {"clubId": str(club_id), "name": info.get("name", ""), "platform": plat}

    platforms = (platform,) if platform else SEARCH_PLATFORMS
    exact = [c for c in find_clubs(name, platforms) if c["exact"]]
    if len(exact) == 1:
        return exact[0]
    if not exact:
        raise SeasonError(
            f"no club named exactly {name!r} on EA. A club created in the last few "
            f"days may not be in EA's search yet -- it searches a leaderboard that "
            f"lags behind. Find the ID another way (see `find` output) and pass "
            f"--club-id."
        )
    listed = ", ".join(f"{c['clubId']} ({c['platform']})" for c in exact)
    raise SeasonError(f"{len(exact)} clubs are named exactly {name!r}: {listed}. "
                      f"Pick yours with --club-id.")


# --------------------------------------------------------------------------- #
# Erasing the old season
# --------------------------------------------------------------------------- #
def erase_history(*, archive: bool = True, label: str = "") -> Path | None:
    """Removes data/history.db (and its WAL/SHM sidecars) and recreates it
    empty. Returns where the old file was archived, or None.

    Moving the file rather than DELETE-ing rows means the erase is total --
    no stale league-table roster or autoincrement counters left behind --
    and the archive is a byte-for-byte copy, openable as-is.
    """
    path = Path(db.DB_PATH)
    archived = None
    if path.exists():
        if archive:
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            suffix = f"-{label}" if label else ""
            archived = ARCHIVE_DIR / f"history{suffix}-{stamp}.db"
            shutil.move(str(path), archived)
        else:
            path.unlink()
    for sidecar in (path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        sidecar.unlink(missing_ok=True)
    # Recreate the empty schema now, so the site and the poller don't each
    # race to be the first to create it.
    db._connect().close()
    return archived


# --------------------------------------------------------------------------- #
# Pointing .env at the new club
# --------------------------------------------------------------------------- #
def set_env_values(values: dict[str, str], path: Path | None = None) -> None:
    """Sets KEY=value lines in .env, replacing each key's first live line
    or appending it. Every other line -- comments, secrets, blank lines --
    is kept exactly as it was.

    Written to a temp file and renamed over the original, so a crash
    mid-write can't leave a half-written secrets file, and the original's
    permissions (600 on the droplet) are carried over.
    """
    path = path or ENV_PATH
    if not path.exists():
        raise SeasonError(f"{path} doesn't exist -- copy .env.example to it first.")
    lines = path.read_text().splitlines(keepends=True)
    pending = dict(values)
    out = []
    for line in lines:
        stripped = line.lstrip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else None
        if key in pending and not stripped.startswith("#"):
            out.append(f"{key}={pending.pop(key)}\n")
        else:
            out.append(line)
    if pending:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.extend(f"{k}={v}\n" for k, v in pending.items())

    mode = path.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.writelines(out)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _confirm(prompt: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    reply = input(f"{prompt} [y/N] ").strip().lower()
    if reply not in ("y", "yes"):
        raise SeasonError("aborted -- nothing was changed.")


def _print_archive(archived: Path | None) -> None:
    if archived:
        print(f"  old stats archived to {archived}")
        print(f"  (delete it for good with: rm {archived})")
    else:
        print("  old stats deleted.")


def cmd_find(args) -> None:
    platforms = (args.platform,) if args.platform else SEARCH_PLATFORMS
    clubs = find_clubs(args.name, platforms)
    if not clubs:
        print(f"EA returned no clubs matching {args.name!r} on {', '.join(platforms)}.")
        print("A club created in the last few days may not be in EA's search yet.")
        print("Its ID is in the URL of the club's page on EA's Pro Clubs site, and on")
        print("most Pro Clubs stat trackers. Then: python season.py switch --club-id <ID>")
        return
    print(f"Clubs matching {args.name!r}:")
    for c in clubs:
        mark = "  <- exact name" if c["exact"] else ""
        current = "  (current CLUB_ID)" if c["clubId"] == str(config.CLUB_ID) else ""
        print(f"  {c['clubId']:>10}  {c['platform']:<12}  {c['name']}{mark}{current}")
    exact = [c for c in clubs if c["exact"]]
    if len(exact) == 1:
        print(f"\nOne exact match. Switch to it with: python season.py switch")
    elif len(exact) > 1:
        print(f"\n{len(exact)} exact matches -- pick yours: python season.py switch --club-id <ID>")


def cmd_switch(args) -> None:
    club = resolve_club(args.name, args.club_id, args.platform)
    old_id = str(config.CLUB_ID or "")
    if club["clubId"] == old_id and club["platform"] == config.CLUB_PLATFORM:
        print(f"CLUB_ID is already {old_id} ({club['name']}). Nothing to switch.")
        print("To erase its stats anyway: python season.py reset")
        return

    print(f"Switching to: {club['name']}  (club {club['clubId']}, {club['platform']})")
    if old_id:
        print(f"Replacing:    club {old_id} ({config.CLUB_PLATFORM})")
    print("This ERASES all accumulated stats history -- snapshots, matches, player")
    print("lines and the league table. Site content (news, events, squad moves,")
    print("clips, gamertags) is kept.")
    _confirm("Proceed?", args.yes)

    archived = erase_history(archive=not args.no_archive, label=old_id or "")
    set_env_values({"CLUB_ID": club["clubId"], "CLUB_PLATFORM": club["platform"]})
    print(f"\nDone. CLUB_ID={club['clubId']}, CLUB_PLATFORM={club['platform']}")
    _print_archive(archived)
    _print_next_steps()


def cmd_reset(args) -> None:
    print("This ERASES all accumulated stats history -- snapshots, matches, player")
    print("lines and the league table. CLUB_ID and site content are unchanged.")
    _confirm("Proceed?", args.yes)
    archived = erase_history(archive=not args.no_archive, label=str(config.CLUB_ID or ""))
    print("\nDone.")
    _print_archive(archived)
    _print_next_steps()


def _print_next_steps() -> None:
    here = Path(__file__).resolve().parent
    print("\nNow restart the site (it reads CLUB_ID at startup) and take a first")
    print("snapshot rather than waiting for the hourly poll:")
    print("  sudo systemctl restart yeehaw-fc")
    print(f"  sudo -u valorlink {here}/.venv/bin/python3 {here}/poll.py")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Switch to a new season's club and erase the old season's stats.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--name", default=config.CLUB_NAME,
                       help=f"club name to search for (default: {config.CLUB_NAME!r})")
        p.add_argument("--platform", choices=sorted(ea_client.PLATFORMS),
                       help="limit to one platform (default: search all current ones)")

    p_find = sub.add_parser("find", help="search EA for the club")
    common(p_find)
    p_find.set_defaults(func=cmd_find)

    p_switch = sub.add_parser("switch", help="point the site at a new club, erasing old stats")
    common(p_switch)
    p_switch.add_argument("--club-id", help="use this club ID instead of searching")
    p_switch.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p_switch.add_argument("--no-archive", action="store_true",
                          help="delete the old stats outright instead of archiving them")
    p_switch.set_defaults(func=cmd_switch)

    p_reset = sub.add_parser("reset", help="erase stats history only")
    p_reset.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p_reset.add_argument("--no-archive", action="store_true",
                         help="delete the old stats outright instead of archiving them")
    p_reset.set_defaults(func=cmd_reset)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except SeasonError as exc:
        print(f"season.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
