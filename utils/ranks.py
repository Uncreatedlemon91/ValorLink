"""Helpers for working with the rank ladder, stored in the `ranks` table and
managed live via /rank_add, /rank_remove, /rank_move, /rank_set_role.
"""
from db.models import Rank


def all_ranks(session) -> list[Rank]:
    return session.query(Rank).order_by(Rank.position).all()


def rank_names(session) -> list[str]:
    return [r.name for r in all_ranks(session)]


def rank_by_name(session, name: str) -> Rank | None:
    if not name:
        return None
    return session.query(Rank).filter(Rank.name == name).one_or_none()


def lowest_rank_name(session) -> str | None:
    ranks = all_ranks(session)
    return ranks[0].name if ranks else None


def default_rank_name(session) -> str:
    """Rank assigned on enlistment -- the lowest rung, or a placeholder if
    no ranks have been configured yet via /rank_add."""
    return lowest_rank_name(session) or "Unranked"


def next_rank(session, name: str) -> str | None:
    names = rank_names(session)
    if name not in names:
        return None
    i = names.index(name)
    return names[i + 1] if i + 1 < len(names) else None


def prev_rank(session, name: str) -> str | None:
    names = rank_names(session)
    if name not in names:
        return None
    i = names.index(name)
    return names[i - 1] if i > 0 else None
