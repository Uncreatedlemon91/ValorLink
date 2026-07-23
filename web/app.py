"""ValorLink Regimental Headquarters — a period-styled web UI over the same
database the Discord bot uses.

This is a read-only companion to the bot. It opens the very same SQLite
database (``DATABASE_URL``, default ``sqlite:///valorlink.db``) through the
bot's own SQLAlchemy models, so whatever the regiment does in Discord is
reflected here with no extra wiring. Nothing here writes to the database.

Run it with::

    uvicorn web.app:app --reload

then visit http://127.0.0.1:8000.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from db.models import (
    AttendanceRecord,
    AwardType,
    Candidacy,
    Company,
    Event,
    Member,
    MemberAward,
    Rank,
    ServiceHistoryEntry,
)
from tenancy import alliance_events
from tenancy import alliances as alliance_mod
from tenancy.registry import registry_session
from tenancy.resolve import all_tenants, listed_tenants, slug_from_host, tenant_by_slug
from tenancy.units import sessionmaker_for
from utils import queue
from utils import ranks as rank_utils
from utils import terminology
from utils.settings import (
    CHANNEL_KEYS,
    ROLE_KEYS,
    default_company_name,
    get_config,
    list_companies,
)
from web import auth, profiles, services
from web.tenant import (
    TenantCtx,
    TenantNotFound,
    ensure_ready,
    get_tenant,
    resolve_tenant,
    tenant_by_slug_ctx,
)

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="ValorLink")

_session_cookie_kwargs = {}
_cookie_domain = os.getenv("SESSION_COOKIE_DOMAIN")
if _cookie_domain:
    # Share the login cookie across all unit subdomains (e.g. ".valorlink.co").
    _session_cookie_kwargs["domain"] = _cookie_domain

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("WEB_SESSION_SECRET", "valorlink-dev-secret-change-me"),
    same_site="lax",
    https_only=os.getenv("WEB_HTTPS_ONLY", "").lower() in ("1", "true", "yes"),
    **_session_cookie_kwargs,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.include_router(auth.router)


@app.on_event("startup")
def _startup():
    # Create the registry and represent this deployment as the default unit.
    ensure_ready()

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _asset_version() -> str:
    """A cache-busting token for the stylesheet, derived from its modification
    time. It changes whenever the file is rewritten (e.g. on a git pull), so
    browsers fetch fresh CSS after a deploy instead of reusing a stale copy."""
    try:
        return str(int((BASE_DIR / "static" / "css" / "valorlink.css").stat().st_mtime))
    except OSError:
        return "1"


templates.env.globals["css_v"] = _asset_version()
templates.env.globals["DARK_THEMES"] = terminology.DARK_THEMES
templates.env.globals["tier_at_least"] = auth.tier_at_least
templates.env.globals["TIER_RECRUITER"] = auth.TIER_RECRUITER
templates.env.globals["TIER_OFFICER"] = auth.TIER_OFFICER
templates.env.globals["TIER_ADMIN"] = auth.TIER_ADMIN


@app.exception_handler(auth.NotAuthenticated)
def _on_unauthenticated(request: Request, exc: auth.NotAuthenticated):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(auth.NotAuthorized)
def _on_unauthorized(request: Request, exc: auth.NotAuthorized):
    tenant = resolve_tenant(request)
    with sessionmaker_for(tenant.db_url)() as session:
        ctx = _base_context(request, session)
    ctx["message"] = (
        f"That action needs the {exc.required} rank or higher. "
        "You're signed in, but without the standing for it."
    )
    return templates.TemplateResponse(request, "not_found.html", ctx, status_code=403)


@app.exception_handler(TenantNotFound)
def _on_tenant_not_found(request: Request, exc: TenantNotFound):
    return templates.TemplateResponse(
        request,
        "unit_not_found.html",
        {"request": request, "slug": exc.slug, "base_domain": os.getenv("PLATFORM_BASE_DOMAIN")},
        status_code=404,
    )


def _flash(request: Request, text: str, level: str = "ok"):
    request.session.setdefault("flash", []).append({"level": level, "text": text})


# --------------------------------------------------------------------------- #
# Presentation helpers
# --------------------------------------------------------------------------- #

STATUS_LABELS = {
    "active": "Present for Duty",
    "loa": "On Furlough",
    "inactive": "Absent",
    "discharged": "Discharged",
}

RECORD_LABELS = {"note": "Note", "warn": "Reprimand", "strike": "Strike"}

ATTENDANCE_LABELS = {
    "accepted": "Answered the Call",
    "tentative": "Uncertain",
    "declined": "Sends Regrets",
    "present": "Mustered",
    "absent": "Absent",
    "excused": "Excused",
    "pending": "Awaiting Reply",
}

EVENT_TYPE_LABELS = {"Drill": "Drill", "Battle": "Battle", "Operation": "Operation"}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status.title() if status else "Unknown")


def record_label(kind: str) -> str:
    return RECORD_LABELS.get(kind, kind.title() if kind else "Record")


def attendance_label(status: str) -> str:
    return ATTENDANCE_LABELS.get(status, status.title() if status else "—")


def brand_hex(color: int | None) -> str:
    """The regiment's stored brand colour (an int) as a CSS hex string."""
    if not color:
        return "#7c1f2b"
    return f"#{color & 0xFFFFFF:06x}"


def fmt_date(value: datetime | None, with_time: bool = False):
    """Render a stored (naive UTC) datetime as a <time> element that the
    browser localizes to the viewer's own timezone. Falls back to a UTC
    string if JavaScript is off."""
    from markupsafe import Markup, escape

    if not value:
        return "—"
    iso = value.strftime("%Y-%m-%dT%H:%M:%SZ")
    fallback = value.strftime("%d %b %Y" + (" · %H:%M" if with_time else ""))
    kind = "datetime" if with_time else "date"
    return Markup(
        f'<time datetime="{iso}" data-local data-fmt="{kind}">{escape(fallback)}</time>'
    )


def avatar_url(member, size: int = 64) -> str:
    """Discord CDN avatar URL for a member (or a (discord_id, hash) pair),
    falling back to Discord's default avatar when we don't have a hash yet."""
    if member is None:
        return ""
    if isinstance(member, (tuple, list)):
        discord_id, avatar = member[0], member[1]
    else:
        discord_id, avatar = member.discord_id, getattr(member, "avatar", None)
    if avatar:
        ext = "gif" if str(avatar).startswith("a_") else "png"
        return f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar}.{ext}?size={size}"
    default = (int(discord_id) >> 22) % 6
    return f"https://cdn.discordapp.com/embed/avatars/{default}.png"


templates.env.filters["status_label"] = status_label
templates.env.filters["record_label"] = record_label
templates.env.filters["attendance_label"] = attendance_label
templates.env.filters["fmt_date"] = fmt_date
templates.env.filters["avatar_url"] = avatar_url


def get_session(tenant: TenantCtx = Depends(get_tenant)):
    """A database session scoped to the unit resolved for this request."""
    session = sessionmaker_for(tenant.db_url)()
    try:
        yield session
    finally:
        session.close()


def _my_units(request: Request, current_slug: str) -> list[dict]:
    """The units the signed-in user belongs to, for the header switcher. One
    Discord sign-in resolves a tier on every unit they're in (auth.tiers), so we
    can offer a one-click hop between them. Empty unless platform mode is on and
    they belong to more than one unit."""
    base_domain = os.getenv("PLATFORM_BASE_DOMAIN")
    if not base_domain:
        return []
    user = auth.current_user(request)
    tiers = (user or {}).get("tiers") or {}
    if len(tiers) < 2:
        return []
    with registry_session() as rs:
        names = {t.slug: t.name for t in all_tenants(rs) if t.slug in tiers}
    units = [
        {"slug": slug, "name": names[slug],
         "url": f"https://{slug}.{base_domain}/",
         "tier": tiers[slug], "current": slug == current_slug}
        for slug in tiers if slug in names
    ]
    units.sort(key=lambda u: u["name"].lower())
    return units


def _base_context(request: Request, session: Session) -> dict:
    """Context every page needs: regiment identity for the banner + nav,
    plus the signed-in officer, a CSRF token, and any flashed messages."""
    cfg = get_config(session)
    flash = request.session.pop("flash", [])
    pending_recruits = session.query(Candidacy).count()
    tenant = resolve_tenant(request)
    return {
        "my_units": _my_units(request, tenant.slug),
        "request": request,
        "regiment_name": cfg.regiment_name,
        "regiment_motto": cfg.regiment_motto,
        "brand_color": brand_hex(cfg.brand_color),
        "now": datetime.utcnow(),
        # Honor the signed-in user only on the unit they signed into.
        "user": auth.effective_user(request, tenant.slug),
        "csrf_token": auth.get_csrf_token(request),
        "flash": flash,
        "pending_recruits": pending_recruits,
        "tenant": tenant,
        "platform_base": os.getenv("PLATFORM_BASE_DOMAIN"),
        # Per-unit vocabulary (preset + any custom overrides); `terms.<key>`.
        "terms": terminology.resolve_terms(cfg.terminology_custom),
        "theme": cfg.theme or terminology.DEFAULT_THEME,
        "crest": cfg.crest,
    }


def directory_mode(request: Request) -> bool:
    """True when this request should show the public directory: platform mode
    is on and the Host is the apex (not a unit subdomain)."""
    if not os.getenv("PLATFORM_BASE_DOMAIN"):
        return False
    return slug_from_host(request.headers.get("host", "")) is None


def _unit_directory_info(db_url: str) -> dict:
    """Public directory facts read from a unit's own database: active member
    count and Discord invite (if any). Empty dict if the DB is unavailable."""
    try:
        with sessionmaker_for(db_url)() as s:
            cfg = get_config(s)
            count = s.query(Member).filter(Member.status == "active").count()
            return {"members": count, "invite": cfg.discord_invite, "crest": cfg.crest}
    except Exception:
        return {}


def _platform_activity(limit: int = 18):
    """Recent public activity across every listed unit: new units, enlistments,
    and honors. Aggregated from each unit's own database; unlisted units (which
    opted out of the directory) and any unreadable database are skipped."""
    base_domain = os.getenv("PLATFORM_BASE_DOMAIN")
    cutoff = datetime.utcnow() - timedelta(days=45)
    items = []
    with registry_session() as rs:
        units = [(t.slug, t.name, t.db_url, t.created_at) for t in listed_tenants(rs)]

    for slug, name, db_url, created_at in units:
        unit_url = f"https://{slug}.{base_domain}/" if base_domain else "/"
        if created_at and created_at >= cutoff:
            items.append({"when": created_at, "kind": "unit", "unit": name,
                          "url": unit_url, "who": None, "text": "joined ValorLink"})
        try:
            with sessionmaker_for(db_url)() as s:
                for m in (
                    s.query(Member)
                    .filter(Member.joined_date >= cutoff)
                    .order_by(Member.joined_date.desc())
                    .limit(8)
                ):
                    items.append({"when": m.joined_date, "kind": "enlist", "unit": name,
                                  "url": unit_url, "who": m.callsign, "text": "enlisted",
                                  "av": (m.discord_id, m.avatar)})
                for a in (
                    s.query(MemberAward)
                    .filter(MemberAward.date_awarded >= cutoff)
                    .order_by(MemberAward.date_awarded.desc())
                    .limit(8)
                ):
                    member = s.get(Member, a.member_id)
                    award = a.award_type
                    items.append({
                        "when": a.date_awarded, "kind": "award", "unit": name, "url": unit_url,
                        "who": member.callsign if member else "A member",
                        "text": f"earned {award.name}" if award else "earned an honor",
                        "av": (member.discord_id, member.avatar) if member else None,
                    })
        except Exception:
            continue

    items.sort(key=lambda x: x["when"] or datetime.min, reverse=True)
    return items[:limit]


def _directory_data():
    """Shared listing data for the home landing and the Find-a-Unit page:
    the listed units (recruiting-first, larger first), grouped into per-game
    sections, plus the set of games for filter chips."""
    base_domain = os.getenv("PLATFORM_BASE_DOMAIN")
    with registry_session() as rs:
        rows = listed_tenants(rs)
        units = []
        for t in rows:
            info = _unit_directory_info(t.db_url)
            units.append({
                "slug": t.slug,
                "name": t.name,
                "motto": t.motto,
                "blurb": t.blurb,
                "brand_color": brand_hex(t.brand_color),
                "recruiting": t.recruiting_open,
                "members": info.get("members"),
                "invite": info.get("invite"),
                "crest": info.get("crest"),
                "game": (t.game or "").strip() or None,
                "tags": [s.strip() for s in (t.tags or "").split(",") if s.strip()],
                "url": f"https://{t.slug}.{base_domain}/",
                "join_url": f"https://{t.slug}.{base_domain}/join",
                "apply_url": f"https://{t.slug}.{base_domain}/apply",
            })

    amap = alliance_mod.alliances_map([u["slug"] for u in units])
    for u in units:
        u["alliances"] = amap.get(u["slug"], [])

    units.sort(key=lambda u: (not u["recruiting"], -(u["members"] or 0), u["name"].lower()))

    OTHER = "Other"
    by_game: dict[str, list] = defaultdict(list)
    for u in units:
        by_game[u["game"] or OTHER].append(u)
    section_names = sorted(by_game, key=lambda g: (g == OTHER, -len(by_game[g]), g.lower()))
    sections = [{"game": g, "units": by_game[g]} for g in section_names]
    games = sorted((g for g in by_game if g != OTHER), key=str.lower)
    return {"units": units, "sections": sections, "games": games,
            "total_units": len(units), "base_domain": base_domain}


def _user_units(request: Request):
    """The units the signed-in user belongs to (from their per-unit tier map),
    with the public facts needed to render a card and a link to each portal."""
    base_domain = os.getenv("PLATFORM_BASE_DOMAIN")
    user = auth.current_user(request)
    tiers = (user or {}).get("tiers") or {}
    if not tiers:
        return []
    units = []
    with registry_session() as rs:
        for slug, tier in tiers.items():
            t = tenant_by_slug(rs, slug)
            if t is None:
                continue
            info = _unit_directory_info(t.db_url)
            units.append({
                "slug": slug, "name": t.name, "tier": tier,
                "brand_color": brand_hex(t.brand_color),
                "crest": info.get("crest"), "members": info.get("members"),
                "recruiting": t.recruiting_open,
                "url": f"https://{slug}.{base_domain}/",
            })
    units.sort(key=lambda u: u["name"].lower())
    return units


def _render_home(request: Request):
    """The apex landing: what ValorLink is, plus navigation to Find a Unit and
    My Units, and the platform activity feed."""
    data = _directory_data()
    user = auth.current_user(request)
    my_count = len((user or {}).get("tiers") or {})
    ctx = {
        "request": request,
        "total_units": data["total_units"],
        "recruiting_total": sum(1 for u in data["units"] if u["recruiting"]),
        "game_count": len(data["games"]),
        "my_unit_count": my_count,
        "base_domain": data["base_domain"],
        "user": user,
        "csrf_token": auth.get_csrf_token(request),
        "flash": request.session.pop("flash", []),
        "now": datetime.utcnow(),
        "activity": _platform_activity(),
        "is_platform_admin": _is_platform_admin(user),
    }
    return templates.TemplateResponse(request, "home.html", ctx)


def _render_find(request: Request, q: str = "", game: str = ""):
    """The full Find-a-Unit browser: every listed unit, searchable, filterable
    by game, and sortable, grouped into per-game sections."""
    data = _directory_data()
    ctx = {
        "request": request,
        "units": data["units"],
        "sections": data["sections"],
        "games": data["games"],
        "total_units": data["total_units"],
        "base_domain": data["base_domain"],
        "q": q,
        "active_game": game if game in data["games"] else "",
        "user": auth.current_user(request),
        "csrf_token": auth.get_csrf_token(request),
        "flash": request.session.pop("flash", []),
        "now": datetime.utcnow(),
    }
    return templates.TemplateResponse(request, "directory.html", ctx)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


def _unit_activity(session: Session, limit: int = 12):
    """Recent activity within one unit (its bound session): enlistments,
    promotions, and honors, newest first."""
    cutoff = datetime.utcnow() - timedelta(days=60)
    items = []
    for m in (
        session.query(Member)
        .filter(Member.joined_date >= cutoff)
        .order_by(Member.joined_date.desc())
        .limit(10)
    ):
        items.append({"when": m.joined_date, "kind": "enlist", "who": m.callsign,
                      "text": "enlisted", "av": (m.discord_id, m.avatar)})
    for e in (
        session.query(ServiceHistoryEntry)
        .filter(ServiceHistoryEntry.date >= cutoff, ServiceHistoryEntry.entry.like("Promoted%"))
        .order_by(ServiceHistoryEntry.date.desc())
        .limit(10)
    ):
        member = session.get(Member, e.member_id)
        who = member.callsign if member else "A member"
        match = re.search(r" to (.+?) by ", e.entry)
        text = f"was promoted to {match.group(1)}" if match else "was promoted"
        items.append({"when": e.date, "kind": "promote", "who": who, "text": text,
                      "av": (member.discord_id, member.avatar) if member else None})
    for a in (
        session.query(MemberAward)
        .filter(MemberAward.date_awarded >= cutoff)
        .order_by(MemberAward.date_awarded.desc())
        .limit(10)
    ):
        member = session.get(Member, a.member_id)
        award = a.award_type
        items.append({
            "when": a.date_awarded, "kind": "award",
            "who": member.callsign if member else "A member",
            "text": f"earned {award.name}" if award else "earned an honor",
            "av": (member.discord_id, member.avatar) if member else None,
        })
    items.sort(key=lambda x: x["when"] or datetime.min, reverse=True)
    return items[:limit]


@app.get("/", response_class=HTMLResponse)
def headquarters(request: Request, session: Session = Depends(get_session)):
    if directory_mode(request):
        return _render_home(request)

    ctx = _base_context(request, session)

    counts = {
        "active": session.query(Member).filter(Member.status == "active").count(),
        "loa": session.query(Member).filter(Member.status == "loa").count(),
        "inactive": session.query(Member).filter(Member.status == "inactive").count(),
        "discharged": session.query(Member).filter(Member.status == "discharged").count(),
    }
    counts["enrolled"] = counts["active"] + counts["loa"] + counts["inactive"]

    activity = _unit_activity(session)

    upcoming = (
        session.query(Event)
        .filter(Event.scheduled_at >= datetime.utcnow())
        .order_by(Event.scheduled_at.asc())
        .limit(4)
        .all()
    )

    pending = session.query(Candidacy).order_by(Candidacy.created_at.desc()).all()

    companies = list_companies(session)
    ranks = rank_utils.all_ranks(session)

    # The signed-in member's own RSVP to each upcoming call, so they can answer
    # the call straight from Headquarters.
    viewer = ctx["user"]
    my_member = session.get(Member, int(viewer["id"])) if viewer else None
    my_rsvps: dict[int, str] = {}
    if my_member and upcoming:
        ev_ids = [e.id for e in upcoming]
        for r in session.query(AttendanceRecord).filter(
            AttendanceRecord.event_id.in_(ev_ids),
            AttendanceRecord.member_id == my_member.discord_id,
        ):
            my_rsvps[r.event_id] = r.status

    # Dashboard: next event, a turnout sparkline, and (for admins) setup progress.
    now = datetime.utcnow()
    next_event = upcoming[0] if upcoming else None
    past_events = (
        session.query(Event)
        .filter(Event.scheduled_at < now)
        .order_by(Event.scheduled_at.desc())
        .limit(10)
        .all()
    )
    spark = [
        {"name": e.name, "present": sum(1 for r in e.attendance_records if r.status == "present"),
         "total": len(e.attendance_records)}
        for e in reversed(past_events)
    ]
    spark_max = max((s["present"] for s in spark), default=0)
    spark_rates = [s["present"] / s["total"] for s in spark if s["total"]]
    spark_pct = round(sum(spark_rates) / len(spark_rates) * 100) if spark_rates else None
    setup = None
    setup_steps = None
    if auth.tier_at_least(ctx["user"], auth.TIER_ADMIN):
        checklist = _setup_checklist(session, get_config(session))
        done = sum(1 for s in checklist if s["done"])
        if done < len(checklist):
            setup = {"done": done, "total": len(checklist)}
            # The full step list drives the actionable onboarding card; the
            # next unfinished step is highlighted so there's an obvious "do this".
            setup_steps = checklist

    ctx.update(
        counts=counts,
        activity=activity,
        upcoming=upcoming,
        pending=pending,
        company_count=len(companies),
        rank_count=len(ranks),
        can_announce=auth.tier_at_least(ctx["user"], auth.TIER_OFFICER),
        announce_ready=bool(get_config(session).announcements_channel_id),
        is_member=bool(my_member),
        my_rsvps=my_rsvps,
        rsvp_choices=[("accepted", "Accept"), ("tentative", "Tentative"), ("declined", "Decline")],
        next_event=next_event,
        spark=spark,
        spark_max=spark_max,
        spark_pct=spark_pct,
        setup=setup,
        setup_steps=setup_steps,
    )
    if os.getenv("PLATFORM_BASE_DOMAIN"):
        allies = alliance_mod.alliances_for_unit(resolve_tenant(request).slug)
        ctx["allies"] = allies
        ctx["next_joint"] = alliance_events.next_event_for_alliances(
            [a["id"] for a in allies])
    return templates.TemplateResponse(request, "headquarters.html", ctx)


@app.get("/find", response_class=HTMLResponse)
def find_units(request: Request, q: str = "", game: str = ""):
    """The Find-a-Unit browser. A platform (apex) page; from a unit subdomain
    it bounces to the apex."""
    base = os.getenv("PLATFORM_BASE_DOMAIN")
    if not base:
        raise TenantNotFound(None)
    if not directory_mode(request):  # on a unit subdomain
        return RedirectResponse(f"https://{base}/find", status_code=307)
    return _render_find(request, q=q, game=game)


@app.get("/my-units", response_class=HTMLResponse)
def my_units_page(request: Request):
    """The signed-in user's units — a launchpad to each portal. A platform
    (apex) page; from a unit subdomain it bounces to the apex."""
    base = os.getenv("PLATFORM_BASE_DOMAIN")
    if not base:
        raise TenantNotFound(None)
    if not directory_mode(request):
        return RedirectResponse(f"https://{base}/my-units", status_code=307)
    ctx = {
        "request": request,
        "units": _user_units(request),
        "base_domain": base,
        "user": auth.current_user(request),
        "csrf_token": auth.get_csrf_token(request),
        "flash": request.session.pop("flash", []),
        "now": datetime.utcnow(),
    }
    return templates.TemplateResponse(request, "my_units.html", ctx)


@app.get("/join", response_class=HTMLResponse)
def join(request: Request, session: Session = Depends(get_session)):
    """A unit's public recruiting page — what they are, how active they are,
    and how to apply. Visible to everyone, including signed-out visitors."""
    if directory_mode(request):
        return RedirectResponse("/", status_code=303)  # the apex is the directory
    ctx = _base_context(request, session)
    tenant = resolve_tenant(request)
    cfg = get_config(session)

    listing_name, motto, blurb, recruiting = cfg.regiment_name, cfg.regiment_motto, None, True
    with registry_session() as rs:
        row = tenant_by_slug(rs, tenant.slug)
        if row is not None:
            listing_name = row.name
            motto = row.motto or cfg.regiment_motto
            blurb = row.blurb
            recruiting = row.recruiting_open

    active = session.query(Member).filter(Member.status == "active").count()
    upcoming = (
        session.query(Event)
        .filter(Event.scheduled_at >= datetime.utcnow())
        .order_by(Event.scheduled_at.asc())
        .limit(5)
        .all()
    )
    # When do members tend to play? Tally their self-reported nights.
    play_nights: dict[str, int] = defaultdict(int)
    for (avail,) in session.query(Member.availability).filter(
        Member.status == "active", Member.availability.isnot(None)
    ):
        for d in (avail or "").split(","):
            if d:
                play_nights[d] += 1
    nights = [d for d in services.DAY_CODES if play_nights.get(d)]

    viewer = ctx["user"]
    already_applied = is_member = False
    if viewer:
        already_applied = session.get(Candidacy, int(viewer["id"])) is not None
        is_member = session.get(Member, int(viewer["id"])) is not None

    ctx.update(
        unit_name=listing_name, motto=motto, blurb=blurb, recruiting=recruiting,
        active=active, upcoming=upcoming, nights=nights,
        apply_slug=tenant.slug, already_applied=already_applied, is_member=is_member,
        invite=cfg.discord_invite,
    )
    return templates.TemplateResponse(request, "join.html", ctx)


@app.get("/apply", response_class=HTMLResponse)
def apply_form(request: Request, session: Session = Depends(get_session)):
    """The unit's application form — its recruitment questions, or a simple
    confirm if it asks none."""
    if directory_mode(request):
        return RedirectResponse("/", status_code=303)
    ctx = _base_context(request, session)
    tenant = resolve_tenant(request)
    name, recruiting = get_config(session).regiment_name, True
    with registry_session() as rs:
        row = tenant_by_slug(rs, tenant.slug)
        if row is not None:
            name, recruiting = row.name, row.recruiting_open
    questions = services.list_recruitment_questions(session, enabled_only=True)
    # Applying only needs the visitor's Discord identity (they may have signed
    # in on the directory), so use the global identity, not the unit-scoped one.
    viewer = auth.current_user(request)
    ctx["user"] = viewer
    already = is_member = False
    if viewer:
        already = session.get(Candidacy, int(viewer["id"])) is not None
        is_member = session.get(Member, int(viewer["id"])) is not None
    ctx.update(unit_name=name, recruiting=recruiting, questions=questions,
               already_applied=already, is_member=is_member,
               invite=get_config(session).discord_invite)
    return templates.TemplateResponse(request, "apply.html", ctx)


@app.post("/apply")
async def apply_submit(request: Request):
    user = auth.current_user(request)
    if not user:
        raise auth.NotAuthenticated()
    form = await request.form()
    if not auth.verify_csrf(request, form.get("csrf", "")):
        _flash(request, "Your session expired. Please try that again.", "error")
        return RedirectResponse("/apply", status_code=303)
    with _tenant_session(request) as session:
        questions = services.list_recruitment_questions(session, enabled_only=True)
        answers = []
        for q in questions:
            val = (form.get(f"q_{q.id}") or "").strip()
            if q.required and not val:
                _flash(request, f"Please answer: {q.prompt}", "error")
                return RedirectResponse("/apply", status_code=303)
            if val:
                answers.append({"q": q.prompt, "a": val[:1000]})
        try:
            msg = services.submit_application(
                session, int(user["id"]), user.get("name", "Applicant"), answers
            )
            _flash(request, msg, "ok")
        except services.ActionError as exc:
            _flash(request, str(exc), "error")
    return RedirectResponse("/", status_code=303)


@app.post("/announce")
def post_announce(
    request: Request,
    csrf: str = Form(...),
    title: str = Form(""),
    body: str = Form(...),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.post_announcement, actor, title, body, redirect="/")


@app.get("/roster", response_class=HTMLResponse)
def roster(request: Request, session: Session = Depends(get_session), tab: str = "company"):
    ctx = _base_context(request, session)

    members = session.query(Member).filter(Member.status == "active").all()
    rank_order = {name: i for i, name in enumerate(rank_utils.rank_names(session))}
    rank_images = {r.name: r.image for r in rank_utils.all_ranks(session) if r.image}

    by_company: dict[str, list[Member]] = defaultdict(list)
    for m in members:
        by_company[m.company].append(m)

    configured = [c.name for c in list_companies(session)]
    order = configured + [c for c in by_company if c not in configured]

    companies = []
    for name in order:
        roster_members = by_company.get(name)
        if not roster_members:
            continue
        roster_members.sort(key=lambda m: rank_order.get(m.rank, -1), reverse=True)
        companies.append({"name": name, "members": roster_members})

    # The same present-for-duty members, grouped by secondary assignment for the
    # "By Assignment" tab. Leadership groups first, seniority within.
    active_ids = {m.discord_id for m in members}
    member_by_id = {m.discord_id: m for m in members}
    assignment_groups = []
    for a in services.list_assignments(session):
        grp = sorted(
            [member_by_id[ma.member_id] for ma in a.members if ma.member_id in active_ids],
            key=lambda m: (-rank_order.get(m.rank, -1), m.callsign.lower()),
        )
        assignment_groups.append({"assignment": a, "members": grp})

    ctx.update(
        companies=companies,
        active_total=len(members),
        assignment_groups=assignment_groups,
        has_assignments=bool(assignment_groups),
        active_tab=("assignments" if tab == "assignments" else "company"),
        rank_images=rank_images,
        can_manage=auth.tier_at_least(ctx["user"], auth.TIER_OFFICER),
    )
    return templates.TemplateResponse(request, "roster.html", ctx)


@app.get("/muster", response_class=HTMLResponse)
def muster(request: Request, session: Session = Depends(get_session)):
    """The full muster roll — every enrolled soul, whatever their standing."""
    ctx = _base_context(request, session)

    rank_order = {name: i for i, name in enumerate(rank_utils.rank_names(session))}
    rank_images = {r.name: r.image for r in rank_utils.all_ranks(session) if r.image}
    status_rank = {"active": 0, "loa": 1, "inactive": 2, "discharged": 3}
    members = session.query(Member).all()
    members.sort(
        key=lambda m: (
            status_rank.get(m.status, 9),
            -rank_order.get(m.rank, -1),
            m.callsign.lower(),
        )
    )

    companies_present = sorted({m.company for m in members})
    ranks_present = [r for r in rank_utils.rank_names(session) if any(m.rank == r for m in members)]
    statuses_present = sorted({m.status for m in members}, key=lambda s: status_rank.get(s, 9))
    ctx.update(
        members=members,
        total=len(members),
        filter_companies=companies_present,
        filter_ranks=ranks_present,
        filter_statuses=statuses_present,
        can_manage=auth.tier_at_least(ctx["user"], auth.TIER_OFFICER),
        company_options=services.company_options(session),
        rank_images=rank_images,
    )
    return templates.TemplateResponse(request, "muster.html", ctx)


@app.post("/muster/bulk")
def post_muster_bulk(
    request: Request,
    csrf: str = Form(...),
    action: str = Form(...),
    ids: list[str] = Form(default=[]),
    company: str = Form(""),
    entry: str = Form(""),
    user: dict = Depends(auth.require_officer),
):
    """Apply one action to several members at once. Each member is handled by
    the same service the single-member forms use (so Discord side-effects are
    queued identically); a per-member failure is counted, not fatal."""
    if not auth.verify_csrf(request, csrf):
        _flash(request, "Your session expired. Please try that again.", "error")
        return RedirectResponse("/muster", status_code=303)

    member_ids = []
    for raw in ids:
        try:
            member_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not member_ids:
        _flash(request, "No members were selected.", "error")
        return RedirectResponse("/muster", status_code=303)

    actor = {"id": user["id"], "name": user["name"]}
    done, skipped = 0, 0
    errors: list[str] = []
    with _tenant_session(request) as session:
        for mid in member_ids:
            try:
                if action == "company":
                    services.assign_company(session, actor, mid, company)
                elif action == "note":
                    services.service_log(session, actor, mid, entry)
                else:
                    _flash(request, "Unknown bulk action.", "error")
                    return RedirectResponse("/muster", status_code=303)
                done += 1
            except services.ActionError as exc:
                # "already in X" / empty note etc. — expected, non-fatal.
                skipped += 1
                if len(errors) < 3 and "already" not in str(exc):
                    errors.append(str(exc))

    verb = "Transferred" if action == "company" else "Logged a note for"
    target = f" to {company}" if action == "company" else ""
    msg = f"{verb} {done} member(s){target}."
    if skipped:
        msg += f" {skipped} skipped."
    if errors:
        msg += " " + " ".join(errors)
    _flash(request, msg, "ok" if done else "error")
    return RedirectResponse("/muster", status_code=303)


def _render_dossier(request: Request, session: Session, member: Member, is_self: bool = False):
    ctx = _base_context(request, session)
    service = sorted(member.service_history, key=lambda e: e.date or datetime.min, reverse=True)
    discipline = sorted(
        member.disciplinary_records, key=lambda r: r.date or datetime.min, reverse=True
    )
    awards = sorted(member.awards, key=lambda a: a.date_awarded or datetime.min, reverse=True)

    att_counts: dict[str, int] = defaultdict(int)
    for rec in member.attendance_records:
        att_counts[rec.status] += 1

    # A single chronological timeline merging enlistment, service entries,
    # conduct records, and honors — the member's story in one column.
    timeline = []
    if member.joined_date:
        timeline.append({"date": member.joined_date, "kind": "enlist",
                         "label": "Enlisted", "text": f"Joined the regiment as {member.rank}."})
    for e in service:
        timeline.append({"date": e.date, "kind": "service", "label": "Service", "text": e.entry})
    for r in discipline:
        timeline.append({"date": r.date, "kind": "conduct", "label": r.record_type,
                         "text": r.reason})
    for a in awards:
        emoji = f"{a.award_type.emoji} " if a.award_type.emoji else ""
        timeline.append({"date": a.date_awarded, "kind": "honor", "label": "Honor",
                         "text": f"Awarded {emoji}{a.award_type.name}."})
    timeline.sort(key=lambda t: t["date"] or datetime.min, reverse=True)

    # A member viewing their own record gets a small dashboard: their upcoming
    # musters (with one-click RSVP) and their turnout rate.
    self_dashboard = None
    if is_self:
        now = datetime.utcnow()
        upcoming_events = (
            session.query(Event)
            .filter(Event.scheduled_at >= now)
            .order_by(Event.scheduled_at.asc())
            .limit(5)
            .all()
        )
        my_rsvps: dict[int, str] = {}
        if upcoming_events:
            ev_ids = [e.id for e in upcoming_events]
            for r in session.query(AttendanceRecord).filter(
                AttendanceRecord.event_id.in_(ev_ids),
                AttendanceRecord.member_id == member.discord_id,
            ):
                my_rsvps[r.event_id] = r.status
        _events, _sbm, rate_for = _attendance_index(session)
        self_dashboard = {
            "upcoming": upcoming_events,
            "my_rsvps": my_rsvps,
            "turnout": rate_for(member),
        }

    held_assignments = sorted(
        member.assignments,
        key=lambda ma: (not ma.assignment.is_leadership, ma.assignment.position, ma.assignment.name),
    )
    held_ids = {ma.assignment_id for ma in member.assignments}
    assignable = [a for a in services.list_assignments(session) if a.id not in held_ids]

    ctx.update(
        self_dashboard=self_dashboard,
        rsvp_choices=[("accepted", "Accept"), ("tentative", "Tentative"), ("declined", "Decline")],
        held_assignments=held_assignments,
        assignable=assignable,
        member=member,
        rank=rank_utils.rank_by_name(session, member.rank),
        service=service,
        discipline=discipline,
        awards=awards,
        timeline=timeline,
        att_counts=dict(att_counts),
        rank_options=services.rank_options(session),
        company_options=services.company_options(session),
        held_award_ids={a.award_type_id for a in member.awards},
        award_catalogue=session.query(AwardType).order_by(AwardType.name).all(),
        day_codes=services.DAY_CODES,
        member_days=(member.availability or "").split(",") if member.availability else [],
        is_self=is_self,
    )
    return templates.TemplateResponse(request, "dossier.html", ctx)


@app.get("/dossier/{discord_id}", response_class=HTMLResponse)
def dossier(request: Request, discord_id: int, session: Session = Depends(get_session)):
    member = session.get(Member, discord_id)
    if member is None:
        ctx = _base_context(request, session)
        ctx["message"] = "No personnel record bears that number."
        return templates.TemplateResponse(request, "not_found.html", ctx, status_code=404)
    viewer = auth.effective_user(request, resolve_tenant(request).slug)
    is_self = bool(viewer and int(viewer["id"]) == member.discord_id)
    return _render_dossier(request, session, member, is_self=is_self)


@app.get("/my-record", response_class=HTMLResponse)
def my_record(request: Request, session: Session = Depends(get_session)):
    user = auth.effective_user(request, resolve_tenant(request).slug)
    if not user:
        raise auth.NotAuthenticated()
    member = session.get(Member, int(user["id"]))
    if member is None:
        ctx = _base_context(request, session)
        ctx["message"] = (
            "You don't have a record with this unit yet. If it's recruiting, "
            "apply from the directory and an officer will enlist you."
        )
        return templates.TemplateResponse(request, "not_found.html", ctx, status_code=404)
    return _render_dossier(request, session, member, is_self=True)


@app.get("/muster-calls", response_class=HTMLResponse)
def events(request: Request, session: Session = Depends(get_session)):
    ctx = _base_context(request, session)
    now = datetime.utcnow()

    all_events = session.query(Event).order_by(Event.scheduled_at.desc()).all()

    def summarize(ev: Event) -> dict:
        counts: dict[str, int] = defaultdict(int)
        for rec in ev.attendance_records:
            counts[rec.status] += 1
        return {"event": ev, "counts": dict(counts), "total": len(ev.attendance_records)}

    upcoming = [summarize(e) for e in reversed(all_events) if e.scheduled_at >= now]
    past = [summarize(e) for e in all_events if e.scheduled_at < now]

    ctx.update(upcoming=upcoming, past=past, event_types=services.EVENT_TYPES)
    return templates.TemplateResponse(request, "events.html", ctx)


ATTENDANCE_WINDOW_DAYS = 90
AT_RISK_RATE = 0.5      # below this counts as at-risk
AT_RISK_MIN_EVENTS = 2  # ...but only once someone's had a fair sample


def _attendance_index(session: Session, window_days: int = ATTENDANCE_WINDOW_DAYS):
    """Return (events, status_by_member, rate_for) for muster calls in the
    window. ``rate_for(member)`` gives that member's turnout, counting only
    calls held after they enrolled and setting excused absences aside. Shared
    by the attendance analytics page and the promotion board."""
    now = datetime.utcnow()
    window_start = now - timedelta(days=window_days)
    events = (
        session.query(Event)
        .filter(Event.scheduled_at <= now, Event.scheduled_at >= window_start)
        .order_by(Event.scheduled_at.desc())
        .all()
    )
    event_ids = [e.id for e in events]

    status_by_member: dict[int, dict[int, str]] = defaultdict(dict)
    if event_ids:
        for rec in (
            session.query(AttendanceRecord)
            .filter(AttendanceRecord.event_id.in_(event_ids))
            .all()
        ):
            status_by_member[rec.member_id][rec.event_id] = rec.status

    def rate_for(member: Member) -> dict:
        joined = member.joined_date or datetime.min
        eligible = [e for e in events if e.scheduled_at >= joined]
        statuses = status_by_member.get(member.discord_id, {})
        present = sum(1 for e in eligible if statuses.get(e.id) == "present")
        excused = sum(1 for e in eligible if statuses.get(e.id) == "excused")
        counted = len(eligible) - excused
        rate = (present / counted) if counted else None
        return {
            "present": present, "counted": counted, "excused": excused,
            "missed": (counted - present) if counted else 0,
            "rate": rate, "pct": round(rate * 100) if rate is not None else None,
        }

    return events, status_by_member, rate_for


@app.get("/attendance", response_class=HTMLResponse)
def attendance(request: Request, session: Session = Depends(get_session)):
    """Attendance analytics: per-member turnout over recent events, an at-risk
    list, and a per-event summary. A member's denominator only counts events
    held after they enrolled, and excused absences are set aside — so the rate
    reflects the muster calls they were actually expected at."""
    ctx = _base_context(request, session)
    events, status_by_member, rate_for = _attendance_index(session)

    members = (
        session.query(Member)
        .filter(Member.status.in_(("active", "loa", "inactive")))
        .all()
    )

    rows = [{"member": m, **rate_for(m)} for m in members]

    # Full table: those with a rate first (best turnout first), then the rest.
    ranked = sorted(
        rows,
        key=lambda r: (r["rate"] is None, -(r["rate"] or 0), r["member"].callsign.lower()),
    )
    at_risk = sorted(
        (r for r in ranked
         if r["rate"] is not None and r["counted"] >= AT_RISK_MIN_EVENTS
         and r["rate"] < AT_RISK_RATE and r["member"].status == "active"),
        key=lambda r: r["rate"],
    )

    # Per-event turnout summary.
    event_summary = []
    for e in events:
        present = sum(1 for s in status_by_member.values() if s.get(e.id) == "present")
        responses = sum(1 for s in status_by_member.values() if e.id in s)
        event_summary.append({"event": e, "present": present, "responses": responses})

    overall_present = sum(r["present"] for r in rows)
    overall_counted = sum(r["counted"] for r in rows)
    ctx.update(
        rows=ranked,
        at_risk=at_risk,
        event_summary=event_summary,
        window_days=ATTENDANCE_WINDOW_DAYS,
        event_count=len(events),
        avg_pct=round(overall_present / overall_counted * 100) if overall_counted else None,
        at_risk_pct=round(AT_RISK_RATE * 100),
    )
    return templates.TemplateResponse(request, "attendance.html", ctx)


PROMOTION_MIN_DAYS_IN_RANK = 30
PROMOTION_MIN_ATTENDANCE = 0.5


@app.get("/promotions", response_class=HTMLResponse)
def promotions(request: Request, session: Session = Depends(get_session)):
    """A promotion board: active members who have a next rank to earn, ranked
    by readiness (time in rank + turnout). It's a shortlist for officers to act
    on, not automation — the criteria are shown so the judgement stays human."""
    ctx = _base_context(request, session)
    now = datetime.utcnow()
    _events, _sbm, rate_for = _attendance_index(session)

    members = session.query(Member).filter(Member.status == "active").all()
    rows = []
    for m in members:
        next_rank = rank_utils.next_rank(session, m.rank)
        if not next_rank:
            continue  # already at the top of the ladder
        since = m.rank_since or m.joined_date or now
        days_in_rank = max((now - since).days, 0)
        att = rate_for(m)
        awards_held = len(m.awards)
        meets_time = days_in_rank >= PROMOTION_MIN_DAYS_IN_RANK
        # Attendance only blocks when there's a rate to judge and it's too low.
        meets_att = att["rate"] is None or att["rate"] >= PROMOTION_MIN_ATTENDANCE
        rows.append({
            "member": m,
            "next_rank": next_rank,
            "days_in_rank": days_in_rank,
            "attendance": att,
            "awards": awards_held,
            "eligible": meets_time and meets_att,
            "meets_time": meets_time,
            "meets_att": meets_att,
        })

    rows.sort(key=lambda r: (not r["eligible"], -r["days_in_rank"], r["member"].callsign.lower()))
    eligible_count = sum(1 for r in rows if r["eligible"])
    rank_images = {r.name: r.image for r in rank_utils.all_ranks(session) if r.image}
    ctx.update(
        rows=rows,
        eligible_count=eligible_count,
        min_days=PROMOTION_MIN_DAYS_IN_RANK,
        min_att_pct=round(PROMOTION_MIN_ATTENDANCE * 100),
        can_promote=auth.tier_at_least(ctx["user"], auth.TIER_OFFICER),
        rank_images=rank_images,
    )
    return templates.TemplateResponse(request, "promotions.html", ctx)


@app.get("/muster-calls/{event_id}", response_class=HTMLResponse)
def event_detail(request: Request, event_id: int, session: Session = Depends(get_session)):
    ctx = _base_context(request, session)

    event = session.get(Event, event_id)
    if event is None:
        ctx["message"] = "No such event is recorded."
        return templates.TemplateResponse(request, "not_found.html", ctx, status_code=404)

    records = []
    for rec in event.attendance_records:
        records.append({"record": rec, "member": session.get(Member, rec.member_id)})
    records.sort(key=lambda r: (r["member"].callsign.lower() if r["member"] else "~"))

    counts: dict[str, int] = defaultdict(int)
    for rec in event.attendance_records:
        counts[rec.status] += 1

    active_members = (
        session.query(Member)
        .filter(Member.status.in_(("active", "loa")))
        .order_by(Member.callsign)
        .all()
    )

    # The signed-in member's own RSVP, so they can set it from the web.
    viewer = ctx["user"]
    my_rsvp = None
    my_slot_id = None
    can_rsvp = False
    if viewer:
        me = session.get(Member, int(viewer["id"]))
        if me is not None:
            can_rsvp = True
            mine = next((r for r in event.attendance_records if r.member_id == me.discord_id), None)
            my_rsvp = mine.status if mine else None
            my_slot_id = mine.slot_id if mine else None

    # How many "accepted" RSVPs each signup slot currently holds.
    slot_counts: dict[int, int] = defaultdict(int)
    for rec in event.attendance_records:
        if rec.status == "accepted" and rec.slot_id:
            slot_counts[rec.slot_id] += 1

    # Roster for the bulk attendance grid: every active/on-leave member with
    # their current recorded standing (blank if not yet marked).
    status_by_member = {r.member_id: r.status for r in event.attendance_records}
    roster_attendance = [
        {"member": m, "status": status_by_member.get(m.discord_id, "")}
        for m in active_members
    ]

    # If this event's Discord announcement is deferred, when does it post?
    announce_at = None
    if not event.announced and event.announce_lead_minutes is not None:
        announce_at = event.scheduled_at - timedelta(minutes=event.announce_lead_minutes)

    ctx.update(
        event=event,
        event_iso=event.scheduled_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        announce_at=announce_at,
        records=records,
        counts=dict(counts),
        active_members=active_members,
        roster_attendance=roster_attendance,
        attendance_statuses=services.ATTENDANCE_STATUSES,
        event_types=terminology.resolve_terms(get_config(session).terminology_custom)["event_types"],
        can_rsvp=can_rsvp,
        my_rsvp=my_rsvp,
        my_slot_id=my_slot_id,
        slot_counts=dict(slot_counts),
        is_past=event.scheduled_at < datetime.utcnow(),
        outcome_options=services.EVENT_OUTCOMES,
    )
    return templates.TemplateResponse(request, "event_detail.html", ctx)


@app.get("/command-staff")
def command_staff(request: Request):
    """The staff & command roster is now the Roster page's 'By Assignment' tab.
    Kept as a redirect so existing links still land in the right place."""
    return RedirectResponse("/roster?tab=assignments", status_code=307)


@app.get("/honors", response_class=HTMLResponse)
def honors(request: Request, session: Session = Depends(get_session)):
    """Awards & qualifications catalogue with their recipients."""
    ctx = _base_context(request, session)

    award_types = session.query(AwardType).order_by(AwardType.name).all()
    catalogue = []
    for at in award_types:
        holders = []
        for grant in at.awards:
            member = session.get(Member, grant.member_id)
            holders.append({"grant": grant, "member": member})
        holders.sort(key=lambda h: (h["member"].callsign.lower() if h["member"] else "~"))
        catalogue.append({"award": at, "holders": holders})

    ctx.update(catalogue=catalogue)
    return templates.TemplateResponse(request, "honors.html", ctx)


def _setup_checklist(session: Session, cfg) -> list[dict]:
    """First-run setup steps for a new unit, each with a done flag and a link
    to the Command Tent section that satisfies it. Drives the onboarding card."""
    rank_count = len(rank_utils.all_ranks(session))
    company_count = len(list_companies(session))
    # A step may carry a Discord slash command when it must be bootstrapped in
    # Discord rather than the web Command Tent — the admin role is the classic
    # chicken-and-egg: you can't reach the web admin pages until it's set.
    steps = [
        ("Name your regiment", cfg.regiment_name not in ("", "Unconfigured Regiment"),
         "#identity", "So the banner and directory show your unit, not a placeholder.", None),
        ("Set the Admin role", bool(cfg.admin_role_id),
         "#roles", "The very first admin role must be set in Discord, by someone with "
         "server Administrator permission — replace @Admin with your role.",
         "/config set_role key:admin role:@Admin"),
        ("Set the Officer role", bool(cfg.officer_role_id),
         "#roles", "Officers manage the roster, events, and discipline.", None),
        ("Set the Recruiter role", bool(cfg.recruiter_role_id),
         "#roles", "Recruiters approve or deny applicants.", None),
        ("Set the Roster channel", bool(cfg.roster_channel_id),
         "#channels", "Where the live roster embed is posted and kept current.", None),
        ("Build the rank ladder", rank_count >= 2,
         "#ranks", "You need ranks before you can promote anyone.", None),
        ("Add a company", company_count >= 1,
         "#companies", "Members are assigned to a company on the roster.", None),
    ]
    return [{"label": s[0], "done": s[1], "anchor": s[2], "hint": s[3], "discord_cmd": s[4]}
            for s in steps]


@app.get("/audit", response_class=HTMLResponse)
def audit_log(request: Request, category: str = "",
              session: Session = Depends(get_session),
              user: dict = Depends(auth.require_officer)):
    """The unit's accountability log: who did what, when, and from where."""
    ctx = _base_context(request, session)
    category = category if category in services.AUDIT_CATEGORIES else ""
    entries = services.list_audit(session, category=category)
    target_ids = {e.target_id for e in entries if e.target_id}
    names = {}
    if target_ids:
        for m in session.query(Member).filter(Member.discord_id.in_(target_ids)):
            names[m.discord_id] = m.callsign
    rows = [
        {"at": e.at, "actor_name": e.actor_name or "—", "source": e.source,
         "category": e.category, "summary": e.summary,
         "target_id": e.target_id, "target_name": names.get(e.target_id)}
        for e in entries
    ]
    ctx.update(entries=rows, categories=services.AUDIT_CATEGORIES, category=category)
    return templates.TemplateResponse(request, "audit.html", ctx)


@app.get("/command-tent", response_class=HTMLResponse)
def command_tent(request: Request, session: Session = Depends(get_session),
                 user: dict = Depends(auth.require_admin)):
    """Admin-only configuration: identity, roles, channels, ranks, companies."""
    ctx = _base_context(request, session)
    cfg = get_config(session)
    checklist = _setup_checklist(session, cfg)
    ctx["checklist"] = checklist
    ctx["checklist_done"] = sum(1 for s in checklist if s["done"])
    # This unit's public directory listing (registry), when platform mode is on.
    listing = None
    if os.getenv("PLATFORM_BASE_DOMAIN"):
        tenant = resolve_tenant(request)
        with registry_session() as rs:
            row = tenant_by_slug(rs, tenant.slug)
            if row is not None:
                listing = {
                    "name": row.name,
                    "motto": row.motto or "",
                    "blurb": row.blurb or "",
                    "game": row.game or "",
                    "tags": row.tags or "",
                    "recruiting_open": row.recruiting_open,
                    "listed": row.listed,
                    "discord_guild_id": row.discord_guild_id or "",
                    "url": f"https://{row.slug}.{os.getenv('PLATFORM_BASE_DOMAIN')}/",
                    "slug": row.slug,
                    "base_domain": os.getenv("PLATFORM_BASE_DOMAIN"),
                }

    ctx.update(
        cfg=cfg,
        role_keys=ROLE_KEYS,
        channel_keys=CHANNEL_KEYS,
        role_values={k: getattr(cfg, col) for k, col in ROLE_KEYS.items()},
        channel_values={k: getattr(cfg, col) for k, col in CHANNEL_KEYS.items()},
        brand_hex=brand_hex(cfg.brand_color),
        ranks=list(reversed(rank_utils.all_ranks(session))),
        companies=list_companies(session),
        assignments=services.list_assignments(session),
        questions=services.list_recruitment_questions(session),
        theme_choices=terminology.THEME_CHOICES,
        theme_swatches=terminology.THEME_SWATCHES,
        term_fields=terminology.EDITABLE_KEYS,
        has_custom_terms=bool(cfg.terminology_custom),
        listing=listing,
        queue_actions=services.list_recent_actions(session),
        queue_counts=services.action_queue_counts(session),
    )
    # Alliances are a cross-unit (platform) feature; only meaningful when units
    # share a platform. Scoped to this unit's own memberships and invitations.
    if os.getenv("PLATFORM_BASE_DOMAIN"):
        slug = resolve_tenant(request).slug
        ctx["alliances"] = alliance_mod.alliances_for_unit(slug)
        ctx["alliance_invites"] = alliance_mod.pending_invites_for_unit(slug)
        ctx["unit_slug"] = slug
        ctx["base_domain"] = os.getenv("PLATFORM_BASE_DOMAIN")
    return templates.TemplateResponse(request, "command_tent.html", ctx)


def _do_alliance(request: Request, csrf: str, fn, *args):
    """Run an alliance action (CSRF + error handling) and redirect back."""
    if not auth.verify_csrf(request, csrf):
        _flash(request, "Your session expired. Please try that again.", "error")
        return RedirectResponse("/command-tent", status_code=303)
    try:
        _flash(request, fn(*args), "ok")
    except alliance_mod.AllianceError as exc:
        _flash(request, str(exc), "error")
    return RedirectResponse("/command-tent", status_code=303)


@app.post("/admin/alliances/create")
def post_alliance_create(
    request: Request,
    csrf: str = Form(...),
    name: str = Form(...),
    slug: str = Form(...),
    user: dict = Depends(auth.require_admin),
):
    me = resolve_tenant(request).slug
    return _do_alliance(request, csrf, alliance_mod.create_alliance, name, slug, me)


@app.post("/admin/alliances/{alliance_id}/invite")
def post_alliance_invite(
    request: Request,
    alliance_id: int,
    csrf: str = Form(...),
    target: str = Form(...),
    user: dict = Depends(auth.require_admin),
):
    me = resolve_tenant(request).slug
    return _do_alliance(request, csrf, alliance_mod.invite_unit, alliance_id, target, me)


@app.post("/admin/alliances/{alliance_id}/accept")
def post_alliance_accept(
    request: Request,
    alliance_id: int,
    csrf: str = Form(...),
    user: dict = Depends(auth.require_admin),
):
    me = resolve_tenant(request).slug
    return _do_alliance(request, csrf, alliance_mod.accept_invite, alliance_id, me)


@app.post("/admin/alliances/{alliance_id}/leave")
def post_alliance_leave(
    request: Request,
    alliance_id: int,
    csrf: str = Form(...),
    user: dict = Depends(auth.require_admin),
):
    me = resolve_tenant(request).slug
    return _do_alliance(request, csrf, alliance_mod.leave_alliance, alliance_id, me)


def _parse_local_dt(value: str, tz_offset: str):
    """A datetime-local value plus the browser's UTC offset (minutes) → UTC."""
    dt = datetime.fromisoformat(value)  # naive local time
    try:
        offset = int(tz_offset)
    except (TypeError, ValueError):
        offset = 0
    return dt + timedelta(minutes=offset)


@app.post("/admin/alliances/{alliance_id}/events/create")
def post_alliance_event_create(
    request: Request,
    alliance_id: int,
    csrf: str = Form(...),
    name: str = Form(...),
    event_type: str = Form("Line Battle"),
    when: str = Form(...),
    tz_offset: str = Form("0"),
    description: str = Form(""),
    user: dict = Depends(auth.require_admin),
):
    me = resolve_tenant(request).slug
    try:
        scheduled_at = _parse_local_dt(when, tz_offset)
    except ValueError:
        _flash(request, "Enter a valid date and time.", "error")
        return RedirectResponse("/command-tent", status_code=303)
    return _do_alliance(request, csrf, alliance_events.create_event,
                        alliance_id, me, name, event_type, scheduled_at,
                        description, int(user["id"]))


@app.post("/alliance/{slug}/events/{event_id}/rsvp")
def post_alliance_rsvp(
    request: Request,
    slug: str,
    event_id: int,
    csrf: str = Form(...),
    status: str = Form(...),
):
    user = auth.current_user(request)
    if not user:
        raise auth.NotAuthenticated()
    dest = RedirectResponse(f"/alliance/{slug}", status_code=303)
    if not auth.verify_csrf(request, csrf):
        _flash(request, "Your session expired. Please try that again.", "error")
        return dest
    detail = alliance_mod.alliance_detail(slug)
    if detail is None:
        return dest
    member_slugs = {m["slug"] for m in detail["members"]}
    mine = set((user.get("tiers") or {}).keys()) & member_slugs
    if not mine:
        _flash(request, "Only members of a unit in this alliance can answer the call.", "error")
        return dest
    if alliance_events.event_alliance_id(event_id) != detail["id"]:
        return dest
    try:
        _flash(request, alliance_events.rsvp(event_id, int(user["id"]),
                                             sorted(mine)[0], status), "ok")
    except alliance_mod.AllianceError as exc:
        _flash(request, str(exc), "error")
    return dest


def _alliance_broadcast(alliance_id: int, alliance_name: str, title: str,
                        body: str, actor_id: int | None) -> int:
    """Queue an alliance-wide message on every member unit's queue; the bot posts
    it to each unit's announcements channel."""
    sent = 0
    for target in alliance_events.member_targets(alliance_id):
        try:
            with sessionmaker_for(target["db_url"])() as s:
                queue.enqueue(s, queue.ALLIANCE_ANNOUNCE,
                              {"alliance_name": alliance_name, "title": title, "body": body},
                              actor_id=actor_id)
                s.commit()
            sent += 1
        except Exception:  # noqa: BLE001 -- one unreadable unit shouldn't stop the rest
            pass
    return sent


@app.post("/admin/alliances/{alliance_id}/announce")
def post_alliance_announce(
    request: Request,
    alliance_id: int,
    csrf: str = Form(...),
    title: str = Form(""),
    body: str = Form(...),
    user: dict = Depends(auth.require_admin),
):
    dest = RedirectResponse("/command-tent", status_code=303)
    if not auth.verify_csrf(request, csrf):
        _flash(request, "Your session expired. Please try that again.", "error")
        return dest
    me = resolve_tenant(request).slug
    mine = [a for a in alliance_mod.alliances_for_unit(me) if a["id"] == alliance_id]
    if not mine:
        _flash(request, "Your unit isn't a member of that alliance.", "error")
        return dest
    body = body.strip()
    if not body:
        _flash(request, "An announcement needs a message.", "error")
        return dest
    count = _alliance_broadcast(alliance_id, mine[0]["name"], title.strip(), body,
                                int(user["id"]))
    _flash(request, f"Announcement queued for {count} unit{'s' if count != 1 else ''}. "
                    "The bot posts it to each announcements channel shortly.", "ok")
    return dest


@app.post("/admin/actions/{action_id}/retry")
def post_action_retry(
    request: Request,
    action_id: int,
    csrf: str = Form(...),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.retry_action, action_id, redirect="/command-tent")


@app.post("/admin/actions/{action_id}/dismiss")
def post_action_dismiss(
    request: Request,
    action_id: int,
    csrf: str = Form(...),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.dismiss_action, action_id, redirect="/command-tent")


@app.post("/admin/actions/retry-all")
def post_action_retry_all(
    request: Request,
    csrf: str = Form(...),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.retry_all_failed_actions, redirect="/command-tent")


@app.get("/login", response_class=HTMLResponse)
def login(request: Request, session: Session = Depends(get_session)):
    ctx = _base_context(request, session)
    ctx["login_error"] = request.session.pop("login_error", None)
    ctx["oauth_enabled"] = auth.OAUTH_ENABLED
    ctx["dev_login_enabled"] = auth.DEV_LOGIN_ENABLED
    if auth.DEV_LOGIN_ENABLED:
        ctx["members"] = (
            session.query(Member).order_by(Member.callsign).limit(50).all()
        )
    return templates.TemplateResponse(request, "login.html", ctx)


RECRUIT_COLUMNS = [
    ("applied", "At the Gate", "New applications awaiting first contact."),
    ("interviewing", "In Interview", "Being spoken with by a recruiter."),
    ("decision", "Awaiting Decision", "Ready to be approved or denied."),
]


@app.get("/recruits", response_class=HTMLResponse)
def recruits(request: Request, session: Session = Depends(get_session)):
    """The recruitment pipeline — a board of applicants by stage."""
    ctx = _base_context(request, session)
    ctx["can_decide"] = auth.tier_at_least(ctx["user"], auth.TIER_RECRUITER)
    candidates = session.query(Candidacy).order_by(Candidacy.created_at.desc()).all()

    by_stage: dict[str, list] = {key: [] for key, _, _ in RECRUIT_COLUMNS}
    for c in candidates:
        try:
            c.parsed_answers = json.loads(c.answers) if c.answers else []
        except (ValueError, TypeError):
            c.parsed_answers = []
        # Anything with a missing/unknown stage falls back to the first column.
        by_stage.get(c.stage, by_stage["applied"]).append(c)

    columns = [
        {"key": key, "label": label, "hint": hint, "cards": by_stage.get(key, [])}
        for key, label, hint in RECRUIT_COLUMNS
    ]
    ctx["columns"] = columns
    ctx["total"] = len(candidates)
    ctx["stages"] = services.RECRUIT_STAGES
    ctx["metrics"] = services.recruitment_metrics(session)
    # Options for choosing a starting rank/company at approval time (recruiters).
    if ctx["can_decide"]:
        ctx["rank_names"] = [r.name for r in rank_utils.all_ranks(session)]
        ctx["company_names"] = [c.name for c in list_companies(session)]
        ctx["default_rank"] = rank_utils.default_rank_name(session)
        ctx["default_company"] = default_company_name(session)
    return templates.TemplateResponse(request, "recruits.html", ctx)


@app.post("/recruits/{discord_id}/stage")
def post_recruit_stage(
    request: Request,
    discord_id: int,
    csrf: str = Form(...),
    stage: str = Form(...),
    user: dict = Depends(auth.require_recruiter),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.set_candidate_stage, actor, discord_id, stage,
               redirect="/recruits")


@app.post("/recruits/{discord_id}/notes")
def post_recruit_notes(
    request: Request,
    discord_id: int,
    csrf: str = Form(...),
    notes: str = Form(""),
    user: dict = Depends(auth.require_recruiter),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.set_candidate_notes, actor, discord_id, notes,
               redirect="/recruits")


# --------------------------------------------------------------------------- #
# Write endpoints — each mutates the DB and queues the Discord side-effect.
# --------------------------------------------------------------------------- #

def _tenant_session(request: Request):
    """A session on the current request's unit database."""
    return sessionmaker_for(resolve_tenant(request).db_url)()


def _do(request: Request, csrf: str, fn, *args, redirect: str):
    """Run a service call with CSRF + error handling, then redirect back."""
    if not auth.verify_csrf(request, csrf):
        _flash(request, "Your session expired. Please try that again.", "error")
        return RedirectResponse(redirect, status_code=303)
    with _tenant_session(request) as session:
        try:
            message = fn(session, *args)
            _flash(request, message, "ok")
        except services.ActionError as exc:
            _flash(request, str(exc), "error")
    return RedirectResponse(redirect, status_code=303)


@app.post("/members/{discord_id}/rank")
def post_rank(
    request: Request,
    discord_id: int,
    csrf: str = Form(...),
    rank: str = Form(...),
    citation: str = Form(""),
    mode: str = Form("promote"),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    fn = services.set_rank if mode == "set" else services.change_rank
    return _do(request, csrf, fn, actor, discord_id, rank, citation,
               redirect=f"/dossier/{discord_id}")


@app.post("/members/{discord_id}/callsign")
def post_callsign(
    request: Request,
    discord_id: int,
    csrf: str = Form(...),
    callsign: str = Form(...),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.rename_member, actor, discord_id, callsign,
               redirect="/roster")


@app.post("/members/{discord_id}/company")
def post_company(
    request: Request,
    discord_id: int,
    csrf: str = Form(...),
    company: str = Form(...),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.assign_company, actor, discord_id, company,
               redirect=f"/dossier/{discord_id}")


@app.post("/members/{discord_id}/service-log")
def post_service_log(
    request: Request,
    discord_id: int,
    csrf: str = Form(...),
    entry: str = Form(...),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.service_log, actor, discord_id, entry,
               redirect=f"/dossier/{discord_id}")


@app.post("/members/{discord_id}/discipline")
def post_discipline(
    request: Request,
    discord_id: int,
    csrf: str = Form(...),
    record_type: str = Form(...),
    reason: str = Form(...),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.discipline, actor, discord_id, record_type, reason,
               redirect=f"/dossier/{discord_id}")


@app.post("/members/{discord_id}/discharge")
def post_discharge(
    request: Request,
    discord_id: int,
    csrf: str = Form(...),
    discharge_type: str = Form(...),
    reason: str = Form(...),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.discharge, actor, discord_id, discharge_type, reason,
               redirect=f"/dossier/{discord_id}")


@app.post("/members/{discord_id}/reinstate")
def post_reinstate(
    request: Request,
    discord_id: int,
    csrf: str = Form(...),
    reason: str = Form(""),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.reinstate, actor, discord_id, reason,
               redirect=f"/dossier/{discord_id}")


@app.post("/members/{discord_id}/loa")
def post_loa(
    request: Request,
    discord_id: int,
    csrf: str = Form(...),
    days: int = Form(...),
    reason: str = Form(""),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.loa, actor, discord_id, days, reason,
               redirect=f"/dossier/{discord_id}")


@app.post("/members/{discord_id}/loa-end")
def post_loa_end(
    request: Request,
    discord_id: int,
    csrf: str = Form(...),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.loa_end, actor, discord_id,
               redirect=f"/dossier/{discord_id}")


def _self_action(request: Request, csrf: str, fn, *args, redirect: str):
    """Run a service call on behalf of the signed-in member (self-service)."""
    user = auth.effective_user(request, resolve_tenant(request).slug)
    if not user:
        raise auth.NotAuthenticated()
    if not auth.verify_csrf(request, csrf):
        _flash(request, "Your session expired. Please try that again.", "error")
        return RedirectResponse(redirect, status_code=303)
    actor = {"id": user["id"], "name": user["name"]}
    with _tenant_session(request) as session:
        try:
            _flash(request, fn(session, actor, int(user["id"]), *args), "ok")
        except services.ActionError as exc:
            _flash(request, str(exc), "error")
    return RedirectResponse(redirect, status_code=303)


@app.post("/my-record/profile")
def post_profile(
    request: Request,
    csrf: str = Form(...),
    timezone: str = Form(""),
    ingame_name: str = Form(""),
    availability: list[str] = Form(default=[]),
    bio: str = Form(""),
    reminders_opt_out: bool = Form(False),
):
    days = ",".join(availability)
    return _self_action(request, csrf, services.update_profile,
                        timezone, ingame_name, days, bio, reminders_opt_out,
                        redirect="/my-record")


@app.post("/my-record/request-loa")
def post_request_loa(
    request: Request,
    csrf: str = Form(...),
    days: int = Form(...),
    reason: str = Form(""),
):
    return _self_action(request, csrf, services.request_loa, days, reason,
                        redirect="/my-record")


@app.post("/members/{discord_id}/loa-request/approve")
def post_loa_request_approve(
    request: Request,
    discord_id: int,
    csrf: str = Form(...),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.approve_loa_request, actor, discord_id,
               redirect="/leave")


@app.post("/members/{discord_id}/loa-request/deny")
def post_loa_request_deny(
    request: Request,
    discord_id: int,
    csrf: str = Form(...),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.deny_loa_request, actor, discord_id,
               redirect="/leave")


@app.get("/leave", response_class=HTMLResponse)
def leave_board(request: Request, session: Session = Depends(get_session),
                user: dict = Depends(auth.require_officer)):
    """The leave board: members currently on furlough, plus pending self-requests."""
    ctx = _base_context(request, session)
    on_leave = (
        session.query(Member)
        .filter(Member.status == "loa")
        .order_by(Member.loa_until)
        .all()
    )
    pending = (
        session.query(Member)
        .filter(Member.loa_requested_until.isnot(None), Member.status != "loa")
        .order_by(Member.loa_requested_until)
        .all()
    )
    ctx.update(on_leave=on_leave, pending=pending, now=datetime.utcnow())
    return templates.TemplateResponse(request, "leave.html", ctx)


@app.post("/muster-calls/{event_id}/after-action")
def post_after_action(
    request: Request,
    event_id: int,
    csrf: str = Form(...),
    outcome: str = Form(""),
    notes: str = Form(""),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.record_after_action, actor, event_id, outcome, notes,
               redirect=f"/muster-calls/{event_id}")


@app.post("/recruits/{discord_id}/approve")
def post_approve(
    request: Request,
    discord_id: int,
    csrf: str = Form(...),
    rank: str = Form(""),
    company: str = Form(""),
    user: dict = Depends(auth.require_recruiter),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.approve_candidate, actor, discord_id,
               rank or None, company or None, redirect="/recruits")


@app.post("/recruits/{discord_id}/deny")
def post_deny(
    request: Request,
    discord_id: int,
    csrf: str = Form(...),
    user: dict = Depends(auth.require_recruiter),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.deny_candidate, actor, discord_id,
               redirect="/recruits")


# --- Events & attendance -------------------------------------------------- #
@app.post("/muster-calls/create")
async def post_create_event(
    request: Request,
    csrf: str = Form(...),
    name: str = Form(...),
    event_type: str = Form(...),
    date: str = Form(...),
    time: str = Form(...),
    tz_offset: str = Form("0"),
    repeat_weeks: str = Form("1"),
    lead_value: str = Form("0"),
    lead_unit: str = Form("days"),
    description: str = Form(""),
    color_hex: str = Form(""),
    image: UploadFile = File(None),
    user: dict = Depends(auth.require_officer),
):
    if not auth.verify_csrf(request, csrf):
        _flash(request, "Your session expired. Please try that again.", "error")
        return RedirectResponse("/muster-calls", status_code=303)
    try:
        uri = await _image_data_uri(image) if image and image.filename else None
    except ValueError as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse("/muster-calls", status_code=303)
    actor = {"id": user["id"], "name": user["name"]}
    with _tenant_session(request) as session:
        try:
            event_id = services.create_event(
                session, actor, name, event_type, f"{date} {time}",
                tz_offset=tz_offset, repeat_weeks=repeat_weeks,
                lead_value=lead_value, lead_unit=lead_unit,
                description=description, image=uri, color_hex=color_hex,
            )
            _flash(request, f"'{name}' announced.", "ok")
            return RedirectResponse(f"/muster-calls/{event_id}", status_code=303)
        except services.ActionError as exc:
            _flash(request, str(exc), "error")
    return RedirectResponse("/muster-calls", status_code=303)


@app.post("/muster-calls/{event_id}/rsvp")
def post_rsvp(
    request: Request,
    event_id: int,
    csrf: str = Form(...),
    status: str = Form(...),
    slot_id: str = Form(""),
    next: str = Form(""),
):
    # Return to wherever the RSVP was made from (Headquarters or the call page).
    dest = next if next.startswith("/") else f"/muster-calls/{event_id}"
    user = auth.effective_user(request, resolve_tenant(request).slug)
    if not user:
        raise auth.NotAuthenticated()
    if not auth.verify_csrf(request, csrf):
        _flash(request, "Your session expired. Please try that again.", "error")
        return RedirectResponse(dest, status_code=303)
    with _tenant_session(request) as session:
        try:
            sid = int(slot_id) if slot_id.strip() else None
            _flash(request, services.rsvp(session, event_id, int(user["id"]), status, sid), "ok")
        except services.ActionError as exc:
            _flash(request, str(exc), "error")
    return RedirectResponse(dest, status_code=303)


@app.post("/muster-calls/{event_id}/attendance")
def post_attendance(
    request: Request,
    event_id: int,
    csrf: str = Form(...),
    member_id: int = Form(...),
    status: str = Form(...),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.mark_attendance, actor, event_id, member_id, status,
               redirect=f"/muster-calls/{event_id}")


@app.post("/muster-calls/{event_id}/attendance/bulk")
async def post_bulk_attendance(
    request: Request,
    event_id: int,
    user: dict = Depends(auth.require_officer),
):
    """Record actual attendance for many members in one submission. Each member
    row posts a `status_<discord_id>` field; blanks are left untouched."""
    form = await request.form()
    if not auth.verify_csrf(request, form.get("csrf", "")):
        _flash(request, "Your session expired. Please try that again.", "error")
        return RedirectResponse(f"/muster-calls/{event_id}", status_code=303)
    statuses: dict[int, str] = {}
    for key, val in form.items():
        if key.startswith("status_") and val:
            try:
                statuses[int(key[len("status_"):])] = str(val)
            except ValueError:
                continue
    actor = {"id": user["id"], "name": user["name"]}
    with _tenant_session(request) as session:
        try:
            _flash(request, services.bulk_mark_attendance(session, actor, event_id, statuses), "ok")
        except services.ActionError as exc:
            _flash(request, str(exc), "error")
    return RedirectResponse(f"/muster-calls/{event_id}", status_code=303)


@app.post("/muster-calls/{event_id}/update")
async def post_update_event(
    request: Request,
    event_id: int,
    csrf: str = Form(...),
    name: str = Form(...),
    event_type: str = Form(...),
    date: str = Form(...),
    time: str = Form(...),
    tz_offset: str = Form("0"),
    description: str = Form(""),
    color_hex: str = Form(""),
    remove_image: str = Form(""),
    image: UploadFile = File(None),
    user: dict = Depends(auth.require_officer),
):
    try:
        uri = await _image_data_uri(image) if image and image.filename else None
    except ValueError as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse(f"/muster-calls/{event_id}", status_code=303)
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.update_event, actor, event_id, name, event_type,
               f"{date} {time}", tz_offset, description, color_hex, uri, bool(remove_image),
               redirect=f"/muster-calls/{event_id}")


@app.post("/muster-calls/{event_id}/delete")
def post_delete_event(
    request: Request,
    event_id: int,
    csrf: str = Form(...),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.delete_event, actor, event_id, redirect="/muster-calls")


@app.post("/muster-calls/{event_id}/slots/add")
def post_add_event_slot(
    request: Request,
    event_id: int,
    csrf: str = Form(...),
    name: str = Form(...),
    capacity: str = Form(""),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.add_event_slot, actor, event_id, name, capacity,
               redirect=f"/muster-calls/{event_id}")


@app.post("/muster-calls/{event_id}/slots/{slot_id}/update")
def post_update_event_slot(
    request: Request,
    event_id: int,
    slot_id: int,
    csrf: str = Form(...),
    name: str = Form(...),
    capacity: str = Form(""),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.update_event_slot, actor, slot_id, name, capacity,
               redirect=f"/muster-calls/{event_id}")


@app.post("/muster-calls/{event_id}/slots/{slot_id}/remove")
def post_remove_event_slot(
    request: Request,
    event_id: int,
    slot_id: int,
    csrf: str = Form(...),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.remove_event_slot, actor, slot_id,
               redirect=f"/muster-calls/{event_id}")


# --- Awards --------------------------------------------------------------- #
@app.post("/members/{discord_id}/award")
def post_award(
    request: Request,
    discord_id: int,
    csrf: str = Form(...),
    award_type_id: int = Form(...),
    notes: str = Form(""),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.grant_award, actor, discord_id, award_type_id, notes,
               redirect=f"/dossier/{discord_id}")


@app.post("/members/{discord_id}/award/{award_type_id}/revoke")
def post_award_revoke(
    request: Request,
    discord_id: int,
    award_type_id: int,
    csrf: str = Form(...),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.revoke_award, actor, discord_id, award_type_id,
               redirect=f"/dossier/{discord_id}")


@app.post("/honors/award-type")
async def post_award_type(
    request: Request,
    csrf: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    emoji: str = Form(""),
    image: UploadFile = File(None),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    try:
        uri = await _image_data_uri(image) if image and image.filename else None
    except ValueError as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse("/honors", status_code=303)
    return _do(request, csrf, services.create_award_type, actor, name, description, emoji, uri,
               redirect="/honors")


@app.post("/admin/awards/{award_type_id}/image")
async def post_award_image(
    request: Request,
    award_type_id: int,
    csrf: str = Form(...),
    remove: str = Form(""),
    image: UploadFile = File(None),
    user: dict = Depends(auth.require_officer),
):
    if remove:
        return _do(request, csrf, services.set_award_image, award_type_id, None,
                   redirect="/honors")
    if not (image and image.filename):
        _flash(request, "Choose an image to upload.", "error")
        return RedirectResponse("/honors", status_code=303)
    try:
        uri = await _image_data_uri(image)
    except ValueError as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse("/honors", status_code=303)
    return _do(request, csrf, services.set_award_image, award_type_id, uri, redirect="/honors")


# --- Admin: identity / roles / channels ----------------------------------- #
@app.post("/admin/identity")
def post_identity(
    request: Request,
    csrf: str = Form(...),
    regiment_name: str = Form(...),
    motto: str = Form(""),
    brand_color: str = Form(...),
    inactivity_days: int = Form(...),
    theme: str = Form(""),
    discord_invite: str = Form(""),
    unit_tag: str = Form(""),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.update_identity,
               regiment_name, motto, brand_color, inactivity_days, theme, discord_invite,
               unit_tag, redirect="/command-tent")


CREST_MAX_BYTES = 256 * 1024
CREST_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


async def _image_data_uri(upload) -> str:
    """Validate an uploaded image and return it as a data URI. Raises ValueError
    with a user-facing message on a bad type or oversize file."""
    if (upload.content_type or "") not in CREST_TYPES:
        raise ValueError("The image must be a PNG, JPEG, WebP, or GIF.")
    data = await upload.read()
    if len(data) > CREST_MAX_BYTES:
        raise ValueError("The image must be under 256 KB.")
    import base64
    return f"data:{upload.content_type};base64,{base64.b64encode(data).decode()}"


@app.post("/admin/crest")
async def post_crest(
    request: Request,
    csrf: str = Form(...),
    crest_file: UploadFile = File(...),
    user: dict = Depends(auth.require_admin),
):
    if not auth.verify_csrf(request, csrf):
        _flash(request, "Your session expired. Please try that again.", "error")
        return RedirectResponse("/command-tent", status_code=303)
    if (crest_file.content_type or "") not in CREST_TYPES:
        _flash(request, "The crest must be a PNG, JPEG, WebP, or GIF image.", "error")
        return RedirectResponse("/command-tent", status_code=303)
    data = await crest_file.read()
    if len(data) > CREST_MAX_BYTES:
        _flash(request, "The crest image must be under 256 KB.", "error")
        return RedirectResponse("/command-tent", status_code=303)
    import base64
    uri = f"data:{crest_file.content_type};base64,{base64.b64encode(data).decode()}"
    with _tenant_session(request) as session:
        get_config(session).crest = uri
        session.commit()
    _flash(request, "Crest updated.", "ok")
    return RedirectResponse("/command-tent", status_code=303)


@app.post("/admin/crest/remove")
def post_crest_remove(request: Request, csrf: str = Form(...),
                      user: dict = Depends(auth.require_admin)):
    if not auth.verify_csrf(request, csrf):
        _flash(request, "Your session expired. Please try that again.", "error")
        return RedirectResponse("/command-tent", status_code=303)
    with _tenant_session(request) as session:
        get_config(session).crest = None
        session.commit()
    _flash(request, "Crest removed.", "ok")
    return RedirectResponse("/command-tent", status_code=303)


@app.post("/admin/terminology")
async def post_terminology(request: Request, user: dict = Depends(auth.require_admin)):
    form = await request.form()
    if not auth.verify_csrf(request, form.get("csrf", "")):
        _flash(request, "Your session expired. Please try that again.", "error")
        return RedirectResponse("/command-tent", status_code=303)
    submitted = {k: str(v) for k, v in form.items()}
    with _tenant_session(request) as session:
        try:
            _flash(request, services.set_terminology(session, submitted), "ok")
        except services.ActionError as exc:
            _flash(request, str(exc), "error")
    return RedirectResponse("/command-tent", status_code=303)


@app.post("/admin/terminology/reset")
def post_terminology_reset(
    request: Request,
    csrf: str = Form(...),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.reset_terminology, redirect="/command-tent")


async def _do_form(request: Request, fn, keys, redirect: str):
    """For the role/channel forms, which submit a dynamic set of fields."""
    form = await request.form()
    if not auth.verify_csrf(request, form.get("csrf", "")):
        _flash(request, "Your session expired. Please try that again.", "error")
        return RedirectResponse(redirect, status_code=303)
    values = {k: form.get(k, "") for k in keys}
    with _tenant_session(request) as session:
        try:
            _flash(request, fn(session, values), "ok")
        except services.ActionError as exc:
            _flash(request, str(exc), "error")
    return RedirectResponse(redirect, status_code=303)


@app.post("/admin/roles")
async def post_roles(request: Request, user: dict = Depends(auth.require_admin)):
    return await _do_form(request, services.set_roles, ROLE_KEYS, "/command-tent")


@app.post("/admin/channels")
async def post_channels(request: Request, user: dict = Depends(auth.require_admin)):
    return await _do_form(request, services.set_channels, CHANNEL_KEYS, "/command-tent")


@app.post("/admin/digest")
def post_digest(
    request: Request,
    csrf: str = Form(...),
    enabled: str = Form(""),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.set_digest_enabled, bool(enabled),
               redirect="/command-tent")


# --- Admin: ranks --------------------------------------------------------- #
@app.post("/admin/ranks/add")
async def post_rank_add(
    request: Request,
    csrf: str = Form(...),
    name: str = Form(...),
    abbreviation: str = Form(...),
    tier: str = Form(""),
    role_id: str = Form(""),
    image: UploadFile = File(None),
    user: dict = Depends(auth.require_admin),
):
    try:
        uri = await _image_data_uri(image) if image and image.filename else None
    except ValueError as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse("/command-tent", status_code=303)
    return _do(request, csrf, services.rank_add, name, abbreviation, tier, role_id, uri,
               redirect="/command-tent")


@app.post("/admin/ranks/{rank_id}/update")
async def post_rank_update(
    request: Request,
    rank_id: int,
    csrf: str = Form(...),
    name: str = Form(...),
    abbreviation: str = Form(...),
    tier: str = Form(""),
    role_id: str = Form(""),
    remove_image: str = Form(""),
    image: UploadFile = File(None),
    user: dict = Depends(auth.require_admin),
):
    try:
        uri = await _image_data_uri(image) if image and image.filename else None
    except ValueError as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse("/command-tent", status_code=303)
    return _do(request, csrf, services.rank_update, rank_id, name, abbreviation, tier, role_id,
               uri, bool(remove_image), redirect="/command-tent")


@app.post("/admin/ranks/{rank_id}/move")
def post_rank_move(
    request: Request,
    rank_id: int,
    csrf: str = Form(...),
    direction: str = Form(...),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.rank_move, rank_id, direction, redirect="/command-tent")


@app.post("/admin/ranks/{rank_id}/remove")
def post_rank_remove(
    request: Request,
    rank_id: int,
    csrf: str = Form(...),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.rank_remove, rank_id, redirect="/command-tent")


# --- Admin: companies ----------------------------------------------------- #
@app.post("/admin/companies/add")
def post_company_add(
    request: Request,
    csrf: str = Form(...),
    name: str = Form(...),
    role_id: str = Form(""),
    is_default: str = Form(""),
    tag: str = Form(""),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.company_add, name, role_id, bool(is_default), tag,
               redirect="/command-tent")


@app.post("/admin/companies/{company_id}/update")
def post_company_update(
    request: Request,
    company_id: int,
    csrf: str = Form(...),
    name: str = Form(...),
    role_id: str = Form(""),
    tag: str = Form(""),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.company_update, company_id, name, role_id, tag,
               redirect="/command-tent")


@app.post("/admin/companies/{company_id}/default")
def post_company_default(
    request: Request,
    company_id: int,
    csrf: str = Form(...),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.company_set_default, company_id, redirect="/command-tent")


@app.post("/admin/companies/{company_id}/remove")
def post_company_remove(
    request: Request,
    company_id: int,
    csrf: str = Form(...),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.company_remove, company_id, redirect="/command-tent")


# --- Admin: secondary assignments ---------------------------------------- #
@app.post("/admin/assignments/add")
def post_assignment_add(
    request: Request,
    csrf: str = Form(...),
    name: str = Form(...),
    role_id: str = Form(""),
    description: str = Form(""),
    is_leadership: str = Form(""),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.assignment_add, name, role_id, description,
               bool(is_leadership), redirect="/command-tent")


@app.post("/admin/assignments/{assignment_id}/update")
def post_assignment_update(
    request: Request,
    assignment_id: int,
    csrf: str = Form(...),
    role_id: str = Form(""),
    description: str = Form(""),
    is_leadership: str = Form(""),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.assignment_update, assignment_id, role_id,
               description, bool(is_leadership), redirect="/command-tent")


@app.post("/admin/assignments/{assignment_id}/remove")
def post_assignment_remove(
    request: Request,
    assignment_id: int,
    csrf: str = Form(...),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.assignment_remove, assignment_id, redirect="/command-tent")


@app.post("/members/{discord_id}/assign")
def post_member_assign(
    request: Request,
    discord_id: int,
    csrf: str = Form(...),
    assignment_id: int = Form(...),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.assign_member, actor, discord_id, assignment_id,
               redirect=f"/dossier/{discord_id}")


@app.post("/members/{discord_id}/unassign")
def post_member_unassign(
    request: Request,
    discord_id: int,
    csrf: str = Form(...),
    assignment_id: int = Form(...),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.unassign_member, actor, discord_id, assignment_id,
               redirect=f"/dossier/{discord_id}")


@app.post("/admin/questions/add")
def post_question_add(
    request: Request,
    csrf: str = Form(...),
    prompt: str = Form(...),
    required: str = Form(""),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.question_add, prompt, bool(required),
               redirect="/command-tent")


@app.post("/admin/questions/{question_id}/move")
def post_question_move(
    request: Request,
    question_id: int,
    csrf: str = Form(...),
    direction: str = Form(...),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.question_move, question_id, direction,
               redirect="/command-tent")


@app.post("/admin/questions/{question_id}/toggle")
def post_question_toggle(
    request: Request,
    question_id: int,
    csrf: str = Form(...),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.question_toggle, question_id, redirect="/command-tent")


@app.post("/admin/questions/{question_id}/remove")
def post_question_remove(
    request: Request,
    question_id: int,
    csrf: str = Form(...),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.question_remove, question_id, redirect="/command-tent")


# Bot invite permissions: manage roles/nicknames/threads, send/embed, history.
_BOT_PERMS = (
    1024 | 2048 | 8192 | 16384 | 65536 | 134217728 | 268435456
    | 17179869184 | 34359738368 | 274877906944
)


def _bot_invite_url() -> str | None:
    cid = os.getenv("DISCORD_CLIENT_ID")
    if not cid:
        return None
    return (
        f"https://discord.com/oauth2/authorize?client_id={cid}"
        f"&permissions={_BOT_PERMS}&scope=bot%20applications.commands"
    )


def _can_register(user: dict | None) -> bool:
    """Who may create a unit. Secure by default, with two independent levers:

    * PLATFORM_OPEN_REGISTRATION — when enabled, any signed-in user may create
      a unit. This takes precedence, so it works even alongside an admin
      allowlist (admins keep their extra powers; the door is simply open).
    * PLATFORM_ADMIN_IDS — when set and open registration is off, only those
      admins may create units (a curated platform).

    With neither set, registration is closed."""
    if not user:
        return False
    if os.getenv("PLATFORM_OPEN_REGISTRATION", "").lower() in ("1", "true", "yes"):
        return True
    allow = os.getenv("PLATFORM_ADMIN_IDS", "").replace(" ", "")
    if allow:
        return str(user.get("id")) in {a for a in allow.split(",") if a}
    return False


def _is_platform_admin(user: dict | None) -> bool:
    """Deleting units is destructive, so it always requires an explicit
    PLATFORM_ADMIN_IDS allowlist (never available under open registration)."""
    if not user:
        return False
    allow = os.getenv("PLATFORM_ADMIN_IDS", "").replace(" ", "")
    if not allow:
        return False
    return str(user.get("id")) in {a for a in allow.split(",") if a}


def _platform_dashboard() -> list[dict]:
    """Per-unit health across the whole platform, for the platform-admin
    dashboard. Reads each unit's own database; an unreadable unit is reported
    rather than skipped, so a broken unit is visible instead of silent."""
    base_domain = os.getenv("PLATFORM_BASE_DOMAIN")
    with registry_session() as rs:
        rows = [
            {"slug": t.slug, "name": t.name, "is_default": t.is_default,
             "listed": t.listed, "recruiting": t.recruiting_open,
             "guild_id": t.discord_guild_id, "db_url": t.db_url,
             "created_at": t.created_at}
            for t in all_tenants(rs)
        ]
    units = []
    for r in rows:
        info = {"slug": r["slug"], "name": r["name"], "is_default": r["is_default"],
                "listed": r["listed"], "recruiting": r["recruiting"],
                "linked": bool(r["guild_id"]), "created_at": r["created_at"],
                "url": f"https://{r['slug']}.{base_domain}/" if base_domain else "/",
                "members": None, "pending": None, "last_active": None, "ok": True}
        try:
            with sessionmaker_for(r["db_url"])() as s:
                info["members"] = s.query(Member).filter(Member.status == "active").count()
                info["pending"] = s.query(Candidacy).count()
                latest = (
                    s.query(Member.last_active_date)
                    .order_by(Member.last_active_date.desc())
                    .first()
                )
                info["last_active"] = latest[0] if latest else None
        except Exception:
            info["ok"] = False
        units.append(info)
    units.sort(key=lambda u: (u["is_default"], u["name"].lower()))
    return units


@app.get("/admin/platform", response_class=HTMLResponse)
def platform_admin(request: Request):
    """A cross-unit control panel for the platform's operators."""
    if not os.getenv("PLATFORM_BASE_DOMAIN"):
        raise TenantNotFound(None)
    user = auth.current_user(request)
    if not user:
        raise auth.NotAuthenticated()
    if not _is_platform_admin(user):
        raise auth.NotAuthorized(auth.TIER_ADMIN)
    units = _platform_dashboard()
    ctx = {
        "request": request,
        "user": user,
        "csrf_token": auth.get_csrf_token(request),
        "flash": request.session.pop("flash", []),
        "base_domain": os.getenv("PLATFORM_BASE_DOMAIN"),
        "units": units,
        "now": datetime.utcnow(),
        "totals": {
            "units": len(units),
            "members": sum(u["members"] or 0 for u in units),
            "pending": sum(u["pending"] or 0 for u in units),
            "unlinked": sum(1 for u in units if not u["linked"]),
        },
    }
    return templates.TemplateResponse(request, "platform_admin.html", ctx)


# --- Alliances ------------------------------------------------------------ #
@app.get("/alliance/{slug}", response_class=HTMLResponse)
def alliance_page(request: Request, slug: str):
    """Public page for an alliance: its member units and combined strength."""
    viewer = auth.current_user(request)
    detail = alliance_mod.alliance_detail(slug)
    ctx = {
        "request": request,
        "user": viewer,
        "flash": request.session.pop("flash", []),
    }
    if detail is None:
        return templates.TemplateResponse(request, "alliance_not_found.html", ctx,
                                          status_code=404)
    base = os.getenv("PLATFORM_BASE_DOMAIN")
    total = 0
    with registry_session() as rs:
        for m in detail["members"]:
            t = tenant_by_slug(rs, m["slug"])
            if t is None:
                continue
            info = _unit_directory_info(t.db_url)
            m["members"] = info.get("members")
            m["crest"] = info.get("crest")
            m["brand_color"] = brand_hex(t.brand_color)
            m["url"] = f"https://{m['slug']}.{base}/" if base else "/"
            total += info.get("members") or 0
    detail["strength"] = total
    ctx["alliance"] = detail
    ctx["csrf_token"] = auth.get_csrf_token(request)
    ctx["events"] = alliance_events.upcoming_events(
        detail["id"], viewer_id=int(viewer["id"]) if viewer else None)
    member_slugs = {m["slug"] for m in detail["members"]}
    ctx["can_rsvp"] = bool(viewer and (set((viewer.get("tiers") or {}).keys()) & member_slugs))
    ctx["rsvp_choices"] = [("accepted", "Answer the call"), ("tentative", "Tentative"),
                           ("declined", "Can't attend")]
    return templates.TemplateResponse(request, "alliance.html", ctx)


# --- Player lookup -------------------------------------------------------- #
@app.get("/players", response_class=HTMLResponse)
def players(request: Request, q: str = ""):
    """Search members across every unit by name (or jump straight to a Discord
    ID), each linking to their cross-unit service record."""
    q = q.strip()
    # A pasted Discord ID goes straight to that record.
    if q.isdigit() and len(q) >= 5:
        return RedirectResponse(f"/u/{q}", status_code=303)
    results = profiles.search_players(q) if len(q) >= 2 else []
    ctx = {
        "request": request,
        "user": auth.current_user(request),
        "flash": request.session.pop("flash", []),
        "q": q,
        "results": results,
        "searched": len(q) >= 2,
    }
    return templates.TemplateResponse(request, "players.html", ctx)


# --- Cross-unit service record ------------------------------------------- #
@app.get("/me")
def my_service_record(request: Request):
    """Shortcut to the signed-in member's own cross-unit service record."""
    user = auth.current_user(request)
    if not user:
        return RedirectResponse("/auth/discord/login", status_code=303)
    return RedirectResponse(f"/u/{user['id']}", status_code=303)


@app.get("/u/{discord_id}", response_class=HTMLResponse)
def service_record(request: Request, discord_id: int):
    """A member's service record, aggregated across every unit. Every record is
    public; the viewer's level (owner / recruiter / public) only controls how
    much detail is shown."""
    viewer = auth.current_user(request)
    level = profiles.viewer_level(viewer, discord_id)
    ctx = {
        "request": request,
        "user": viewer,
        "csrf_token": auth.get_csrf_token(request),
        "flash": request.session.pop("flash", []),
        "level": level,
        "is_owner": level == profiles.LEVEL_OWNER,
        "discord_id": discord_id,
    }
    record = profiles.build_service_record(discord_id, level)
    ctx["record"] = record
    if not record["found"]:
        return templates.TemplateResponse(request, "profile_private.html", ctx,
                                          status_code=404)
    return templates.TemplateResponse(request, "profile.html", ctx)


def _broadcast_to_all_units(title: str, body: str, actor_id: int | None) -> int:
    """Drop a platform-update action on every unit's queue. The bot's bridge
    drains each unit's queue against its own guild and posts the update to that
    unit's admin-log channel. Returns the number of units the update reached."""
    with registry_session() as rs:
        db_urls = [t.db_url for t in all_tenants(rs)]
    sent = 0
    for db_url in db_urls:
        try:
            with sessionmaker_for(db_url)() as s:
                queue.enqueue(s, queue.PLATFORM_BROADCAST,
                              {"title": title, "body": body}, actor_id=actor_id)
                s.commit()
            sent += 1
        except Exception:  # noqa: BLE001 -- one unreadable unit shouldn't stop the rest
            pass
    return sent


@app.post("/admin/platform/broadcast")
def platform_broadcast(
    request: Request,
    csrf: str = Form(...),
    title: str = Form(""),
    body: str = Form(...),
):
    """Send a platform update to every unit's admin-log channel (platform-admin
    only). Handy for announcing site changes to unit operators."""
    if not os.getenv("PLATFORM_BASE_DOMAIN"):
        raise TenantNotFound(None)
    user = auth.current_user(request)
    if not user:
        raise auth.NotAuthenticated()
    if not _is_platform_admin(user):
        raise auth.NotAuthorized(auth.TIER_ADMIN)
    if not auth.verify_csrf(request, csrf):
        _flash(request, "Your session expired. Please try that again.", "error")
        return RedirectResponse("/admin/platform", status_code=303)
    body = body.strip()
    if not body:
        _flash(request, "An update needs a message.", "error")
        return RedirectResponse("/admin/platform", status_code=303)
    count = _broadcast_to_all_units(title.strip(), body, user.get("id"))
    _flash(request, f"Update queued for {count} unit{'s' if count != 1 else ''}. "
                    "The bot posts it to each admin-log channel shortly.", "ok")
    return RedirectResponse("/admin/platform", status_code=303)


# --- Self-serve: register a unit ------------------------------------------ #
@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    if not os.getenv("PLATFORM_BASE_DOMAIN"):
        raise TenantNotFound(None)
    user = auth.current_user(request)
    if not user:
        raise auth.NotAuthenticated()
    is_admin = _is_platform_admin(user)
    existing = []
    if is_admin:
        with registry_session() as rs:
            existing = [
                {"slug": t.slug, "name": t.name, "is_default": t.is_default}
                for t in all_tenants(rs) if not t.is_default
            ]
            existing.sort(key=lambda u: u["slug"])
    ctx = {
        "request": request,
        "user": user,
        "csrf_token": auth.get_csrf_token(request),
        "flash": request.session.pop("flash", []),
        "base_domain": os.getenv("PLATFORM_BASE_DOMAIN"),
        "can_register": _can_register(user),
        "is_platform_admin": is_admin,
        "existing_units": existing,
        "now": datetime.utcnow(),
    }
    return templates.TemplateResponse(request, "register.html", ctx)


@app.get("/register/check")
def register_check(request: Request, slug: str = ""):
    """Live 'is this handle free?' check for the register form."""
    from fastapi.responses import JSONResponse

    from tenancy.provision import slug_available

    if not auth.current_user(request):
        raise auth.NotAuthenticated()
    available, reason = slug_available(slug)
    return JSONResponse({"available": available, "reason": reason})


@app.post("/admin/units/{slug}/delete")
def post_delete_unit(request: Request, slug: str, csrf: str = Form(...)):
    user = auth.current_user(request)
    if not user:
        raise auth.NotAuthenticated()
    if not _is_platform_admin(user):
        raise auth.NotAuthorized(auth.TIER_ADMIN)
    if not auth.verify_csrf(request, csrf):
        _flash(request, "Your session expired. Please try that again.", "error")
        return RedirectResponse("/register", status_code=303)
    from tenancy.provision import ProvisionError, delete_unit

    try:
        result = delete_unit(slug)
        _flash(request, f"Removed unit '{result['name']}'. Its data was archived.", "ok")
    except ProvisionError as exc:
        _flash(request, str(exc), "error")
    return RedirectResponse("/register", status_code=303)


@app.post("/register")
def register_submit(
    request: Request,
    csrf: str = Form(...),
    slug: str = Form(...),
    name: str = Form(...),
    guild_id: str = Form(""),
    motto: str = Form(""),
    blurb: str = Form(""),
):
    user = auth.current_user(request)
    if not user:
        raise auth.NotAuthenticated()
    if not _can_register(user):
        raise auth.NotAuthorized(auth.TIER_ADMIN)
    if not auth.verify_csrf(request, csrf):
        _flash(request, "Your session expired. Please try that again.", "error")
        return RedirectResponse("/register", status_code=303)

    from tenancy.provision import ProvisionError, create_unit

    gid = None
    if guild_id.strip():
        try:
            gid = int(guild_id.strip())
        except ValueError:
            _flash(request, "The Discord server ID must be all digits.", "error")
            return RedirectResponse("/register", status_code=303)
    try:
        create_unit(slug, name, guild_id=gid, motto=motto, blurb=blurb)
    except ProvisionError as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse("/register", status_code=303)

    from tenancy.provision import normalize_slug

    request.session["registered_slug"] = normalize_slug(slug)
    return RedirectResponse("/registered", status_code=303)


@app.get("/registered", response_class=HTMLResponse)
def registered(request: Request):
    slug = request.session.pop("registered_slug", None)
    if not slug:
        return RedirectResponse("/", status_code=303)
    base = os.getenv("PLATFORM_BASE_DOMAIN")
    ctx = {
        "request": request,
        "slug": slug,
        "portal_url": f"https://{slug}.{base}/",
        "invite_url": _bot_invite_url(),
        "user": auth.current_user(request),
        "now": datetime.utcnow(),
    }
    return templates.TemplateResponse(request, "registered.html", ctx)


@app.get("/tls-allow")
def tls_allow(domain: str = ""):
    """Caddy on-demand TLS ask endpoint: 200 to issue a cert, 404 to refuse."""
    from fastapi import Response

    base = os.getenv("PLATFORM_BASE_DOMAIN")
    d = (domain or "").strip().lower().rstrip(".")
    if not base or d in (base, f"www.{base}"):
        return Response(status_code=200)
    slug = slug_from_host(d)
    if slug and tenant_by_slug_ctx(slug):
        return Response(status_code=200)
    return Response(status_code=404)


# --- Public directory: apply to a unit ------------------------------------ #
@app.post("/apply/{slug}")
def post_apply(request: Request, slug: str, csrf: str = Form(...)):
    user = auth.current_user(request)
    if not user:
        raise auth.NotAuthenticated()
    if not auth.verify_csrf(request, csrf):
        _flash(request, "Your session expired. Please try that again.", "error")
        return RedirectResponse("/", status_code=303)
    ctx = tenant_by_slug_ctx(slug)
    if ctx is None:
        raise TenantNotFound(slug)
    with sessionmaker_for(ctx.db_url)() as session:
        try:
            msg = services.submit_application(
                session, int(user["id"]), user.get("name", "Applicant")
            )
            _flash(request, msg, "ok")
        except services.ActionError as exc:
            _flash(request, str(exc), "error")
    return RedirectResponse("/", status_code=303)


# --- Admin: this unit's public listing (registry) ------------------------- #
@app.post("/admin/listing")
def post_listing(
    request: Request,
    csrf: str = Form(...),
    name: str = Form(...),
    motto: str = Form(""),
    blurb: str = Form(""),
    game: str = Form(""),
    tags: str = Form(""),
    recruiting_open: str = Form(""),
    listed: str = Form(""),
    user: dict = Depends(auth.require_admin),
):
    if not auth.verify_csrf(request, csrf):
        _flash(request, "Your session expired. Please try that again.", "error")
        return RedirectResponse("/command-tent", status_code=303)
    tenant = resolve_tenant(request)
    name = name.strip()
    if not name:
        _flash(request, "The public name can't be empty.", "error")
        return RedirectResponse("/command-tent", status_code=303)
    # Normalise tags to a clean comma-separated list.
    clean_tags = ", ".join(s.strip() for s in tags.split(",") if s.strip()) or None
    with registry_session() as rs:
        row = tenant_by_slug(rs, tenant.slug)
        if row is not None:
            row.name = name
            row.motto = motto.strip() or None
            row.blurb = blurb.strip() or None
            row.game = game.strip() or None
            row.tags = clean_tags
            row.recruiting_open = bool(recruiting_open)
            row.listed = bool(listed)
            rs.commit()
            _flash(request, "Public listing updated.", "ok")
        else:
            _flash(request, "This unit isn't in the registry.", "error")
    return RedirectResponse("/command-tent", status_code=303)


@app.post("/admin/slug")
def post_slug(
    request: Request,
    csrf: str = Form(...),
    slug: str = Form(...),
    user: dict = Depends(auth.require_admin),
):
    if not auth.verify_csrf(request, csrf):
        _flash(request, "Your session expired. Please try that again.", "error")
        return RedirectResponse("/command-tent", status_code=303)
    base_domain = os.getenv("PLATFORM_BASE_DOMAIN")
    if not base_domain:
        _flash(request, "Subdomains aren't used in single-unit mode.", "error")
        return RedirectResponse("/command-tent", status_code=303)
    from tenancy.provision import ProvisionError, rename_unit_slug
    tenant = resolve_tenant(request)
    old_slug = tenant.slug
    try:
        new_slug = rename_unit_slug(old_slug, slug)
    except ProvisionError as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse("/command-tent", status_code=303)
    # The session's per-unit tier map is keyed by slug, so without this the
    # acting admin would look like a visitor on their own unit's new address
    # until they signed in again.
    sess_user = request.session.get("user")
    if sess_user:
        tiers = sess_user.get("tiers")
        if tiers and old_slug in tiers:
            tiers[new_slug] = tiers.pop(old_slug)
        if sess_user.get("tenant") == old_slug:
            sess_user["tenant"] = new_slug
    # The current host no longer resolves to this unit once its slug has
    # changed, so send the admin to the new subdomain rather than a relative
    # path (which would 404 against the old, now-unclaimed handle).
    _flash(request, f"Web address changed to {new_slug}.{base_domain} — update any saved links.", "ok")
    return RedirectResponse(f"https://{new_slug}.{base_domain}/command-tent", status_code=303)


@app.post("/admin/import-roster")
def post_import_roster(
    request: Request,
    csrf: str = Form(...),
    role_id: str = Form(""),
    user: dict = Depends(auth.require_admin),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.import_roster, actor, role_id,
               redirect="/command-tent")


@app.post("/admin/discord-link")
def post_discord_link(
    request: Request,
    csrf: str = Form(...),
    guild_id: str = Form(""),
    user: dict = Depends(auth.require_admin),
):
    """Re-point this unit at a different Discord server (or unlink it).
    Changing this remaps which server the bot manages for the unit, so it
    validates the id and rejects a server already claimed by another unit."""
    if not auth.verify_csrf(request, csrf):
        _flash(request, "Your session expired. Please try that again.", "error")
        return RedirectResponse("/command-tent", status_code=303)

    gid = None
    raw = guild_id.strip()
    if raw:
        if not raw.isdigit():
            _flash(request, "The Discord server ID must be all digits.", "error")
            return RedirectResponse("/command-tent", status_code=303)
        gid = int(raw)

    tenant = resolve_tenant(request)
    from tenancy.resolve import tenant_by_guild
    from tenancy.routing import invalidate

    with registry_session() as rs:
        row = tenant_by_slug(rs, tenant.slug)
        if row is None:
            _flash(request, "This unit isn't in the registry.", "error")
            return RedirectResponse("/command-tent", status_code=303)
        if gid is not None:
            other = tenant_by_guild(rs, gid)
            if other is not None and other.slug != tenant.slug:
                _flash(request, "That Discord server is already linked to another unit.", "error")
                return RedirectResponse("/command-tent", status_code=303)
        row.discord_guild_id = gid
        rs.commit()

    invalidate()  # drop the bot's cached guild → database mapping
    if gid is None:
        _flash(request, "Discord server unlinked. The bot no longer manages a server for this unit.", "ok")
    else:
        _flash(request, "Discord server updated. Invite the bot to that server (or "
                        "restart it) so it syncs commands there.", "ok")
    return RedirectResponse("/command-tent", status_code=303)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
