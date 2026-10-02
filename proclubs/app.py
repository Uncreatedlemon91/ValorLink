"""Pro Clubs team site: news, events, a Twitch streamer showcase, and the
club's EA stats dashboard, gated by Discord roles.

FastAPI + Jinja2 + SQLAlchemy. This is the whole application: the repo
used to carry an unrelated Discord bot alongside it, and the isolation
that was designed for -- own venv, own service, own domain, own database,
own .env -- is simply how it is built now.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

import auth
import config
import db
import discord_announce
import discord_roster
import discord_rsvp
import discord_notify
import ea_client
import matchweek_routes
import navigation
import roles
import services
import squad
import twitch_client
import web
from web import check_csrf as _check_csrf, ctx as _ctx, flash as _flash, not_found as _not_found
from web import pop_flash as _pop_flash, safe_redirect as _safe_redirect
from database import get_session, init_db
from formations import BENCH_SLOTS, FORMATIONS
from models import ARTICLE_CATEGORIES, ATTENDANCE_STATUSES, SIGNUP_LABELS, SIGNUP_STATUSES, EventSignup

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title=config.SITE_NAME)


# --------------------------------------------------------------------------- #
# The login gate
# --------------------------------------------------------------------------- #
# Everything is for members of the club's Discord server, except these:
# the public splash page, signing in and out, the stylesheet and scripts,
# Discord's own calls into the site (button presses), and article cover
# images, which Discord fetches unauthenticated to show in an announcement.
_PUBLIC_PATHS = {"/", "/welcome", "/login", "/logout", "/discord/interactions"}
_PUBLIC_PREFIXES = ("/static/", "/auth/")
_PUBLIC_PATTERNS = (re.compile(r"^/news/[^/]+/cover-image$"),)


def _is_public(path: str) -> bool:
    return (path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES)
            or any(p.match(path) for p in _PUBLIC_PATTERNS))


async def _login_gate(request: Request, call_next):
    """Default-deny: a new route is members-only unless it's added above,
    rather than public until somebody remembers to protect it."""
    path = request.url.path
    if _is_public(path):
        return await call_next(request)
    user = await run_in_threadpool(auth.current_user, request)
    if not user:
        if path.startswith("/api/"):
            return JSONResponse({"error": "Sign in to see this."}, status_code=401)
        if request.method == "GET":
            auth.remember_login_next(request, path + (f"?{request.url.query}" if request.url.query else ""))
        return RedirectResponse("/login", status_code=303)
    if not auth.is_member(user):
        if path.startswith("/api/"):
            return JSONResponse({"error": "Members only."}, status_code=403)
        return templates.TemplateResponse(request, "error.html", _ctx(
            request, message="The site is for members of our Discord server. Join it, then "
                             "sign out and back in so we can see you're there.",
            show_invite=True), status_code=403)
    return await call_next(request)


# Added before the session middleware so it runs inside it: Starlette
# wraps each new middleware around the ones already added, and the gate
# needs the session.
app.add_middleware(BaseHTTPMiddleware, dispatch=_login_gate)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET or "proclubs-dev-secret-change-me",
    same_site="lax",
    https_only=config.HTTPS_ONLY,
)
class _VersionedStatic(StaticFiles):
    """StaticFiles, but with a Cache-Control header.

    Starlette sends only ETag/Last-Modified, so a returning visitor still
    pays a revalidation round trip per asset before the browser will reuse
    anything -- on a page pulling the stylesheet plus a couple of scripts
    that's several serial-ish requests just to be told "unchanged."

    Every template references static files through the asset_version helper
    below, which stamps the file's own mtime into the query string, so a
    changed file is a changed URL and a long immutable cache can't serve a
    stale one. A request without that stamp (someone's bookmark, a hand-typed
    URL) gets a short max-age instead, since nothing would bust it."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        query = scope.get("query_string", b"").decode("latin-1")
        versioned = "v=" in query
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if versioned else "public, max-age=300"
        )
        return response


app.mount("/static", _VersionedStatic(directory=BASE_DIR / "static"), name="static")
app.include_router(auth.router)
app.include_router(matchweek_routes.router)

templates = web.templates


def _asset_version(rel_path: str = "css/site.css") -> str:
    """Cache-busting query value for a static file, keyed off its own
    mtime -- a plain `git pull` doesn't touch the mtime of files a deploy
    left unchanged, so this must be per-file rather than one shared value,
    or updating only a .js file (leaving site.css untouched) wouldn't bust
    a browser's cached copy of that script."""
    try:
        return str(int((BASE_DIR / "static" / rel_path).stat().st_mtime))
    except OSError:
        return "1"


def _initials(name: str, limit: int = 3) -> str:
    """A short crest-style abbreviation for the match-center badges, e.g.
    "YeeHaw FC" -> "YF", "Rivals FC" -> "RF" -- first letter of each word."""
    letters = "".join(word[0] for word in (name or "").split() if word)
    return (letters[:limit] or "?").upper()


# What each format renders server-side. The browser replaces the text with
# the same moment in the viewer's own zone (static/js/localtime.js); these
# are what stands if JavaScript never runs, so they say UTC rather than
# quietly implying local.
_TIME_FALLBACKS = {
    "full": "%A, %b %-d, %Y at %H:%M UTC",
    "short": "%A, %b %-d at %H:%M UTC",
    "date": "%A, %b %-d, %Y",
    "daymonth": "%a %b %-d",
    "time": "%H:%M UTC",
    "month": "%b",
    "day": "%-d",
}


def _localtime(when: datetime, fmt: str = "full") -> Markup:
    """A <time> element the browser rewrites into the viewer's own zone.

    Event times are stored naive-UTC, so every reader outside UTC was doing
    the conversion in their head. The datetime attribute carries the
    instant; localtime.js reformats the text on load. Without it the
    server-rendered UTC text stands -- correct, and labelled UTC.

    Date parts (the month/day badges) are localised too, not just the
    clock: 23:00 UTC on the 20th is the 21st in Sydney, and a badge that
    disagrees with the time beside it is worse than either alone.
    """
    iso = when.replace(tzinfo=timezone.utc).isoformat()
    fallback = when.strftime(_TIME_FALLBACKS.get(fmt, _TIME_FALLBACKS["full"]))
    return Markup(
        f'<time datetime="{escape(iso)}" data-localtime="{escape(fmt)}">{escape(fallback)}</time>'
    )


def _focal_position(article) -> str:
    """CSS object-position/background-position value for an article's
    cover image -- the same image gets cropped to several different aspect
    ratios across the site (home hero, article header, card thumbnails),
    so a plain center crop often loses the part that matters. Falls back
    to dead-center for rows saved before this field existed."""
    x = article.cover_focal_x if article.cover_focal_x is not None else 50
    y = article.cover_focal_y if article.cover_focal_y is not None else 50
    return f"{x}% {y}%"


def _media_url(kind: str, obj, variant: str = "thumb") -> str:
    """Versioned URL for a stored image (see the /media routes above the
    news section). The token is the row's own updated_at, so an edited
    cover gets a new URL and the year-long immutable cache stays honest."""
    stamp = getattr(obj, "updated_at", None) or getattr(obj, "created_at", None)
    version = int(stamp.timestamp()) if stamp else 0
    if kind == "streamer":
        return f"/media/streamer/{obj.id}/avatar?v={version}"
    return f"/media/article/{obj.id}/{variant}?v={version}"


templates.env.globals["css_v"] = _asset_version()
templates.env.globals["asset_version"] = _asset_version
templates.env.globals["SITE_NAME"] = config.SITE_NAME
templates.env.globals["SITE_TAGLINE"] = config.SITE_TAGLINE
templates.env.globals["SITE_SHORT"] = config.SITE_SHORT
templates.env.globals["SITE_MOTTO"] = config.SITE_MOTTO
templates.env.globals["OAUTH_ENABLED"] = config.OAUTH_ENABLED
templates.env.globals["DEV_LOGIN_ENABLED"] = config.DEV_LOGIN_ENABLED
templates.env.globals["ARTICLE_CATEGORIES"] = ARTICLE_CATEGORIES
templates.env.globals["initials"] = _initials
templates.env.globals["focal_position"] = _focal_position
templates.env.globals["localtime"] = _localtime
templates.env.globals["media_url"] = _media_url
templates.env.globals["CLIPS_SYNC_ENABLED"] = config.CLIPS_SYNC_ENABLED
templates.env.globals["status_label"] = roles.status_label


def _warm_home_caches():
    """Fetch what the home page reads from EA before anyone asks for it.

    Without this the SWR caches in ea_client/twitch_client still leave one
    visitor per restart paying the cold-fetch cost -- two EA round trips at
    a 10-second timeout each, right in the middle of their page load. The
    thread is fire-and-forget: a failure here just leaves the cache cold,
    which is exactly where it would have been anyway."""
    if not config.CLUB_ID:
        return

    def _warm():
        for fetch in (ea_client.overall_stats, ea_client.crest_colors):
            try:
                fetch(config.CLUB_PLATFORM, config.CLUB_ID)
            except ea_client.EAApiError:
                pass

    threading.Thread(target=_warm, daemon=True).start()


@app.on_event("startup")
def _startup():
    init_db()
    # Contracts carried over from last season, or recorded before player
    # records existed, get one now.
    with get_session() as session:
        services.backfill_players(session)
    _warm_home_caches()


@app.exception_handler(auth.NotAuthenticated)
def _on_unauthenticated(request: Request, exc: auth.NotAuthenticated):
    return RedirectResponse(f"/login?next={request.url.path}", status_code=303)


@app.exception_handler(auth.NotStaff)
def _on_not_staff(request: Request, exc: auth.NotStaff):
    return templates.TemplateResponse(
        request, "error.html",
        _ctx(request, message="That's for club staff: the Club President, Head Coach and Coaches."),
        status_code=403,
    )


@app.exception_handler(auth.NotManagement)
def _on_not_management(request: Request, exc: auth.NotManagement):
    return templates.TemplateResponse(
        request, "error.html",
        _ctx(request, message="That's for club management: the Club President and Head Coach."),
        status_code=403,
    )


@app.exception_handler(auth.NotMember)
def _on_not_member(request: Request, exc: auth.NotMember):
    return templates.TemplateResponse(
        request, "error.html",
        _ctx(request, message="That needs you to be a member of our Discord server. You're signed in, but not in the guild."),
        status_code=403,
    )


@app.exception_handler(services.ServiceError)
def _on_service_error(request: Request, exc: services.ServiceError):
    return templates.TemplateResponse(
        request, "error.html", _ctx(request, message=str(exc)), status_code=400,
    )


templates.env.globals["pop_flash"] = _pop_flash
# The sidebar and each section's tab bar (see navigation.py and base.html).
templates.env.globals["nav_visible"] = navigation.visible
templates.env.globals["nav_locate"] = navigation.locate


def _section_shortcut(target: str):
    def redirect():
        return RedirectResponse(target, status_code=302)
    return redirect


# /matchday, /club, /media: short addresses for a section, landing on its
# first tab. 302 rather than 301 -- which tab comes first is a design
# choice that may change, and a permanent redirect would be cached by
# browsers long after it did.
for _section in navigation.SECTIONS:
    if _section.shortcut:
        app.add_api_route(_section.shortcut, _section_shortcut(_section.url),
                          methods=["GET"], include_in_schema=False)


def _announce_article(request: Request, session, article) -> None:
    """Posts to Discord that this article just went live -- best-effort:
    a Discord hiccup must never block publishing, so failure is flashed to
    staff (so it isn't silently invisible) rather than raised. On success,
    saves the new message's id (discord_reactions_poll.py needs it later
    to check reaction counts -- see discord_announce.fetch_reaction_count)."""
    if not config.NEWS_ANNOUNCE_ENABLED:
        return
    if not config.SITE_BASE_URL:
        _flash(request, "Published, but SITE_BASE_URL isn't configured -- skipped the Discord announcement.", level="error")
        return
    url = f"{config.SITE_BASE_URL}/news/{article.slug}"
    cover_image_url = f"{config.SITE_BASE_URL}/news/{article.slug}/cover-image" if article.cover_image else None
    embed = discord_announce.build_embed(
        title=article.title, url=url, summary=article.summary, category=article.category,
        author_name=article.author_name, cover_image_url=cover_image_url,
        published_at=article.published_at,
    )
    try:
        message_id = discord_announce.announce(config.NEWS_ANNOUNCE_CHANNEL_ID, embed)
    except discord_announce.DiscordApiError as exc:
        # The specific reason (bad token, bot not in that channel/guild,
        # missing Send Messages/Embed Links permission, etc.) matters for
        # staff to actually fix this -- see discord_api._discord_error_detail.
        _flash(request, f"Published, but the Discord announcement failed to send: {exc}", level="error")
        return
    article.discord_message_id = message_id
    session.commit()


def _int(value) -> int:
    """EA returns every stat as a string, and missing ones as absent."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _standing_teaser(overall: dict) -> dict:
    """The home page's standing band, from live clubs/overallStats only.

    No division: EA exposes none that is current (see
    ea_client.division_stats), so the band leads on skill rating -- the one
    standing number in that API that actually moves with results -- and
    backs it with the season record, which is live for the same reason."""
    wins, ties, losses = _int(overall.get("wins")), _int(overall.get("ties")), _int(overall.get("losses"))
    played = wins + ties + losses
    return {
        "skillRating": overall.get("skillRating"),
        "record": f"{wins}W {ties}D {losses}L" if played else None,
        "winRate": round(wins / played * 100) if played else None,
        "played": played or None,
    }


# --------------------------------------------------------------------------- #
# Home
# --------------------------------------------------------------------------- #
def _club_standing() -> tuple[dict | None, dict | None]:
    """(standing teaser, crest colours) from EA, both non-blocking: these
    are decoration and a page must not inherit EA's latency to render. On
    a cold cache they're None and fill in behind the request."""
    stats_teaser = crest_colors = None
    if not config.CLUB_ID:
        return None, None
    # Two independent calls, so one failing doesn't blank the other -- the
    # crest is club identity and shouldn't disappear because a stats
    # endpoint had a bad minute. overallStats, not division_stats: the
    # latter is an all-time leaderboard snapshot whose division can be a
    # hundred matches out of date. Skill rating here is live.
    try:
        overall = ea_client.overall_stats(config.CLUB_PLATFORM, config.CLUB_ID, blocking=False) or {}
        stats_teaser = _standing_teaser(overall)
    except ea_client.EAApiError:
        pass
    try:
        crest_colors = ea_client.crest_colors(config.CLUB_PLATFORM, config.CLUB_ID, blocking=False)
    except ea_client.EAApiError:
        pass
    return stats_teaser, crest_colors


def _welcome(request: Request):
    """The public splash page: the one page anybody can see. Promotes the
    club with what it says about itself (the Club profile, edited by
    management) and what the site already knows -- standing, recent
    results, the squad, the staff, how the next fixture is filling, and
    the positions nobody covers in the current formation."""
    with get_session() as session:
        profile = services.get_club_profile(session)
        contracts = services.live_contracts(session)
        staff = services.club_staff(session)
        formation = services.get_active_formation(session)
        upcoming = services.list_events(session, upcoming_only=True, limit=1)
        next_event = upcoming[0] if upcoming else None
        going = services.signup_counts(session, next_event.id).get("going", 0) if next_event else 0
    slots = FORMATIONS.get(formation) or FORMATIONS["4-3-3"]
    depth = squad.squad_depth(slots, contracts)
    # Only positions nobody covers: "no backup" is a staff concern, and on
    # a squad of 11-20 it would list nearly every position.
    looking_for = [r["position"] for r in depth["rows"] if r["state"] == squad.DEPTH_GAP]
    stats_teaser, crest_colors = _club_standing()
    form, matches_recorded = [], 0
    if config.CLUB_ID:
        form = db.recent_form(config.CLUB_PLATFORM, str(config.CLUB_ID), limit=5)
        matches_recorded = len(db.match_history(config.CLUB_PLATFORM, str(config.CLUB_ID)))
    president = next((p for p in staff if p.club_role == roles.CLUB_PRESIDENT), None)
    return templates.TemplateResponse(request, "welcome.html", _ctx(
        request, profile=profile, squad_size=len(contracts), staff=staff, president=president,
        looking_for=looking_for, lines=squad.line_cards(slots, depth), formation=formation,
        shirts=len(slots), stats_teaser=stats_teaser, crest_colors=crest_colors, form=form,
        matches_recorded=matches_recorded, next_event=next_event, going=going,
        club_name=config.CLUB_NAME, platform_label=_platform_label(config.CLUB_PLATFORM),
    ))


def _platform_label(platform: str) -> str:
    return {"common-gen5": "PS5 · Xbox Series X|S · PC", "common-gen4": "PS4 · Xbox One",
            "nx": "Switch"}.get(platform, platform)


@app.get("/welcome", response_class=HTMLResponse)
def welcome(request: Request):
    return _welcome(request)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """Members get the club dashboard; everybody else the splash page."""
    if not auth.is_member(auth.current_user(request)):
        return _welcome(request)
    with get_session() as session:
        latest = services.list_articles(session, limit=9)
        featured, rest = (latest[0], latest[1:]) if latest else (None, [])
        transfers = services.list_articles(session, category="Transfer", limit=4)
        highlights = services.list_articles(session, category="Match Highlight", limit=4)
        # Engagement counts shown on thumbnails -- only the two sections that
        # actually have thumbnails (Latest News rail, Match Highlights grid);
        # one batched query each rather than one round-trip per card.
        thumbnail_article_ids = [a.id for a in rest] + [a.id for a in highlights]
        like_counts = services.like_counts_for(session, thumbnail_article_ids)
        comment_counts = services.comment_counts_for(session, thumbnail_article_ids)
        upcoming = services.list_events(session, upcoming_only=True, limit=1)
        # Confirmed signings and departures only -- see
        # services.public_roster_moves for what is deliberately left out.
        squad_moves = services.public_roster_moves(session, limit=6)
        streamers = services.list_streamers(session)
        live = twitch_client.live_streams([s.twitch_login for s in streamers])
        featured_streamer = services.get_featured_streamer(session)
        other_live_streamers = [
            s for s in streamers if s.twitch_login in live and (not featured_streamer or s.id != featured_streamer.id)
        ]
        stats_teaser, crest_colors = _club_standing()
        return templates.TemplateResponse(request, "home.html", _ctx(
            request,
            featured=featured,
            articles=rest,
            transfers=transfers,
            highlights=highlights,
            next_event=upcoming[0] if upcoming else None,
            squad_moves=squad_moves,
            featured_streamer=featured_streamer,
            featured_streamer_live=bool(featured_streamer and featured_streamer.twitch_login in live),
            other_live_streamers=other_live_streamers,
            live=live,
            stats_teaser=stats_teaser,
            crest_colors=crest_colors,
            like_counts=like_counts,
            comment_counts=comment_counts,
        ))


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = ""):
    auth.remember_login_next(request, next)
    if auth.is_member(auth.current_user(request)):
        return RedirectResponse(auth.pop_login_next(request), status_code=303)
    error = request.session.pop("login_error", None)
    return templates.TemplateResponse(request, "login.html", _ctx(request, error=error))


# --------------------------------------------------------------------------- #
# News
# --------------------------------------------------------------------------- #
@app.get("/news", response_class=HTMLResponse)
def news_list(request: Request, category: str = ""):
    category = category if category in ARTICLE_CATEGORIES else ""
    with get_session() as session:
        user = auth.current_user(request)
        articles = services.list_articles(session, include_drafts=auth.is_staff(user), category=category or None)
        article_ids = [a.id for a in articles]
        like_counts = services.like_counts_for(session, article_ids)
        comment_counts = services.comment_counts_for(session, article_ids)
        return templates.TemplateResponse(request, "news_list.html", _ctx(
            request, articles=articles, selected_category=category,
            like_counts=like_counts, comment_counts=comment_counts,
        ))


@app.get("/news/new", response_class=HTMLResponse)
def news_new_form(request: Request, _staff=Depends(auth.require_staff)):
    return templates.TemplateResponse(request, "news_form.html", _ctx(request, article=None))


@app.post("/news/new")
async def news_new(
    request: Request, title: str = Form(...), category: str = Form("News"), summary: str = Form(""),
    body_html: str = Form(...), published: str = Form(""), csrf_token: str = Form(...),
    cover_focal_x: str = Form("50"), cover_focal_y: str = Form("50"),
    cover_image: UploadFile | None = None, staff=Depends(auth.require_staff),
):
    _check_csrf(request, csrf_token)
    cover, cover_thumb = await services.process_image_upload(cover_image)
    with get_session() as session:
        article = services.create_article(
            session, title=title, category=category, summary=summary, body_html=body_html,
            cover_image=cover, cover_thumb=cover_thumb, published=bool(published), author=staff,
            cover_focal_x=cover_focal_x, cover_focal_y=cover_focal_y,
        )
        _flash(request, "Article published." if article.published else "Draft saved.")
        if article.published:
            _announce_article(request, session, article)
        return RedirectResponse(f"/news/{article.slug}", status_code=303)


@app.get("/news/{slug}/cover-image")
def news_cover_image(slug: str, request: Request):
    """An article's cover image at a real fetchable URL, addressed by slug.

    Kept alongside the /media routes below because Discord's embed API
    needs a URL it can fetch (see discord_announce.py) and the slug is what
    the announcement already has. Unversioned, so it only gets a short
    max-age -- the site's own pages use /media/article/... instead, which
    carries a version token and can be cached indefinitely."""
    with get_session() as session:
        image = services.article_image(session, slug=slug, variant="cover")
    return _image_response(request, image, cache="public, max-age=300")


# --------------------------------------------------------------------------- #
# Media
#
# Uploaded images live in the database as data URIs (see images.py), but
# they are served from here rather than pasted into the markup. Inlining
# them made the home page ~10MB of HTML and the news index ~23MB: a data URI
# can't be cached separately from the document, can't be fetched in
# parallel, and can't be skipped for an image scrolled out of view, so every
# navigation re-downloaded every cover before the page could finish.
#
# Templates build these URLs via the `media_url` global above, which appends
# a version token derived from the row's own updated_at -- that's what makes
# the long immutable cache safe: editing an article changes the URL.
# --------------------------------------------------------------------------- #
_IMMUTABLE = "public, max-age=31536000, immutable"


def _image_response(request: Request, image: str | None, *, cache: str) -> Response:
    content_type, raw = services.decode_data_uri(image)
    if content_type is None:
        raise HTTPException(status_code=404)
    etag = '"%s"' % hashlib.md5(raw).hexdigest()
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": cache})
    return Response(
        content=raw, media_type=content_type,
        headers={"ETag": etag, "Cache-Control": cache},
    )


@app.get("/media/article/{article_id}/{variant}")
def media_article(request: Request, article_id: int, variant: str):
    if variant not in ("cover", "thumb"):
        raise HTTPException(status_code=404)
    with get_session() as session:
        image = services.article_image(session, article_id=article_id, variant=variant)
    return _image_response(request, image, cache=_IMMUTABLE)


@app.get("/media/streamer/{streamer_id}/avatar")
def media_streamer_avatar(request: Request, streamer_id: int):
    with get_session() as session:
        image = services.streamer_avatar(session, streamer_id)
    return _image_response(request, image, cache=_IMMUTABLE)


@app.get("/news/{slug}", response_class=HTMLResponse)
def news_detail(request: Request, slug: str):
    with get_session() as session:
        article = services.get_article(session, slug)
        if article is None or (not article.published and not auth.is_staff(auth.current_user(request))):
            return templates.TemplateResponse(
                request, "error.html", _ctx(request, message="That article doesn't exist."), status_code=404,
            )
        user = auth.current_user(request)
        return templates.TemplateResponse(request, "news_detail.html", _ctx(
            request, article=article,
            body_html=services.render_clip_embeds(session, article.body_html),
            comments=services.list_comments(session, article),
            like_count=services.combined_like_count(
                article, services.count_likes(session, article)),
            user_has_liked=bool(user) and services.has_liked(session, article, user["id"]),
        ))


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
    request: Request, slug: str, title: str = Form(...), category: str = Form("News"), summary: str = Form(""),
    body_html: str = Form(...), published: str = Form(""), csrf_token: str = Form(...),
    cover_focal_x: str = Form("50"), cover_focal_y: str = Form("50"),
    cover_image: UploadFile | None = None, staff=Depends(auth.require_staff),
):
    _check_csrf(request, csrf_token)
    cover, cover_thumb = await services.process_image_upload(cover_image)
    with get_session() as session:
        article = services.get_article(session, slug)
        if article is None:
            return templates.TemplateResponse(
                request, "error.html", _ctx(request, message="That article doesn't exist."), status_code=404,
            )
        was_published = article.published
        article = services.update_article(
            session, article, title=title, category=category, summary=summary, body_html=body_html,
            cover_image=cover, cover_thumb=cover_thumb, published=bool(published),
            cover_focal_x=cover_focal_x, cover_focal_y=cover_focal_y,
        )
        _flash(request, "Article updated.")
        # Announce a draft's first publish, same as a brand-new article --
        # but not a re-save of an article that was already live, or every
        # typo fix would repost it to Discord.
        if article.published and not was_published:
            _announce_article(request, session, article)
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


@app.post("/news/{slug}/like")
def news_like(request: Request, slug: str, csrf_token: str = Form(...), member=Depends(auth.require_member)):
    _check_csrf(request, csrf_token)
    with get_session() as session:
        article = services.get_article(session, slug)
        if article is None:
            return templates.TemplateResponse(
                request, "error.html", _ctx(request, message="That article doesn't exist."), status_code=404,
            )
        services.toggle_like(session, article, member["id"])
    return RedirectResponse(f"/news/{slug}#comments", status_code=303)


@app.post("/news/{slug}/comments")
def news_add_comment(request: Request, slug: str, body: str = Form(...),
                      csrf_token: str = Form(...), member=Depends(auth.require_member)):
    _check_csrf(request, csrf_token)
    with get_session() as session:
        article = services.get_article(session, slug)
        if article is None:
            return templates.TemplateResponse(
                request, "error.html", _ctx(request, message="That article doesn't exist."), status_code=404,
            )
        services.add_comment(session, article, author=member, body=body)
    return RedirectResponse(f"/news/{slug}#comments", status_code=303)


@app.post("/news/{slug}/comments/{comment_id}/delete")
def news_delete_comment(request: Request, slug: str, comment_id: int,
                         csrf_token: str = Form(...), user=Depends(auth.require_signed_in)):
    _check_csrf(request, csrf_token)
    with get_session() as session:
        article = services.get_article(session, slug)
        comment = services.get_comment(session, comment_id)
        if (article is not None and comment is not None and comment.article_id == article.id
                and (auth.is_staff(user) or comment.author_discord_id == user["id"])):
            services.delete_comment(session, comment)
    return RedirectResponse(f"/news/{slug}#comments", status_code=303)


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
@app.get("/events", response_class=HTMLResponse)
def events_list(request: Request):
    with get_session() as session:
        upcoming = services.list_events(session, upcoming_only=True)
        past = [e for e in services.list_events(session) if e.scheduled_at < datetime.utcnow()]
        counts = {e.id: services.signup_counts(session, e.id) for e in upcoming}
        return templates.TemplateResponse(
            request, "events_list.html",
            _ctx(request, upcoming=upcoming, past=past, signup_counts=counts),
        )


def _slot_labels() -> dict[str, str]:
    """slot_key -> display position across every formation and the bench.

    Merged rather than per-formation because a slot key means the same
    position wherever it appears ("CM1" is a CM in all of them), and
    services.tactics_roles_for only ever looks up keys from the formation
    that's actually active."""
    labels = {key: meta["label"] for key, meta in BENCH_SLOTS.items()}
    for formation in FORMATIONS.values():
        labels.update({key: meta["label"] for key, meta in formation.items()})
    return labels


def _parse_scheduled_at(value: str, tz_offset: str = "") -> datetime | None:
    """Parses the <input type="datetime-local"> value into stored UTC.

    The control submits the wall-clock time the author typed with no zone
    at all, so something has to say which zone that was. The form sends a
    hidden tz_offset alongside it -- JavaScript\'s getTimezoneOffset() for
    the *selected* instant, so a fixture booked across a daylight-saving
    boundary uses the offset in force on the day rather than today\'s.

    Offset is minutes behind UTC (JS sign convention: UTC-5 gives 300), so
    adding it to the local wall clock gives UTC.

    With no offset -- JavaScript off, or an old bookmarked form -- the
    value is read as UTC, which is what this field meant before. That keeps
    the form working rather than rejecting the save, and the label says
    which of the two is in force.
    """
    try:
        local = datetime.fromisoformat((value or "").strip())
    except ValueError:
        return None
    try:
        minutes = int((tz_offset or "").strip())
    except ValueError:
        return local
    # Real offsets run UTC-12..UTC+14; anything else is a broken client, and
    # shifting a fixture by a nonsense amount is worse than ignoring it.
    if not -14 * 60 <= minutes <= 12 * 60:
        return local
    return local + timedelta(minutes=minutes)


def _event_view(session, event, user):
    """Everything the event page and the Discord embed both need: the
    roster, each player's Tactics position, and how often they've actually
    turned up."""
    signups = services.list_signups(session, event.id)
    user_ids = [s.discord_user_id for s in signups]
    slots = services.event_slots(event)
    # The shirt someone claimed for THIS event wins over their usual spot on
    # the Tactics board -- the board is the default lineup, the claim is what
    # they actually signed up to play here.
    tactics_roles = services.tactics_roles_for(session, user_ids, _slot_labels())
    positions = dict(tactics_roles)
    for signup in signups:
        if signup.slot_key and signup.slot_key in slots:
            positions[signup.discord_user_id] = slots[signup.slot_key]
    return {
        "signups": signups,
        "slots": slots,
        "claimed": {k: v for k, v in services.claimed_slots(session, event.id).items()},
        "roles": positions,
        "tactics_roles": tactics_roles,
        "records": services.attendance_records_for(session, user_ids),
        "counts": services.signup_counts(session, event.id),
        "my_signup": (
            services.get_signup(session, event.id, int(user["id"])) if user else None
        ),
    }


def _event_url(request: Request, event) -> str:
    base = (config.SITE_BASE_URL or str(request.base_url)).rstrip("/")
    return f"{base}/events/{event.id}"


def _refresh_announcement(request: Request, session, event) -> None:
    """Pushes the current roster back to the Discord announcement. Never
    lets a Discord failure break the site action that triggered it -- the
    sign-up is already saved, and the announcement catches up on the next
    change; surfacing it as a flash is enough."""
    if not (event.discord_message_id and config.EVENT_RSVP_ENABLED):
        return
    signups = services.list_signups(session, event.id)
    roles = services.tactics_roles_for(
        session, [s.discord_user_id for s in signups], _slot_labels())
    try:
        discord_rsvp.refresh(event, signups, roles, _event_url(request, event),
                             services.event_slots(event))
    except discord_rsvp.DiscordApiError as exc:
        _flash(request, f"Saved, but couldn't update the Discord post: {exc}", "warn")


@app.get("/events/new", response_class=HTMLResponse)
def event_new_form(request: Request, _staff=Depends(auth.require_staff)):
    with get_session() as session:
        # Default to whatever the Tactics board is currently set to -- that's
        # the shape the squad is actually drilled in.
        suggested = services.get_active_formation(session)
    return templates.TemplateResponse(request, "event_form.html", _ctx(
        request, event=None, event_types=services.EVENT_TYPES,
        formations=list(FORMATIONS), suggested_formation=suggested))


@app.post("/events/new")
async def event_new(
    request: Request, title: str = Form(...), event_type: str = Form("Match"),
    scheduled_at: str = Form(...), tz_offset: str = Form(""),
    opponent: str = Form(""), description: str = Form(""),
    formation: str = Form(""), announce: str = Form(""), csrf_token: str = Form(...),
    image: UploadFile | None = None, staff=Depends(auth.require_staff),
):
    _check_csrf(request, csrf_token)
    # Events keep a single image column, so they take the display variant
    # and drop the thumbnail. They still go through the same downscale and
    # re-encode as everything else rather than being stored verbatim --
    # image_to_data_uri, which did store verbatim, is gone (see images.py).
    image_uri, _ = await services.process_image_upload(image)
    with get_session() as session:
        event = services.create_event(
            session, title=title, event_type=event_type,
            scheduled_at=_parse_scheduled_at(scheduled_at, tz_offset), opponent=opponent,
            description=description, image=image_uri, staff_name=staff["name"],
            formation=formation,
        )
        _flash(request, "Event created.")
        if announce:
            _announce_event(request, session, event)
        return RedirectResponse(f"/events/{event.id}", status_code=303)


@app.get("/events/{event_id}", response_class=HTMLResponse)
def event_detail(request: Request, event_id: int):
    user = auth.current_user(request)
    with get_session() as session:
        event = services.get_event(session, event_id)
        if event is None:
            raise HTTPException(status_code=404)
        view = _event_view(session, event, user)
        my_link = services.get_player_link(session, int(user["id"])) if user else None
        return templates.TemplateResponse(request, "event_detail.html", _ctx(
            request, event=event, event_types=services.EVENT_TYPES,
            signup_labels=SIGNUP_LABELS, signup_statuses=SIGNUP_STATUSES,
            attendance_statuses=ATTENDANCE_STATUSES,
            my_link=my_link, is_past=event.scheduled_at < datetime.utcnow(),
            rsvp_enabled=config.EVENT_RSVP_ENABLED,
            pitch=FORMATIONS.get(event.formation or "", {}), bench_slots=BENCH_SLOTS, **view,
            **(matchweek_routes.event_page_extras(session, event, user) if user else {}),
        ))


@app.get("/events/{event_id}/edit", response_class=HTMLResponse)
def event_edit_form(request: Request, event_id: int, _staff=Depends(auth.require_staff)):
    with get_session() as session:
        event = services.get_event(session, event_id)
        if event is None:
            raise HTTPException(status_code=404)
        return templates.TemplateResponse(request, "event_form.html", _ctx(
            request, event=event, event_types=services.EVENT_TYPES,
            formations=list(FORMATIONS), suggested_formation=event.formation))


@app.post("/events/{event_id}/edit")
async def event_edit(
    request: Request, event_id: int, title: str = Form(...), event_type: str = Form("Match"),
    scheduled_at: str = Form(...), tz_offset: str = Form(""),
    opponent: str = Form(""), description: str = Form(""),
    result: str = Form(""), formation: str = Form(""), csrf_token: str = Form(...),
    image: UploadFile | None = None, _staff=Depends(auth.require_staff),
):
    _check_csrf(request, csrf_token)
    # Events keep a single image column, so they take the display variant
    # and drop the thumbnail. They still go through the same downscale and
    # re-encode as everything else rather than being stored verbatim --
    # image_to_data_uri, which did store verbatim, is gone (see images.py).
    image_uri, _ = await services.process_image_upload(image)
    with get_session() as session:
        event = services.get_event(session, event_id)
        if event is None:
            raise HTTPException(status_code=404)
        services.update_event(
            session, event, title=title, event_type=event_type,
            scheduled_at=_parse_scheduled_at(scheduled_at, tz_offset), opponent=opponent,
            description=description, image=image_uri, result=result, formation=formation,
        )
        _flash(request, "Event updated.")
        _refresh_announcement(request, session, event)
        return RedirectResponse(f"/events/{event_id}", status_code=303)


@app.post("/events/{event_id}/delete")
def event_delete(request: Request, event_id: int, csrf_token: str = Form(...),
                 _staff=Depends(auth.require_staff)):
    _check_csrf(request, csrf_token)
    with get_session() as session:
        event = services.get_event(session, event_id)
        if event is None:
            raise HTTPException(status_code=404)
        services.delete_event(session, event)
    _flash(request, "Event deleted.")
    return RedirectResponse("/events", status_code=303)


@app.post("/events/{event_id}/signup")
def event_signup(request: Request, event_id: int, status: str = Form(...),
                 csrf_token: str = Form(...), user=Depends(auth.require_member)):
    _check_csrf(request, csrf_token)
    with get_session() as session:
        event = services.get_event(session, event_id)
        if event is None:
            raise HTTPException(status_code=404)
        services.set_signup(
            session, event, discord_user_id=int(user["id"]), discord_name=user["name"],
            discord_avatar=user.get("avatar"), status=status, source="site",
        )
        _flash(request, f"You're down as {SIGNUP_LABELS[status].lower()}.")
        _refresh_announcement(request, session, event)
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@app.post("/events/{event_id}/claim")
def event_claim(request: Request, event_id: int, slot_key: str = Form(""),
                csrf_token: str = Form(...), user=Depends(auth.require_member)):
    """Take a shirt (or hand it back, with an empty slot_key). Claiming a
    position is itself the sign-up -- see services.claim_slot."""
    _check_csrf(request, csrf_token)
    with get_session() as session:
        event = services.get_event(session, event_id)
        if event is None:
            raise HTTPException(status_code=404)
        try:
            if slot_key:
                services.claim_slot(
                    session, event, discord_user_id=int(user["id"]), discord_name=user["name"],
                    discord_avatar=user.get("avatar"), slot_key=slot_key, source="site")
                _flash(request, f"You're in at {services.event_slots(event).get(slot_key, slot_key)}.")
            else:
                services.release_slot(session, event, discord_user_id=int(user["id"]))
                _flash(request, "Position given up -- you're still down as going.")
        except services.ServiceError as exc:
            _flash(request, str(exc), "warn")
            return RedirectResponse(f"/events/{event_id}", status_code=303)
        _refresh_announcement(request, session, event)
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@app.post("/events/{event_id}/signups-open")
def event_signups_open(request: Request, event_id: int, open_: str = Form(""),
                       csrf_token: str = Form(...), _staff=Depends(auth.require_staff)):
    _check_csrf(request, csrf_token)
    with get_session() as session:
        event = services.get_event(session, event_id)
        if event is None:
            raise HTTPException(status_code=404)
        services.set_signups_open(session, event, open_=bool(open_))
        _flash(request, "Sign-ups reopened." if open_ else "Sign-ups closed.")
        _refresh_announcement(request, session, event)
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@app.post("/events/{event_id}/attendance")
def event_attendance(request: Request, event_id: int, signup_id: int = Form(...),
                     attendance: str = Form(""), csrf_token: str = Form(...),
                     staff=Depends(auth.require_staff)):
    """Marks what actually happened for one player. This is what feeds the
    reliability figure -- without it every player shows "no history"."""
    _check_csrf(request, csrf_token)
    with get_session() as session:
        signup = session.get(EventSignup, signup_id)
        if signup is None or signup.event_id != event_id:
            raise HTTPException(status_code=404)
        try:
            services.mark_attendance(
                session, signup, attendance=attendance or None, staff_name=staff["name"])
        except services.ServiceError as exc:
            _flash(request, str(exc), "warn")
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@app.post("/events/{event_id}/announce")
def event_announce(request: Request, event_id: int, csrf_token: str = Form(...),
                   _staff=Depends(auth.require_staff)):
    _check_csrf(request, csrf_token)
    with get_session() as session:
        event = services.get_event(session, event_id)
        if event is None:
            raise HTTPException(status_code=404)
        _announce_event(request, session, event)
    return RedirectResponse(f"/events/{event_id}", status_code=303)


def _announce_event(request: Request, session, event) -> None:
    if not config.EVENT_RSVP_ENABLED:
        # Name only what's actually missing: pointing at settings that are
        # already filled in sends people hunting through a correct .env.
        missing = ", ".join(config.event_rsvp_missing())
        _flash(request, f"Discord sign-ups aren't configured -- set {missing} in "
                        "proclubs/.env, then restart yeehaw-fc.", "warn")
        return
    if event.discord_message_id:
        _flash(request, "That event is already posted in Discord.", "warn")
        return
    signups = services.list_signups(session, event.id)
    roles = services.tactics_roles_for(
        session, [s.discord_user_id for s in signups], _slot_labels())
    try:
        channel_id, message_id = discord_rsvp.announce(
            event, signups, roles, _event_url(request, event), services.event_slots(event))
    except discord_rsvp.DiscordApiError as exc:
        _flash(request, f"Couldn't post to Discord: {exc}", "warn")
        return
    services.set_event_announcement(session, event, channel_id=channel_id, message_id=message_id)
    _flash(request, "Posted to Discord with sign-up buttons.")
    _invite_first_tier(request, session, event)


def _invite_first_tier(request: Request, session, event) -> None:
    """Ping the first rung of the invite ladder as soon as the event is up.

    Only the first tier is done here; the rest are time-based and belong to
    event_invites_poll.py. Waiting up to a poll interval to tell the people
    who get first pick would make "first pick" mean very little.

    Best-effort, like the announcement itself: the event and its thread
    already exist, so a Discord hiccup here is a flash message and a tier
    the poller will pick up on its next run -- never a failed save.
    """
    if not config.EVENT_STAGED_INVITES_ENABLED:
        return
    tiers = services.due_invite_tiers(session, event, config.EVENT_INVITE_TIERS)
    first = next((t for t in tiers if t["hours_before"] is None), None)
    if first is None:
        return
    try:
        added = discord_rsvp.invite_role_to_thread(event.discord_channel_id, first["role_id"])
        discord_rsvp.ping_tier(
            event.discord_channel_id, first["role_id"], event,
            _event_url(request, event), first=True)
    except discord_rsvp.DiscordApiError as exc:
        _flash(request, f"Posted, but couldn't invite the first group yet: {exc}", "warn")
        return
    services.record_tier_invite(session, event, first, added)
    if added == 0:
        _flash(request, "Posted, but nobody was added to the thread -- if that role has "
                        "members, the bot is missing the Server Members intent.", "warn")


# --------------------------------------------------------------------------- #
# Discord interactions (button presses on an event announcement)
# --------------------------------------------------------------------------- #
@app.post("/discord/interactions")
async def discord_interactions(request: Request):
    """Discord's webhook for button presses. Public by necessity -- Discord
    calls it, not a signed-in user -- so the Ed25519 signature is the only
    thing standing between this and forged sign-ups. Reject first, parse
    second: an unverified body is never even JSON-decoded."""
    body = await request.body()
    if not discord_rsvp.verify_signature(
        signature=request.headers.get("X-Signature-Ed25519", ""),
        timestamp=request.headers.get("X-Signature-Timestamp", ""),
        body=body,
    ):
        # Discord requires a 401 here; it probes with bad signatures on setup
        # and won't accept the endpoint unless they're refused.
        raise HTTPException(status_code=401, detail="invalid request signature")

    interaction = json.loads(body)
    if interaction.get("type") == discord_rsvp.INTERACTION_PING:
        return {"type": discord_rsvp.RESPONSE_PONG}
    if interaction.get("type") != discord_rsvp.INTERACTION_MESSAGE_COMPONENT:
        raise HTTPException(status_code=400, detail="unsupported interaction type")

    data = interaction.get("data") or {}
    custom_id = data.get("custom_id", "")
    try:
        presser = discord_rsvp.interaction_user(interaction)
    except discord_rsvp.InteractionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Squad offers share this endpoint with event sign-ups; the custom_id
    # prefix is what tells them apart, checked before either side tries to
    # parse an id that isn't theirs.
    if custom_id.startswith(discord_roster.CUSTOM_ID_PREFIX + ":"):
        return _handle_offer_response(custom_id, presser)
    # The post-match vote and self-rating pickers.
    if custom_id.startswith((discord_notify.MOTM_PREFIX + ":", discord_notify.SELF_RATE_PREFIX + ":")):
        return matchweek_routes.handle_picker(custom_id, data.get("values") or [], presser)

    try:
        # A position pick and a plain answer arrive through the same
        # interaction type; the custom_id prefix is what tells them apart.
        if custom_id.startswith(discord_rsvp.SLOT_CUSTOM_ID_PREFIX + ":"):
            event_id = discord_rsvp.parse_slot_custom_id(custom_id)
            slot_key, status = (data.get("values") or [None])[0], None
            if not slot_key:
                raise discord_rsvp.InteractionError("position picker sent no value")
        else:
            event_id, status = discord_rsvp.parse_custom_id(custom_id)
            slot_key = None
    except discord_rsvp.InteractionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    with get_session() as session:
        event = services.get_event(session, event_id)
        if event is None:
            return _interaction_note("That event no longer exists.")
        try:
            if slot_key:
                services.claim_slot(
                    session, event, discord_user_id=presser["id"], discord_name=presser["name"],
                    discord_avatar=presser["avatar"], slot_key=slot_key, source="discord",
                )
            else:
                services.set_signup(
                    session, event, discord_user_id=presser["id"], discord_name=presser["name"],
                    discord_avatar=presser["avatar"], status=status, source="discord",
                )
        except services.ServiceError as exc:
            # e.g. someone took that shirt a second earlier. Ephemeral, so
            # only the presser sees it, and the post stays as it was.
            return _interaction_note(str(exc))

        signups = services.list_signups(session, event.id)
        roles = services.tactics_roles_for(
            session, [s.discord_user_id for s in signups], _slot_labels())
        slots = services.event_slots(event)
        # Responding with UPDATE_MESSAGE re-renders the announcement in the
        # same round trip -- no follow-up PATCH, and no rate-limit cost.
        return {
            "type": discord_rsvp.RESPONSE_UPDATE_MESSAGE,
            "data": {
                "embeds": [discord_rsvp.build_embed(
                    event, signups, roles, _event_url(request, event), slots)],
                "components": discord_rsvp.build_components(event, signups, slots),
            },
        }


def _handle_offer_response(custom_id: str, presser: dict) -> dict:
    """One player answering their own squad offer.

    Three refusals, all ephemeral so only the presser sees them and the
    post stays as it was for everyone else: an id that isn't ours, a
    press by somebody the offer isn't for, and a press on an offer that
    is already settled.

    Accepting a PLAYER offer grants the configured squad role -- the only
    role write in this app, and one the player triggers for themselves. A
    staff offer grants nothing: the staff role is what controls this site,
    and a coach needn't be a player (see discord_roster.py). If the write
    fails, the ACCEPTANCE STILL STANDS: it's theirs, and a permissions
    problem on our side is not a reason to pretend they didn't answer.
    The failure is recorded against the row and surfaced on /roster,
    where somebody can fix it.
    """
    try:
        response, move_id = discord_roster.parse_offer_custom_id(custom_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="unrecognized roster button")

    with get_session() as session:
        move = services.get_roster_move(session, move_id)
        if move is None:
            return _interaction_note("That offer no longer exists.")
        if str(move.discord_id) != str(presser["id"]):
            return _interaction_note(
                "This offer isn't yours to answer -- only the player it names can.")
        if not services.offer_is_open(move):
            already = move.response or "closed"
            return _interaction_note(f"This offer has already been {already}.")

        if move.kind == discord_roster.MOVE_RENEWAL:
            return _handle_renewal_response(session, move, response)

        role_granted, role_error = False, None
        if (response == discord_roster.RESPONSE_ACCEPTED
                and move.kind == discord_roster.MOVE_OFFER
                and config.ROSTER_ROLE_GRANT_ENABLED):
            try:
                discord_roster.grant_squad_role(str(move.discord_id))
                role_granted = True
            except discord_roster.DiscordApiError as exc:
                role_error = str(exc)

        services.record_offer_response(
            session, move, response=response,
            role_granted=role_granted, role_error=role_error,
        )
        member = discord_roster.member_from_move(move)
        embed = discord_roster.build_move_embed(
            kind=move.kind, member=member, position=move.position, note=move.note,
            announced_by=move.announced_by_name, announced_at=move.announced_at,
            response=response, contract_weeks=move.contract_weeks,
            squad_status=move.squad_status, secondary_position=move.secondary_position,
        )

    # The member list now shows a role that changed, so don't serve a
    # minute-old copy of it to the next staff member to open /roster.
    if role_granted:
        discord_roster.invalidate_members_cache()

    # UPDATE_MESSAGE re-renders the offer in the same round trip, and
    # passing no components clears the buttons -- a settled offer with
    # live buttons only invites presses that can't be honoured.
    return {
        "type": discord_rsvp.RESPONSE_UPDATE_MESSAGE,
        "data": {"embeds": [embed], "components": []},
    }


def _interaction_note(text: str) -> dict:
    """An ephemeral reply, visible only to whoever pressed the button --
    for the cases where the press can't be honoured."""
    return {"type": 4, "data": {"content": text, "flags": 64}}


# --------------------------------------------------------------------------- #
# Gamertag link (how a Discord account maps to a Tactics position)
# --------------------------------------------------------------------------- #
@app.post("/me/gamertag")
def set_gamertag(request: Request, player_name: str = Form(""), csrf_token: str = Form(...),
                 redirect_to: str = Form("/events"), user=Depends(auth.require_member)):
    _check_csrf(request, csrf_token)
    with get_session() as session:
        try:
            if player_name.strip():
                services.set_player_link(
                    session, discord_user_id=int(user["id"]), player_name=player_name)
                _flash(request, f"Linked to {player_name.strip()}.")
            else:
                services.clear_player_link(session, int(user["id"]))
                _flash(request, "Gamertag unlinked.")
        except services.ServiceError as exc:
            _flash(request, str(exc), "warn")
    return RedirectResponse(_safe_redirect(redirect_to, "/events"), status_code=303)


# --------------------------------------------------------------------------- #
# Clips
# --------------------------------------------------------------------------- #
@app.get("/clips", response_class=HTMLResponse)
def clips_list(request: Request):
    with get_session() as session:
        clips = services.list_clips(session)
        return templates.TemplateResponse(request, "clips.html", _ctx(
            request, clips=clips, clips_enabled=config.CLIPS_SYNC_ENABLED,
        ))


@app.get("/api/clips")
def api_clips(_staff=Depends(auth.require_staff)):
    """Feeds the "Insert Clip" picker in the article editor -- staff-only,
    same as the editor page it's called from. Deliberately omits video_url:
    the picker only needs enough to identify a clip, and the URL would be
    dead within a day anyway (see services.render_clip_embeds)."""
    with get_session() as session:
        return [
            {
                "id": c.id,
                "title": c.title,
                "filename": c.filename,
                "authorName": c.author_name,
                "postedAt": c.posted_at.isoformat(),
            }
            for c in services.list_clips(session, limit=30)
        ]


# --------------------------------------------------------------------------- #
# Streamers
# --------------------------------------------------------------------------- #
@app.get("/streamers", response_class=HTMLResponse)
def streamers_page(request: Request):
    with get_session() as session:
        streamers = services.list_streamers(session)
        live = twitch_client.live_streams([s.twitch_login for s in streamers])
        featured_streamer = services.get_featured_streamer(session)
        other_streamers = [s for s in streamers if not featured_streamer or s.id != featured_streamer.id]
        return templates.TemplateResponse(request, "streamers.html", _ctx(
            request, featured_streamer=featured_streamer,
            featured_streamer_live=bool(featured_streamer and featured_streamer.twitch_login in live),
            streamers=other_streamers, live=live, twitch_enabled=config.TWITCH_ENABLED,
        ))


@app.post("/streamers/add")
async def streamer_add(
    request: Request, display_name: str = Form(...), twitch_login: str = Form(...),
    featured: str = Form(""), csrf_token: str = Form(...),
    avatar: UploadFile | None = None, staff=Depends(auth.require_staff),
):
    _check_csrf(request, csrf_token)
    avatar_uri, avatar_thumb = await services.process_image_upload(avatar)
    with get_session() as session:
        services.create_streamer(
            session, display_name=display_name, twitch_login=twitch_login,
            avatar=avatar_uri, avatar_thumb=avatar_thumb,
            author_name=staff.get("name", "Staff"), featured=bool(featured),
        )
        _flash(request, "Streamer added to the showcase.")
    return RedirectResponse("/streamers", status_code=303)


@app.post("/streamers/{streamer_id}/feature")
def streamer_feature(request: Request, streamer_id: int, csrf_token: str = Form(...), staff=Depends(auth.require_staff)):
    _check_csrf(request, csrf_token)
    with get_session() as session:
        streamer = services.get_streamer(session, streamer_id)
        if streamer is None:
            return templates.TemplateResponse(
                request, "error.html", _ctx(request, message="That streamer doesn't exist."), status_code=404,
            )
        services.set_featured_streamer(session, streamer)
        _flash(request, f"{streamer.display_name} is now the featured channel.")
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


def _handle_renewal_response(session, move, response: str) -> dict:
    """The player answering a contract renewal. No role is involved --
    they're already in the squad -- so accepting only extends the
    contract, and declining leaves it to run out as it was going to.

    Refused if the contract was released while the renewal sat
    unanswered: accepting would otherwise extend a deal the club has
    already ended.
    """
    contract = services.get_contract(session, move.contract_id) if move.contract_id else None
    if contract is None or contract.ended_at is not None:
        return _interaction_note(
            "This contract has already ended, so the renewal can't be accepted.")
    services.record_offer_response(
        session, move, response=response, role_granted=False, role_error=None)
    if response == discord_roster.RESPONSE_ACCEPTED:
        services.apply_renewal(session, contract, move)
    embed = discord_roster.build_move_embed(
        kind=move.kind, member=discord_roster.member_from_move(move),
        position=move.position, note=move.note, announced_by=move.announced_by_name,
        announced_at=move.announced_at, response=response,
        contract_weeks=move.contract_weeks, squad_status=move.squad_status,
        contract_ends_at=contract.expires_at, secondary_position=move.secondary_position,
    )
    return {
        "type": discord_rsvp.RESPONSE_UPDATE_MESSAGE,
        "data": {"embeds": [embed], "components": []},
    }


# --------------------------------------------------------------------------- #
# Squad moves: pick somebody out of Discord, announce an offer or a departure
# --------------------------------------------------------------------------- #
@app.get("/roster", response_class=HTMLResponse)
def roster_page(request: Request, _staff=Depends(auth.require_management)):
    """The picker. Reads the Discord member list live (cached briefly) so
    the only people who can be announced are people actually in the
    server.

    A failure to read that list is shown on the page rather than raised:
    the recent-moves history below it is still worth seeing, and the 403
    that a missing GUILD_MEMBERS intent produces is exactly the message
    somebody needs in order to go and fix it.
    """
    members, load_error = [], None
    if config.ROSTER_MOVES_ENABLED:
        try:
            members = discord_roster.roster_choices()
        except discord_roster.DiscordApiError as exc:
            load_error = str(exc)
    now = datetime.utcnow()
    with get_session() as session:
        moves = services.recent_roster_moves(session)
        contracts = services.live_contracts(session)
        renewals = services.open_renewals(session)
        # Existing links for anybody awaiting a signing confirmation, so
        # the gamertag field arrives pre-filled when there's one already.
        awaiting = [int(m.discord_id) for m in moves
                    if services.offer_awaits_confirmation(m) and str(m.discord_id).isdigit()]
        links = services.player_links_for(session, awaiting)
    history_names = (db.player_names(config.CLUB_PLATFORM, str(config.CLUB_ID))
                     if config.CLUB_ID else [])
    states = {c.id: services.contract_state(c, now) for c in contracts}
    return templates.TemplateResponse(request, "roster.html", _ctx(
        request, members=members, load_error=load_error, moves=moves,
        roster_enabled=config.ROSTER_MOVES_ENABLED,
        roster_missing=config.roster_moves_missing(),
        pitch_positions=discord_roster.PITCH_POSITIONS,
        staff_roles=discord_roster.STAFF_ROLES,
        squad_statuses=discord_roster.SQUAD_STATUSES,
        min_weeks=discord_roster.CONTRACT_MIN_WEEKS,
        max_weeks=discord_roster.CONTRACT_MAX_WEEKS,
        role_grant_enabled=config.ROSTER_ROLE_GRANT_ENABLED,
        offer_is_open=services.offer_is_open,
        awaits_confirmation=services.offer_awaits_confirmation,
        contracts=contracts, contract_states=states, renewals=renewals,
        links=links, gamertag_options=_gamertag_options(history_names),
        # Who is already signed, so the picker can say so next to their
        # name -- the difference between an offer and a renewal is
        # otherwise only discovered by getting refused.
        contracted={c.discord_id: c for c in contracts},
        expired_count=sum(1 for st in states.values() if st == services.CONTRACT_EXPIRED),
        time_left=lambda c: services.contract_time_left(c, now),
        weeks_label=discord_roster.weeks_label,
    ))


def _require_roster_configured() -> None:
    if not config.ROSTER_MOVES_ENABLED:
        raise services.ServiceError(
            "Squad announcements aren't configured -- missing "
            + ", ".join(config.roster_moves_missing()) + " in .env."
        )


def _resolve_member(discord_id: str) -> dict:
    """The picked member, re-resolved against the live list rather than
    trusted from the form, so a stale tab or a hand-edited id can't name
    somebody who isn't in the server."""
    try:
        member = discord_roster.find_member(discord_roster.roster_choices(), discord_id)
    except discord_roster.DiscordApiError as exc:
        raise services.ServiceError(f"Couldn't check Discord's member list: {exc}") from exc
    if member is None:
        raise services.ServiceError("Pick somebody from the Discord member list first.")
    return member


def _publish_move(staff: dict, member: dict, *, kind: str, position: str | None,
                  note: str | None = None, contract_weeks: int | None = None,
                  squad_status: str | None = None,
                  contract_id: int | None = None,
                  secondary_position: str | None = None) -> str | None:
    """Records one squad move and posts it. Returns why the post failed,
    or None if it went out.

    The row is written BEFORE the post, because an offer's buttons carry
    its row id -- there is no id to put in a custom_id until it exists.
    The message id is backfilled after, so a post that fails still leaves
    the honest record (and the page marks it undelivered).
    """
    with get_session() as session:
        move = services.record_roster_move(
            session, discord_id=member["id"], display_name=member["name"],
            avatar_url=member["avatar_url"], kind=kind, position=position, note=note,
            announced_by_name=staff.get("name"),
            announced_by_discord_id=_int(staff.get("id")) or None,
            discord_message_id=None, contract_weeks=contract_weeks,
            squad_status=squad_status, contract_id=contract_id,
            secondary_position=secondary_position,
        )
        move_id = move.id

    embed = discord_roster.build_move_embed(
        kind=kind, member=member, position=position, note=note,
        announced_by=staff.get("name"), contract_weeks=contract_weeks,
        squad_status=squad_status, secondary_position=secondary_position,
    )
    # Only offers and renewals are answerable. Nobody declines being let go.
    components = (discord_roster.build_offer_components(move_id)
                  if kind in discord_roster.ANSWERABLE_KINDS else None)
    try:
        message_id = discord_roster.announce_move(
            config.ROSTER_ANNOUNCE_CHANNEL_ID, embed, mention_id=member["id"],
            components=components,
        )
    except discord_roster.DiscordApiError as exc:
        return str(exc)
    with get_session() as session:
        services.set_roster_move_message(
            session, move_id, channel_id=config.ROSTER_ANNOUNCE_CHANNEL_ID,
            message_id=message_id,
        )
    return None


@app.post("/roster/announce")
def roster_announce(request: Request, discord_id: str = Form(""), kind: str = Form(""),
                    position: str = Form(""), secondary_position: str = Form(""),
                    note: str = Form(""),
                    contract_weeks: str = Form(""), squad_status: str = Form(""),
                    staff_role: str = Form(""), staff_note: str = Form(""),
                    release_position: str = Form(""), release_note: str = Form(""),
                    csrf_token: str = Form(...), staff=Depends(auth.require_management)):
    """Publishes one squad announcement.

    The page has three separate panels -- a player contract, a staff role,
    a departure -- sharing one member picker, so each posts its own
    fields and only the pressed panel's are read. A player offer carries
    the contract terms (both positions, length, squad status); a staff
    offer carries only the role; a departure ends whatever contract they
    were on.

    No Discord role is added or removed here; see discord_roster.py.
    """
    _check_csrf(request, csrf_token)
    _require_roster_configured()
    if kind not in discord_roster.ANNOUNCE_KINDS:
        raise services.ServiceError("Pick Offer Contract, Offer Staff Role or Let Go.")

    # Everything the form can get wrong is checked before anything is
    # looked up or written, so a bad submit costs nothing.
    weeks = status = secondary = None
    if kind == discord_roster.MOVE_OFFER:
        weeks, status = services.parse_contract_terms(contract_weeks, squad_status)
        position, secondary = services.parse_positions(position, secondary_position)
    elif kind == discord_roster.MOVE_STAFF_OFFER:
        position, note = services.parse_staff_role(staff_role), staff_note
    else:
        position, note = release_position, release_note

    member = _resolve_member(discord_id)
    if kind == discord_roster.MOVE_OFFER:
        with get_session() as session:
            if services.live_contract_for(session, member["id"]) is not None:
                raise services.ServiceError(
                    f"{member['name']} is already under contract -- renew it from "
                    f"the Contracts list instead of sending a new offer.")

    failure = _publish_move(staff, member, kind=kind, position=position, note=note,
                            contract_weeks=weeks, squad_status=status,
                            secondary_position=secondary)

    if kind == discord_roster.MOVE_RELEASE:
        # The decision is made whether or not the post went out -- a
        # failed post is flagged on the page to be re-sent, not a reason
        # to keep somebody on a contract the club has ended.
        with get_session() as session:
            contract = services.live_contract_for(session, member["id"])
            if contract is not None:
                services.end_contract(session, contract, ended_by_name=staff.get("name"))
            # Leaving the club ends a club role too, and its access here.
            player = services.get_player(session, member["id"])
            if player is not None and player.club_role:
                services.set_club_role(session, player, "")

    if failure:
        _flash(request, f"The announcement didn't send: {failure}", level="error")
    elif kind == discord_roster.MOVE_OFFER:
        _flash(request, f"Contract offered to {member['name']} — waiting on their answer in Discord.")
    elif kind == discord_roster.MOVE_STAFF_OFFER:
        _flash(request, f"{position} role offered to {member['name']} — waiting on their "
                        f"answer in Discord.")
    else:
        _flash(request, f"Departure announced for {member['name']}.")
        # A departure is usually followed by a role change made by hand in
        # Discord; don't serve a minute-old member list over the top of it.
        discord_roster.invalidate_members_cache()
    return RedirectResponse("/roster", status_code=303)


@app.post("/roster/{move_id}/confirm")
def roster_confirm(request: Request, move_id: int, csrf_token: str = Form(...),
                   player_name: str = Form(""), staff=Depends(auth.require_management)):
    """Publishes the signing announcement for an offer the player accepted
    -- or, for a staff offer, the appointment announcement.

    Deliberately a second, human step rather than something the
    acceptance triggers by itself: the player accepting is them agreeing,
    and the club announcing a signing is the club's own act. It also
    leaves room for the paperwork between the two.

    A signing can carry the player's gamertag (`player_name`), linked
    here so the squad screen can track their playing time from day one.
    Optional, because a new signing may not have joined the EA club yet;
    the squad screen flags anybody still unlinked.
    """
    _check_csrf(request, csrf_token)
    with get_session() as session:
        move = services.get_roster_move(session, move_id)
        if move is None:
            raise services.ServiceError("That squad move no longer exists.")
        if not services.offer_awaits_confirmation(move):
            # Covers every wrong state with the truth rather than a
            # generic refusal: not accepted yet, declined, already
            # confirmed, or a departure.
            raise services.ServiceError(
                "Only an offer that has been accepted, and that hasn't been "
                "confirmed yet, can be announced."
            )
        appointment = move.kind == discord_roster.MOVE_STAFF_OFFER
        # Checked before anything is posted: announcing a signing and then
        # failing to write its contract would leave the two disagreeing.
        if (not appointment and move.contract_weeks
                and services.live_contract_for(session, move.discord_id)):
            raise services.ServiceError(
                f"{move.display_name} is already under contract, so this offer "
                f"can't start another one. Renew the existing contract instead.")
        # Linked after every refusal above and before anything is posted: a
        # refused confirm writes nothing, and a gamertag another member
        # already holds stops the signing with nothing announced.
        linked_as = None
        if not appointment and player_name.strip():
            services.set_player_link(session, discord_user_id=int(move.discord_id),
                                     player_name=player_name)
            linked_as = player_name.strip()
        member = discord_roster.member_from_move(move)
        if appointment:
            embed = discord_roster.build_appointment_embed(
                member=member, role=move.position, confirmed_by=staff.get("name"))
        else:
            embed = discord_roster.build_signing_embed(
                member=member, position=move.position, confirmed_by=staff.get("name"),
                contract_weeks=move.contract_weeks, squad_status=move.squad_status,
                secondary_position=move.secondary_position,
            )
        # Back into the channel the offer went to, not wherever
        # ROSTER_ANNOUNCE_CHANNEL_ID points today -- the two can differ if
        # the setting changed between the offer and the signing.
        channel_id = move.discord_channel_id or config.ROSTER_ANNOUNCE_CHANNEL_ID
        confirm_message_id, failure = None, None
        try:
            confirm_message_id = discord_roster.announce_move(
                channel_id, embed, mention_id=member["id"],
            )
        except discord_roster.DiscordApiError as exc:
            failure = str(exc)
        services.confirm_roster_move(
            session, move, confirmed_by_name=staff.get("name"),
            confirm_message_id=confirm_message_id,
        )
        # The contract starts on the club's confirmation, not the
        # player's press -- that is the moment the signing is official.
        # Offers from before contracts existed have no terms to start, and
        # a staff appointment never has any.
        if not appointment and move.contract_weeks and move.squad_status:
            services.create_contract(
                session, discord_id=move.discord_id, display_name=move.display_name,
                avatar_url=move.avatar_url, position=move.position,
                secondary_position=move.secondary_position,
                squad_status=move.squad_status, weeks=move.contract_weeks,
                source="signing", signing_move_id=move.id,
                created_by_name=staff.get("name"), starts_at=move.confirmed_at,
            )
        # An appointment to a club role gives the person that role's
        # permissions here, from the moment the club announces it.
        role_given = False
        if appointment and move.position in roles.CLUB_ROLES:
            player = services.ensure_player(session, discord_id=move.discord_id,
                                            display_name=move.display_name,
                                            avatar_url=move.avatar_url)
            services.set_club_role(session, player, move.position)
            role_given = True
        name, role = move.display_name, move.position
        unlinked = (not appointment and linked_as is None
                    and services.get_player_link(session, int(move.discord_id)) is None)

    what = "appointment" if appointment else "signing"
    linked_note = f" Linked to {linked_as}." if linked_as else ""
    if failure:
        _flash(request, f"The {what} is recorded, but the announcement didn't send: {failure}"
                        + linked_note, level="error")
    elif appointment:
        _flash(request, f"{name}'s appointment{' as ' + role if role else ''} is announced."
                        + (f" They now have {role} access on the site." if role_given else ""))
    else:
        _flash(request, f"{name}'s signing is announced. Welcome to the squad." + linked_note)
    if unlinked:
        _flash(request, f"{name} has no gamertag linked yet — link it on the Squad page "
                        f"so their playing time is tracked.", "warn")
    return RedirectResponse("/roster", status_code=303)


# --------------------------------------------------------------------------- #
# Contracts: how long somebody is signed for, renewals and releases
# --------------------------------------------------------------------------- #
@app.post("/roster/contracts")
def roster_record_contract(request: Request, discord_id: str = Form(""),
                           position: str = Form(""), secondary_position: str = Form(""),
                           contract_weeks: str = Form(""),
                           squad_status: str = Form(""), csrf_token: str = Form(...),
                           staff=Depends(auth.require_management)):
    """Records a contract for somebody who is already in the squad.

    For players who joined before the site tracked contracts: they never
    had an offer to accept, and sending them one now would announce a
    signing of somebody who has been here all along. So nothing is posted
    and no role is touched -- this only writes down terms staff have
    already agreed with the player.
    """
    _check_csrf(request, csrf_token)
    _require_roster_configured()
    weeks, status = services.parse_contract_terms(contract_weeks, squad_status)
    position, secondary = services.parse_positions(position, secondary_position)
    member = _resolve_member(discord_id)
    with get_session() as session:
        contract = services.create_contract(
            session, discord_id=member["id"], display_name=member["name"],
            avatar_url=member["avatar_url"], position=position,
            secondary_position=secondary, squad_status=status,
            weeks=weeks, source="recorded", created_by_name=staff.get("name"),
        )
        until = contract.expires_at
    _flash(request, f"Contract recorded for {member['name']}: "
                    f"{discord_roster.weeks_label(weeks)}, {status}, "
                    f"until {until.strftime('%b %-d, %Y')}.")
    return RedirectResponse("/roster", status_code=303)


@app.post("/roster/contracts/{contract_id}/renew")
def roster_renew_contract(request: Request, contract_id: int,
                          contract_weeks: str = Form(""), squad_status: str = Form(""),
                          position: str = Form(""), secondary_position: str = Form(""),
                          note: str = Form(""),
                          csrf_token: str = Form(...), staff=Depends(auth.require_management)):
    """Offers the player a new contract, which they accept or decline in
    Discord exactly like the original offer.

    Nothing changes until they accept: a renewal is a question, and the
    current contract carries on -- and runs out -- as normal if they say
    no or never answer.
    """
    _check_csrf(request, csrf_token)
    _require_roster_configured()
    weeks, status = services.parse_contract_terms(contract_weeks, squad_status)
    with get_session() as session:
        contract = services.get_contract(session, contract_id)
        if contract is None or contract.ended_at is not None:
            raise services.ServiceError("That contract has ended, so it can't be renewed.")
        if contract_id in services.open_renewals(session):
            raise services.ServiceError(
                f"{contract.display_name} already has a renewal waiting on their answer.")
        member = {"id": contract.discord_id, "name": contract.display_name,
                  "avatar_url": contract.avatar_url or ""}
        # A renewal restates both positions. The contract's current primary
        # is accepted even if it predates the fixed position list, so a
        # legacy contract can be renewed without being forced to change.
        position, secondary = services.parse_positions(
            position.strip() or contract.position or "", secondary_position,
            keep=contract.position)
    # Refreshed from the live list when they're still in the server, so
    # the renewal shows their current name and face; the stored snapshot
    # is only the fallback for somebody Discord can't find right now.
    try:
        live = discord_roster.find_member(discord_roster.roster_choices(), member["id"])
    except discord_roster.DiscordApiError:
        live = None
    if live is not None:
        member = live

    failure = _publish_move(
        staff, member, kind=discord_roster.MOVE_RENEWAL, position=position, note=note,
        contract_weeks=weeks, squad_status=status, contract_id=contract_id,
        secondary_position=secondary,
    )
    if failure:
        _flash(request, f"The renewal didn't send: {failure}", level="error")
    else:
        _flash(request, f"Renewal sent to {member['name']} — waiting on their answer in Discord.")
    return RedirectResponse("/roster", status_code=303)


@app.post("/roster/contracts/{contract_id}/release")
def roster_release_contract(request: Request, contract_id: int,
                            note: str = Form(""), csrf_token: str = Form(...),
                            staff=Depends(auth.require_management)):
    """Ends a contract and announces the departure -- Let Go, started
    from the contract rather than the member picker, so it works for
    somebody who has already left the server too.

    Like Let Go, it never removes a role; see discord_roster.py.
    """
    _check_csrf(request, csrf_token)
    _require_roster_configured()
    with get_session() as session:
        contract = services.get_contract(session, contract_id)
        if contract is None or contract.ended_at is not None:
            raise services.ServiceError("That contract has already ended.")
        member = {"id": contract.discord_id, "name": contract.display_name,
                  "avatar_url": contract.avatar_url or ""}
        position = contract.position
        services.end_contract(session, contract, ended_by_name=staff.get("name"))

    failure = _publish_move(staff, member, kind=discord_roster.MOVE_RELEASE,
                            position=position, note=note)
    if failure:
        _flash(request, f"Contract ended, but the departure didn't send: {failure}",
               level="error")
    else:
        _flash(request, f"{member['name']} released — departure announced.")
    discord_roster.invalidate_members_cache()
    return RedirectResponse("/roster", status_code=303)


# --------------------------------------------------------------------------- #
# Squad screen: contracts against what EA says actually happened
# --------------------------------------------------------------------------- #
def _squad_usage() -> dict:
    return squad.current_usage()


def _gamertag_options(history_names) -> list[str]:
    """Gamertags to suggest when linking: everybody seen in a recorded
    match, plus the live EA club roster if it's already cached.

    Non-blocking on EA, so a slow or down API never holds up a staff page;
    the recorded history alone covers anybody who has actually played.
    """
    names = set(history_names)
    if config.CLUB_ID:
        try:
            roster = ea_client.member_stats(
                config.CLUB_PLATFORM, config.CLUB_ID, blocking=False) or {}
            names.update(m["name"] for m in roster.get("members", []) if m.get("name"))
        except ea_client.EAApiError:
            pass
    return sorted(names, key=str.casefold)


@app.get("/squad", response_class=HTMLResponse)
def squad_page(request: Request, _staff=Depends(auth.require_staff)):
    """Football Manager's squad screen, from real data: every contracted
    player's terms beside their appearances, form and attendance, with the
    mismatches flagged, and how well the current formation is covered."""
    now = datetime.utcnow()
    with get_session() as session:
        contracts = services.live_contracts(session)
        ids = [int(c.discord_id) for c in contracts]
        links = services.player_links_for(session, ids)
        attendance = services.attendance_records_for(session, ids)
        formation = services.get_active_formation(session)
    usage = _squad_usage()
    states = {c.id: services.contract_state(c, now) for c in contracts}
    rows = squad.squad_rows(
        contracts=contracts, links=links, usage=usage, attendance=attendance,
        contract_states=states, time_left=lambda c: services.contract_time_left(c, now),
    )
    slots = FORMATIONS.get(formation) or FORMATIONS["4-3-3"]
    return templates.TemplateResponse(request, "squad.html", _ctx(
        request, rows=rows, summary=squad.summarize(rows),
        depth=squad.squad_depth(slots, contracts), formation=formation,
        regulars=squad.uncontracted_regulars(usage, links, contracts),
        window=usage["window"], usage_window=squad.USAGE_WINDOW,
        min_window=squad.MIN_WINDOW_TO_JUDGE,
        gamertag_options=_gamertag_options(u["name"] for u in usage["players"].values()),
        has_history=bool(usage["players"]), club_configured=bool(config.CLUB_ID),
        form_label=squad.form_label, weeks_label=discord_roster.weeks_label,
    ))


@app.post("/squad/gamertag")
def squad_link_gamertag(request: Request, discord_id: str = Form(""),
                        player_name: str = Form(""), redirect_to: str = Form("/squad"),
                        csrf_token: str = Form(...), _staff=Depends(auth.require_staff)):
    """Staff linking a contracted player's gamertag on their behalf.

    Limited to people under contract: that's who the squad screen tracks,
    and staff have no reason to rewrite anybody else's link -- members
    still link their own from an event page. A gamertag somebody else has
    already claimed is refused, the same rule as self-service.
    """
    _check_csrf(request, csrf_token)
    back = _safe_redirect(redirect_to, "/squad")
    if not discord_id.isdigit():
        raise services.ServiceError("Pick a player to link.")
    with get_session() as session:
        contract = services.live_contract_for(session, discord_id)
        if contract is None:
            raise services.ServiceError("Only players under contract can be linked from here.")
        try:
            if player_name.strip():
                services.set_player_link(session, discord_user_id=int(discord_id),
                                         player_name=player_name)
                _flash(request, f"{contract.display_name} linked to {player_name.strip()}.")
            else:
                services.clear_player_link(session, int(discord_id))
                _flash(request, f"{contract.display_name}'s gamertag unlinked.")
        except services.ServiceError as exc:
            _flash(request, str(exc), "warn")
    return RedirectResponse(back, status_code=303)


# --------------------------------------------------------------------------- #
# Players: the personnel file
# --------------------------------------------------------------------------- #
# How many of a player's latest matches their file lists.
RECENT_MATCHES = 10


def _usage_for(usage: dict, gamertag: str | None) -> dict | None:
    return usage["players"].get(gamertag.casefold()) if gamertag else None


@app.get("/players", response_class=HTMLResponse)
def players_page(request: Request):
    """The squad list every member sees: who's on the staff, who's under
    contract at which status, and how each player is doing."""
    with get_session() as session:
        staff = services.club_staff(session)
        contracts = services.live_contracts(session)
        ids = {c.discord_id for c in contracts} | {p.discord_id for p in staff}
        people = services.players_by_id(session, list(ids))
        links = services.player_links_for(session, [int(i) for i in ids if i.isdigit()])
    usage = _squad_usage()
    groups = {status: [] for status in roles.SQUAD_STATUSES}
    for c in contracts:
        gamertag = links.get(int(c.discord_id)) if c.discord_id.isdigit() else None
        groups.setdefault(c.squad_status, []).append({
            "contract": c, "player": people.get(c.discord_id), "gamertag": gamertag,
            "usage": _usage_for(usage, gamertag),
        })
    for rows in groups.values():
        rows.sort(key=lambda r: r["contract"].display_name.casefold())
    return templates.TemplateResponse(request, "players.html", _ctx(
        request, staff=staff, groups=groups, window=usage["window"],
        squad_size=len(contracts), form_label=squad.form_label,
    ))


@app.get("/players/me")
def my_file(request: Request, user=Depends(auth.require_member)):
    return RedirectResponse(f"/players/{user['id']}", status_code=303)


@app.get("/players/{discord_id}", response_class=HTMLResponse)
def player_page(request: Request, discord_id: str):
    """One person's file. Every member sees anybody's profile and match
    stats. Their contract, attendance and squad-move history are for
    them and the staff. Coach notes are for the staff only -- including
    from the player they're about."""
    user = auth.current_user(request)
    if not discord_id.isdigit():
        return _not_found(request, "There's no player file at that address.")
    is_self = str(user["id"]) == discord_id
    is_staff = auth.is_staff(user)
    see_private = is_self or is_staff
    now = datetime.utcnow()
    with get_session() as session:
        player = services.get_player(session, discord_id)
        if player is None:
            return _not_found(request, "There's no player file for that person yet.")
        contract = services.live_contract_for(session, discord_id)
        link = services.get_player_link(session, int(discord_id))
        history = services.contract_history(session, discord_id) if see_private else []
        moves = services.moves_for(session, discord_id) if see_private else []
        attendance = services.attendance_record(session, int(discord_id)) if see_private else None
        # Never on your own file, even for staff: a coach who also plays
        # doesn't read what the other coaches wrote about them.
        notes_visible = is_staff and not is_self
        notes = services.list_coach_notes(session, discord_id) if notes_visible else []
        # Ratings: the coach's are the player's and the staff's alone.
        match_ratings = matchweek_routes.mw.ratings_history(session, discord_id) if see_private else []
        motm_total = matchweek_routes.mw.motm_wins(session).get(discord_id, 0)
    gamertag = link.player_name if link else None
    usage = _squad_usage()
    recent = []
    if gamertag and config.CLUB_ID:
        trend = db.player_trend(config.CLUB_PLATFORM, str(config.CLUB_ID), gamertag)
        for m in reversed(trend[-RECENT_MATCHES:]):
            m["played"] = (datetime.fromtimestamp(m["played_at"], tz=timezone.utc).replace(tzinfo=None)
                           if m.get("played_at") else None)
            recent.append(m)
    return templates.TemplateResponse(request, "player.html", _ctx(
        request, player=player, contract=contract, gamertag=gamertag,
        usage=_usage_for(usage, gamertag), window=usage["window"], recent=recent,
        history=history, moves=moves, attendance=attendance, notes=notes,
        is_self=is_self, see_private=see_private, notes_visible=notes_visible,
        match_ratings=match_ratings, motm_total=motm_total,
        can_set_role=auth.is_management(user) and not is_self,
        contract_state=services.contract_state(contract, now) if contract else None,
        time_left=services.contract_time_left(contract, now) if contract else None,
        club_roles=roles.CLUB_ROLES, preferred_feet=services.PREFERRED_FEET,
        form_label=squad.form_label, weeks_label=discord_roster.weeks_label,
        gamertag_options=_gamertag_options(u["name"] for u in usage["players"].values())
        if is_self and not gamertag else [],
    ))


@app.post("/players/{discord_id}/profile")
def player_update_profile(request: Request, discord_id: str, preferred_foot: str = Form(""),
                          archetype: str = Form(""), bio: str = Form(""),
                          csrf_token: str = Form(...), user=Depends(auth.require_member)):
    """A player's own details. Nobody else's: this is what they say about
    themselves."""
    _check_csrf(request, csrf_token)
    if str(user["id"]) != discord_id:
        raise services.ServiceError("You can only edit your own profile.")
    with get_session() as session:
        player = services.get_player(session, discord_id)
        if player is None:
            raise services.ServiceError("There's no player file for you yet.")
        services.update_player_profile(session, player, preferred_foot=preferred_foot,
                                       archetype=archetype, bio=bio)
    _flash(request, "Profile saved.")
    return RedirectResponse(f"/players/{discord_id}", status_code=303)


@app.post("/players/{discord_id}/role")
def player_set_role(request: Request, discord_id: str, club_role: str = Form(""),
                    csrf_token: str = Form(...), staff=Depends(auth.require_management)):
    """Gives or clears a club role directly, without an offer -- for
    setting the club up, or correcting it. Not your own: nobody promotes
    themselves, and nobody locks themselves out by mistake."""
    _check_csrf(request, csrf_token)
    if str(staff["id"]) == discord_id:
        raise services.ServiceError("You can't change your own club role.")
    with get_session() as session:
        player = services.get_player(session, discord_id)
        if player is None:
            raise services.ServiceError("There's no player file for that person.")
        services.set_club_role(session, player, club_role)
        name, role = player.display_name, player.club_role
    _flash(request, f"{name} is now {role}." if role else f"{name} no longer holds a club role.")
    return RedirectResponse(f"/players/{discord_id}", status_code=303)


@app.post("/players/{discord_id}/notes")
def player_add_note(request: Request, discord_id: str, body: str = Form(""),
                    csrf_token: str = Form(...), staff=Depends(auth.require_staff)):
    _check_csrf(request, csrf_token)
    if str(staff["id"]) == discord_id:
        raise services.ServiceError("Coach notes are about other people -- you can't see notes on yourself.")
    with get_session() as session:
        if services.get_player(session, discord_id) is None:
            raise services.ServiceError("There's no player file for that person.")
        services.add_coach_note(session, discord_id=discord_id, body=body, author=staff)
    _flash(request, "Note added. Only staff can see it.")
    return RedirectResponse(f"/players/{discord_id}#coach-notes", status_code=303)


@app.post("/players/{discord_id}/notes/{note_id}/delete")
def player_delete_note(request: Request, discord_id: str, note_id: int,
                       csrf_token: str = Form(...), staff=Depends(auth.require_staff)):
    """The note's author, or management, can remove it."""
    _check_csrf(request, csrf_token)
    with get_session() as session:
        note = services.get_coach_note(session, note_id)
        if note is None or note.discord_id != discord_id:
            raise services.ServiceError("That note no longer exists.")
        if not (auth.is_management(staff) or note.author_discord_id == str(staff["id"])):
            raise services.ServiceError("Only the note's author or management can delete it.")
        services.delete_coach_note(session, note)
    _flash(request, "Note deleted.")
    return RedirectResponse(f"/players/{discord_id}#coach-notes", status_code=303)


# --------------------------------------------------------------------------- #
# Club profile: what the public splash page says
# --------------------------------------------------------------------------- #
@app.get("/club-profile", response_class=HTMLResponse)
def club_profile_page(request: Request, _staff=Depends(auth.require_management)):
    with get_session() as session:
        profile = services.get_club_profile(session)
    return templates.TemplateResponse(request, "club_profile.html", _ctx(
        request, profile=profile, fields=services.CLUB_PROFILE_FIELDS,
    ))


@app.post("/club-profile")
async def club_profile_save(request: Request, staff=Depends(auth.require_management)):
    form = await request.form()
    _check_csrf(request, str(form.get("csrf_token") or ""))
    values = {key: str(form.get(key) or "") for key in services.CLUB_PROFILE_FIELDS}
    with get_session() as session:
        services.save_club_profile(session, values, by_name=staff.get("name"))
    _flash(request, "Club profile saved. It's live on the public page.")
    return RedirectResponse("/club-profile", status_code=303)


# --------------------------------------------------------------------------- #
# Stats dashboard (locked to our own club -- see ea_client.py / db.py)
# --------------------------------------------------------------------------- #
@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    return templates.TemplateResponse(request, "stats.html", _ctx(
        request, club_platform=config.CLUB_PLATFORM, club_id=config.CLUB_ID,
    ))


# --------------------------------------------------------------------------- #
# League table (auto-built from clubs we actually play -- see db.py)
# --------------------------------------------------------------------------- #
@app.get("/league", response_class=HTMLResponse)
def league_page(request: Request):
    table = []
    excluded = []
    our_snapshot = None
    roster_size = 0
    if config.CLUB_ID:
        table = db.league_table(config.CLUB_PLATFORM, config.CLUB_ID)
        our_snapshot = db.latest_snapshot(config.CLUB_PLATFORM, config.CLUB_ID)
        roster = db.league_roster(config.CLUB_PLATFORM)
        roster_size = len(roster)
        # Clubs in the roster but not the main table -- either a different
        # division (a real, working exclusion) or no snapshot yet (just
        # added, hasn't been polled). league_table() only ever returns the
        # first kind, so surface the difference here instead of a tracked
        # club silently vanishing with no explanation (see README.md).
        shown_ids = {row["club_id"] for row in table}
        for entry in roster:
            if entry["club_id"] in shown_ids:
                continue
            snap = db.latest_snapshot(config.CLUB_PLATFORM, entry["club_id"])
            excluded.append({
                "label": entry["label"] or entry["club_id"],
                "division": snap.get("division") if snap else None,
            })
    return templates.TemplateResponse(request, "league.html", _ctx(
        request, club_id=config.CLUB_ID, table=table, excluded=excluded,
        our_division=our_snapshot.get("division") if our_snapshot else None,
        max_teams=config.LEAGUE_TABLE_MAX_TEAMS, roster_size=roster_size,
    ))


# Formation/bench definitions live in formations.py -- services.py needs
# them too (validating a claimed position), and importing app.py from
# there would be a cycle. Re-exported here so the existing references
# and templates keep working unchanged.


@app.get("/tactics", response_class=HTMLResponse)
def tactics_page(request: Request):
    with get_session() as session:
        active_formation = services.get_active_formation(session)
        all_slots = services.get_all_tactics_slots(session, list(FORMATIONS.keys()))
    return templates.TemplateResponse(request, "tactics.html", _ctx(
        request, formations=FORMATIONS, active_formation=active_formation, all_slots=all_slots,
        bench_slots=BENCH_SLOTS,
    ))


@app.post("/api/tactics")
def api_tactics_save(
    request: Request, formation: str = Form(...), slots_json: str = Form(...),
    csrf_token: str = Form(...), staff=Depends(auth.require_staff),
):
    _check_csrf(request, csrf_token)
    if formation not in FORMATIONS:
        return JSONResponse({"error": f"Unknown formation {formation!r}."}, status_code=400)
    try:
        slots = json.loads(slots_json)
        if not isinstance(slots, dict):
            raise ValueError("slots must be an object")
    except (ValueError, TypeError):
        return JSONResponse({"error": "Malformed lineup data."}, status_code=400)

    with get_session() as session:
        try:
            services.save_tactics_lineup(
                session, formation=formation, slots=slots,
                valid_slot_keys=set(FORMATIONS[formation]) | set(BENCH_SLOTS), staff_name=staff["name"],
            )
        except services.ServiceError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    return {"ok": True}


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
    # No division is returned, deliberately -- see the module docstring on
    # ea_client.division_stats. EA has no live division field, the one it
    # does return is a snapshot that can be a hundred matches stale, and it
    # can't be derived from skill rating either (promotion/relegation set
    # it, and EA publishes no rating thresholds). Rather than show a wrong
    # number or ask someone to retype the right one every promotion, the
    # site doesn't report a division. `points` is dropped for the same
    # reason: its only source was that same stale record.
    return {
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


@app.get("/api/history/rivals")
def api_history_rivals():
    return {
        "trackedSince": db.tracked_since(config.CLUB_PLATFORM, config.CLUB_ID),
        "rivals": db.rival_records(config.CLUB_PLATFORM, config.CLUB_ID),
    }


@app.get("/api/streamers/live")
def api_streamers_live():
    with get_session() as session:
        streamers = services.list_streamers(session)
    return twitch_client.live_streams([s.twitch_login for s in streamers])
