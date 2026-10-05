"""Development goals and reviews, from the player's file. Rules are in
development.py."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

import auth
import config
import development as dev
import discord_notify
import services
from database import get_session
from web import check_csrf, flash

router = APIRouter()


def _back(discord_id: str) -> RedirectResponse:
    return RedirectResponse(f"/players/{discord_id}#development", status_code=303)


def _player_or_error(session, discord_id: str):
    player = services.get_player(session, discord_id) if discord_id.isdigit() else None
    if player is None:
        raise services.ServiceError("There's no player file for that person.")
    return player


@router.post("/players/{discord_id}/goals")
def goal_set(request: Request, discord_id: str, text: str = Form(""), area: str = Form(""),
             due_on: str = Form(""), csrf_token: str = Form(...),
             staff=Depends(auth.require_staff)):
    check_csrf(request, csrf_token)
    try:
        due = date.fromisoformat(due_on) if due_on.strip() else None
    except ValueError:
        raise services.ServiceError("Give the target date as day, month and year.")
    with get_session() as session:
        player = _player_or_error(session, discord_id)
        goal = dev.set_goal(session, discord_id=discord_id, text=text, area=area,
                            due_on=due, coach=staff)
        name, goal_text = player.display_name, goal.text
    note = ""
    if config.NOTIFY_ENABLED and config.NOTIFY_DMS:
        try:
            discord_notify.send_dm(discord_id, discord_notify.goal_dm(goal_text, staff.get("name")))
        except discord_notify.DiscordApiError:
            note = " (They couldn't be DMed -- their DMs are closed.)"
    flash(request, f"Goal set for {name}.{note}", "warn" if note else "ok")
    return _back(discord_id)


@router.post("/players/{discord_id}/goals/{goal_id}/progress")
def goal_progress(request: Request, discord_id: str, goal_id: int, progress: str = Form(""),
                  note: str = Form(""), csrf_token: str = Form(...),
                  user=Depends(auth.require_member)):
    check_csrf(request, csrf_token)
    with get_session() as session:
        goal = dev.get_goal(session, goal_id)
        if goal is None or goal.discord_id != discord_id:
            raise services.ServiceError("That goal no longer exists.")
        dev.update_progress(session, goal, user["id"], progress=progress, note=note)
    flash(request, "Progress saved.")
    return _back(discord_id)


@router.post("/players/{discord_id}/goals/{goal_id}/status")
def goal_status(request: Request, discord_id: str, goal_id: int, status: str = Form(""),
                csrf_token: str = Form(...), staff=Depends(auth.require_staff)):
    check_csrf(request, csrf_token)
    with get_session() as session:
        goal = dev.get_goal(session, goal_id)
        if goal is None or goal.discord_id != discord_id:
            raise services.ServiceError("That goal no longer exists.")
        dev.close_goal(session, goal, status=status, by_name=staff.get("name") or "Coach")
    flash(request, {"achieved": "Goal achieved.", "dropped": "Goal dropped.",
                    "open": "Goal reopened."}[status])
    return _back(discord_id)


@router.post("/players/{discord_id}/reviews")
def review_save(request: Request, discord_id: str, month: str = Form(""), summary: str = Form(""),
                csrf_token: str = Form(...), staff=Depends(auth.require_staff)):
    check_csrf(request, csrf_token)
    with get_session() as session:
        _player_or_error(session, discord_id)
        dev.save_review(session, discord_id=discord_id, month=month, summary=summary, coach=staff)
    flash(request, "Review saved.")
    return _back(discord_id)
