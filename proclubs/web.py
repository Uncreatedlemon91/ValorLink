"""What every page route shares: the template environment, the base
context, flash messages, the CSRF check and safe redirects.

Its own module so the feature routers (matchweek_routes.py and friends)
can use these without importing app.py, which imports them.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

import auth
import config
import roles
import services

BASE_DIR = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def ctx(request: Request, **extra) -> dict:
    user = auth.current_user(request)
    out = {
        "request": request,
        "user": user,
        "is_staff": auth.is_staff(user),
        "is_member": auth.is_member(user),
        "is_management": auth.is_management(user),
        "viewer_level": user["level"] if user else roles.GUEST,
        "csrf_token": auth.get_csrf_token(request),
        "DISCORD_INVITE_URL": config.DISCORD_INVITE_URL,
    }
    out.update(extra)
    return out


def flash(request: Request, text: str, level: str = "ok"):
    request.session.setdefault("flash", []).append({"level": level, "text": text})


def pop_flash(request: Request) -> list[dict]:
    return request.session.pop("flash", [])


def check_csrf(request: Request, token: str):
    if not auth.verify_csrf(request, token):
        raise services.ServiceError("Your session expired before that finished submitting. Please try again.")


def safe_redirect(target: str, default: str) -> str:
    """Only same-site paths -- "//evil.example" is a protocol-relative URL
    to somewhere else entirely."""
    return target if target.startswith("/") and not target.startswith("//") else default


def not_found(request: Request, message: str):
    return templates.TemplateResponse(request, "error.html", ctx(request, message=message),
                                      status_code=404)


def render(request: Request, template: str, **extra):
    return templates.TemplateResponse(request, template, ctx(request, **extra))
