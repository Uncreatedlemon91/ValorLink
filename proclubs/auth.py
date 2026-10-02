"""Discord OAuth2 sign-in and access levels.

Single-guild: this site belongs to one team's one Discord server. Signing
in records two facts from Discord in the session -- are you in our
server, and do you hold DISCORD_STAFF_ROLE_ID there -- and the club role
you hold on the site is read fresh on every request (see current_user),
so a promotion or demotion takes effect on the next page load rather
than at the next sign-in. roles.py has the levels and what each allows.

Only the splash page is public (see app._is_public); everything else
needs a signed-in member of the guild. This is a Discord app anyone can
authorize, so membership in *our* guild is what's actually checked.
"""
from __future__ import annotations

import secrets
import zlib

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

import config
import roles

_DISCORD_API = "https://discord.com/api"
OAUTH_SCOPE = "identify guilds.members.read"

router = APIRouter()


class NotAuthenticated(Exception):
    """Raised when a route needs a signed-in user and there isn't one."""


class NotStaff(Exception):
    """Raised when a route needs a club role and the viewer doesn't have one."""


class NotManagement(NotStaff):
    """Raised when a route needs Club President or Head Coach."""


class NotMember(Exception):
    """Raised when a route needs the viewer to belong to DISCORD_GUILD_ID
    (comments/likes) and they're signed in but not a member of it."""


_UNSET = object()


def _avatar_url(user: dict) -> str | None:
    if not user.get("avatar"):
        return None
    return f"https://cdn.discordapp.com/avatars/{user['id']}/{user['avatar']}.png?size=128"


def _resolve(request: Request) -> dict | None:
    """The signed-in user from the session, plus what the site itself
    says about them: their club role, and the access level that adds up
    to. Also makes sure every member has a player record, the first time
    they're seen."""
    session_user = request.session.get("user")
    if not session_user:
        return None
    # Imported here: auth is imported by everything, the database layer
    # shouldn't be a cost of importing it.
    import services
    from database import get_session

    discord_staff = bool(session_user.get("is_staff"))
    is_guild_member = bool(session_user.get("is_member")) or discord_staff
    with get_session() as session:
        player = services.get_player(session, session_user["id"])
        if player is None and is_guild_member:
            player = services.ensure_player(
                session, discord_id=str(session_user["id"]),
                display_name=session_user.get("name") or "Member",
                avatar_url=_avatar_url(session_user))
        club_role = player.club_role if player else None
    level = roles.access_level(signed_in=True, is_member=is_guild_member,
                               discord_staff=discord_staff, club_role=club_role)
    return {
        **session_user,
        "discord_staff": discord_staff,
        "club_role": club_role,
        "level": level,
        "is_member": level >= roles.MEMBER,
        "is_staff": level >= roles.STAFF,
        "is_management": level >= roles.MANAGEMENT,
    }


def current_user(request: Request) -> dict | None:
    """Resolved once per request and kept on request.state, which the
    login gate and the route share."""
    viewer = getattr(request.state, "viewer", _UNSET)
    if viewer is _UNSET:
        viewer = _resolve(request)
        request.state.viewer = viewer
    return viewer


def forget_viewer(request: Request) -> None:
    """Drop the per-request copy after the session's user changes."""
    if hasattr(request.state, "viewer"):
        del request.state.viewer


def is_staff(user: dict | None) -> bool:
    """Any club role: Club President, Head Coach or Coach."""
    return bool(user and user.get("is_staff"))


def is_management(user: dict | None) -> bool:
    """Club President or Head Coach (or the Discord staff role)."""
    return bool(user and user.get("is_management"))


def is_member(user: dict | None) -> bool:
    """Signed in and in our Discord server (or holding a club role)."""
    return bool(user and user.get("is_member"))


def require_signed_in(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise NotAuthenticated()
    return user


def require_staff(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise NotAuthenticated()
    if not is_staff(user):
        raise NotStaff()
    return user


def require_management(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise NotAuthenticated()
    if not is_management(user):
        raise NotManagement()
    return user


def require_member(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise NotAuthenticated()
    if not is_member(user):
        raise NotMember()
    return user


# --- CSRF ------------------------------------------------------------------ #
def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def verify_csrf(request: Request, token: str) -> bool:
    expected = request.session.get("csrf")
    return bool(expected) and secrets.compare_digest(expected, token or "")


# --- Where to go after signing in ----------------------------------------- #
def safe_path(target: str | None) -> str | None:
    """Only same-site paths -- "//evil.example" is a protocol-relative URL
    to somewhere else entirely."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


def remember_login_next(request: Request, target: str | None) -> None:
    path = safe_path(target)
    if path:
        request.session["login_next"] = path


def pop_login_next(request: Request) -> str:
    return safe_path(request.session.pop("login_next", None)) or "/"


# --- Dev login (local only) ------------------------------------------------ #
@router.post("/auth/dev")
def dev_login(request: Request, name: str = Form(...), staff: str = Form(""), member: str = Form("")):
    if not config.DEV_LOGIN_ENABLED:
        return RedirectResponse("/login", status_code=303)
    # A real Discord ID, distinct per display name (not just "0" for
    # everyone) -- needed so dev-testing comment/like ownership (which
    # compares author_discord_id to the signed-in user's id) behaves like
    # two different real accounts would.
    fake_id = zlib.crc32(name.encode())
    # Staff implies membership in real life too -- you can't hold a guild
    # role without being in the guild (see the OAuth callback, which checks
    # membership before it ever looks at roles).
    request.session["user"] = {
        "id": fake_id, "name": name, "avatar": None,
        "is_staff": bool(staff), "is_member": bool(member) or bool(staff),
    }
    forget_viewer(request)
    return RedirectResponse(pop_login_next(request), status_code=303)


# --- Discord OAuth2 ---------------------------------------------------------- #
@router.get("/auth/discord/login")
def discord_login(request: Request):
    if not config.OAUTH_ENABLED:
        return RedirectResponse("/login", status_code=303)
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    params = {
        "client_id": config.DISCORD_CLIENT_ID,
        "redirect_uri": config.DISCORD_OAUTH_REDIRECT,
        "response_type": "code",
        "scope": OAUTH_SCOPE,
        "state": state,
        "prompt": "consent",
    }
    from urllib.parse import urlencode

    return RedirectResponse(f"{_DISCORD_API}/oauth2/authorize?{urlencode(params)}", status_code=303)


@router.get("/auth/discord/callback")
def discord_callback(request: Request, code: str = "", state: str = ""):
    if not config.OAUTH_ENABLED:
        return RedirectResponse("/login", status_code=303)
    if not code or not state or state != request.session.pop("oauth_state", None):
        request.session["login_error"] = "The sign-in response could not be verified. Please try again."
        return RedirectResponse("/login", status_code=303)

    token_data = {
        "client_id": config.DISCORD_CLIENT_ID,
        "client_secret": config.DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.DISCORD_OAUTH_REDIRECT,
    }
    try:
        with httpx.Client(timeout=15) as client:
            tok = client.post(
                f"{_DISCORD_API}/oauth2/token",
                data=token_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            tok.raise_for_status()
            access_token = tok.json()["access_token"]
            bearer = {"Authorization": f"Bearer {access_token}"}

            me = client.get(f"{_DISCORD_API}/users/@me", headers=bearer)
            me.raise_for_status()
            me = me.json()

            # Their member object in the one configured guild -- also doubles
            # as the "is_member" check that gates comments/likes, not just
            # the staff-role check below. A non-200 here (not a member of
            # the guild) just means "not staff, not a member," not a failed
            # login -- everyone can still sign in and read the site.
            is_guild_member = False
            is_staff_role = False
            gm = client.get(
                f"{_DISCORD_API}/users/@me/guilds/{config.DISCORD_GUILD_ID}/member",
                headers=bearer,
            )
            if gm.status_code == 200:
                is_guild_member = True
                role_ids = {int(r) for r in gm.json().get("roles", [])}
                is_staff_role = config.DISCORD_STAFF_ROLE_ID in role_ids
    except Exception:
        request.session["login_error"] = "We couldn't reach Discord to sign you in. Please try again."
        return RedirectResponse("/login", status_code=303)

    request.session["user"] = {
        "id": int(me["id"]),
        "name": me.get("global_name") or me.get("username") or "Fan",
        "avatar": me.get("avatar"),
        "is_staff": is_staff_role,
        "is_member": is_guild_member,
    }
    forget_viewer(request)
    return RedirectResponse(pop_login_next(request), status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.pop("user", None)
    forget_viewer(request)
    return RedirectResponse("/", status_code=303)
