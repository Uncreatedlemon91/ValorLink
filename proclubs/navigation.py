"""The site's sections and their tabs -- one list that the sidebar, each
section's tab bar, the section shortcut URLs and the tests all read.

Grouped by what people come to do rather than by data source:

  Home
  News
  Matchday   Fixtures & sign-ups · Tactics      -- getting ready for a match
  Club       Stats · League table               -- how the club is doing
  Media      Clips · Live                       -- watching
  Squad      Overview · Moves & contracts       -- running the club (staff)

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


@dataclass(frozen=True)
class Tab:
    label: str
    href: str
    # Paths that count as this tab, so /events/12 lights up "Fixtures".
    prefixes: tuple[str, ...]


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    icon: str
    tabs: tuple[Tab, ...] = field(default_factory=tuple)
    # For sections without tabs.
    href: str = ""
    prefixes: tuple[str, ...] = ()
    staff_only: bool = False
    # A short URL for the section itself, redirecting to its first tab.
    shortcut: str = ""

    @property
    def url(self) -> str:
        return self.tabs[0].href if self.tabs else self.href

    def all_prefixes(self) -> tuple[str, ...]:
        return self.prefixes + tuple(p for t in self.tabs for p in t.prefixes)


SECTIONS: tuple[Section, ...] = (
    Section("home", "Home", "home", href="/"),
    Section("news", "News", "news", href="/news", prefixes=("/news",)),
    Section("matchday", "Matchday", "matchday", shortcut="/matchday", tabs=(
        Tab("Fixtures & sign-ups", "/events", ("/events",)),
        Tab("Tactics", "/tactics", ("/tactics",)),
    )),
    Section("club", "Club", "club", shortcut="/club", tabs=(
        Tab("Stats", "/stats", ("/stats",)),
        Tab("League table", "/league", ("/league",)),
    )),
    Section("media", "Media", "media", shortcut="/media", tabs=(
        Tab("Clips", "/clips", ("/clips",)),
        Tab("Live", "/streamers", ("/streamers",)),
    )),
    Section("squad", "Squad", "squad", staff_only=True, tabs=(
        Tab("Overview", "/squad", ("/squad",)),
        Tab("Moves & contracts", "/roster", ("/roster",)),
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


def visible(is_staff: bool) -> tuple[Section, ...]:
    """The sections this viewer sees in the sidebar. Staff-only sections
    are hidden from everybody else -- the routes are gated regardless;
    this only stops a dead link showing up."""
    return tuple(s for s in SECTIONS if is_staff or not s.staff_only)
