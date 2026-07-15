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

import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from db.models import AwardType, Candidacy, Company, Event, Member, Rank
from tenancy.registry import registry_session
from tenancy.resolve import all_tenants, listed_tenants, slug_from_host, tenant_by_slug
from tenancy.units import sessionmaker_for
from utils import ranks as rank_utils
from utils.settings import CHANNEL_KEYS, ROLE_KEYS, get_config, list_companies
from web import auth, services
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


templates.env.filters["status_label"] = status_label
templates.env.filters["record_label"] = record_label
templates.env.filters["attendance_label"] = attendance_label
templates.env.filters["fmt_date"] = fmt_date


def get_session(tenant: TenantCtx = Depends(get_tenant)):
    """A database session scoped to the unit resolved for this request."""
    session = sessionmaker_for(tenant.db_url)()
    try:
        yield session
    finally:
        session.close()


def _base_context(request: Request, session: Session) -> dict:
    """Context every page needs: regiment identity for the banner + nav,
    plus the signed-in officer, a CSRF token, and any flashed messages."""
    cfg = get_config(session)
    flash = request.session.pop("flash", [])
    pending_recruits = session.query(Candidacy).count()
    tenant = resolve_tenant(request)
    return {
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
    }


def directory_mode(request: Request) -> bool:
    """True when this request should show the public directory: platform mode
    is on and the Host is the apex (not a unit subdomain)."""
    if not os.getenv("PLATFORM_BASE_DOMAIN"):
        return False
    return slug_from_host(request.headers.get("host", "")) is None


def _render_directory(request: Request):
    base_domain = os.getenv("PLATFORM_BASE_DOMAIN")
    with registry_session() as rs:
        units = [
            {
                "slug": t.slug,
                "name": t.name,
                "motto": t.motto,
                "blurb": t.blurb,
                "brand_color": brand_hex(t.brand_color),
                "recruiting": t.recruiting_open,
                "url": f"https://{t.slug}.{base_domain}/",
            }
            for t in listed_tenants(rs)
        ]
    ctx = {
        "request": request,
        "units": units,
        "base_domain": base_domain,
        "user": auth.current_user(request),
        "csrf_token": auth.get_csrf_token(request),
        "flash": request.session.pop("flash", []),
        "now": datetime.utcnow(),
    }
    return templates.TemplateResponse(request, "directory.html", ctx)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@app.get("/", response_class=HTMLResponse)
def headquarters(request: Request, session: Session = Depends(get_session)):
    if directory_mode(request):
        return _render_directory(request)

    ctx = _base_context(request, session)

    counts = {
        "active": session.query(Member).filter(Member.status == "active").count(),
        "loa": session.query(Member).filter(Member.status == "loa").count(),
        "inactive": session.query(Member).filter(Member.status == "inactive").count(),
        "discharged": session.query(Member).filter(Member.status == "discharged").count(),
    }
    counts["enrolled"] = counts["active"] + counts["loa"] + counts["inactive"]

    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    recent_enlistments = (
        session.query(Member)
        .filter(Member.joined_date >= thirty_days_ago)
        .order_by(Member.joined_date.desc())
        .limit(6)
        .all()
    )

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

    ctx.update(
        counts=counts,
        recent_enlistments=recent_enlistments,
        upcoming=upcoming,
        pending=pending,
        company_count=len(companies),
        rank_count=len(ranks),
    )
    return templates.TemplateResponse(request, "headquarters.html", ctx)


@app.get("/roster", response_class=HTMLResponse)
def roster(request: Request, session: Session = Depends(get_session)):
    ctx = _base_context(request, session)

    members = session.query(Member).filter(Member.status == "active").all()
    rank_order = {name: i for i, name in enumerate(rank_utils.rank_names(session))}

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

    ctx.update(companies=companies, active_total=len(members))
    return templates.TemplateResponse(request, "roster.html", ctx)


@app.get("/muster", response_class=HTMLResponse)
def muster(request: Request, session: Session = Depends(get_session)):
    """The full muster roll — every enrolled soul, whatever their standing."""
    ctx = _base_context(request, session)

    rank_order = {name: i for i, name in enumerate(rank_utils.rank_names(session))}
    status_rank = {"active": 0, "loa": 1, "inactive": 2, "discharged": 3}
    members = session.query(Member).all()
    members.sort(
        key=lambda m: (
            status_rank.get(m.status, 9),
            -rank_order.get(m.rank, -1),
            m.callsign.lower(),
        )
    )

    ctx.update(members=members, total=len(members))
    return templates.TemplateResponse(request, "muster.html", ctx)


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

    ctx.update(
        member=member,
        rank=rank_utils.rank_by_name(session, member.rank),
        service=service,
        discipline=discipline,
        awards=awards,
        att_counts=dict(att_counts),
        rank_options=services.rank_options(session),
        company_options=services.company_options(session),
        held_award_ids={a.award_type_id for a in member.awards},
        award_catalogue=session.query(AwardType).order_by(AwardType.name).all(),
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


@app.get("/muster-calls/{event_id}", response_class=HTMLResponse)
def event_detail(request: Request, event_id: int, session: Session = Depends(get_session)):
    ctx = _base_context(request, session)

    event = session.get(Event, event_id)
    if event is None:
        ctx["message"] = "No such muster call is recorded."
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
    can_rsvp = False
    if viewer:
        me = session.get(Member, int(viewer["id"]))
        if me is not None:
            can_rsvp = True
            mine = next((r for r in event.attendance_records if r.member_id == me.discord_id), None)
            my_rsvp = mine.status if mine else None

    ctx.update(
        event=event,
        records=records,
        counts=dict(counts),
        active_members=active_members,
        attendance_statuses=services.ATTENDANCE_STATUSES,
        can_rsvp=can_rsvp,
        my_rsvp=my_rsvp,
    )
    return templates.TemplateResponse(request, "event_detail.html", ctx)


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
    steps = [
        ("Name your regiment", cfg.regiment_name not in ("", "Unconfigured Regiment"),
         "#identity", "So the banner and directory show your unit, not a placeholder."),
        ("Set the Admin role", bool(cfg.admin_role_id),
         "#roles", "Grants full control here and in Discord."),
        ("Set the Officer role", bool(cfg.officer_role_id),
         "#roles", "Officers manage the roster, events, and discipline."),
        ("Set the Recruiter role", bool(cfg.recruiter_role_id),
         "#roles", "Recruiters approve or deny applicants."),
        ("Set the Roster channel", bool(cfg.roster_channel_id),
         "#channels", "Where the live roster embed is posted and kept current."),
        ("Set the Recruitment channel", bool(cfg.recruitment_channel_id),
         "#channels", "Where enlistment applications land for review."),
        ("Build the rank ladder", rank_count >= 2,
         "#ranks", "You need ranks before you can promote anyone."),
        ("Add a company", company_count >= 1,
         "#companies", "Members are assigned to a company on the roster."),
    ]
    return [{"label": s[0], "done": s[1], "anchor": s[2], "hint": s[3]} for s in steps]


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
                    "recruiting_open": row.recruiting_open,
                    "listed": row.listed,
                    "discord_guild_id": row.discord_guild_id or "",
                    "url": f"https://{row.slug}.{os.getenv('PLATFORM_BASE_DOMAIN')}/",
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
        listing=listing,
    )
    return templates.TemplateResponse(request, "command_tent.html", ctx)


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


@app.get("/recruits", response_class=HTMLResponse)
def recruits(request: Request, session: Session = Depends(get_session)):
    """The recruitment queue — applicants awaiting an approve/deny decision."""
    ctx = _base_context(request, session)
    ctx["can_decide"] = auth.tier_at_least(ctx["user"], auth.TIER_RECRUITER)
    ctx["candidates"] = (
        session.query(Candidacy).order_by(Candidacy.created_at.desc()).all()
    )
    return templates.TemplateResponse(request, "recruits.html", ctx)


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


@app.post("/recruits/{discord_id}/approve")
def post_approve(
    request: Request,
    discord_id: int,
    csrf: str = Form(...),
    user: dict = Depends(auth.require_recruiter),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.approve_candidate, actor, discord_id,
               redirect="/recruits")


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
def post_create_event(
    request: Request,
    csrf: str = Form(...),
    name: str = Form(...),
    event_type: str = Form(...),
    date: str = Form(...),
    time: str = Form(...),
    tz_offset: str = Form("0"),
    user: dict = Depends(auth.require_officer),
):
    if not auth.verify_csrf(request, csrf):
        _flash(request, "Your session expired. Please try that again.", "error")
        return RedirectResponse("/muster-calls", status_code=303)
    actor = {"id": user["id"], "name": user["name"]}
    with _tenant_session(request) as session:
        try:
            event_id = services.create_event(
                session, actor, name, event_type, f"{date} {time}", tz_offset=tz_offset
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
):
    user = auth.effective_user(request, resolve_tenant(request).slug)
    if not user:
        raise auth.NotAuthenticated()
    if not auth.verify_csrf(request, csrf):
        _flash(request, "Your session expired. Please try that again.", "error")
        return RedirectResponse(f"/muster-calls/{event_id}", status_code=303)
    with _tenant_session(request) as session:
        try:
            _flash(request, services.rsvp(session, event_id, int(user["id"]), status), "ok")
        except services.ActionError as exc:
            _flash(request, str(exc), "error")
    return RedirectResponse(f"/muster-calls/{event_id}", status_code=303)


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
def post_award_type(
    request: Request,
    csrf: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    emoji: str = Form(""),
    user: dict = Depends(auth.require_officer),
):
    actor = {"id": user["id"], "name": user["name"]}
    return _do(request, csrf, services.create_award_type, actor, name, description, emoji,
               redirect="/honors")


# --- Admin: identity / roles / channels ----------------------------------- #
@app.post("/admin/identity")
def post_identity(
    request: Request,
    csrf: str = Form(...),
    regiment_name: str = Form(...),
    motto: str = Form(""),
    brand_color: str = Form(...),
    inactivity_days: int = Form(...),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.update_identity,
               regiment_name, motto, brand_color, inactivity_days,
               redirect="/command-tent")


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


# --- Admin: ranks --------------------------------------------------------- #
@app.post("/admin/ranks/add")
def post_rank_add(
    request: Request,
    csrf: str = Form(...),
    name: str = Form(...),
    abbreviation: str = Form(...),
    tier: str = Form(""),
    role_id: str = Form(""),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.rank_add, name, abbreviation, tier, role_id,
               redirect="/command-tent")


@app.post("/admin/ranks/{rank_id}/update")
def post_rank_update(
    request: Request,
    rank_id: int,
    csrf: str = Form(...),
    abbreviation: str = Form(...),
    tier: str = Form(""),
    role_id: str = Form(""),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.rank_update, rank_id, abbreviation, tier, role_id,
               redirect="/command-tent")


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
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.company_add, name, role_id, bool(is_default),
               redirect="/command-tent")


@app.post("/admin/companies/{company_id}/update")
def post_company_update(
    request: Request,
    company_id: int,
    csrf: str = Form(...),
    role_id: str = Form(""),
    user: dict = Depends(auth.require_admin),
):
    return _do(request, csrf, services.company_update, company_id, role_id, redirect="/command-tent")


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
    """Who may create a unit. Secure by default: with a PLATFORM_ADMIN_IDS
    allowlist, only those admins; otherwise registration is closed unless
    PLATFORM_OPEN_REGISTRATION is explicitly enabled."""
    if not user:
        return False
    allow = os.getenv("PLATFORM_ADMIN_IDS", "").replace(" ", "")
    if allow:
        return str(user.get("id")) in {a for a in allow.split(",") if a}
    return os.getenv("PLATFORM_OPEN_REGISTRATION", "").lower() in ("1", "true", "yes")


def _is_platform_admin(user: dict | None) -> bool:
    """Deleting units is destructive, so it always requires an explicit
    PLATFORM_ADMIN_IDS allowlist (never available under open registration)."""
    if not user:
        return False
    allow = os.getenv("PLATFORM_ADMIN_IDS", "").replace(" ", "")
    if not allow:
        return False
    return str(user.get("id")) in {a for a in allow.split(",") if a}


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
    with registry_session() as rs:
        row = tenant_by_slug(rs, tenant.slug)
        if row is not None:
            row.name = name
            row.motto = motto.strip() or None
            row.blurb = blurb.strip() or None
            row.recruiting_open = bool(recruiting_open)
            row.listed = bool(listed)
            rs.commit()
            _flash(request, "Public listing updated.", "ok")
        else:
            _flash(request, "This unit isn't in the registry.", "error")
    return RedirectResponse("/command-tent", status_code=303)


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
