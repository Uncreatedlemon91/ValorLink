"""Character of service — the vocabulary shared by the ``/discharge`` slash
command and the web discharge form.

Lives in utils/ rather than web/services.py because the bot needs it too, and
the bot must not import the web layer. Both paths write the same wording, so a
service record reads identically however the discharge was ordered.
"""
from __future__ import annotations

# Best to worst. "honorable"/"dishonorable" are the two values this started
# with and keep their exact spelling, so records written before the middle
# grades existed still resolve.
DISCHARGE_TYPES = [
    ("honorable", "Honorable"),
    ("general", "General (Under Honorable Conditions)"),
    ("other_than_honorable", "Other Than Honorable"),
    ("bad_conduct", "Bad Conduct"),
    ("dishonorable", "Dishonorable"),
]

DISCHARGE_LABELS = {key: label for key, label in DISCHARGE_TYPES}

# Characterisations that count against a member when a recruiter vets them.
ADVERSE_DISCHARGES = {"other_than_honorable", "bad_conduct", "dishonorable"}

# Which read as a clean separation, for embed colouring and badge styling.
FAVOURABLE_DISCHARGES = {"honorable", "general"}


def discharge_label(discharge_type: str | None) -> str | None:
    """The human-readable character of service, or None if never discharged.
    An unrecognised value is titled rather than dropped, so a hand-edited or
    future value still renders as something."""
    if not discharge_type:
        return None
    return DISCHARGE_LABELS.get(discharge_type, discharge_type.replace("_", " ").title())
