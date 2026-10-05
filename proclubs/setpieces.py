"""The set-piece book: named routines with a taker, a description and the
targets, grouped by kind. Every member reads it; staff write it."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import SetPiece
from services import ServiceError

KINDS = ("Corner", "Free kick", "Penalty", "Throw-in", "Kick-off")
SIDES = ("Left", "Right", "Central", "Either")


def book(session: Session) -> dict[str, list[SetPiece]]:
    rows = session.execute(select(SetPiece).order_by(SetPiece.position, SetPiece.id)).scalars()
    out = {k: [] for k in KINDS}
    for r in rows:
        out.setdefault(r.kind, []).append(r)
    return out


def get(session: Session, piece_id: int) -> SetPiece | None:
    return session.get(SetPiece, piece_id)


def save(session: Session, piece: SetPiece | None, *, name: str, kind: str, side: str,
         taker_id: str, taker_name: str, routine: str, targets: str, by_name: str) -> SetPiece:
    name = (name or "").strip()
    if not name:
        raise ServiceError("Name the routine.")
    if len(name) > 60:
        raise ServiceError("Keep the name under 60 characters.")
    if kind not in KINDS:
        raise ServiceError("Pick what kind of set piece it is.")
    if side and side not in SIDES:
        raise ServiceError("Pick a side from the list.")
    routine, targets = (routine or "").strip(), (targets or "").strip()
    if len(routine) > 1500:
        raise ServiceError("Keep the routine under 1,500 characters.")
    if len(targets) > 200:
        raise ServiceError("Keep the targets under 200 characters.")
    if piece is None:
        top = session.execute(select(func.max(SetPiece.position))).scalar() or 0
        piece = SetPiece(position=top + 1)
        session.add(piece)
    piece.name, piece.kind, piece.side = name, kind, side or None
    piece.taker_id = taker_id if (taker_id or "").isdigit() else None
    piece.taker_name = (taker_name or "").strip()[:60] or None if piece.taker_id else None
    piece.routine, piece.targets, piece.updated_by = routine or None, targets or None, by_name
    session.commit()
    return piece


def delete(session: Session, piece: SetPiece) -> None:
    session.delete(piece)
    session.commit()
