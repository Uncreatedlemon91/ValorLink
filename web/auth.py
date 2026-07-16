"""Authentication and permission gating for the officer-facing web UI.

Two ways to sign in:

* **Discord OAuth2** (production). The officer logs in with Discord; we read
  their roles in the configured guild and map them to a permission tier by
  comparing against the same admin/officer/recruiter role IDs the bot's
  `/config` command sets. Enabled when DISCORD_CLIENT_ID / _SECRET /
  DISCORD_OAUTH_REDIRECT are set in the environment.

* **Dev login** (local only). A simple "act as" form, gated behind
  WEB_DEV_LOGIN=1 so it can never be reached in a real deployment. Handy for
  developing the UI and for the test suite.

Permission tiers are hierarchical: admin > officer > recruiter > none. A
control shown to recruiters is also shown to officers and admins.
"""
from __future__ import annotations

import os
import re
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

import config
from tenancy.units import sessionmaker_for
from utils.settings import get_config
from web.tenant import get_tenant, resolve_tenant, tenant_by_slug_ctx

# --- Permission tiers ---------------------------------------------------- #
TIER_NONE = "none"
TIER_RECRUITER = "recruiter"
TIER_OFFICER = "officer"
TIER_ADMIN = "admin"
_ORDER = {TIER_NONE: 0, TIER_RECRUITER: 1, TIER_OFFICER: 2, TIER_ADMIN: 3}


def tier_at_least(user: dict | None, required: str) -> bool:
    if not user:
        return False
    return _ORDER.get(user.get("tier", TIER_NONE), 0) >= _ORDER[required]


def tier_from_role_ids(session, role_ids: set[int]) -> str:
    """Map a set of the user's Discord role IDs to a permission tier."""
    cfg = get_config(session)
    if cfg.admin_role_id and cfg.admin_role_id in role_ids:
        return TIER_ADMIN
    if cfg.officer_role_id and cfg.officer_role_id in role_ids:
        return TIER_OFFICER
    if cfg.recruiter_role_id and cfg.recruiter_role_id in role_ids:
        return TIER_RECRUITER
    return TIER_NONE


# --- Session helpers ----------------------------------------------------- #
def current_user(request: Request) -> dict | None:
    """The raw session user, regardless of which unit they signed into."""
    return request.session.get("user")


def effective_user(request: Request, tenant_slug: str | None = None) -> dict | None:
    """The signed-in user, but only when they're viewing the unit they signed
    into. Signing into one unit grants nothing on another."""
    user = request.session.get("user")
    if not user:
        return None
    if tenant_slug is None:
        ctx = getattr(request.state, "tenant", None)
        tenant_slug = ctx.slug if ctx else None
    user_tenant = user.get("tenant")
    if user_tenant is not None and tenant_slug is not None and user_tenant != tenant_slug:
        return None
    return user


class NotAuthenticated(Exception):
    """Raised when a route needs a signed-in user and there isn't one."""


class NotAuthorized(Exception):
    def __init__(self, required: str):
        self.required = required


def _require(required: str):
    def dep(request: Request, tenant=Depends(get_tenant)) -> dict:
        user = effective_user(request, tenant.slug)
        if not user:
            raise NotAuthenticated()
        if not tier_at_least(user, required):
            raise NotAuthorized(required)
        return user

    return dep


require_recruiter = _require(TIER_RECRUITER)
require_officer = _require(TIER_OFFICER)
require_admin = _require(TIER_ADMIN)


# --- CSRF ---------------------------------------------------------------- #
def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def verify_csrf(request: Request, token: str) -> bool:
    expected = request.session.get("csrf")
    return bool(expected) and secrets.compare_digest(expected, token or "")


# --- OAuth / dev-login config -------------------------------------------- #
DEV_LOGIN_ENABLED = os.getenv("WEB_DEV_LOGIN", "").lower() in ("1", "true", "yes")

OAUTH_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
OAUTH_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
OAUTH_REDIRECT = os.getenv("DISCORD_OAUTH_REDIRECT", "")
OAUTH_SCOPE = "identify guilds.members.read"
OAUTH_ENABLED = bool(OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET and OAUTH_REDIRECT)

_DISCORD_API = "https://discord.com/api"


router = APIRouter()


@router.get("/logout")
def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse("/", status_code=303)


# --- Dev login ----------------------------------------------------------- #
@router.post("/auth/dev")
def dev_login(
    request: Request,
    discord_id: int = Form(...),
    name: str = Form(...),
    tier: str = Form(...),
):
    if not DEV_LOGIN_ENABLED:
        return RedirectResponse("/login", status_code=303)
    if tier not in _ORDER:
        tier = TIER_NONE
    slug = resolve_tenant(request).slug
    request.session["user"] = {
        "id": discord_id, "name": name, "tier": tier, "via": "dev", "tenant": slug
    }
    return RedirectResponse("/", status_code=303)


# --- Discord OAuth2 ------------------------------------------------------ #
# One central callback (OAUTH_REDIRECT, registered once with Discord) serves
# every unit. The unit the user is signing into, and the URL to send them back
# to, ride along in the session (shared across subdomains via
# SESSION_COOKIE_DOMAIN) rather than in per-subdomain redirect URIs.
@router.get("/auth/discord/login")
def discord_login(request: Request):
    if not OAUTH_ENABLED:
        return RedirectResponse("/login", status_code=303)
    tenant = resolve_tenant(request)
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    request.session["oauth_tenant"] = tenant.slug
    request.session["oauth_origin"] = str(request.base_url).rstrip("/")
    params = {
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT,
        "response_type": "code",
        "scope": OAUTH_SCOPE,
        "state": state,
        "prompt": "consent",
    }
    from urllib.parse import urlencode

    return RedirectResponse(f"{_DISCORD_API}/oauth2/authorize?{urlencode(params)}", status_code=303)


def _display_name(nick: str | None, global_name: str | None, username: str | None) -> str:
    """The name to greet a signed-in user by. Prefers their Discord server
    nickname (their WoR name), stripping the bot's rank prefix (e.g. "Pvt. ")
    so it matches the callsign shown elsewhere; falls back to the Discord
    account display name, then the handle."""
    raw = (nick or "").strip() or (global_name or "").strip() or (username or "").strip() or "Officer"
    return re.sub(r"^\w+\.\s*", "", raw).strip() or raw


@router.get("/auth/discord/callback")
def discord_callback(request: Request, code: str = "", state: str = ""):
    if not OAUTH_ENABLED:
        return RedirectResponse("/login", status_code=303)
    if not code or not state or state != request.session.pop("oauth_state", None):
        return _login_error(request, "The sign-in response could not be verified. Please try again.")

    tenant_slug = request.session.pop("oauth_tenant", None)
    origin = request.session.pop("oauth_origin", None) or "/"
    tenant = tenant_by_slug_ctx(tenant_slug) if tenant_slug else None
    guild_id = tenant.guild_id if tenant else config.GUILD_ID

    import httpx

    token_data = {
        "client_id": OAUTH_CLIENT_ID,
        "client_secret": OAUTH_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": OAUTH_REDIRECT,
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

            role_ids: set[int] = set()
            guild_nick: str | None = None
            if guild_id:
                gm = client.get(
                    f"{_DISCORD_API}/users/@me/guilds/{guild_id}/member",
                    headers=bearer,
                )
                if gm.status_code == 200:
                    gm_data = gm.json()
                    role_ids = {int(r) for r in gm_data.get("roles", [])}
                    guild_nick = gm_data.get("nick")
    except Exception:
        return _login_error(request, "We couldn't reach Discord to sign you in. Please try again.")

    tier = TIER_NONE
    if tenant:
        with sessionmaker_for(tenant.db_url)() as session:
            tier = tier_from_role_ids(session, role_ids)

    # Show the name the regiment knows them by: their server nickname (their WoR
    # name) with the bot's rank prefix stripped, falling back to their Discord
    # account name.
    display = _display_name(guild_nick, me.get("global_name"), me.get("username"))
    request.session["user"] = {
        "id": int(me["id"]),
        "name": display,
        "tier": tier,
        "via": "discord",
        "tenant": tenant.slug if tenant else tenant_slug,
    }
    return RedirectResponse(origin, status_code=303)


def _login_error(request: Request, message: str):
    request.session["login_error"] = message
    return RedirectResponse("/login", status_code=303)
