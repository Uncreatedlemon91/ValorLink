"""The 'current unit database' for the bot, as an ambient context variable.

The one bot serves many Discord guilds, each mapped to its own unit database.
While the bot handles an interaction (or a queued action) for a given guild, a
context variable holds that unit's database URL; :func:`db.base.db_session`
opens sessions against it. When the variable is unset — a single-guild
deployment, or code paths not bound to a guild — everything falls back to the
one configured ``DATABASE_URL``, so existing behaviour is unchanged.

Context variables are per-async-task, so each interaction is bound
independently and nothing leaks between concurrently-handled guilds.
"""
import contextvars

_current_db_url: contextvars.ContextVar = contextvars.ContextVar(
    "valorlink_db_url", default=None
)


def current_db_url() -> str | None:
    return _current_db_url.get()


def set_current_db_url(url: str | None):
    """Bind the current unit database; returns a token for :func:`reset`."""
    return _current_db_url.set(url)


def reset_current_db_url(token) -> None:
    _current_db_url.reset(token)
