"""The site's sections and their tabs -- one list that the sidebar, each
section's tab bar, the section shortcut URLs and the tests all read.

Grouped by what people come to do rather than by data source:

  Home
  News
  Matchday   Fixtures & sign-ups · Availability · Tactics   -- getting ready for a match
  Squad      Players · Overview · Moves & contracts   -- the people
  Club       Stats · League table · Club profile      -- how the club is doing
  Media      Clips · Live                       -- watching

Each tab says the access level it needs (roles.py): every member sees the
players and their stats, staff see the squad overview, management see
contracts and the club profile. A section shows only the tabs its viewer
can open, and disappears when that's none of them.

Every tab keeps its own URL (/events, /tactics, /stats, ...). The section
is one place in the navigation, with the same header and tab bar on every
tab, but each tab is still a real address: links people have shared keep
working, the back button behaves, and a page only loads the scripts it
needs (the tactics board and the stats dashboard are both script-heavy and
have nothing to share). /matchday, /club and /media are shortcuts to a
section's first tab.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import roles


@dataclass(frozen=True)
class Tab:
    label: str
    href: str
    # Paths that count as this tab, so /events/12 lights up "Fixtures".
    prefixes: tuple[str, ...]
    level: int = roles.MEMBER


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    icon: str
    tabs: tuple[Tab, ...] = field(default_factory=tuple)
    # For sections without tabs.
    href: str = ""
    prefixes: tuple[str, ...] = ()
    # For sections without tabs; a section with tabs goes by theirs.
    level: int = roles.MEMBER
    # A short URL for the section itself, redirecting to its first tab.
    shortcut: str = ""

    @property
    def url(self) -> str:
        return self.tabs[0].href if self.tabs else self.href

    def tabs_for(self, level: int) -> tuple[Tab, ...]:
        return tuple(t for t in self.tabs if level >= t.level)

    def url_for(self, level: int) -> str:
        tabs = self.tabs_for(level)
        return tabs[0].href if tabs else self.href

    def all_prefixes(self) -> tuple[str, ...]:
        return self.prefixes + tuple(p for t in self.tabs for p in t.prefixes)


SECTIONS: tuple[Section, ...] = (
    Section("home", "Home", "home", href="/", level=roles.GUEST),
    Section("news", "News", "news", href="/news", prefixes=("/news",)),
    Section("matchday", "Matchday", "matchday", shortcut="/matchday", tabs=(
        Tab("Fixtures & sign-ups", "/events", ("/events",)),
        Tab("Availability", "/availability", ("/availability",)),
        Tab("Tactics", "/tactics", ("/tactics",)),
    )),
    Section("squad", "Squad", "squad", tabs=(
        Tab("Players", "/players", ("/players",)),
        Tab("Overview", "/squad", ("/squad",), level=roles.STAFF),
        Tab("Moves & contracts", "/roster", ("/roster",), level=roles.MANAGEMENT),
    )),
    Section("club", "Club", "club", shortcut="/club", tabs=(
        Tab("Stats", "/stats", ("/stats",)),
        Tab("League table", "/league", ("/league",)),
        Tab("Club profile", "/club-profile", ("/club-profile",), level=roles.MANAGEMENT),
    )),
    Section("media", "Media", "media", shortcut="/media", tabs=(
        Tab("Clips", "/clips", ("/clips",)),
        Tab("Live", "/streamers", ("/streamers",)),
    )),
)


def _matches(path: str, prefix: str) -> bool:
    """A prefix matches itself or a sub-path -- "/news" covers "/news/x"
    but not "/newsletter". "/" only ever matches the home page exactly."""
    if prefix == "/":
        return path == "/"
    return path == prefix or path.startswith(prefix + "/")


def locate(path: str) -> tuple[Section | None, Tab | None]:
    """The section and tab a path belongs to, or (None, None) for pages
    outside every section (sign-in, errors)."""
    for section in SECTIONS:
        for tab in section.tabs:
            if any(_matches(path, p) for p in tab.prefixes):
                return section, tab
        if section.href and any(_matches(path, p) for p in (section.href, *section.prefixes)):
            return section, None
    return None, None


def visible(level: int) -> tuple[Section, ...]:
    """The sections this viewer sees in the sidebar: those with at least
    one tab they can open. The routes are gated regardless; this only
    stops a dead link showing up."""
    return tuple(s for s in SECTIONS
                 if (s.tabs_for(level) if s.tabs else level >= s.level))
