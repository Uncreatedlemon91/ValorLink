"""Pro Clubs team site: news, events, a Twitch streamer showcase, and the
club's EA stats dashboard, gated by Discord roles.

FastAPI + Jinja2, matching the main ValorLink platform's stack so proven
patterns (Discord OAuth, CSRF, template conventions) carry over -- but this
app remains fully isolated: own venv, own service, own subdomain, own
database, no imports from valorlink's web/ or db/ packages (see README.md).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import auth
import config
import db
import ea_client
import services
import twitch_client
from database import get_session, init_db

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title=config.SITE_NAME)

app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET or "proclubs-dev-secret-change-me",
    same_site="lax",
    https_only=config.HTTPS_ONLY,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.include_router(auth.router)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _asset_version() -> str:
    try:
        return str(int((BASE_DIR / "static" / "css" / "site.css").stat().st_mtime))
    except OSError:
        return "1"


templates.env.globals["css_v"] = _asset_version()
templates.env.globals["SITE_NAME"] = config.SITE_NAME
templates.env.globals["SITE_TAGLINE"] = config.SITE_TAGLINE
templates.env.globals["OAUTH_ENABLED"] = config.OAUTH_ENABLED
templates.env.globals["DEV_LOGIN_ENABLED"] = config.DEV_LOGIN_ENABLED


@app.on_event("startup")
def _startup():
    init_db()


@app.exception_handler(auth.NotAuthenticated)
def _on_unauthenticated(request: Request, exc: auth.NotAuthenticated):
    return RedirectResponse(f"/login?next={request.url.path}", status_code=303)


@app.exception_handler(auth.NotStaff)
def _on_not_staff(request: Request, exc: auth.NotStaff):
    return templates.TemplateResponse(
        request, "error.html",
        _ctx(request, message="That needs staff standing in Discord. You're signed in, but without the role for it."),
        status_code=403,
    )


@app.exception_handler(services.ServiceError)
def _on_service_error(request: Request, exc: services.ServiceError):
    return templates.TemplateResponse(
        request, "error.html", _ctx(request, message=str(exc)), status_code=400,
    )


def _ctx(request: Request, **extra) -> dict:
    user = auth.current_user(request)
    ctx = {
        "request": request,
        "user": user,
        "is_staff": auth.is_staff(user),
        "csrf_token": auth.get_csrf_token(request),
    }
    ctx.update(extra)
    return ctx


def _flash(request: Request, text: str, level: str = "ok"):
    request.session.setdefault("flash", []).append({"level": level, "text": text})


def _pop_flash(request: Request) -> list[dict]:
    return request.session.pop("flash", [])


templates.env.globals["pop_flash"] = _pop_flash


def _check_csrf(request: Request, token: str):
    if not auth.verify_csrf(request, token):
        raise services.ServiceError("Your session expired before that finished submitting. Please try again.")


# --------------------------------------------------------------------------- #
# Home
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with get_session() as session:
        articles = services.list_articles(session, limit=3)
        upcoming = services.list_events(session, upcoming_only=True, limit=1)
        streamers = services.list_streamers(session)
        live = twitch_client.live_streams([s.twitch_login for s in streamers])
        live_streamers = [s for s in streamers if s.twitch_login in live]
        stats_teaser = None
        if config.CLUB_ID:
            try:
                stats_teaser = ea_client.division_stats(config.CLUB_PLATFORM, config.CLUB_ID)
            except ea_client.EAApiError:
                stats_teaser = None
        return templates.TemplateResponse(request, "home.html", _ctx(
            request,
            articles=articles,
            next_event=upcoming[0] if upcoming else None,
            live_streamers=live_streamers,
            live=live,
            stats_teaser=stats_teaser,
        ))


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    error = request.session.pop("login_error", None)
    return templates.TemplateResponse(request, "login.html", _ctx(request, error=error))


# --------------------------------------------------------------------------- #
# News
# --------------------------------------------------------------------------- #
@app.get("/news", response_class=HTMLResponse)
def news_list(request: Request):
    with get_session() as session:
        user = auth.current_user(request)
        articles = services.list_articles(session, include_drafts=auth.is_staff(user))
        return templates.TemplateResponse(request, "news_list.html", _ctx(request, articles=articles))


@app.get("/news/new", response_class=HTMLResponse)
def news_new_form(request: Request, _staff=Depends(auth.require_staff)):
    return templates.TemplateResponse(request, "news_form.html", _ctx(request, article=None))


@app.post("/news/new")
async def news_new(
    request: Request, title: str = Form(...), summary: str = Form(""),
    body_md: str = Form(...), published: str = Form(""), csrf_token: str = Form(...),
    cover_image: UploadFile | None = None, staff=Depends(auth.require_staff),
):
    _check_csrf(request, csrf_token)
    cover = await services.image_to_data_uri(cover_image)
    with get_session() as session:
        article = services.create_article(
            session, title=title, summary=summary, body_md=body_md,
            cover_image=cover, published=bool(published), author=staff,
        )
        _flash(request, "Article published." if article.published else "Draft saved.")
        return RedirectResponse(f"/news/{article.slug}", status_code=303)


@app.get("/news/{slug}", response_class=HTMLResponse)
def news_detail(request: Request, slug: str):
    with get_session() as session:
        article = services.get_article(session, slug)
        if article is None or (not article.published and not auth.is_staff(auth.current_user(request))):
            return templates.TemplateResponse(
                request, "error.html", _ctx(request, message="That article doesn't exist."), status_code=404,
            )
        return templates.TemplateResponse(request, "news_detail.html", _ctx(request, article=article))


@app.get("/news/{slug}/edit", response_class=HTMLResponse)
def news_edit_form(request: Request, slug: str, _staff=Depends(auth.require_staff)):
    with get_session() as session:
        article = services.get_article(session, slug)
        if article is None:
            return templates.TemplateResponse(
                request, "error.html", _ctx(request, message="That article doesn't exist."), status_code=404,
            )
        return templates.TemplateResponse(request, "news_form.html", _ctx(request, article=article))


@app.post("/news/{slug}/edit")
async def news_edit(
    request: Request, slug: str, title: str = Form(...), summary: str = Form(""),
    body_md: str = Form(...), published: str = Form(""), csrf_token: str = Form(...),
    cover_image: UploadFile | None = None, staff=Depends(auth.require_staff),
):
    _check_csrf(request, csrf_token)
    cover = await services.image_to_data_uri(cover_image)
    with get_session() as session:
        article = services.get_article(session, slug)
        if article is None:
            return templates.TemplateResponse(
                request, "error.html", _ctx(request, message="That article doesn't exist."), status_code=404,
            )
        article = services.update_article(
            session, article, title=title, summary=summary, body_md=body_md,
            cover_image=cover, published=bool(published),
        )
        _flash(request, "Article updated.")
        return RedirectResponse(f"/news/{article.slug}", status_code=303)


@app.post("/news/{slug}/delete")
def news_delete(request: Request, slug: str, csrf_token: str = Form(...), staff=Depends(auth.require_staff)):
    _check_csrf(request, csrf_token)
    with get_session() as session:
        article = services.get_article(session, slug)
        if article is not None:
            services.delete_article(session, article)
            _flash(request, "Article deleted.")
    return RedirectResponse("/news", status_code=303)


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
@app.get("/events", response_class=HTMLResponse)
def events_list(request: Request):
    with get_session() as session:
        upcoming = services.list_events(session, upcoming_only=True)
        past = [e for e in services.list_events(session) if e.scheduled_at < datetime.utcnow()]
        return templates.TemplateResponse(
            request, "events_list.html", _ctx(request, upcoming=upcoming, past=past),
        )


@app.get("/events/new", response_class=HTMLResponse)
def event_new_form(request: Request, _staff=Depends(auth.require_staff)):
    return templates.TemplateResponse(request, "event_form.html", _ctx(request, event=None))


@app.post("/events/new")
async def event_new(
    request: Request, title: str = Form(...), event_type: str = Form("Match"),
    opponent: str = Form(""), description: str = Form(""), scheduled_at: str = Form(...),
    result: str = Form(""), csrf_token: str = Form(...), image: UploadFile | None = None,
    staff=Depends(auth.require_staff),
):
    _check_csrf(request, csrf_token)
    img = await services.image_to_data_uri(image)
    try:
        when = datetime.fromisoformat(scheduled_at)
    except ValueError:
        raise services.ServiceError("That date/time doesn't look right.")
    with get_session() as session:
        event = services.create_event(
            session, title=title, event_type=event_type, opponent=opponent,
            description=description, scheduled_at=when, image=img, result=result,
            author_name=staff.get("name", "Staff"),
        )
        _flash(request, "Event added.")
        return RedirectResponse("/events", status_code=303)


@app.get("/events/{event_id}/edit", response_class=HTMLResponse)
def event_edit_form(request: Request, event_id: int, _staff=Depends(auth.require_staff)):
    with get_session() as session:
        event = services.get_event(session, event_id)
        if event is None:
            return templates.TemplateResponse(
                request, "error.html", _ctx(request, message="That event doesn't exist."), status_code=404,
            )
        return templates.TemplateResponse(request, "event_form.html", _ctx(request, event=event))


@app.post("/events/{event_id}/edit")
async def event_edit(
    request: Request, event_id: int, title: str = Form(...), event_type: str = Form("Match"),
    opponent: str = Form(""), description: str = Form(""), scheduled_at: str = Form(...),
    result: str = Form(""), csrf_token: str = Form(...), image: UploadFile | None = None,
    staff=Depends(auth.require_staff),
):
    _check_csrf(request, csrf_token)
    img = await services.image_to_data_uri(image)
    try:
        when = datetime.fromisoformat(scheduled_at)
    except ValueError:
        raise services.ServiceError("That date/time doesn't look right.")
    with get_session() as session:
        event = services.get_event(session, event_id)
        if event is None:
            return templates.TemplateResponse(
                request, "error.html", _ctx(request, message="That event doesn't exist."), status_code=404,
            )
        services.update_event(
            session, event, title=title, event_type=event_type, opponent=opponent,
            description=description, scheduled_at=when, image=img, result=result,
        )
        _flash(request, "Event updated.")
        return RedirectResponse("/events", status_code=303)


@app.post("/events/{event_id}/delete")
def event_delete(request: Request, event_id: int, csrf_token: str = Form(...), staff=Depends(auth.require_staff)):
    _check_csrf(request, csrf_token)
    with get_session() as session:
        event = services.get_event(session, event_id)
        if event is not None:
            services.delete_event(session, event)
            _flash(request, "Event deleted.")
    return RedirectResponse("/events", status_code=303)


# --------------------------------------------------------------------------- #
# Streamers
# --------------------------------------------------------------------------- #
@app.get("/streamers", response_class=HTMLResponse)
def streamers_page(request: Request):
    with get_session() as session:
        streamers = services.list_streamers(session)
        live = twitch_client.live_streams([s.twitch_login for s in streamers])
        return templates.TemplateResponse(request, "streamers.html", _ctx(
            request, streamers=streamers, live=live, twitch_enabled=config.TWITCH_ENABLED,
        ))


@app.post("/streamers/add")
async def streamer_add(
    request: Request, display_name: str = Form(...), twitch_login: str = Form(...),
    csrf_token: str = Form(...), avatar: UploadFile | None = None, staff=Depends(auth.require_staff),
):
    _check_csrf(request, csrf_token)
    avatar_uri = await services.image_to_data_uri(avatar)
    with get_session() as session:
        services.create_streamer(
            session, display_name=display_name, twitch_login=twitch_login,
            avatar=avatar_uri, author_name=staff.get("name", "Staff"),
        )
        _flash(request, "Streamer added to the showcase.")
    return RedirectResponse("/streamers", status_code=303)


@app.post("/streamers/{streamer_id}/delete")
def streamer_delete(request: Request, streamer_id: int, csrf_token: str = Form(...), staff=Depends(auth.require_staff)):
    _check_csrf(request, csrf_token)
    with get_session() as session:
        streamer = services.get_streamer(session, streamer_id)
        if streamer is not None:
            services.delete_streamer(session, streamer)
            _flash(request, "Streamer removed.")
    return RedirectResponse("/streamers", status_code=303)


# --------------------------------------------------------------------------- #
# Stats dashboard (locked to our own club -- see ea_client.py / db.py)
# --------------------------------------------------------------------------- #
@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    return templates.TemplateResponse(request, "stats.html", _ctx(
        request, club_platform=config.CLUB_PLATFORM, club_id=config.CLUB_ID,
    ))


def _stats_error(exc: ea_client.EAApiError) -> JSONResponse:
    status = exc.status_code if isinstance(exc.status_code, int) else 502
    return JSONResponse({"error": str(exc)}, status_code=status)


@app.get("/api/overview")
def api_overview():
    try:
        info = ea_client.club_info(config.CLUB_PLATFORM, config.CLUB_ID)
        stats = ea_client.overall_stats(config.CLUB_PLATFORM, config.CLUB_ID)
    except ea_client.EAApiError as exc:
        return _stats_error(exc)
    if not info and not stats:
        return JSONResponse({"error": "club not found"}, status_code=404)
    return {"info": info, "stats": stats}


@app.get("/api/standings")
def api_standings():
    try:
        division = ea_client.division_stats(config.CLUB_PLATFORM, config.CLUB_ID)
        stats = ea_client.overall_stats(config.CLUB_PLATFORM, config.CLUB_ID)
    except ea_client.EAApiError as exc:
        return _stats_error(exc)
    if not division and not stats:
        return JSONResponse({"error": "club not found"}, status_code=404)
    division = division or {}
    stats = stats or {}
    return {
        "currentDivision": division.get("currentDivision"),
        "bestDivision": division.get("bestDivision") or stats.get("bestDivision"),
        "points": division.get("points"),
        "bestFinishGroup": stats.get("bestFinishGroup"),
        "skillRating": stats.get("skillRating"),
        "promotions": stats.get("promotions") or division.get("promotions"),
        "relegations": stats.get("relegations") or division.get("relegations"),
        "wstreak": stats.get("wstreak"),
        "unbeatenstreak": stats.get("unbeatenstreak"),
        "leagueAppearances": stats.get("leagueAppearances"),
    }


@app.get("/api/members")
def api_members():
    try:
        current = ea_client.member_stats(config.CLUB_PLATFORM, config.CLUB_ID) or {}
        career = ea_client.member_career_stats(config.CLUB_PLATFORM, config.CLUB_ID) or {}
    except ea_client.EAApiError as exc:
        return _stats_error(exc)

    career_by_name = {m.get("name"): m for m in career.get("members", [])}
    merged = []
    for m in current.get("members", []):
        row = dict(m)
        c = career_by_name.get(m.get("name"))
        if c:
            row["careerGoals"] = c.get("goals")
            row["careerAssists"] = c.get("assists")
            row["careerGamesPlayed"] = c.get("gamesPlayed")
            row["careerManOfTheMatch"] = c.get("manOfTheMatch")
            row["careerRatingAve"] = c.get("ratingAve")
        merged.append(row)

    return {"members": merged, "positionCount": current.get("positionCount", {})}


@app.get("/api/matches")
def api_matches(matchType: str = "leagueMatch", count: int = 10):
    count = max(1, min(count, 30))
    try:
        data = ea_client.matches_stats(config.CLUB_PLATFORM, config.CLUB_ID, matchType, max_results=count)
    except ea_client.EAApiError as exc:
        return _stats_error(exc)
    return data


@app.get("/api/history/division")
def api_history_division():
    return {
        "trackedSince": db.tracked_since(config.CLUB_PLATFORM, config.CLUB_ID),
        "snapshots": db.division_history(config.CLUB_PLATFORM, config.CLUB_ID),
    }


@app.get("/api/history/matches")
def api_history_matches(matchType: str | None = None):
    return {
        "trackedSince": db.tracked_since(config.CLUB_PLATFORM, config.CLUB_ID),
        "matches": db.match_history(config.CLUB_PLATFORM, config.CLUB_ID, matchType),
    }


@app.get("/api/history/players")
def api_history_players(name: str = ""):
    name = name.strip()
    tracked_since = db.tracked_since(config.CLUB_PLATFORM, config.CLUB_ID)
    if name:
        return {
            "trackedSince": tracked_since,
            "player": name,
            "matches": db.player_trend(config.CLUB_PLATFORM, config.CLUB_ID, name),
        }
    return {"trackedSince": tracked_since, "players": db.player_names(config.CLUB_PLATFORM, config.CLUB_ID)}


@app.get("/api/streamers/live")
def api_streamers_live():
    with get_session() as session:
        streamers = services.list_streamers(session)
    return twitch_client.live_streams([s.twitch_login for s in streamers])
