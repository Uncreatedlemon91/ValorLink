"""Formation definitions -- the shape of a team sheet.

Lives in its own module rather than app.py because both the routes and
services.py need it: a sign-up claims a position, and validating "is CM2 a
real slot in this event's formation" is a service-layer question, while
app.py can't be imported from services.py without a cycle.
"""
from __future__ import annotations

# --------------------------------------------------------------------------- #
# Tactics board -- staff drag names from the live EA roster onto a pitch;
# see services.py's tactics functions and templates/tactics.html.
# --------------------------------------------------------------------------- #
# Slot coordinates are percentages into the pitch (top/left), pitch attacks
# upward (GK near the bottom, at ~92%, forwards near the top, at ~12%) --
# the common vertical tactics-board orientation. `label` is just the chip
# shown in an empty slot; the dict's own keys are the real slot identity
# used for validation and storage, not the label text.
FORMATIONS = {
    "4-3-3": {
        "GK": {"label": "GK", "top": 92, "left": 50},
        "LB": {"label": "LB", "top": 72, "left": 15}, "CB1": {"label": "CB", "top": 78, "left": 35},
        "CB2": {"label": "CB", "top": 78, "left": 65}, "RB": {"label": "RB", "top": 72, "left": 85},
        "CM1": {"label": "CM", "top": 52, "left": 25}, "CM2": {"label": "CM", "top": 48, "left": 50},
        "CM3": {"label": "CM", "top": 52, "left": 75},
        "LW": {"label": "LW", "top": 20, "left": 18}, "ST": {"label": "ST", "top": 12, "left": 50},
        "RW": {"label": "RW", "top": 20, "left": 82},
    },
    "4-4-2": {
        "GK": {"label": "GK", "top": 92, "left": 50},
        "LB": {"label": "LB", "top": 72, "left": 15}, "CB1": {"label": "CB", "top": 78, "left": 35},
        "CB2": {"label": "CB", "top": 78, "left": 65}, "RB": {"label": "RB", "top": 72, "left": 85},
        "LM": {"label": "LM", "top": 48, "left": 12}, "CM1": {"label": "CM", "top": 50, "left": 38},
        "CM2": {"label": "CM", "top": 50, "left": 62}, "RM": {"label": "RM", "top": 48, "left": 88},
        "ST1": {"label": "ST", "top": 15, "left": 38}, "ST2": {"label": "ST", "top": 15, "left": 62},
    },
    "4-2-3-1": {
        "GK": {"label": "GK", "top": 92, "left": 50},
        "LB": {"label": "LB", "top": 74, "left": 15}, "CB1": {"label": "CB", "top": 80, "left": 35},
        "CB2": {"label": "CB", "top": 80, "left": 65}, "RB": {"label": "RB", "top": 74, "left": 85},
        "CDM1": {"label": "CDM", "top": 58, "left": 38}, "CDM2": {"label": "CDM", "top": 58, "left": 62},
        "LW": {"label": "LW", "top": 32, "left": 18}, "CAM": {"label": "CAM", "top": 32, "left": 50},
        "RW": {"label": "RW", "top": 32, "left": 82}, "ST": {"label": "ST", "top": 12, "left": 50},
    },
    "3-5-2": {
        "GK": {"label": "GK", "top": 92, "left": 50},
        "CB1": {"label": "CB", "top": 78, "left": 25}, "CB2": {"label": "CB", "top": 82, "left": 50},
        "CB3": {"label": "CB", "top": 78, "left": 75},
        "LWB": {"label": "LWB", "top": 52, "left": 8}, "CM1": {"label": "CM", "top": 50, "left": 32},
        "CM2": {"label": "CM", "top": 46, "left": 50}, "CM3": {"label": "CM", "top": 50, "left": 68},
        "RWB": {"label": "RWB", "top": 52, "left": 92},
        "ST1": {"label": "ST", "top": 15, "left": 38}, "ST2": {"label": "ST", "top": 15, "left": 62},
    },
    "4-3-2-1": {
        "GK": {"label": "GK", "top": 92, "left": 50},
        "LB": {"label": "LB", "top": 72, "left": 15}, "CB1": {"label": "CB", "top": 78, "left": 35},
        "CB2": {"label": "CB", "top": 78, "left": 65}, "RB": {"label": "RB", "top": 72, "left": 85},
        "CM1": {"label": "CM", "top": 55, "left": 30}, "CM2": {"label": "CM", "top": 50, "left": 50},
        "CM3": {"label": "CM", "top": 55, "left": 70},
        "LF": {"label": "LF", "top": 30, "left": 35}, "RF": {"label": "RF", "top": 30, "left": 65},
        "ST": {"label": "ST", "top": 12, "left": 50},
    },
    "4-2-2-2": {
        "GK": {"label": "GK", "top": 92, "left": 50},
        "LB": {"label": "LB", "top": 72, "left": 15}, "CB1": {"label": "CB", "top": 78, "left": 35},
        "CB2": {"label": "CB", "top": 78, "left": 65}, "RB": {"label": "RB", "top": 72, "left": 85},
        "CDM1": {"label": "CDM", "top": 58, "left": 38}, "CDM2": {"label": "CDM", "top": 58, "left": 62},
        "LAM": {"label": "LAM", "top": 35, "left": 25}, "RAM": {"label": "RAM", "top": 35, "left": 75},
        "ST1": {"label": "ST", "top": 15, "left": 38}, "ST2": {"label": "ST", "top": 15, "left": 62},
    },
    "4-4-1-1": {
        "GK": {"label": "GK", "top": 92, "left": 50},
        "LB": {"label": "LB", "top": 72, "left": 15}, "CB1": {"label": "CB", "top": 78, "left": 35},
        "CB2": {"label": "CB", "top": 78, "left": 65}, "RB": {"label": "RB", "top": 72, "left": 85},
        "LM": {"label": "LM", "top": 48, "left": 12}, "CM1": {"label": "CM", "top": 50, "left": 38},
        "CM2": {"label": "CM", "top": 50, "left": 62}, "RM": {"label": "RM", "top": 48, "left": 88},
        "CF": {"label": "CF", "top": 26, "left": 50}, "ST": {"label": "ST", "top": 12, "left": 50},
    },
    "4-1-2-1-2": {
        "GK": {"label": "GK", "top": 92, "left": 50},
        "LB": {"label": "LB", "top": 72, "left": 15}, "CB1": {"label": "CB", "top": 78, "left": 35},
        "CB2": {"label": "CB", "top": 78, "left": 65}, "RB": {"label": "RB", "top": 72, "left": 85},
        "CDM": {"label": "CDM", "top": 62, "left": 50},
        "CM1": {"label": "CM", "top": 48, "left": 30}, "CM2": {"label": "CM", "top": 48, "left": 70},
        "CAM": {"label": "CAM", "top": 30, "left": 50},
        "ST1": {"label": "ST", "top": 14, "left": 38}, "ST2": {"label": "ST", "top": 14, "left": 62},
    },
    "4-1-3-2": {
        "GK": {"label": "GK", "top": 92, "left": 50},
        "LB": {"label": "LB", "top": 72, "left": 15}, "CB1": {"label": "CB", "top": 78, "left": 35},
        "CB2": {"label": "CB", "top": 78, "left": 65}, "RB": {"label": "RB", "top": 72, "left": 85},
        "CDM": {"label": "CDM", "top": 62, "left": 50},
        "LM": {"label": "LM", "top": 42, "left": 18}, "CM": {"label": "CM", "top": 40, "left": 50},
        "RM": {"label": "RM", "top": 42, "left": 82},
        "ST1": {"label": "ST", "top": 15, "left": 38}, "ST2": {"label": "ST", "top": 15, "left": 62},
    },
    "4-1-4-1": {
        "GK": {"label": "GK", "top": 92, "left": 50},
        "LB": {"label": "LB", "top": 72, "left": 15}, "CB1": {"label": "CB", "top": 78, "left": 35},
        "CB2": {"label": "CB", "top": 78, "left": 65}, "RB": {"label": "RB", "top": 72, "left": 85},
        "CDM": {"label": "CDM", "top": 62, "left": 50},
        "LM": {"label": "LM", "top": 42, "left": 12}, "CM1": {"label": "CM", "top": 45, "left": 38},
        "CM2": {"label": "CM", "top": 45, "left": 62}, "RM": {"label": "RM", "top": 42, "left": 88},
        "ST": {"label": "ST", "top": 15, "left": 50},
    },
    "4-5-1": {
        "GK": {"label": "GK", "top": 92, "left": 50},
        "LB": {"label": "LB", "top": 72, "left": 15}, "CB1": {"label": "CB", "top": 78, "left": 35},
        "CB2": {"label": "CB", "top": 78, "left": 65}, "RB": {"label": "RB", "top": 72, "left": 85},
        "LM": {"label": "LM", "top": 45, "left": 10}, "CM1": {"label": "CM", "top": 48, "left": 32},
        "CM2": {"label": "CM", "top": 45, "left": 50}, "CM3": {"label": "CM", "top": 48, "left": 68},
        "RM": {"label": "RM", "top": 45, "left": 90},
        "ST": {"label": "ST", "top": 15, "left": 50},
    },
    "3-4-3": {
        "GK": {"label": "GK", "top": 92, "left": 50},
        "CB1": {"label": "CB", "top": 80, "left": 25}, "CB2": {"label": "CB", "top": 84, "left": 50},
        "CB3": {"label": "CB", "top": 80, "left": 75},
        "LM": {"label": "LM", "top": 52, "left": 10}, "CM1": {"label": "CM", "top": 50, "left": 35},
        "CM2": {"label": "CM", "top": 50, "left": 65}, "RM": {"label": "RM", "top": 52, "left": 90},
        "LW": {"label": "LW", "top": 20, "left": 18}, "ST": {"label": "ST", "top": 12, "left": 50},
        "RW": {"label": "RW", "top": 20, "left": 82},
    },
    "3-4-2-1": {
        "GK": {"label": "GK", "top": 92, "left": 50},
        "CB1": {"label": "CB", "top": 80, "left": 25}, "CB2": {"label": "CB", "top": 84, "left": 50},
        "CB3": {"label": "CB", "top": 80, "left": 75},
        "LM": {"label": "LM", "top": 52, "left": 10}, "CM1": {"label": "CM", "top": 50, "left": 35},
        "CM2": {"label": "CM", "top": 50, "left": 65}, "RM": {"label": "RM", "top": 52, "left": 90},
        "CAM1": {"label": "CAM", "top": 28, "left": 35}, "CAM2": {"label": "CAM", "top": 28, "left": 65},
        "ST": {"label": "ST", "top": 12, "left": 50},
    },
    "3-4-1-2": {
        "GK": {"label": "GK", "top": 92, "left": 50},
        "CB1": {"label": "CB", "top": 80, "left": 25}, "CB2": {"label": "CB", "top": 84, "left": 50},
        "CB3": {"label": "CB", "top": 80, "left": 75},
        "LM": {"label": "LM", "top": 52, "left": 10}, "CM1": {"label": "CM", "top": 50, "left": 35},
        "CM2": {"label": "CM", "top": 50, "left": 65}, "RM": {"label": "RM", "top": 52, "left": 90},
        "CAM": {"label": "CAM", "top": 30, "left": 50},
        "ST1": {"label": "ST", "top": 14, "left": 38}, "ST2": {"label": "ST", "top": 14, "left": 62},
    },
    "3-5-1-1": {
        "GK": {"label": "GK", "top": 92, "left": 50},
        "CB1": {"label": "CB", "top": 80, "left": 25}, "CB2": {"label": "CB", "top": 84, "left": 50},
        "CB3": {"label": "CB", "top": 80, "left": 75},
        "LWB": {"label": "LWB", "top": 55, "left": 8}, "CM1": {"label": "CM", "top": 50, "left": 32},
        "CM2": {"label": "CM", "top": 48, "left": 50}, "CM3": {"label": "CM", "top": 50, "left": 68},
        "RWB": {"label": "RWB", "top": 55, "left": 92},
        "CF": {"label": "CF", "top": 26, "left": 50}, "ST": {"label": "ST", "top": 12, "left": 50},
    },
    "3-1-4-2": {
        "GK": {"label": "GK", "top": 92, "left": 50},
        "CB1": {"label": "CB", "top": 80, "left": 25}, "CB2": {"label": "CB", "top": 84, "left": 50},
        "CB3": {"label": "CB", "top": 80, "left": 75},
        "CDM": {"label": "CDM", "top": 65, "left": 50},
        "LM": {"label": "LM", "top": 45, "left": 12}, "CM1": {"label": "CM", "top": 42, "left": 38},
        "CM2": {"label": "CM", "top": 42, "left": 62}, "RM": {"label": "RM", "top": 45, "left": 88},
        "ST1": {"label": "ST", "top": 15, "left": 38}, "ST2": {"label": "ST", "top": 15, "left": 62},
    },
    "5-2-1-2": {
        "GK": {"label": "GK", "top": 92, "left": 50},
        "LWB": {"label": "LWB", "top": 68, "left": 10}, "CB1": {"label": "CB", "top": 78, "left": 30},
        "CB2": {"label": "CB", "top": 82, "left": 50}, "CB3": {"label": "CB", "top": 78, "left": 70},
        "RWB": {"label": "RWB", "top": 68, "left": 90},
        "CM1": {"label": "CM", "top": 48, "left": 38}, "CM2": {"label": "CM", "top": 48, "left": 62},
        "CAM": {"label": "CAM", "top": 30, "left": 50},
        "ST1": {"label": "ST", "top": 14, "left": 38}, "ST2": {"label": "ST", "top": 14, "left": 62},
    },
    "5-2-3": {
        "GK": {"label": "GK", "top": 92, "left": 50},
        "LWB": {"label": "LWB", "top": 68, "left": 10}, "CB1": {"label": "CB", "top": 78, "left": 30},
        "CB2": {"label": "CB", "top": 82, "left": 50}, "CB3": {"label": "CB", "top": 78, "left": 70},
        "RWB": {"label": "RWB", "top": 68, "left": 90},
        "CM1": {"label": "CM", "top": 50, "left": 38}, "CM2": {"label": "CM", "top": 50, "left": 62},
        "LW": {"label": "LW", "top": 20, "left": 18}, "ST": {"label": "ST", "top": 12, "left": 50},
        "RW": {"label": "RW", "top": 20, "left": 82},
    },
    "5-3-2": {
        "GK": {"label": "GK", "top": 92, "left": 50},
        "LWB": {"label": "LWB", "top": 68, "left": 10}, "CB1": {"label": "CB", "top": 78, "left": 30},
        "CB2": {"label": "CB", "top": 82, "left": 50}, "CB3": {"label": "CB", "top": 78, "left": 70},
        "RWB": {"label": "RWB", "top": 68, "left": 90},
        "CM1": {"label": "CM", "top": 48, "left": 30}, "CM2": {"label": "CM", "top": 46, "left": 50},
        "CM3": {"label": "CM", "top": 48, "left": 70},
        "ST1": {"label": "ST", "top": 15, "left": 38}, "ST2": {"label": "ST", "top": 15, "left": 62},
    },
    "5-4-1": {
        "GK": {"label": "GK", "top": 92, "left": 50},
        "LWB": {"label": "LWB", "top": 68, "left": 10}, "CB1": {"label": "CB", "top": 78, "left": 30},
        "CB2": {"label": "CB", "top": 82, "left": 50}, "CB3": {"label": "CB", "top": 78, "left": 70},
        "RWB": {"label": "RWB", "top": 68, "left": 90},
        "LM": {"label": "LM", "top": 45, "left": 12}, "CM1": {"label": "CM", "top": 42, "left": 38},
        "CM2": {"label": "CM", "top": 42, "left": 62}, "RM": {"label": "RM", "top": 45, "left": 88},
        "ST": {"label": "ST", "top": 12, "left": 50},
    },
}

# Substitutes bench -- fixed slots that sit alongside the pitch, independent
# of formation (a formation only defines the 11 starting positions). Stored
# in the same TacticsSlot table as pitch slots, keyed by formation +
# slot_key, so switching formations keeps its own bench too.
BENCH_SLOTS = {f"SUB{i}": {"label": f"SUB {i}"} for i in range(1, 8)}
