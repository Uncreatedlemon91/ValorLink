"""Per-unit terminology.

The platform's mechanics are game-agnostic; only the vocabulary is flavoured.
There is one default vocabulary (below); an admin overrides any individual word
from the Command Tent's Custom Wording editor. Overrides are stored as JSON in
GuildConfig.terminology_custom and merged on top of the default, so a unit can
speak in whatever terms suit its game.

Visual themes (parchment / modern) are separate and chosen alongside.
"""
import json

# The default vocabulary. Every key an admin can override lives here so no
# template ever renders a blank, and the Custom Wording editor has a baseline.
DEFAULT_TERMS = {
    "unit": "Regiment",
    "unit_plural": "Regiments",
    "subunit": "Company",
    "subunit_plural": "Companies",
    "roster_nav": "Muster Roll",
    "roster_full": "Complete Muster Roll",
    "active_roster": "Roster",
    "events_nav": "Events",
    "event": "Event",
    "events": "Events",
    "event_create": "Create Event",
    "event_upcoming": "Upcoming Events",
    "event_past": "Past Events",
    "attendance": "Attendance",
    "honors": "Honors",
    "assignments": "Assignments",
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

# Visual themes (CSS skins), chosen per unit.
THEME_CHOICES = [
    ("parchment", "Parchment (period)"),
    ("modern", "Modern (dark)"),
]
DEFAULT_THEME = "parchment"
THEMES = {value for value, _ in THEME_CHOICES}

# Words an admin can override, in display order, with a friendly editor label.
# (event_types is handled separately as a comma-separated list.)
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
    ("assignments", "Secondary assignments"),
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


def resolve_terms(custom_json: str | None) -> dict:
    """Effective terminology: the default words with any per-unit overrides."""
    terms = dict(DEFAULT_TERMS)
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


def diff_overrides(submitted: dict) -> dict:
    """Given submitted field values, keep only those that differ from the
    defaults, so unedited fields keep following the default wording."""
    overrides: dict = {}
    for key, _ in EDITABLE_KEYS:
        val = (submitted.get(key) or "").strip()
        if val and val != DEFAULT_TERMS.get(key):
            overrides[key] = val
    raw_types = (submitted.get("event_types") or "").strip()
    if raw_types:
        types = [t.strip() for t in raw_types.split(",") if t.strip()]
        if types and types != DEFAULT_TERMS.get("event_types"):
            overrides["event_types"] = types
    return overrides
