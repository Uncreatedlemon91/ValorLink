"""Squad moves: who is in the Discord server, and announcing when someone
joins the squad or leaves it.

Two halves, both site -> Discord:

* Reading the server's member list (with their avatars) so staff can pick
  a real person from a menu instead of typing a name and hoping it matches
  someone. Needs the privileged GUILD_MEMBERS intent -- Developer Portal
  -> Bot -> Server Members Intent -- because Discord has no "list a role's
  members" or "search members" route that works without it.

* Posting the announcement embed for an offer or a departure.

DELIBERATELY DOES NOT TOUCH ROLES. "Offer Position" and "Let Go" publish
an announcement and nothing else: nobody is added to or removed from a
Discord role, kicked, or banned by this module. Roles are how this app
decides who is staff and who is a member (see auth.py), so mutating them
from a web form would mean a mis-click silently changing somebody's
access to the site as well as their standing in the club. Whoever makes
the announcement still moves the role in Discord, where the action is
visible, reversible, and audited.

REST only, same as every other Discord integration here -- no gateway, no
always-on process. Reuses DISCORD_BOT_TOKEN; see discord_api.py for the
shared-credential tradeoff that was accepted.
"""
from __future__ import annotations

from datetime import datetime, timezone

import cache
import config
import discord_api

DiscordApiError = discord_api.DiscordApiError

# The two kinds of move this module announces. Kept as constants because
# they're persisted (RosterMove.kind) and posted back from a form, so a
# typo in one place should fail loudly in the other.
MOVE_OFFER = "offer"
MOVE_RELEASE = "release"
MOVE_KINDS = (MOVE_OFFER, MOVE_RELEASE)

# Broadcast palette (see static/css/site.css): green for the performance
# side of the brand, amber for the sober one. A departure is news, not an
# alarm, so it gets amber rather than the red reserved for destructive
# actions.
_OFFER_COLOR = 0x00E27A
_RELEASE_COLOR = 0xFFB020

# Positions offered to staff as suggestions. A datalist, not a closed
# select -- clubs invent roles ("Set Piece Coach") and a fixed list would
# just push people into picking the nearest wrong one.
POSITION_SUGGESTIONS = [
    "Goalkeeper", "Centre Back", "Full Back", "Wing Back",
    "Defensive Midfield", "Centre Midfield", "Attacking Midfield",
    "Winger", "Striker", "Any Outfield",
    "Manager", "Assistant Manager", "Coach",
]

_CDN = "https://cdn.discordapp.com"

# Listing every member is several requests for a big server and the staff
# page re-renders on every form post, so it's cached. Short fresh window
# because the whole point of the page is picking a person who is in the
# server right now -- someone who joined a minute ago should show up.
_members_cache = cache.SwrCache(fresh_for=60, max_stale=900)


def _default_avatar_url(user: dict) -> str:
    """Discord's own fallback art for an account with no avatar set.

    Two schemes, because Discord migrated off discriminators: legacy
    accounts still carry one ("#1234") and index by it, while accounts on
    the new username system have discriminator "0" and index by a shift of
    the snowflake instead.
    """
    discriminator = str(user.get("discriminator") or "0")
    if discriminator not in ("", "0"):
        try:
            index = int(discriminator) % 5
        except ValueError:
            index = 0
    else:
        try:
            index = (int(user.get("id") or 0) >> 22) % 6
        except (TypeError, ValueError):
            index = 0
    return f"{_CDN}/embed/avatars/{index}.png"


def avatar_url(member: dict, size: int = 128) -> str:
    """Best avatar for a guild member object, always a usable URL.

    Preference order matches what Discord itself shows in the member list:
    a per-server avatar if they've set one, then their account avatar,
    then the default art. Animated avatars (hash prefixed "a_") are
    requested as .png on purpose -- the CDN renders a still frame, which
    is what a static page wants.
    """
    user = member.get("user") or {}
    user_id = user.get("id")
    guild_avatar = member.get("avatar")
    if user_id and guild_avatar:
        return f"{_CDN}/guilds/{config.DISCORD_GUILD_ID}/users/{user_id}/avatars/{guild_avatar}.png?size={size}"
    if user_id and user.get("avatar"):
        return f"{_CDN}/avatars/{user_id}/{user['avatar']}.png?size={size}"
    return _default_avatar_url(user)


def display_name(member: dict) -> str:
    """What to call this person, in the order Discord's own UI resolves it:
    their server nickname, then their display name, then their username."""
    user = member.get("user") or {}
    return (member.get("nick") or user.get("global_name")
            or user.get("username") or "Unknown member")


def fetch_guild_members() -> list[dict]:
    """Every member of the configured guild, one page of 1000 at a time.

    Public, and uncached, because discord_rsvp's staged event invites need
    the same walk to find who holds a role -- this is the one place in the
    app that knows how to page Discord's member list.

    Raises DiscordApiError -- notably a 403 when the GUILD_MEMBERS intent
    hasn't been enabled, which is the failure worth naming to staff since
    it's a checkbox in the Developer Portal rather than anything wrong
    with this app.
    """
    members: list[dict] = []
    after = "0"
    while True:
        resp = discord_api.get(
            f"/guilds/{config.DISCORD_GUILD_ID}/members",
            {"limit": 1000, "after": after},
        )
        page = resp.json() or []
        if not page:
            break
        members.extend(page)
        after = str(page[-1]["user"]["id"])
        if len(page) < 1000:
            break
    return members


def guild_members() -> list[dict]:
    """Raw member objects for the guild, cached. See fetch_guild_members."""
    return _members_cache.get("members", fetch_guild_members)


def invalidate_members_cache() -> None:
    """Drop the cached member list.

    Called after an announcement so the page staff land back on reflects
    anything that changed while they were on it, and available to tests so
    one test's stub members don't leak into the next.
    """
    _members_cache.clear()


def roster_choices() -> list[dict]:
    """The member list as the picker needs it: real people, sorted by name.

    Bots are dropped -- you don't offer a position to a webhook -- and the
    shape is flattened to exactly what the template renders so the
    template never reaches into Discord's nested member/user objects.
    """
    choices = []
    for member in guild_members():
        user = member.get("user") or {}
        if user.get("bot"):
            continue
        user_id = user.get("id")
        if not user_id:
            continue
        choices.append({
            "id": str(user_id),
            "name": display_name(member),
            "username": user.get("username") or "",
            "avatar_url": avatar_url(member),
        })
    choices.sort(key=lambda c: c["name"].casefold())
    return choices


def find_member(members: list[dict], discord_id: str) -> dict | None:
    """The chosen member out of a roster_choices() list, or None.

    The form posts back an id; resolving it against the list Discord
    actually returned is what stops a hand-edited form announcing a
    position for somebody who isn't in the server.
    """
    for member in members:
        if member["id"] == str(discord_id):
            return member
    return None


def build_move_embed(*, kind: str, member: dict, position: str | None,
                     note: str | None, announced_by: str | None,
                     announced_at: datetime | None = None) -> dict:
    """The announcement embed. Pure -- no network, so the wording and shape
    can be tested without mocking Discord."""
    if kind not in MOVE_KINDS:
        raise ValueError(f"unknown roster move: {kind!r}")
    name = member["name"]
    mention = f"<@{member['id']}>"
    club = config.SITE_NAME
    # Capped server-side as well as in the form's maxlength: the form is
    # only a suggestion to a browser, and an over-long position would push
    # the description past Discord's 4096-character limit and fail the
    # whole post.
    position = (position or "").strip()[:80]
    note = (note or "").strip()

    if kind == MOVE_OFFER:
        title = f"{name} — Offer Extended"
        where = f" as **{position}**" if position else ""
        description = (
            f"{mention} has been offered a position with **{club}**{where}. "
            f"We're looking forward to seeing what they bring to the pitch."
        )
        color = _OFFER_COLOR
        heading = "Squad Announcement · Offer"
    else:
        title = f"{name} — Departure"
        held = f", who leaves us from **{position}**," if position else ""
        description = (
            f"{mention}{held} is moving on from **{club}**. "
            f"We thank them for their time with the club and wish them "
            f"every success in what comes next."
        )
        color = _RELEASE_COLOR
        heading = "Squad Announcement · Departure"

    embed: dict = {
        "title": title,
        "description": description,
        "color": color,
        "author": {"name": heading},
        "thumbnail": {"url": member["avatar_url"]},
        "timestamp": (announced_at or datetime.now(timezone.utc)).isoformat(),
    }
    if position:
        embed["fields"] = [{"name": "Position", "value": position, "inline": True}]
    if note:
        embed.setdefault("fields", []).append(
            {"name": "From the staff", "value": note[:1024], "inline": False}
        )
    if announced_by:
        embed["footer"] = {"text": f"Announced by {announced_by} · {club}"}
    return embed


def announce_move(channel_id: str, embed: dict, *, mention_id: str | None = None) -> str:
    """Posts the embed and returns the new message's id.

    The player is mentioned in the message body as well as inside the
    embed, because a mention in an embed renders as a link but notifies
    nobody -- and the person the announcement is about should hear about
    it. allowed_mentions is explicit and lists only them, so an
    announcement can never turn into an @everyone.
    """
    payload: dict = {
        "embeds": [embed],
        "allowed_mentions": {"users": [str(mention_id)] if mention_id else []},
    }
    if mention_id:
        payload["content"] = f"<@{mention_id}>"
    resp = discord_api.post(f"/channels/{channel_id}/messages", json=payload)
    return str(resp.json()["id"])
