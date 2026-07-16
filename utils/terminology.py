"""Per-unit terminology presets.

The platform's mechanics are game-agnostic; only the vocabulary is flavoured.
A unit picks a preset (Command Tent → Identity) and every page renders that
preset's words. New keys added here fall back to the War of Rights wording, so
templates can reference a key before every preset defines it.

A unit may also override individual words on top of its preset; those overrides
are stored as JSON in GuildConfig.terminology_custom and merged last.
"""
import json

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


# Words an admin can override, in display order, with a friendly label for the
# editor. (event_types is handled separately as a comma-separated list.)
EDITABLE_KEYS = [
    ("unit", "Unit (e.g. Regiment)"),
    ("subunit", "Sub-unit (e.g. Company)"),
    ("hq", "Home / headquarters"),
    ("active_roster", "Active roster (nav)"),
    ("roster_nav", "Full roster (nav)"),
    ("events_nav", "Events (nav)"),
    ("event", "A single event"),
    ("event_create", "Create-event button"),
    ("event_upcoming", "Upcoming-events heading"),
    ("event_past", "Past-events heading"),
    ("attendance", "Attendance"),
    ("honors", "Awards / honors"),
    ("leave", "Leave (nav)"),
    ("on_leave", "On-leave heading"),
    ("promotions", "Promotions"),
    ("recruits", "Recruits (nav)"),
    ("join", "Join page"),
    ("command", "Admin area"),
    ("member", "member (singular)"),
    ("members", "members (plural)"),
    ("tagline", "Banner tagline"),
]
_EDITABLE = {k for k, _ in EDITABLE_KEYS}


def resolve_terms(preset: str | None, custom_json: str | None) -> dict:
    """Effective terminology: preset words with any per-unit overrides applied."""
    terms = get_terms(preset)
    if not custom_json:
        return terms
    try:
        custom = json.loads(custom_json)
    except (ValueError, TypeError):
        return terms
    if not isinstance(custom, dict):
        return terms
    for key, value in custom.items():
        if key == "event_types" and isinstance(value, list) and value:
            terms["event_types"] = [str(v) for v in value]
        elif key in _EDITABLE and isinstance(value, str) and value.strip():
            terms[key] = value.strip()
    return terms


def diff_overrides(preset: str | None, submitted: dict) -> dict:
    """Given submitted field values, keep only those that actually differ from
    the preset — so unedited fields keep following the preset, and changing the
    preset later still flows through for them."""
    base = get_terms(preset)
    overrides: dict = {}
    for key, _ in EDITABLE_KEYS:
        val = (submitted.get(key) or "").strip()
        if val and val != base.get(key):
            overrides[key] = val
    raw_types = (submitted.get("event_types") or "").strip()
    if raw_types:
        types = [t.strip() for t in raw_types.split(",") if t.strip()]
        if types and types != base.get("event_types"):
            overrides["event_types"] = types
    return overrides
