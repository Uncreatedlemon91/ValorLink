"""Staff tools' pages: the squad planner, the trials pipeline and the
set-piece book. (The action inbox is on the home dashboard.)"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

import auth
import config
import discord_notify
import discord_roster
import matchweek as mw
import recruitment
import role_settings
import role_sync
import roles
import services
import setpieces
import staff_tools
from database import get_session
from formations import FORMATIONS
from models import Event
from web import check_csrf, flash, not_found, render

router = APIRouter()


# --------------------------------------------------------------------------- #
# Squad planner
# --------------------------------------------------------------------------- #
@router.get("/squad/planner")
def planner_page(request: Request, _staff=Depends(auth.require_staff)):
    with get_session() as session:
        contracts = services.live_contracts(session)
        formation = services.get_active_formation(session)
        prospects = [p for p in recruitment.board(session)["trial"] + recruitment.board(session)["offered"]
                     if p.position]
    params = request.query_params
    changes, signings = staff_tools.parse_planner_form(params, contracts)
    slots = FORMATIONS.get(formation) or FORMATIONS["4-3-3"]
    now_plan = staff_tools.plan_summary(staff_tools.plan_squad(contracts, {}, []), slots)
    planned = staff_tools.plan_squad(contracts, changes, signings)
    order = {s: i for i, s in enumerate(roles.SQUAD_STATUSES)}
    contracts = sorted(contracts, key=lambda c: (order.get(c.squad_status, 9), c.display_name.casefold()))
    return render(request, "planner.html", contracts=contracts, changes=changes, signings=signings,
                  plan=staff_tools.plan_summary(planned, slots), now_plan=now_plan,
                  formation=formation, statuses=staff_tools.PLANNER_STATUSES,
                  squad_statuses=roles.SQUAD_STATUSES, positions=discord_roster.PITCH_POSITIONS,
                  max_signings=staff_tools.MAX_PLANNED_SIGNINGS, prospects=prospects,
                  is_what_if=bool(changes or signings))


def _sync(request: Request, discord_id: str | None) -> None:
    """The Trialist role follows the pipeline (role_sync.py)."""
    if discord_id:
        problem = role_sync.sync_quietly(discord_id)
        if problem:
            flash(request, problem, "warn")


# --------------------------------------------------------------------------- #
# Recruitment: from joining the server to settling in (recruitment.py)
# --------------------------------------------------------------------------- #
def _members_ready() -> bool:
    return bool(config.DISCORD_BOT_TOKEN and config.DISCORD_GUILD_ID)


def _guild_members() -> tuple[list[dict], str | None]:
    """The raw member list (cached), or why it couldn't be read."""
    if not _members_ready():
        return [], None
    try:
        return discord_roster.guild_members(), None
    except discord_roster.DiscordApiError as exc:
        return [], str(exc)


def _member(discord_id: str) -> dict:
    """Somebody picked out of the server, re-resolved against the live list."""
    try:
        member = discord_roster.find_member(discord_roster.roster_choices(), discord_id)
    except discord_roster.DiscordApiError as exc:
        raise services.ServiceError(f"Couldn't read Discord's member list: {exc}") from exc
    if member is None:
        raise services.ServiceError("They're no longer in the Discord server.")
    return member


def _prospect_or_404(session, request: Request, prospect_id: int):
    p = recruitment.get_prospect(session, prospect_id)
    if p is None:
        return None, not_found(request, "That prospect is no longer in the pipeline.")
    return p, None


@router.get("/recruitment")
def recruitment_page(request: Request, _staff=Depends(auth.require_staff)):
    members, members_error = _guild_members()
    with get_session() as session:
        board = recruitment.board(session)
        summaries = recruitment.summaries(session)
        arrivals = recruitment.new_arrivals(session, members) if members else []
        progress = {p.id: recruitment.progress(recruitment.onboarding(session, p)) for p in board["signed"]}
    return render(request, "recruitment.html", board=board, stages=recruitment.STAGES,
                  labels=recruitment.STAGE_LABELS, positions=discord_roster.PITCH_POSITIONS,
                  summaries=summaries, arrivals=arrivals, members_ready=_members_ready(),
                  members_error=members_error, progress=progress,
                  arrival_days=recruitment.ARRIVAL_WINDOW.days,
                  feedback_enabled=config.RECRUITMENT_FEEDBACK_ENABLED)


@router.post("/recruitment")
def recruitment_add(request: Request, name: str = Form(""), discord_id: str = Form(""),
                    gamertag: str = Form(""), position: str = Form(""), secondary: str = Form(""),
                    source: str = Form(""), csrf_token: str = Form(...),
                    staff=Depends(auth.require_staff)):
    check_csrf(request, csrf_token)
    with get_session() as session:
        p = recruitment.add_prospect(session, name=name, discord_id=discord_id, gamertag=gamertag,
                                     position=position, secondary=secondary, source=source,
                                     added_by=staff.get("name") or "Staff")
        pid = p.id
    flash(request, f"{name.strip()} added to the pipeline.")
    return RedirectResponse(f"/recruitment/{pid}", status_code=303)


@router.post("/recruitment/arrivals/{discord_id}/start")
def recruitment_start_file(request: Request, discord_id: str, csrf_token: str = Form(...),
                           staff=Depends(auth.require_staff)):
    check_csrf(request, csrf_token)
    member = _member(discord_id)
    with get_session() as session:
        p = recruitment.start_file(session, member, added_by=staff.get("name") or "Staff")
        pid = p.id
    flash(request, f"File started for {member['name']}. Add their positions and gamertag.")
    return RedirectResponse(f"/recruitment/{pid}", status_code=303)


@router.post("/recruitment/arrivals/{discord_id}/dismiss")
def recruitment_dismiss(request: Request, discord_id: str, csrf_token: str = Form(...),
                        staff=Depends(auth.require_staff)):
    check_csrf(request, csrf_token)
    with get_session() as session:
        recruitment.dismiss_arrival(session, discord_id, staff.get("name") or "Staff")
    flash(request, "Dismissed — they won't show as a new arrival again.")
    return RedirectResponse("/recruitment", status_code=303)


@router.get("/recruitment/{prospect_id}")
def prospect_page(request: Request, prospect_id: int, _staff=Depends(auth.require_staff)):
    now = datetime.utcnow()
    with get_session() as session:
        p, missing = _prospect_or_404(session, request, prospect_id)
        if missing:
            return missing
        notes = recruitment.notes_for(session, p.id)
        matches = list(session.execute(select(Event).where(
            Event.scheduled_at <= now + timedelta(days=14), Event.scheduled_at >= now - timedelta(days=30))
            .order_by(Event.scheduled_at.desc())).scalars())
        offer = recruitment.latest_offer(session, p)
        steps = recruitment.onboarding(session, p) if p.stage == "signed" else []
        contract = services.live_contract_for(session, p.discord_id) if p.discord_id else None
        link = services.get_player_link(session, int(p.discord_id)) if p.discord_id else None
    choices = []
    if not p.discord_id and _members_ready():
        try:
            choices = discord_roster.roster_choices()
        except discord_roster.DiscordApiError:
            choices = []
    trial_matches = sum(1 for _, ev in notes if ev is not None)
    return render(request, "prospect.html", p=p, notes=notes, avg=recruitment.average_rating(notes),
                  stages=recruitment.STAGES, labels=recruitment.STAGE_LABELS,
                  positions=discord_roster.PITCH_POSITIONS, trial_matches=trial_matches,
                  matches=[m for m in matches if mw.is_match(m) or m.event_type in ("Training", "Theory")],
                  offer=offer, offer_state=recruitment.offer_state(offer), contract=contract,
                  linked_gamertag=link.player_name if link else None,
                  steps=steps, step_progress=recruitment.progress(steps) if steps else None,
                  member_choices=choices, roster_enabled=config.ROSTER_MOVES_ENABLED,
                  squad_statuses=discord_roster.SQUAD_STATUSES,
                  min_weeks=discord_roster.CONTRACT_MIN_WEEKS, max_weeks=discord_roster.CONTRACT_MAX_WEEKS,
                  feedback_enabled=config.RECRUITMENT_FEEDBACK_ENABLED, dms_enabled=config.NOTIFY_ENABLED)


@router.post("/recruitment/{prospect_id}/edit")
def prospect_edit(request: Request, prospect_id: int, name: str = Form(""), discord_id: str = Form(""),
                  gamertag: str = Form(""), position: str = Form(""), secondary: str = Form(""),
                  source: str = Form(""), csrf_token: str = Form(...), _staff=Depends(auth.require_staff)):
    check_csrf(request, csrf_token)
    with get_session() as session:
        p, missing = _prospect_or_404(session, request, prospect_id)
        if missing:
            return missing
        before = p.discord_id
        recruitment.update_prospect(session, p, name=name, discord_id=discord_id, gamertag=gamertag,
                                    position=position, secondary=secondary, source=source)
        after = p.discord_id
    flash(request, "Details saved.")
    if before != after:
        _sync(request, before)
        _sync(request, after)
    return RedirectResponse(f"/recruitment/{prospect_id}", status_code=303)


@router.post("/recruitment/{prospect_id}/stage")
def prospect_stage(request: Request, prospect_id: int, stage: str = Form(""),
                   csrf_token: str = Form(...), _staff=Depends(auth.require_staff)):
    check_csrf(request, csrf_token)
    with get_session() as session:
        p, missing = _prospect_or_404(session, request, prospect_id)
        if missing:
            return missing
        recruitment.set_stage(session, p, stage)
        did = p.discord_id
    flash(request, f"Moved to {recruitment.STAGE_LABELS[stage]}.")
    _sync(request, did)
    return RedirectResponse(f"/recruitment/{prospect_id}", status_code=303)


@router.post("/recruitment/{prospect_id}/notes")
def prospect_note(request: Request, prospect_id: int, body: str = Form(""), rating: str = Form(""),
                  event_id: str = Form(""), csrf_token: str = Form(...),
                  staff=Depends(auth.require_staff)):
    check_csrf(request, csrf_token)
    with get_session() as session:
        p, missing = _prospect_or_404(session, request, prospect_id)
        if missing:
            return missing
        note = recruitment.add_note(session, p, body=body, rating=rating, event_id=event_id,
                                    author=staff.get("name") or "Staff")
        failure = recruitment.post_feedback(session, p, note)
        did = p.discord_id
    if failure:
        flash(request, f"Note saved, but it couldn't be posted to the recruitment channel: {failure}", "warn")
    elif config.RECRUITMENT_FEEDBACK_ENABLED:
        flash(request, "Note added and posted to the recruitment channel.")
    else:
        flash(request, "Note added.")
    _sync(request, did)  # a trial note can move them onto trial
    return RedirectResponse(f"/recruitment/{prospect_id}", status_code=303)


@router.post("/recruitment/{prospect_id}/onboarding")
def prospect_onboarding(request: Request, prospect_id: int, step: str = Form(""), done: str = Form(""),
                        csrf_token: str = Form(...), _staff=Depends(auth.require_staff)):
    check_csrf(request, csrf_token)
    with get_session() as session:
        p, missing = _prospect_or_404(session, request, prospect_id)
        if missing:
            return missing
        recruitment.tick(session, p, step, done == "1")
    return RedirectResponse(f"/recruitment/{prospect_id}#settling-in", status_code=303)


@router.post("/recruitment/{prospect_id}/welcome")
def prospect_welcome(request: Request, prospect_id: int, csrf_token: str = Form(...),
                     _staff=Depends(auth.require_staff)):
    check_csrf(request, csrf_token)
    with get_session() as session:
        p, missing = _prospect_or_404(session, request, prospect_id)
        if missing:
            return missing
        if not p.discord_id:
            raise services.ServiceError("Link their Discord account first.")
        if not config.NOTIFY_ENABLED:
            raise services.ServiceError("The bot isn't set up (DISCORD_BOT_TOKEN), so it can't send DMs.")
        try:
            discord_notify.send_dm(p.discord_id, recruitment.welcome_dm(p))
        except discord_notify.DiscordApiError as exc:
            flash(request, f"The welcome DM didn't send — they may have DMs from server members "
                           f"turned off ({exc}). Message them yourself and tick it off.", "warn")
            return RedirectResponse(f"/recruitment/{prospect_id}#settling-in", status_code=303)
        recruitment.tick(session, p, "welcome", True)
        name = p.name
    flash(request, f"Welcome message sent to {name}.")
    return RedirectResponse(f"/recruitment/{prospect_id}#settling-in", status_code=303)


# --------------------------------------------------------------------------- #
# The set-piece book
# --------------------------------------------------------------------------- #
@router.get("/set-pieces")
def setpieces_page(request: Request, user=Depends(auth.require_member)):
    with get_session() as session:
        book = setpieces.book(session)
        people = mw.squad_people(session) if auth.is_staff(user) else []
    return render(request, "setpieces.html", book=book, kinds=setpieces.KINDS, sides=setpieces.SIDES,
                  people=people, total=sum(len(v) for v in book.values()))


async def _save_piece(request: Request, staff: dict, piece_id: int | None):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token") or ""))
    with get_session() as session:
        piece = setpieces.get(session, piece_id) if piece_id else None
        if piece_id and piece is None:
            return not_found(request, "That routine no longer exists.")
        taker_id = str(form.get("taker_id") or "")
        names = {p["id"]: p["name"] for p in mw.squad_people(session)}
        setpieces.save(session, piece, name=str(form.get("name") or ""), kind=str(form.get("kind") or ""),
                       side=str(form.get("side") or ""), taker_id=taker_id,
                       taker_name=names.get(taker_id, ""), routine=str(form.get("routine") or ""),
                       targets=str(form.get("targets") or ""), by_name=staff.get("name") or "Staff")
    flash(request, "Routine saved.")
    return RedirectResponse("/set-pieces", status_code=303)


@router.post("/set-pieces")
async def setpieces_add(request: Request, staff=Depends(auth.require_staff)):
    return await _save_piece(request, staff, None)


@router.post("/set-pieces/{piece_id}")
async def setpieces_edit(request: Request, piece_id: int, staff=Depends(auth.require_staff)):
    return await _save_piece(request, staff, piece_id)


@router.post("/set-pieces/{piece_id}/delete")
def setpieces_delete(request: Request, piece_id: int, csrf_token: str = Form(...),
                     _staff=Depends(auth.require_staff)):
    check_csrf(request, csrf_token)
    with get_session() as session:
        piece = setpieces.get(session, piece_id)
        if piece is not None:
            setpieces.delete(session, piece)
    flash(request, "Routine deleted.")
    return RedirectResponse("/set-pieces", status_code=303)


# --------------------------------------------------------------------------- #
# Discord roles: the whole server against the site
# --------------------------------------------------------------------------- #
def _members() -> tuple[list[dict], str | None]:
    try:
        return discord_roster.fetch_guild_members(), None
    except discord_roster.DiscordApiError as exc:
        return [], str(exc)


def _server_roles() -> tuple[list[dict] | None, int | None, str | None]:
    """(roles, bot's top position, error). roles is None when the bot isn't
    set up or Discord couldn't be read -- the page falls back to typed ids."""
    if not role_settings.bot_ready():
        return None, None, None
    try:
        roles, bot_top = role_settings.fetch_server_roles()
        return roles, bot_top, None
    except discord_roster.DiscordApiError as exc:
        return None, None, str(exc)


@router.get("/discord-roles")
def discord_roles_page(request: Request, _staff=Depends(auth.require_management)):
    with get_session() as session:
        current = role_settings.current(session)
        managed = role_sync.managed(session)
        enabled = role_settings.bot_ready() and bool(managed)
        members, error = _members() if enabled else ([], None)
        plan = role_sync.server_plan(session, members) if members else []
    server_roles, bot_top, roles_error = _server_roles()
    by_id = {r["id"]: r for r in server_roles or []}
    return render(request, "discord_roles.html", enabled=enabled, bot_ready=role_settings.bot_ready(),
                  error=error, managed=managed, keys=role_settings.KEYS, labels=role_sync.LABELS,
                  current=current, server_roles=server_roles, roles_by_id=by_id, bot_top=bot_top,
                  roles_error=roles_error, holders=role_sync.holders(members, managed),
                  plan=plan, member_count=len(members))


@router.post("/discord-roles/settings")
async def discord_roles_settings(request: Request, staff=Depends(auth.require_management)):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token") or ""))
    server_roles, _bot_top, _err = _server_roles()
    values = {key: str(form.get(f"role__{key}") or "") for key in role_settings.KEYS}
    with get_session() as session:
        before = role_sync.managed(session)
        try:
            role_settings.save(session, values, by_name=staff.get("name") or "Management",
                               server_roles=server_roles)
        except services.ServiceError as exc:
            flash(request, f"Not saved: {exc}", "error")
            return RedirectResponse("/discord-roles", status_code=303)
        after = role_sync.managed(session)
    dropped = [k for k, v in before.items() if after.get(k) != v]
    message = "Discord roles saved."
    if dropped:
        message += (" The site no longer manages the old " + ", ".join(dropped)
                    + " role — nobody loses it, so take it off by hand if it should go.")
    flash(request, message)
    return RedirectResponse("/discord-roles", status_code=303)


@router.post("/discord-roles/apply")
def discord_roles_apply(request: Request, csrf_token: str = Form(...),
                        _staff=Depends(auth.require_management)):
    check_csrf(request, csrf_token)
    if not role_sync.enabled():
        raise services.ServiceError("No managed Discord roles are set.")
    members, error = _members()
    if error:
        raise services.ServiceError(f"Couldn't read the server's members: {error}")
    # Worked out again here rather than trusted from the page, so what's
    # applied is what's true now.
    with get_session() as session:
        plan = role_sync.server_plan(session, members)
    changed, errors = role_sync.apply_plan(plan)
    discord_roster.invalidate_members_cache()
    if errors:
        flash(request, f"Updated {changed} member(s); {len(errors)} failed — first: {errors[0]}", "warn")
    else:
        flash(request, f"Discord roles brought in line for {changed} member(s)." if changed
              else "Discord already matches the site.")
    return RedirectResponse("/discord-roles", status_code=303)
