"""Discord OAuth2 sign-in and staff-role gating.

Single-guild, unlike ValorLink's multi-tenant auth: this site belongs to one
team's one Discord server, so "is this person staff" is just "do they hold
the configured role in that one guild" -- no per-unit tier map to resolve.

Everyone (including signed-out visitors) can read the public site. Only
staff -- holders of DISCORD_STAFF_ROLE_ID in DISCORD_GUILD_ID -- can write
articles, manage events, or manage the streamer list.
"""
from __future__ import annotations

import secrets

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

import config

_DISCORD_API = "https://discord.com/api"
OAUTH_SCOPE = "identify guilds.members.read"

router = APIRouter()


class NotAuthenticated(Exception):
    """Raised when a route needs a signed-in user and there isn't one."""


class NotStaff(Exception):
    """Raised when a route needs staff standing and the viewer doesn't have it."""


def current_user(request: Request) -> dict | None:
    return request.session.get("user")


def is_staff(user: dict | None) -> bool:
    return bool(user and user.get("is_staff"))


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


# --- Dev login (local only) ------------------------------------------------ #
@router.post("/auth/dev")
def dev_login(request: Request, name: str = Form(...), staff: str = Form("")):
    if not config.DEV_LOGIN_ENABLED:
        return RedirectResponse("/login", status_code=303)
    request.session["user"] = {
        "id": 0, "name": name, "avatar": None, "is_staff": bool(staff),
    }
    return RedirectResponse("/", status_code=303)


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

            # Their member object in the one configured guild, to check roles.
            # A non-200 here (not a member of the guild) just means "not staff",
            # not a failed login -- everyone can sign in and read the site.
            is_staff_role = False
            gm = client.get(
                f"{_DISCORD_API}/users/@me/guilds/{config.DISCORD_GUILD_ID}/member",
                headers=bearer,
            )
            if gm.status_code == 200:
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
    }
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse("/", status_code=303)
