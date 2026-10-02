"""Pages for training sessions: the Training tab (upcoming sessions, past
reviews, the suggestion box) and each session's plan editor. Rules are in
training.py."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

import auth
import services
import training
from database import get_session
from formations import BENCH_SLOTS, FORMATIONS
from web import check_csrf, flash, not_found, render

router = APIRouter()


@router.get("/training")
def training_page(request: Request, user=Depends(auth.require_member)):
    is_staff = auth.is_staff(user)
    with get_session() as session:
        upcoming = training.upcoming_sessions(session)
        recent = training.recent_sessions(session)
        suggestions = training.list_suggestions(session, None if is_staff else str(user["id"]))
    return render(request, "training.html", upcoming=upcoming, recent=recent,
                  suggestions=suggestions, statuses=training.SUGGESTION_STATUSES,
                  status_labels=training.SUGGESTION_LABELS)


@router.post("/training/suggestions")
def training_suggest(request: Request, body: str = Form(""), csrf_token: str = Form(...),
                     user=Depends(auth.require_member)):
    check_csrf(request, csrf_token)
    with get_session() as session:
        training.add_suggestion(session, discord_id=str(user["id"]),
                                name=user.get("name") or "Player", body=body)
    flash(request, "Thanks — the coaches will see it.")
    return RedirectResponse("/training", status_code=303)


@router.post("/training/suggestions/{suggestion_id}")
def training_suggestion_status(request: Request, suggestion_id: int, status: str = Form(""),
                               staff_note: str = Form(""), csrf_token: str = Form(...),
                               staff=Depends(auth.require_staff)):
    check_csrf(request, csrf_token)
    with get_session() as session:
        training.set_suggestion_status(session, suggestion_id, status=status, note=staff_note,
                                       by_name=staff.get("name") or "Staff")
    flash(request, "Suggestion updated.")
    return RedirectResponse("/training#suggestions", status_code=303)


@router.get("/events/{event_id}/plan")
def plan_page(request: Request, event_id: int, _staff=Depends(auth.require_staff)):
    with get_session() as session:
        event = services.get_event(session, event_id)
        if event is None:
            return not_found(request, "That event no longer exists.")
        briefs = training.briefs(event)
    return render(request, "session_plan.html", event=event, briefs=briefs,
                  pitch=FORMATIONS.get(event.formation or "", {}), bench_slots=BENCH_SLOTS,
                  is_past=event.scheduled_at <= datetime.utcnow())


@router.post("/events/{event_id}/plan")
async def plan_save(request: Request, event_id: int, staff=Depends(auth.require_staff)):
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token") or ""))
    with get_session() as session:
        event = services.get_event(session, event_id)
        if event is None:
            return not_found(request, "That event no longer exists.")
        if form.get("action") == "review":
            training.save_review(session, event, str(form.get("review") or ""),
                                 staff.get("name") or "Staff")
            flash(request, "Session review saved.")
            return RedirectResponse(f"/events/{event_id}#session-plan", status_code=303)
        briefs = {key[len("brief__"):]: str(value) for key, value in form.items()
                  if key.startswith("brief__")}
        training.save_plan(session, event, objective=str(form.get("objective") or ""),
                           plan=str(form.get("plan") or ""), role_briefs=briefs)
        # The Discord post carries the plan, so it's brought up to date too.
        from app import _refresh_announcement
        _refresh_announcement(request, session, event)
    flash(request, "Session plan saved.")
    return RedirectResponse(f"/events/{event_id}#session-plan", status_code=303)
