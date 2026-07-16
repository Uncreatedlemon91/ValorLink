"""Per-unit terminology presets.

The platform's mechanics are game-agnostic; only the vocabulary is flavoured.
A unit picks a preset (Command Tent → Identity) and every page renders that
preset's words. New keys added here fall back to the War of Rights wording, so
templates can reference a key before every preset defines it.
"""

# War of Rights / 1860s regimental flavour — also the fallback for any key a
# preset omits.
WAR_OF_RIGHTS = {
    "unit": "Regiment",
    "unit_plural": "Regiments",
    "subunit": "Company",
    "subunit_plural": "Companies",
    "roster_nav": "Muster Roll",
    "roster_full": "Complete Muster Roll",
    "active_roster": "Roster",
    "events_nav": "Muster Calls",
    "event": "Muster Call",
    "events": "Muster Calls",
    "event_create": "Call a Muster",
    "event_upcoming": "Muster Calls to Come",
    "event_past": "Calls Past",
    "attendance": "Attendance",
    "honors": "Honors",
    "leave": "Furlough",
    "leave_full": "Leave of Absence",
    "on_leave": "On Furlough",
    "enlist": "Enlist",
    "enlisted": "Enlisted",
    "hq": "Headquarters",
    "command": "Command Tent",
    "promotions": "Promotions",
    "recruits": "Recruits",
    "recruitment": "Recruitment",
    "join": "Join Us",
    "member": "member",
    "members": "members",
    "orders": "Post Orders",
    "tagline": "Regimental Headquarters · Order Book",
    "event_types": ["Drill", "Battle", "Operation"],
}

def _preset(**overrides) -> dict:
    """A preset built on the War of Rights defaults with the given overrides,
    so a preset only has to state the words that differ."""
    base = dict(WAR_OF_RIGHTS)
    base.update(overrides)
    return base


# Neutral modern-military flavour, for tactical shooters / milsim units.
MODERN_MILITARY = {
    "unit": "Unit",
    "unit_plural": "Units",
    "subunit": "Squad",
    "subunit_plural": "Squads",
    "roster_nav": "Roster",
    "roster_full": "Full Roster",
    "active_roster": "Active Roster",
    "events_nav": "Operations",
    "event": "Operation",
    "events": "Operations",
    "event_create": "Schedule an Op",
    "event_upcoming": "Upcoming Operations",
    "event_past": "Past Operations",
    "attendance": "Attendance",
    "honors": "Commendations",
    "leave": "Leave",
    "leave_full": "Leave",
    "on_leave": "On Leave",
    "enlist": "Enlist",
    "enlisted": "Enlisted",
    "hq": "HQ",
    "command": "Command",
    "promotions": "Promotions",
    "recruits": "Recruits",
    "recruitment": "Recruitment",
    "join": "Join Us",
    "member": "operator",
    "members": "operators",
    "orders": "Post Orders",
    "tagline": "Unit Command · Roster",
    "event_types": ["Training", "Operation", "Scrim"],
}

# Squad — tactical shooter clans organised into squads.
SQUAD = _preset(
    unit="Unit", unit_plural="Units", subunit="Squad", subunit_plural="Squads",
    roster_nav="Roster", roster_full="Full Roster", active_roster="Active Roster",
    events_nav="Operations", event="Operation", events="Operations",
    event_create="Schedule an Op", event_upcoming="Upcoming Operations",
    event_past="Past Operations", honors="Commendations",
    leave="Leave", leave_full="Leave", on_leave="On Leave",
    hq="HQ", command="Command", member="member", members="members", tagline="Unit Command · Roster",
    event_types=["Training", "Scrimmage", "Operation"],
)

# Hell Let Loose — WWII company/squad flavour.
HELL_LET_LOOSE = _preset(
    unit="Company", unit_plural="Companies", subunit="Squad", subunit_plural="Squads",
    roster_nav="Roster", roster_full="Full Roster", active_roster="Active Roster",
    events_nav="Operations", event="Operation", events="Operations",
    event_create="Schedule an Op", event_upcoming="Upcoming Operations",
    event_past="Past Operations", honors="Commendations",
    leave="Leave", leave_full="Leave", on_leave="On Leave",
    hq="HQ", command="Command", member="soldier", members="soldiers", tagline="Company Command · Roster",
    event_types=["Drill", "Battle", "Scrimmage"],
)

# Foxhole — persistent-war regiments, logistics and fronts.
FOXHOLE = _preset(
    unit="Regiment", unit_plural="Regiments", subunit="Squad", subunit_plural="Squads",
    roster_nav="Roster", roster_full="Full Roster", active_roster="Active Roster",
    events_nav="Operations", event="Operation", events="Operations",
    event_create="Plan an Op", event_upcoming="Upcoming Operations",
    event_past="Past Operations", honors="Commendations",
    leave="Leave", leave_full="Leave", on_leave="On Leave",
    hq="HQ", command="Command", member="soldier", members="soldiers", tagline="Regimental Command · Roster",
    event_types=["Logi Run", "Operation", "Defense"],
)

# EVE Online — corporations, fleets, pilots.
EVE_ONLINE = _preset(
    unit="Corporation", unit_plural="Corporations", subunit="Fleet", subunit_plural="Fleets",
    roster_nav="Members", roster_full="Corporation Members", active_roster="Members",
    events_nav="Fleets", event="Fleet", events="Fleets",
    event_create="Form a Fleet", event_upcoming="Upcoming Fleets",
    event_past="Past Fleets", honors="Medals",
    leave="Leave", leave_full="Leave", on_leave="On Leave",
    hq="Corp HQ", command="Directorate", member="pilot", members="pilots",
    enlist="Apply", enlisted="Joined", tagline="Corporation · Fleet Command",
    event_types=["Roam", "Fleet Op", "Structure"],
)

PRESETS = {
    "wor": WAR_OF_RIGHTS,
    "modern": MODERN_MILITARY,
    "squad": SQUAD,
    "hll": HELL_LET_LOOSE,
    "foxhole": FOXHOLE,
    "eve": EVE_ONLINE,
}

# (value, human label) for a picker; first entry is the default.
PRESET_CHOICES = [
    ("wor", "War of Rights (period)"),
    ("modern", "Modern military"),
    ("squad", "Squad"),
    ("hll", "Hell Let Loose"),
    ("foxhole", "Foxhole"),
    ("eve", "EVE Online"),
]

DEFAULT_PRESET = "wor"

# Visual themes (CSS skins). Independent of the vocabulary preset.
THEME_CHOICES = [
    ("parchment", "Parchment (period)"),
    ("modern", "Modern (dark)"),
]
DEFAULT_THEME = "parchment"
THEMES = {value for value, _ in THEME_CHOICES}


def get_terms(preset: str | None) -> dict:
    """The terminology map for a preset, with any missing keys filled from the
    War of Rights defaults so no template ever renders a blank."""
    chosen = PRESETS.get(preset or DEFAULT_PRESET, WAR_OF_RIGHTS)
    if chosen is WAR_OF_RIGHTS:
        return dict(WAR_OF_RIGHTS)
    merged = dict(WAR_OF_RIGHTS)
    merged.update(chosen)
    return merged


def event_types_for(preset: str | None) -> list[str]:
    return get_terms(preset)["event_types"]
