"""The squad screen: who's under contract, whether they're playing as
promised, and whether the formation can be covered.

Pure functions over data the routes gather -- contracts (site.db),
gamertag links (site.db), match appearances (history.db) and attendance
(site.db) -- so every rule here is testable without a database or EA.

Two things are computed:

* SQUAD ROWS. One per player under contract, joining their contract to
  what EA says they actually did, through their linked gamertag. The
  useful part is the flags: Football Manager's "unhappy with playing time",
  turned into a real check of squad status against appearances.

* SQUAD DEPTH. For the formation on the tactics board, how many contracted
  players cover each position -- naturally (primary position) or as cover
  (secondary). "One goalkeeper, no backup" is a recruitment need.

What "playing time" means here: the number of the club's recent matches
(league and playoff -- poll.py records nothing else) in which EA lists the
player. EA can't tell a start from a substitute appearance, so nothing
here claims to count starts.
"""
from __future__ import annotations

import discord_roster

# --------------------------------------------------------------------------- #
# Tuning. Named so the thresholds behind every flag are in one place.
# --------------------------------------------------------------------------- #
# How many of the club's most recent matches "playing time" looks at.
USAGE_WINDOW = 10
# Below this many club matches in the window, playing time isn't judged at
# all -- "played 1 of 2" says nothing about whether a promise is being kept.
MIN_WINDOW_TO_JUDGE = 5
# Share of the window each squad status should expect to play in, after
# FM's playing-time expectations. A Reserve is promised nothing.
EXPECTED_SHARE = {"Starter": 0.5, "Rotation": 0.2, "Reserve": 0.0}
# Form is the average match rating over a player's last few appearances.
FORM_GAMES = 5
MIN_APPS_FOR_FORM = 3
# A Rotation or Reserve player averaging this or better is flagged as
# worth promoting; an expiring contract is flagged louder at it too.
IN_FORM_RATING = 7.5
# Attendance below this (once there's enough history to be a rate at all,
# see services.MIN_EVENTS_FOR_RELIABILITY) is flagged.
LOW_ATTENDANCE = 60
# An uncontracted gamertag appearing this often in the window is listed as
# "playing without a contract".
REGULAR_APPS = 3

STATUS_ORDER = {s: i for i, s in enumerate(discord_roster.SQUAD_STATUSES)}
POSITION_ORDER = {p: i for i, p in enumerate(discord_roster.PITCH_POSITIONS)}

# --------------------------------------------------------------------------- #
# Formation slots -> the positions a contract names
# --------------------------------------------------------------------------- #
# A formation's slots are the tactics board's chips (formations.py); a
# contract names a broader pitch position (discord_roster.PITCH_POSITIONS).
# Every label any formation uses must appear here -- a test enforces it, so
# adding a formation with a new label fails loudly instead of quietly
# dropping a position from the depth chart.
SLOT_POSITION = {
    "GK": "Goalkeeper",
    "CB": "Centre Back",
    "LB": "Full Back", "RB": "Full Back",
    "LWB": "Wing Back", "RWB": "Wing Back",
    "CDM": "Defensive Midfield",
    "CM": "Centre Midfield",
    "CAM": "Attacking Midfield", "LAM": "Attacking Midfield", "RAM": "Attacking Midfield",
    "LM": "Winger", "RM": "Winger", "LW": "Winger", "RW": "Winger",
    "ST": "Striker", "CF": "Striker", "LF": "Striker", "RF": "Striker",
}
ANY_OUTFIELD = "Any Outfield"

DEPTH_GAP = "gap"            # can't field this position from contracted players
DEPTH_THIN = "thin"          # can field it, with nobody spare
DEPTH_OK = "ok"              # fielded, with at least one spare


def formation_needs(slots: dict) -> dict[str, int]:
    """position -> how many of it the formation fields, in pitch order."""
    needs: dict[str, int] = {}
    for meta in slots.values():
        position = SLOT_POSITION[meta["label"]]
        needs[position] = needs.get(position, 0) + 1
    return dict(sorted(needs.items(), key=lambda kv: POSITION_ORDER[kv[0]]))


def squad_depth(slots: dict, contracts: list) -> dict:
    """Depth for each position the formation uses.

    A player counts as a NATURAL at their primary position and as COVER at
    their secondary. "Any Outfield" as a primary counts as cover for every
    outfield position -- versatile, but not a specialist anywhere -- and
    never for goalkeeper.

    States, per position:
      gap   -- naturals + cover can't fill the slots at all
      thin  -- they can, but there's nobody spare for an absence
      ok    -- filled, with at least one spare
    """
    rows = []
    for position, needed in formation_needs(slots).items():
        naturals = [c.display_name for c in contracts if c.position == position]
        cover = [c.display_name for c in contracts
                 if c.position != position and (
                     c.secondary_position == position
                     or (c.position == ANY_OUTFIELD and position != "Goalkeeper"))]
        total = len(naturals) + len(cover)
        if total < needed:
            state = DEPTH_GAP
        elif total < needed + 1:
            state = DEPTH_THIN
        else:
            state = DEPTH_OK
        rows.append({"position": position, "needed": needed, "naturals": naturals,
                     "cover": cover, "state": state})

    # Contracted specialists the formation has no slot for -- worth knowing
    # before offering another one, or when choosing the next formation.
    used = {r["position"] for r in rows}
    unused: dict[str, int] = {}
    for c in contracts:
        if c.position and c.position not in used and c.position != ANY_OUTFIELD:
            unused[c.position] = unused.get(c.position, 0) + 1
    return {
        "rows": rows,
        "unused": dict(sorted(unused.items(), key=lambda kv: POSITION_ORDER.get(kv[0], 99))),
        "gaps": sum(1 for r in rows if r["state"] == DEPTH_GAP),
        "thin": sum(1 for r in rows if r["state"] == DEPTH_THIN),
    }


# --------------------------------------------------------------------------- #
# Squad rows
# --------------------------------------------------------------------------- #
def _flag(kind: str, text: str) -> dict:
    """kind: 'warn' (needs a decision), 'good' (an opportunity), 'info'."""
    return {"kind": kind, "text": text}


def _apps_phrase(apps: int, window: int) -> str:
    return f"played {apps} of the last {window}"


def player_flags(*, status: str, linked: bool, usage: dict | None, window: int,
                 attendance: dict | None, contract_state: str,
                 time_left: str) -> list[dict]:
    """The things about one player a manager would want pointed out.

    Every flag is grounded in something recorded -- a contract term, an EA
    appearance, a marked attendance -- and says which, so it can be checked
    rather than taken on trust.
    """
    flags = []
    if not linked:
        flags.append(_flag("info", "No gamertag linked, so playing time and form can't be tracked."))

    form = (usage or {}).get("form")
    apps = (usage or {}).get("apps_window", 0)
    if linked and window >= MIN_WINDOW_TO_JUDGE:
        expected = EXPECTED_SHARE.get(status, 0.0)
        if expected and apps / window < expected:
            flags.append(_flag(
                "warn", f"{status}, but has {_apps_phrase(apps, window)} matches."))
        if status in ("Rotation", "Reserve") and form is not None and form >= IN_FORM_RATING:
            flags.append(_flag(
                "good", f"{status}, averaging {form:.1f} — worth a promotion?"))

    in_form = form is not None and form >= IN_FORM_RATING
    if contract_state == "expired":
        flags.append(_flag("warn", "Contract has run out — renew or release."
                                   + (f" In form at {form:.1f}." if in_form else "")))
    elif contract_state == "expiring":
        # time_left is services.contract_time_left's phrase ("4 days left",
        # "ends today"), so it stands after a dash rather than mid-sentence.
        flags.append(_flag("warn", f"Contract expiring — {time_left}."
                                   + (f" In form at {form:.1f}." if in_form else "")))

    rate = (attendance or {}).get("rate")
    if rate is not None and rate < LOW_ATTENDANCE:
        flags.append(_flag("warn", f"Attended {rate}% of marked events."))
    return flags


def squad_rows(*, contracts: list, links: dict[int, str], usage: dict,
               attendance: dict[int, dict], contract_states: dict[int, str],
               time_left) -> list[dict]:
    """One row per contracted player, sorted Starter -> Reserve, then by
    position down the pitch, then name.

    links: discord_user_id -> gamertag. usage: db.squad_usage()'s result.
    attendance: services.attendance_records_for()'s result.
    """
    players = usage.get("players", {})
    window = usage.get("window", 0)
    rows = []
    for c in contracts:
        uid = int(c.discord_id)
        gamertag = links.get(uid)
        u = players.get(gamertag.casefold()) if gamertag else None
        att = attendance.get(uid)
        state = contract_states.get(c.id, "active")
        left = time_left(c)
        rows.append({
            "contract": c,
            "gamertag": gamertag,
            "usage": u,
            "attendance": att,
            "contract_state": state,
            "time_left": left,
            "flags": player_flags(status=c.squad_status, linked=bool(gamertag), usage=u,
                                  window=window, attendance=att,
                                  contract_state=state, time_left=left),
        })
    rows.sort(key=lambda r: (STATUS_ORDER.get(r["contract"].squad_status, 9),
                             POSITION_ORDER.get(r["contract"].position, 99),
                             r["contract"].display_name.casefold()))
    return rows


def uncontracted_regulars(usage: dict, links: dict[int, str], contracts: list) -> list[dict]:
    """Gamertags playing regularly for the club with no contract behind
    them -- either somebody who should have one recorded, or a contracted
    player whose gamertag hasn't been linked yet."""
    contracted_ids = {int(c.discord_id) for c in contracts}
    covered = {name.casefold() for uid, name in links.items() if uid in contracted_ids}
    out = [u for key, u in usage.get("players", {}).items()
           if key not in covered and u["apps_window"] >= REGULAR_APPS]
    out.sort(key=lambda u: (-u["apps_window"], u["name"].casefold()))
    return out


def summarize(rows: list[dict]) -> dict:
    return {
        "players": len(rows),
        "warnings": sum(1 for r in rows for f in r["flags"] if f["kind"] == "warn"),
        "unlinked": sum(1 for r in rows if not r["gamertag"]),
        "by_status": {s: sum(1 for r in rows if r["contract"].squad_status == s)
                      for s in discord_roster.SQUAD_STATUSES},
    }


def form_label(form: float | None) -> str:
    return "—" if form is None else f"{form:.1f}"
