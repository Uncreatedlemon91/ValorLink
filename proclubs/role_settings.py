"""Which Discord role is which: set on the site, by management, on
/discord-roles.

Each managed key (Starter, Coach, Trialist, ...) maps to one Discord role
id, kept in ClubSetting under "discord_role:<key>". A key the page has
never saved falls back to its old .env variable (config.MANAGED_ROLE_SETTINGS),
so an install that set them there keeps working until somebody saves the
page; from then on the page is the only source. Saving a key blank turns it
off, whatever .env says.

Three things stay in .env on purpose: the bot token and guild id (secrets
and wiring, not club choices), and DISCORD_STAFF_ROLE_ID -- the way into
the site's management, which the site itself can't edit and never manages.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

import config
import discord_api
from database import get_session
from models import ClubSetting
from services import ServiceError

# The managed keys, in the order the page shows them, with what each one is
# for. role_sync.py decides who should hold each.
KEYS = {
    "Club President": "Club President on the site",
    "Head Coach": "Head Coach on the site",
    "Coach": "Coach on the site",
    "Squad": "Anyone under contract",
    "Starter": "Under contract as a Starter",
    "Rotation": "Under contract as Rotation",
    "Substitute": "Under contract as a Substitute",
    "Trialist": "On trial or offered, not yet signed",
}
_PREFIX = "discord_role:"


def _stored(session: Session) -> dict[str, str]:
    rows = session.execute(select(ClubSetting).where(ClubSetting.key.like(_PREFIX + "%"))).scalars()
    return {row.key[len(_PREFIX):]: (row.value or "") for row in rows}


def current(session: Session | None = None) -> dict[str, dict]:
    """key -> {"value": role id or "", "source": "site" | "env" | ""}."""
    if session is None:
        with get_session() as s:
            return current(s)
    stored = _stored(session)
    out = {}
    for key in KEYS:
        if key in stored:
            out[key] = {"value": stored[key], "source": "site"}
        else:
            env = (config.ROSTER_SQUAD_ROLE_ID if key == "Squad"
                   else config.MANAGED_ROLE_SETTINGS.get(key) or "").strip()
            out[key] = {"value": env, "source": "env" if env else ""}
    return out


def managed_ids(session: Session | None = None) -> dict[str, str]:
    """key -> role id for the roles that are set, never the staff role."""
    values = {k: v["value"] for k, v in current(session).items()}
    return config.managed_role_ids(values, config.DISCORD_STAFF_ROLE_ID)


def bot_ready() -> bool:
    return bool(config.DISCORD_BOT_TOKEN and config.DISCORD_GUILD_ID)


def sync_enabled(session: Session | None = None) -> bool:
    return bot_ready() and bool(managed_ids(session))


def squad_role_id(session: Session | None = None) -> str:
    return managed_ids(session).get("Squad", "")


def role_grant_enabled(session: Session | None = None) -> bool:
    """Whether a player's own Accept on an offer adds the Squad role."""
    return bool(config.ROSTER_MOVES_ENABLED and squad_role_id(session))


def save(session: Session, values: dict[str, str], *, by_name: str | None,
         server_roles: list[dict] | None = None) -> None:
    """Saves every key at once. `server_roles` (fetch_server_roles), when
    the bot could read them, is used to refuse ids that aren't a role the
    bot could ever hand out."""
    cleaned = {key: (values.get(key) or "").strip() for key in KEYS}
    known = {r["id"]: r for r in server_roles} if server_roles is not None else None
    seen: dict[str, str] = {}
    for key, value in cleaned.items():
        if not value:
            continue
        if not value.isdigit():
            raise ServiceError(f"The {key} role should be a Discord role ID (a long number).")
        if int(value) == config.DISCORD_STAFF_ROLE_ID:
            raise ServiceError(f"The {key} role can't be the Discord staff role — "
                               "that's the way into the site's management, and the site never manages it.")
        if config.DISCORD_GUILD_ID and int(value) == config.DISCORD_GUILD_ID:
            raise ServiceError(f"The {key} role can't be @everyone.")
        if known is not None:
            role = known.get(value)
            if role is None:
                raise ServiceError(f"The {key} role isn't a role in the server.")
            if role["managed"]:
                raise ServiceError(f"{role['name']} belongs to a bot or integration, so Discord won't let "
                                   "anybody hand it out.")
        if value in seen:
            raise ServiceError(f"{key} and {seen[value]} are set to the same role — each needs its own.")
        seen[value] = key
    for key, value in cleaned.items():
        row = session.get(ClubSetting, _PREFIX + key)
        if row is None:
            session.add(ClubSetting(key=_PREFIX + key, value=value, updated_by_name=by_name))
        else:
            row.value = value
            row.updated_by_name = by_name
    session.commit()


# --------------------------------------------------------------------------- #
# The server's roles, for the page's pickers
# --------------------------------------------------------------------------- #
_bot_user_id: str | None = None


def fetch_server_roles() -> tuple[list[dict], int | None]:
    """The guild's roles, highest first, as [{id, name, position, managed,
    color, above_bot}], and the position of the bot's own highest role (None
    if that couldn't be read). @everyone is left out. Raises DiscordApiError."""
    global _bot_user_id
    roles = discord_api.get(f"/guilds/{config.DISCORD_GUILD_ID}/roles").json() or []
    bot_top = None
    try:
        if _bot_user_id is None:
            _bot_user_id = str(discord_api.get("/users/@me").json()["id"])
        bot_roles = {str(r) for r in discord_api.get(
            f"/guilds/{config.DISCORD_GUILD_ID}/members/{_bot_user_id}").json().get("roles", [])}
        bot_top = max((int(r.get("position") or 0) for r in roles if str(r.get("id")) in bot_roles), default=0)
    except (discord_api.DiscordApiError, KeyError, TypeError, ValueError):
        bot_top = None
    out = []
    for r in roles:
        rid = str(r.get("id") or "")
        if not rid or rid == str(config.DISCORD_GUILD_ID):
            continue
        position = int(r.get("position") or 0)
        out.append({"id": rid, "name": r.get("name") or rid, "position": position,
                    "managed": bool(r.get("managed")), "color": int(r.get("color") or 0),
                    "above_bot": bot_top is not None and position >= bot_top})
    out.sort(key=lambda r: -r["position"])
    return out, bot_top
