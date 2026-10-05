"""Pages for the match-week loop: availability, position preferences, the
team sheet, and the match report with its vote and ratings. The rules
are in matchweek.py; what the bot posts is in discord_notify.py.
"""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

import auth
import config
import db
import discord_notify
import discord_roster
import matchweek as mw
import services
import squad
from database import get_session
from formations import BENCH_SLOTS, FORMATIONS
from web import check_csrf, flash, not_found, render

router = APIRouter()


def _event_or_404(session, request, event_id):
    event = services.get_event(session, event_id)
    return event, (None if event else not_found(request, "That event no longer exists."))


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        raise services.ServiceError("Give the dates as day, month and year.")


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #
@router.get("/availability")
def availability_page(request: Request, user=Depends(auth.require_member)):
    today = datetime.utcnow().date()
    me = str(user["id"])
    with get_session() as session:
        people = mw.squad_people(session)
        grid = mw.availability_grid(session, people, today)
        pattern = mw.get_pattern(session, me)
        away = mw.list_away(session, me, from_day=today)
    return render(request, "availability.html", grid=grid, pattern=pattern, away=away,
                  weekdays=mw.WEEKDAYS, today=today, in_squad=any(p["id"] == me for p in people))


@router.post("/availability/pattern")
async def availability_set_pattern(request: Request, user=Depends(auth.require_member)):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token") or ""))
    days = []
    for value in form.getlist("day"):
        if not str(value).isdigit():
            raise services.ServiceError("Those aren't days of the week.")
        days.append(int(value))
    with get_session() as session:
        mw.set_pattern(session, str(user["id"]), days, str(form.get("note") or ""))
    flash(request, "Your usual nights are saved.")
    return RedirectResponse("/availability", status_code=303)


@router.post("/availability/away")
def availability_add_away(request: Request, starts_on: str = Form(""), ends_on: str = Form(""),
                          note: str = Form(""), csrf_token: str = Form(...),
                          user=Depends(auth.require_member)):
    check_csrf(request, csrf_token)
    start = _parse_day(starts_on)
    end = _parse_day(ends_on) if ends_on.strip() else start
    with get_session() as session:
        mw.add_away(session, str(user["id"]), start, end, note)
    flash(request, "Away dates added.")
    return RedirectResponse("/availability", status_code=303)


@router.post("/availability/away/{away_id}/delete")
def availability_delete_away(request: Request, away_id: int, csrf_token: str = Form(...),
                             user=Depends(auth.require_member)):
    check_csrf(request, csrf_token)
    with get_session() as session:
        mw.delete_away(session, away_id, str(user["id"]))
    flash(request, "Away dates removed.")
    return RedirectResponse("/availability", status_code=303)


# --------------------------------------------------------------------------- #
# Position preferences (on the event page)
# --------------------------------------------------------------------------- #
@router.post("/events/{event_id}/preferences")
def event_preferences(request: Request, event_id: int, pref_1: str = Form(""),
                      pref_2: str = Form(""), pref_3: str = Form(""),
                      csrf_token: str = Form(...), user=Depends(auth.require_member)):
    check_csrf(request, csrf_token)
    with get_session() as session:
        event, missing = _event_or_404(session, request, event_id)
        if missing:
            return missing
        mw.set_preferences(session, event, int(user["id"]), [pref_1, pref_2, pref_3])
    flash(request, "Your positions are saved.")
    return RedirectResponse(f"/events/{event_id}#your-answer", status_code=303)


def event_page_extras(session, event, user) -> dict:
    """What the event page adds for the match week: your preferences,
    your shirt, the published team sheet, and the report's state."""
    me = str(user["id"]) if user else None
    signup = None
    if me:
        from models import EventSignup
        from sqlalchemy import select
        signup = session.execute(select(EventSignup).where(
            EventSignup.event_id == event.id,
            EventSignup.discord_user_id == int(me))).scalar_one_or_none()
    published = []
    if event.lineup_published_at:
        slots = services.event_slots(event)
        rows = mw.lineup_for(session, event.id)
        published = [{"key": k, "label": slots.get(k, k), "id": str(r.discord_user_id),
                      "name": r.display_name, "bench": k in BENCH_SLOTS}
                     for k, r in rows.items()]
        order = list(slots)
        published.sort(key=lambda p: order.index(p["key"]) if p["key"] in order else 99)
    return {
        "my_prefs": mw.preferences(signup),
        "my_shirt": mw.my_shirt(session, event, me) if me else None,
        "published_sheet": published,
        "is_match": mw.is_match(event),
        "vote_state": mw.vote_state(event),
        "pitch_positions": discord_roster.PITCH_POSITIONS,
    }


# --------------------------------------------------------------------------- #
# The team sheet
# --------------------------------------------------------------------------- #
@router.get("/events/{event_id}/teamsheet")
def teamsheet_page(request: Request, event_id: int, _staff=Depends(auth.require_staff)):
    usage = squad.current_usage()
    with get_session() as session:
        event, missing = _event_or_404(session, request, event_id)
        if missing:
            return missing
        ids = [int(p["id"]) for p in mw.squad_people(session) if p["id"].isdigit()]
        links = services.player_links_for(session, ids)
        candidates = mw.team_sheet_candidates(session, event, usage, links)
        draft = mw.draft_lineup(session, event)
    formation = FORMATIONS.get(event.formation or "", {})
    return render(request, "teamsheet.html", event=event, candidates=candidates, draft=draft,
                  pitch=formation, bench_slots=BENCH_SLOTS, formations=list(FORMATIONS),
                  by_id={c["id"]: c for c in candidates}, form_label=squad.form_label,
                  notify_enabled=config.NOTIFY_ENABLED)


@router.post("/events/{event_id}/teamsheet")
async def teamsheet_save(request: Request, event_id: int, staff=Depends(auth.require_staff)):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token") or ""))
    action = str(form.get("action") or "save")
    with get_session() as session:
        event, missing = _event_or_404(session, request, event_id)
        if missing:
            return missing
        new_formation = str(form.get("formation") or "")
        if action == "formation":
            if new_formation not in FORMATIONS:
                raise services.ServiceError("Pick a formation from the list.")
            event.formation = new_formation
            session.commit()
            flash(request, f"Formation set to {new_formation}.")
            return RedirectResponse(f"/events/{event_id}/teamsheet", status_code=303)
        names = {c["id"]: c["name"] for c in mw.team_sheet_candidates(session, event)}
        picks = {key[len("slot__"):]: str(value) for key, value in form.items()
                 if key.startswith("slot__")}
        lineup = mw.save_lineup(session, event, picks, names)
        if action != "publish":
            flash(request, "Team sheet saved. Players see it once you publish it.")
            return RedirectResponse(f"/events/{event_id}/teamsheet", status_code=303)
        if not lineup:
            raise services.ServiceError("Pick at least one player before publishing.")
        message_id, problems = _publish_lineup(event, lineup, staff.get("name"))
        mw.mark_lineup_published(session, event, message_id)
    if problems:
        flash(request, "Team sheet published on the site. " + " ".join(problems), "warn")
    else:
        flash(request, "Team sheet published" + (" and sent to Discord." if config.NOTIFY_ENABLED else "."))
    return RedirectResponse(f"/events/{event_id}", status_code=303)


def _publish_lineup(event, lineup: dict, by_name: str | None) -> tuple[str | None, list[str]]:
    """Posts the sheet and DMs each player their shirt. Returns the
    message id (if posted) and anything that went wrong, for the flash."""
    if not config.NOTIFY_ENABLED:
        return None, []
    slots = services.event_slots(event)
    order = list(slots)
    rows = sorted(lineup.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99)
    xi = [(slots[k], str(r.discord_user_id), r.display_name) for k, r in rows if k not in BENCH_SLOTS]
    bench = [(slots[k], str(r.discord_user_id), r.display_name) for k, r in rows if k in BENCH_SLOTS]
    problems, message_id = [], None
    try:
        message_id = discord_notify.post(
            discord_notify.channel_for(event),
            embeds=[discord_notify.lineup_embed(event, xi, bench, by_name)],
            mention_ids=[uid for _, uid, _ in xi + bench])
    except discord_notify.DiscordApiError as exc:
        problems.append(f"The Discord post failed: {exc}.")
    shirt = {uid: (label, k not in BENCH_SLOTS)
             for k, r in rows for label, uid in [(slots[k], str(r.discord_user_id))]}
    _, failed = discord_notify.dm_many(
        list(shirt), lambda uid: discord_notify.shirt_dm(event, shirt[uid][0], shirt[uid][1]))
    if failed:
        problems.append(f"{len(failed)} player(s) couldn't be DMed (their DMs are closed).")
    return message_id, problems


# --------------------------------------------------------------------------- #
# After the match: the report, the vote, ratings
# --------------------------------------------------------------------------- #
def _ea_night(event) -> tuple[list[dict], list[dict]]:
    """EA's matches from this match night, and their player ratings."""
    if not config.CLUB_ID:
        return [], []
    near = mw.ea_matches_near(event, db.match_history(config.CLUB_PLATFORM, str(config.CLUB_ID)))
    ratings = db.match_player_ratings(str(config.CLUB_ID), [m["match_id"] for m in near])
    return near, ratings


def _top_rated(ratings: list[dict], name_for: dict[str, str], n: int = 3) -> list[tuple[str, float]]:
    """Average EA rating per player across the night, best first."""
    totals: dict[str, list[float]] = {}
    for r in ratings:
        if r.get("rating") is not None:
            totals.setdefault(r["player_name"], []).append(r["rating"])
    avg = sorted(((name_for.get(k.casefold(), k), sum(v) / len(v)) for k, v in totals.items()),
                 key=lambda kv: -kv[1])
    return avg[:n]


def _gamertag_names(session) -> dict[str, str]:
    """casefolded gamertag -> the player's display name, for EA rows."""
    from models import PlayerLink, Player
    from sqlalchemy import select
    names = {p.discord_id: p.display_name for p in session.execute(select(Player)).scalars()}
    return {link.player_name.casefold(): names.get(str(link.discord_user_id), link.player_name)
            for link in session.execute(select(PlayerLink)).scalars()}


@router.get("/events/{event_id}/report")
def report_page(request: Request, event_id: int):
    user = auth.current_user(request)
    me = str(user["id"])
    is_staff = auth.is_staff(user)
    with get_session() as session:
        event, missing = _event_or_404(session, request, event_id)
        if missing:
            return missing
        players = mw.participants(session, event)
        ratings = mw.ratings_for(session, event.id)
        result = mw.motm_result(session, event.id)
        names = {p["id"]: p["name"] for p in players}
        near, ea_ratings = _ea_night(event)
        top = _top_rated(ea_ratings, _gamertag_names(session))
        mine = ratings.get(me)
        my_vote = mw.my_vote(session, event.id, me)
    return render(
        request, "match_report.html", event=event, players=players, ratings=ratings,
        result=result, names=names, near=near, top=top, mine=mine, my_vote=my_vote,
        is_participant=any(p["id"] == me for p in players), vote_state=mw.vote_state(event),
        is_past=event.scheduled_at <= datetime.utcnow(), is_match=mw.is_match(event),
        clips=mw.clip_links(event), rating_words=mw.RATING_WORDS, me=me,
        can_edit=is_staff, notify_enabled=config.NOTIFY_ENABLED,
    )


@router.post("/events/{event_id}/report/mine")
def report_mine(request: Request, event_id: int, nominee_id: str = Form(""),
                self_rating: str = Form(""), csrf_token: str = Form(...),
                user=Depends(auth.require_member)):
    check_csrf(request, csrf_token)
    with get_session() as session:
        event, missing = _event_or_404(session, request, event_id)
        if missing:
            return missing
        if nominee_id:
            mw.cast_vote(session, event, user["id"], nominee_id)
        if self_rating:
            mw.set_self_rating(session, event, user["id"], user.get("name") or "Player", self_rating)
    flash(request, "Thanks — your vote and rating are in.")
    return RedirectResponse(f"/events/{event_id}/report", status_code=303)


@router.post("/events/{event_id}/report")
async def report_save(request: Request, event_id: int, staff=Depends(auth.require_staff)):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token") or ""))
    action = str(form.get("action") or "save")
    with get_session() as session:
        event, missing = _event_or_404(session, request, event_id)
        if missing:
            return missing
        mw.save_review(session, event, us_score=form.get("us_score"), opp_score=form.get("opp_score"),
                       notes=str(form.get("notes") or ""), clips=str(form.get("clips") or ""))
        players = mw.participants(session, event)
        names = {p["id"]: p["name"] for p in players}
        ratings = {p["id"]: (str(form.get(f"rating__{p['id']}") or ""),
                             str(form.get(f"comment__{p['id']}") or "")) for p in players}
        mw.set_coach_ratings(session, event, ratings, names, staff.get("name") or "Staff")
        if action != "publish":
            flash(request, "Report saved. Coach ratings are visible to each player now; "
                           "the report goes out when you publish it.")
            return RedirectResponse(f"/events/{event_id}/report", status_code=303)
        mw.publish_review(session, event, staff.get("name") or "Staff")
        problem = _post_report(session, event)
    if problem:
        flash(request, f"Report published on the site, but the Discord post failed: {problem}", "warn")
    else:
        flash(request, "Report published" + (" and posted to Discord." if config.NOTIFY_ENABLED else "."))
    return RedirectResponse(f"/events/{event_id}/report", status_code=303)


def _post_report(session, event) -> str | None:
    if not config.NOTIFY_ENABLED:
        return None
    result = mw.motm_result(session, event.id)
    names = {p["id"]: p["name"] for p in mw.participants(session, event)}
    _, ea_ratings = _ea_night(event)
    top = _top_rated(ea_ratings, _gamertag_names(session))
    winners = [names.get(w, "A teammate") for w in result["winners"]]
    try:
        discord_notify.post(discord_notify.channel_for(event), embeds=[discord_notify.report_embed(
            event, motm_names=winners,
            motm_votes=max(result["votes"].values(), default=0), top_rated=top,
            clips=mw.clip_links(event))])
    except discord_notify.DiscordApiError as exc:
        return str(exc)
    return None


@router.post("/events/{event_id}/vote/open")
def vote_open(request: Request, event_id: int, csrf_token: str = Form(...),
              _staff=Depends(auth.require_staff)):
    check_csrf(request, csrf_token)
    with get_session() as session:
        event, missing = _event_or_404(session, request, event_id)
        if missing:
            return missing
        mw.open_vote(session, event)
        problem = post_vote(session, event)
    flash(request, "Voting is open." + (f" The Discord post failed: {problem}" if problem else ""),
          "warn" if problem else "ok")
    return RedirectResponse(f"/events/{event_id}/report", status_code=303)


def post_vote(session, event) -> str | None:
    """Posts the vote message once (the notifier and the button share
    this, and the notification log stops a second post)."""
    if not config.NOTIFY_ENABLED or mw.already_sent(session, "vote", str(event.id)):
        return None
    players = mw.participants(session, event)
    if not players:
        return None
    try:
        message_id = discord_notify.post(discord_notify.channel_for(event),
                                         **discord_notify.vote_message(event, players))
    except discord_notify.DiscordApiError as exc:
        return str(exc)
    event.vote_message_id = message_id
    mw.mark_sent(session, "vote", str(event.id))
    return None


def handle_picker(custom_id: str, values: list, presser: dict) -> dict:
    """A Man of the Match vote or a self-rating from the Discord post.
    Always answered privately, so the post itself never changes."""
    try:
        kind, event_id = discord_notify.parse_picker(custom_id)
    except ValueError:
        return _note("That control isn't recognised.")
    value = (values or [None])[0]
    with get_session() as session:
        event = services.get_event(session, event_id)
        if event is None:
            return _note("That match no longer exists.")
        try:
            if kind == discord_notify.MOTM_PREFIX:
                mw.cast_vote(session, event, presser["id"], value)
                name = next((p["name"] for p in mw.participants(session, event) if p["id"] == str(value)),
                            "your pick")
                return _note(f"Vote counted: **{name}**. You can change it until voting closes.")
            row = mw.set_self_rating(session, event, presser["id"], presser["name"], value)
            return _note(f"Rated your game **{row.self_rating}/10**. "
                         f"The coach's rating shows on your player file.")
        except services.ServiceError as exc:
            return _note(str(exc))


def _note(text: str) -> dict:
    return {"type": 4, "data": {"content": text, "flags": 64}}
