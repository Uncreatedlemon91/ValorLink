"""Tests for the platform-wide social feed (posts, comments, likes).

The feed is registry-backed (cross-unit by design), so these tests don't need
a unit database at all -- just the registry schema and dev-login sessions.

Runs under pytest or directly:  python -m web.tests.test_feed
"""
import os
import re
import tempfile

_TMP = tempfile.mkdtemp(prefix="valorlink-feed-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["REGISTRY_DATABASE_URL"] = f"sqlite:///{_TMP}/registry.db"
os.environ.pop("PLATFORM_BASE_DOMAIN", None)   # single-tenant: everything resolves to default
os.environ["WEB_DEV_LOGIN"] = "1"
os.environ["WEB_SESSION_SECRET"] = "test-secret"

import config  # noqa: E402
config.DATABASE_URL = os.environ["DATABASE_URL"]

from fastapi.testclient import TestClient  # noqa: E402

from tenancy.registry import (  # noqa: E402
    Post,
    PostComment,
    PostLike,
    RegistryBase,
    registry_engine,
    registry_session,
)
from web.app import app  # noqa: E402

MEMBER_A = 101
MEMBER_B = 202

# create_all is idempotent and never touches existing rows/tables, unlike
# drop_all -- safe to run up front so the schema exists before the first test.
RegistryBase.metadata.create_all(registry_engine)


def _reset():
    """Clear feed data before each test. The registry schema (and the default
    tenant row it holds) is left alone: `web.tenant.ensure_ready()` caches its
    own readiness for the life of the process, so dropping the tenant row out
    from under it would strand dev-login without recreating it."""
    with registry_session() as s:
        s.query(PostComment).delete()
        s.query(PostLike).delete()
        s.query(Post).delete()
        s.commit()


def _login(discord_id, name):
    c = TestClient(app)
    # Dev login redirects to "/" (the unit Headquarters page) on success; skip
    # following it since these tests don't provision a unit database and only
    # need the session cookie it sets.
    c.post("/auth/dev", data={"discord_id": discord_id, "name": name, "tier": "none"},
           follow_redirects=False)
    return c


def _csrf(client):
    html = client.get("/feed").text
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no CSRF token on /feed"
    return m.group(1)


def test_feed_page_loads_signed_out():
    _reset()
    c = TestClient(app)
    html = c.get("/feed").text
    assert "No posts yet" in html
    assert "Sign in with Discord" in html


def test_post_requires_signin():
    _reset()
    c = TestClient(app)
    # Auth is checked before CSRF, so a placeholder token is enough here.
    r = c.post("/feed", data={"csrf": "x", "body": "hello"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_signed_in_user_can_post_and_see_it():
    _reset()
    a = _login(MEMBER_A, "Alice")
    token = _csrf(a)
    r = a.post("/feed", data={"csrf": token, "body": "First post on the platform!"},
               follow_redirects=False)
    assert r.status_code == 303
    html = a.get("/feed").text
    assert "First post on the platform!" in html
    assert "Alice" in html


def test_empty_post_without_image_is_rejected():
    _reset()
    a = _login(MEMBER_A, "Alice")
    token = _csrf(a)
    a.post("/feed", data={"csrf": token, "body": "   "}, follow_redirects=False)
    html = a.get("/feed").text
    assert "No posts yet" in html
    assert "Say something" in html


def test_comment_and_like_flow():
    _reset()
    a = _login(MEMBER_A, "Alice")
    a.post("/feed", data={"csrf": _csrf(a), "body": "Line battle tonight!"}, follow_redirects=False)
    post_id = re.search(r'id="post-(\d+)"', a.get("/feed").text).group(1)

    b = _login(MEMBER_B, "Bob")
    b.post(f"/feed/{post_id}/comment", data={"csrf": _csrf(b), "body": "Count me in."},
           follow_redirects=False)
    b.post(f"/feed/{post_id}/like", data={"csrf": _csrf(b)}, follow_redirects=False)

    html = b.get("/feed").text
    assert "Count me in." in html
    assert 'class="like-btn liked"' in html

    # liking again un-likes
    b.post(f"/feed/{post_id}/like", data={"csrf": _csrf(b)}, follow_redirects=False)
    html = b.get("/feed").text
    assert 'class="like-btn liked"' not in html


def test_only_the_author_can_delete_a_post():
    _reset()
    a = _login(MEMBER_A, "Alice")
    a.post("/feed", data={"csrf": _csrf(a), "body": "Mine to keep or delete."}, follow_redirects=False)
    post_id = re.search(r'id="post-(\d+)"', a.get("/feed").text).group(1)

    b = _login(MEMBER_B, "Bob")
    b.post(f"/feed/{post_id}/delete", data={"csrf": _csrf(b)}, follow_redirects=False)
    assert "Mine to keep or delete." in b.get("/feed").text

    a.post(f"/feed/{post_id}/delete", data={"csrf": _csrf(a)}, follow_redirects=False)
    assert "Mine to keep or delete." not in a.get("/feed").text


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
