"""Authentication and permission gating for the officer-facing web UI.

Two ways to sign in:

* **Discord OAuth2** (production). The officer logs in with Discord once, and
  the single grant is used to read their member roles in *every* platform unit
  they belong to — mapping each to a permission tier by comparing against the
  admin/officer/recruiter role IDs that unit's `/config` sets. The result is a
  ``{slug: tier}`` map on the session, so one sign-in works across the whole
  platform (e.g. browse the directory, then administer your own unit without a
  second login). Enabled when DISCORD_CLIENT_ID / _SECRET /
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

from tenancy.registry import registry_session
from tenancy.resolve import all_tenants
from tenancy.units import sessionmaker_for
from utils.settings import get_config
from web.tenant import get_tenant, resolve_tenant

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
    """The signed-in user as seen by the unit currently being viewed, with
    ``tier`` set to their permission tier *on that unit*.

    A single Discord sign-in resolves the user's tier for every unit they
    belong to (stored as a ``tiers`` map). Viewing a unit they have no standing
    in returns ``None`` — they're a visitor there — so signing in still grants
    nothing on units where they hold no role.
    """
    user = request.session.get("user")
    if not user:
        return None
    if tenant_slug is None:
        ctx = getattr(request.state, "tenant", None)
        tenant_slug = ctx.slug if ctx else None

    tiers = user.get("tiers")
    if tiers is None:
        # Legacy session from before platform-wide sign-in: a single-unit
        # binding. Honor it only on the unit that was signed into.
        if user.get("tenant") == tenant_slug:
            return user
        return None
    if tenant_slug not in tiers:
        return None
    return {**user, "tier": tiers[tenant_slug]}


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
# `guilds` lets us list which units the user is in; `guilds.members.read` lets
# us read their roles in each — together, one sign-in resolves their tier across
# every unit they belong to.
OAUTH_SCOPE = "identify guilds guilds.members.read"
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
    # Dev login is a per-unit "act as": grant the chosen tier on this unit only,
    # mirroring production's per-unit tier map.
    request.session["user"] = {
        "id": discord_id, "name": name, "via": "dev",
        "tenant": slug, "tiers": {slug: tier},
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


def _resolve_membership(client, bearer: dict, me: dict, signin_slug: str | None):
    """Resolve the user's permission tier on every platform unit they belong to.

    One OAuth grant (scopes ``guilds`` + ``guilds.members.read``) can read the
    user's member object in any guild they're in, so we look up which units
    they're a member of and compute a tier for each in a single sign-in.

    Returns ``(tiers, signin_nick)`` where ``tiers`` maps unit slug → tier and
    ``signin_nick`` is their server nickname in the unit they signed in from
    (used only for the greeting name).
    """
    from db.models import Member

    # Which guilds is the user in? (Best-effort — if this call is throttled we
    # still probe the unit they signed in from below.)
    guild_ids: set[int] = set()
    gr = client.get(f"{_DISCORD_API}/users/@me/guilds", headers=bearer)
    if gr.status_code == 200:
        guild_ids = {int(g["id"]) for g in gr.json() if g.get("id")}

    # Snapshot units before opening any per-unit session.
    with registry_session() as reg:
        units = [
            (t.slug, int(t.discord_guild_id), t.db_url)
            for t in all_tenants(reg)
            if t.discord_guild_id
        ]

    tiers: dict[str, str] = {}
    signin_nick: str | None = None
    uid = int(me["id"])
    avatar = me.get("avatar")
    for slug, gid, db_url in units:
        # Probe a unit if the user is in its guild, and always probe the unit
        # they signed in from so that path never regresses if the guilds list
        # was unavailable.
        if gid not in guild_ids and slug != signin_slug:
            continue
        gm = client.get(f"{_DISCORD_API}/users/@me/guilds/{gid}/member", headers=bearer)
        if gm.status_code != 200:
            continue
        gm_data = gm.json()
        role_ids = {int(r) for r in gm_data.get("roles", [])}
        with sessionmaker_for(db_url)() as session:
            tiers[slug] = tier_from_role_ids(session, role_ids)
            # Keep the member's avatar fresh from their Discord profile.
            record = session.get(Member, uid)
            if record is not None and record.avatar != avatar:
                record.avatar = avatar
                session.commit()
        if slug == signin_slug:
            signin_nick = gm_data.get("nick")
    return tiers, signin_nick


@router.get("/auth/discord/callback")
def discord_callback(request: Request, code: str = "", state: str = ""):
    if not OAUTH_ENABLED:
        return RedirectResponse("/login", status_code=303)
    if not code or not state or state != request.session.pop("oauth_state", None):
        return _login_error(request, "The sign-in response could not be verified. Please try again.")

    tenant_slug = request.session.pop("oauth_tenant", None)
    origin = request.session.pop("oauth_origin", None) or "/"

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

            # One grant → the user's tier on every unit they belong to.
            tiers, signin_nick = _resolve_membership(client, bearer, me, tenant_slug)
    except Exception:
        return _login_error(request, "We couldn't reach Discord to sign you in. Please try again.")

    # Show the name the regiment knows them by: their server nickname (their WoR
    # name) with the bot's rank prefix stripped, falling back to their Discord
    # account name.
    display = _display_name(signin_nick, me.get("global_name"), me.get("username"))
    request.session["user"] = {
        "id": int(me["id"]),
        "name": display,
        "via": "discord",
        "tenant": tenant_slug,
        "tiers": tiers,
    }
    return RedirectResponse(origin, status_code=303)


def _login_error(request: Request, message: str):
    request.session["login_error"] = message
    return RedirectResponse("/login", status_code=303)
