"""Who can do what on the site.

Two ladders, kept apart because they answer different questions:

  Club roles     Club President, Head Coach, Coach -- who runs the club.
                 Assigned on the site (Player.club_role), by management.
  Squad status   Starter, Rotation, Substitute -- what a player can expect
                 of their playing time. Part of their contract
                 (Contract.squad_status), not a permission.

Access levels, lowest first:

  GUEST       not signed in, or signed in but not in our Discord server.
              Sees the public splash page and nothing else.
  MEMBER      in our Discord server. Sees the whole site: the squad,
              everybody's stats, fixtures, tactics. Not coach notes.
  STAFF       any club role. Runs matchday: events, tactics, attendance,
              news, coach notes.
  MANAGEMENT  Club President or Head Coach. Also runs the squad: offers,
              contracts, releases, and who holds which club role.

Holding DISCORD_STAFF_ROLE_ID in Discord counts as management. That's how
the site worked before club roles existed, and it means the club can't
lock itself out: someone with the Discord role can always fix the roles
here.
"""
from __future__ import annotations

CLUB_PRESIDENT = "Club President"
HEAD_COACH = "Head Coach"
COACH = "Coach"
# Most senior first; also the order staff are listed in.
CLUB_ROLES = (CLUB_PRESIDENT, HEAD_COACH, COACH)
MANAGEMENT_ROLES = (CLUB_PRESIDENT, HEAD_COACH)

# Stored on the contract as the short value; the long form is how the
# club talks about it.
SQUAD_STATUSES = ("Starter", "Rotation", "Substitute")
SQUAD_STATUS_LABELS = {
    "Starter": "Starting Player",
    "Rotation": "Rotation Player",
    "Substitute": "Substitute Player",
}
# Values earlier versions stored, renamed in place at startup (see
# database._rename_legacy_values).
LEGACY_SQUAD_STATUSES = {"Reserve": "Substitute"}

GUEST, MEMBER, STAFF, MANAGEMENT = 0, 1, 2, 3


def access_level(*, signed_in: bool, is_member: bool, discord_staff: bool,
                 club_role: str | None) -> int:
    if not signed_in:
        return GUEST
    if discord_staff or club_role in MANAGEMENT_ROLES:
        return MANAGEMENT
    if club_role in CLUB_ROLES:
        return STAFF
    return MEMBER if is_member else GUEST


def status_label(status: str | None) -> str:
    return SQUAD_STATUS_LABELS.get(status or "", status or "")
