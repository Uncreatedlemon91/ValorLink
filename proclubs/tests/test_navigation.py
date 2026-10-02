"""The section navigation: what's grouped where, what each path belongs to,
and what each viewer sees.

Run with: pytest proclubs/tests/test_navigation.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import database  # noqa: E402
import navigation  # noqa: E402
import roles  # noqa: E402


@pytest.fixture
def client():
    database.Base.metadata.drop_all(database.engine)
    with TestClient(appmod.app) as c:
        yield c


def _login(client, *, staff):
    data = {"name": "Coach", "member": "1"}
    if staff:
        data["staff"] = "1"
    client.post("/auth/dev", data=data, follow_redirects=False)


# --- The registry ------------------------------------------------------------ #
def test_the_five_sections_group_what_they_should():
    tabs = {s.key: [t.href for t in s.tabs] for s in navigation.SECTIONS}
    assert tabs["matchday"] == ["/events", "/tactics"]
    assert tabs["club"] == ["/stats", "/league", "/club-profile"]
    assert tabs["media"] == ["/clips", "/streamers"]
    assert tabs["squad"] == ["/players", "/squad", "/roster"]


@pytest.mark.parametrize("path, section, tab", [
    ("/", "home", None),
    ("/news", "news", None),
    ("/news/some-story", "news", None),
    ("/events", "matchday", "/events"),
    ("/events/12", "matchday", "/events"),
    ("/events/new", "matchday", "/events"),
    ("/tactics", "matchday", "/tactics"),
    ("/stats", "club", "/stats"),
    ("/league", "club", "/league"),
    ("/clips", "media", "/clips"),
    ("/streamers", "media", "/streamers"),
    ("/players", "squad", "/players"),
    ("/players/42", "squad", "/players"),
    ("/squad", "squad", "/squad"),
    ("/roster", "squad", "/roster"),
])
def test_every_page_belongs_to_its_section_and_tab(path, section, tab):
    s, t = navigation.locate(path)
    assert s.key == section
    assert (t.href if t else None) == tab


@pytest.mark.parametrize("path", ["/login", "/newsletter", "/eventsx", "/squadron"])
def test_lookalike_paths_belong_to_no_section(path):
    """A prefix must match a whole path segment -- /newsletter isn't News."""
    assert navigation.locate(path) == (None, None)


def test_guests_see_only_home_in_the_sidebar():
    assert [s.key for s in navigation.visible(roles.GUEST)] == ["home"]


@pytest.mark.parametrize("level, tabs", [
    (roles.MEMBER, ["/players"]),
    (roles.STAFF, ["/players", "/squad"]),
    (roles.MANAGEMENT, ["/players", "/squad", "/roster"]),
])
def test_squad_tabs_follow_access_level(level, tabs):
    squad = next(s for s in navigation.SECTIONS if s.key == "squad")
    assert [t.href for t in squad.tabs_for(level)] == tabs


def test_every_tab_route_exists():
    """A tab pointing at a route that was renamed would be a dead link in
    every page's header."""
    routes = {getattr(r, "path", None) for r in appmod.app.routes}
    for s in navigation.SECTIONS:
        for href in [s.url] + [t.href for t in s.tabs]:
            assert href in routes, href


# --- Rendered -------------------------------------------------------------- #
def test_the_sidebar_marks_the_current_section(client):
    _login(client, staff=False)
    html = client.get("/league").text
    # The active link is Club's, and the Club tabs are shown with League current.
    assert 'href="/stats" class="side-link active"' in html
    assert 'href="/league" class="tab active" aria-current="page"' in html
    assert 'href="/stats" class="tab"' in html


def test_sections_without_tabs_show_no_tab_bar(client):
    _login(client, staff=False)
    assert 'class="tab-bar"' not in client.get("/news").text


@pytest.mark.parametrize("shortcut, target", [
    ("/matchday", "/events"), ("/club", "/stats"), ("/media", "/clips"),
])
def test_section_shortcuts_land_on_the_first_tab(client, shortcut, target):
    _login(client, staff=False)
    r = client.get(shortcut, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == target


def test_old_page_addresses_still_work(client):
    """Grouping pages into sections must not break a link anybody saved."""
    _login(client, staff=False)
    for path in ["/events", "/tactics", "/stats", "/league", "/clips", "/streamers", "/news"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 200, path


def test_squad_is_in_the_sidebar_for_members(client):
    """Every member sees the squad (its Players tab); guests don't."""
    assert 'href="/players" class="side-link' not in client.get("/login").text
    _login(client, staff=False)
    assert 'href="/players" class="side-link' in client.get("/").text
