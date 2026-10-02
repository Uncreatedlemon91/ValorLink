"""Staff tools' pages: the squad planner, the trials pipeline and the
set-piece book. (The action inbox is on the home dashboard.)"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

import auth
import discord_roster
import matchweek as mw
import recruitment
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


# --------------------------------------------------------------------------- #
# Recruitment: the trials pipeline
# --------------------------------------------------------------------------- #
@router.get("/recruitment")
def recruitment_page(request: Request, _staff=Depends(auth.require_staff)):
    with get_session() as session:
        board = recruitment.board(session)
    return render(request, "recruitment.html", board=board, stages=recruitment.STAGES,
                  labels=recruitment.STAGE_LABELS, positions=discord_roster.PITCH_POSITIONS)


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


@router.get("/recruitment/{prospect_id}")
def prospect_page(request: Request, prospect_id: int, _staff=Depends(auth.require_staff)):
    now = datetime.utcnow()
    with get_session() as session:
        p = recruitment.get_prospect(session, prospect_id)
        if p is None:
            return not_found(request, "That prospect is no longer in the pipeline.")
        notes = recruitment.notes_for(session, p.id)
        matches = list(session.execute(select(Event).where(
            Event.scheduled_at <= now + timedelta(days=14), Event.scheduled_at >= now - timedelta(days=30))
            .order_by(Event.scheduled_at.desc())).scalars())
    return render(request, "prospect.html", p=p, notes=notes, avg=recruitment.average_rating(notes),
                  stages=recruitment.STAGES, labels=recruitment.STAGE_LABELS,
                  matches=[m for m in matches if mw.is_match(m) or m.event_type in ("Training", "Theory")])


@router.post("/recruitment/{prospect_id}/stage")
def prospect_stage(request: Request, prospect_id: int, stage: str = Form(""),
                   csrf_token: str = Form(...), _staff=Depends(auth.require_staff)):
    check_csrf(request, csrf_token)
    with get_session() as session:
        p = recruitment.get_prospect(session, prospect_id)
        if p is None:
            return not_found(request, "That prospect is no longer in the pipeline.")
        recruitment.set_stage(session, p, stage)
    flash(request, f"Moved to {recruitment.STAGE_LABELS[stage]}.")
    return RedirectResponse(f"/recruitment/{prospect_id}", status_code=303)


@router.post("/recruitment/{prospect_id}/notes")
def prospect_note(request: Request, prospect_id: int, body: str = Form(""), rating: str = Form(""),
                  event_id: str = Form(""), csrf_token: str = Form(...),
                  staff=Depends(auth.require_staff)):
    check_csrf(request, csrf_token)
    with get_session() as session:
        p = recruitment.get_prospect(session, prospect_id)
        if p is None:
            return not_found(request, "That prospect is no longer in the pipeline.")
        recruitment.add_note(session, p, body=body, rating=rating, event_id=event_id,
                             author=staff.get("name") or "Staff")
    flash(request, "Note added.")
    return RedirectResponse(f"/recruitment/{prospect_id}", status_code=303)


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
