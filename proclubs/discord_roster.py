"""Squad moves: who is in the Discord server, and announcing when someone
joins the squad or leaves it.

Two halves, both site -> Discord:

* Reading the server's member list (with their avatars) so staff can pick
  a real person from a menu instead of typing a name and hoping it matches
  someone. Needs the privileged GUILD_MEMBERS intent -- Developer Portal
  -> Bot -> Server Members Intent -- because Discord has no "list a role's
  members" or "search members" route that works without it.

* Posting the announcement embed for an offer or a departure.

NEVER REMOVES A ROLE, AND ONLY EVER ADDS ONE ON THE PLAYER'S OWN PRESS.
Roles are how this app decides who is staff and who is a member (see
auth.py), so a web form that moved them would mean a mis-click silently
changing somebody's access to the site as well as their standing in the
club. So:

* "Let Go" publishes an announcement and nothing else. Removing access
  is the irreversible half, and it stays a human action taken in
  Discord, where it is visible, reversible, and audited.
* "Offer Position" publishes an offer the player answers themselves,
  with Accept / Decline buttons that only they can press. Accepting adds
  exactly one configured role (ROSTER_SQUAD_ROLE_ID) -- grant-only,
  self-triggered, and to one named role rather than whatever a form
  posts. Declining touches nothing.
* Staff then CONFIRM an accepted offer, which publishes the signing
  announcement. Two steps on purpose: the player accepting is them
  agreeing, not the club announcing.

grant_squad_role() below is the only role write in this app, and there
is no corresponding remove.

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
# A declined offer is neither good news nor bad news; it's closed. Grey
# rather than red, which on this site means "on air" and nothing else.
_DECLINED_COLOR = 0x8A93A5

# How the player answers their own offer. Persisted on RosterMove.response
# and parsed back out of a button's custom_id, so the two must agree.
RESPONSE_ACCEPTED = "accepted"
RESPONSE_DECLINED = "declined"
OFFER_RESPONSES = (RESPONSE_ACCEPTED, RESPONSE_DECLINED)

# Prefix on this feature's button custom_ids. The interactions endpoint is
# shared with event sign-ups, which parse their own ids -- the prefix is
# what tells the two apart before either tries.
CUSTOM_ID_PREFIX = "roster"

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
                     announced_at: datetime | None = None,
                     response: str | None = None) -> dict:
    """The announcement embed. Pure -- no network, so the wording and shape
    can be tested without mocking Discord.

    `response` re-renders an offer that has been answered: the same
    message is edited in place when the player presses, so the channel
    shows one live record of the offer rather than an original plus a
    reply nobody scrolls back to.
    """
    if kind not in MOVE_KINDS:
        raise ValueError(f"unknown roster move: {kind!r}")
    if response is not None and response not in OFFER_RESPONSES:
        raise ValueError(f"unknown offer response: {response!r}")
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
        where = f" as **{position}**" if position else ""
        if response == RESPONSE_ACCEPTED:
            title = f"{name} — Offer Accepted"
            description = (
                f"{mention} has accepted a position with **{club}**{where}. "
                f"The club will confirm the signing shortly."
            )
            color, heading = _OFFER_COLOR, "Squad Announcement · Offer Accepted"
        elif response == RESPONSE_DECLINED:
            title = f"{name} — Offer Declined"
            description = (
                f"{mention} has declined the offer of a position with **{club}**. "
                f"We wish them well."
            )
            color, heading = _DECLINED_COLOR, "Squad Announcement · Offer Declined"
        else:
            title = f"{name} — Offer Extended"
            description = (
                f"{mention} has been offered a position with **{club}**{where}. "
                f"Accept or decline below — only {mention} can answer this one."
            )
            color, heading = _OFFER_COLOR, "Squad Announcement · Offer"
    else:
        title = f"{name} — Departure"
        held = f", who leaves us from **{position}**," if position else ""
        description = (
            f"{mention}{held} is moving on from **{club}**. "
            f"We thank them for their time with the club and wish them "
            f"every success in what comes next."
        )
        color, heading = _RELEASE_COLOR, "Squad Announcement · Departure"

    embed: dict = {
        "title": title,
        "description": description,
        "color": color,
        "author": {"name": heading},
        "timestamp": (announced_at or datetime.now(timezone.utc)).isoformat(),
    }
    # Only when there is one: Discord rejects an embed carrying a
    # thumbnail with an empty url, and RosterMove.avatar_url is nullable,
    # so a re-render of an old row would fail the whole press.
    if member.get("avatar_url"):
        embed["thumbnail"] = {"url": member["avatar_url"]}
    if position:
        embed["fields"] = [{"name": "Position", "value": position, "inline": True}]
    if note:
        embed.setdefault("fields", []).append(
            {"name": "From the staff", "value": note[:1024], "inline": False}
        )
    if announced_by:
        embed["footer"] = {"text": f"Announced by {announced_by} · {club}"}
    return embed


def build_signing_embed(*, member: dict, position: str | None,
                        confirmed_by: str | None,
                        confirmed_at: datetime | None = None) -> dict:
    """The celebration, published when staff confirm an accepted offer.

    A second message rather than another edit of the offer: the offer is a
    record, this is news, and news that edits a week-old message into
    place reaches nobody.
    """
    club = config.SITE_NAME
    position = (position or "").strip()[:80]
    where = f" as **{position}**" if position else ""
    embed: dict = {
        "title": f"{member['name']} has signed for {club}",
        "description": (
            f"It's official — <@{member['id']}> joins **{club}**{where}. "
            f"Welcome to the squad."
        ),
        "color": _OFFER_COLOR,
        "author": {"name": "Squad Announcement · Signing"},
        "timestamp": (confirmed_at or datetime.now(timezone.utc)).isoformat(),
    }
    if member.get("avatar_url"):
        embed["thumbnail"] = {"url": member["avatar_url"]}
    if position:
        embed["fields"] = [{"name": "Position", "value": position, "inline": True}]
    if confirmed_by:
        embed["footer"] = {"text": f"Confirmed by {confirmed_by} · {club}"}
    return embed


def build_offer_components(move_id: int) -> list[dict]:
    """Accept / Decline under an open offer.

    Returned empty by callers once the offer is answered: leaving dead
    buttons on a settled offer only invites presses that can't be
    honoured, and the edited embed already says what happened.
    """
    return [{
        "type": 1,  # action row
        "components": [
            {"type": 2, "style": 3, "label": "Accept",
             "custom_id": f"{CUSTOM_ID_PREFIX}:{RESPONSE_ACCEPTED}:{move_id}"},
            {"type": 2, "style": 2, "label": "Decline",
             "custom_id": f"{CUSTOM_ID_PREFIX}:{RESPONSE_DECLINED}:{move_id}"},
        ],
    }]


def parse_offer_custom_id(custom_id: str) -> tuple[str, int]:
    """("accepted"|"declined", move_id) out of a button's custom_id.

    Raises ValueError on anything that isn't one of ours -- the
    interactions endpoint shares a URL with event sign-ups, so "this isn't
    mine" has to be a clean, distinguishable answer rather than an
    IndexError.
    """
    parts = custom_id.split(":")
    if len(parts) != 3 or parts[0] != CUSTOM_ID_PREFIX:
        raise ValueError(f"not a roster custom_id: {custom_id!r}")
    _, response, raw_id = parts
    if response not in OFFER_RESPONSES:
        raise ValueError(f"unknown offer response: {response!r}")
    if not raw_id.isdigit():
        raise ValueError(f"bad move id in custom_id: {custom_id!r}")
    return response, int(raw_id)


def member_from_move(move) -> dict:
    """A stored RosterMove back in the shape the embed builders take.

    The row snapshots the name and avatar from announcement time, which is
    the point: re-rendering an offer must not depend on the player still
    being in the server, or on Discord being reachable while somebody is
    mid-press.
    """
    return {"id": str(move.discord_id), "name": move.display_name,
            "avatar_url": move.avatar_url or ""}


def grant_squad_role(user_id: str) -> None:
    """Adds config.ROSTER_SQUAD_ROLE_ID to one member. The ONLY role write
    in this app, and there is no remove to pair with it.

    Discord treats adding a role somebody already has as success, so a
    double-press costs a call and changes nothing. Raises DiscordApiError
    when the bot lacks Manage Roles, or when its own highest role sits
    below the squad role -- both show up as a 403, and both are fixed in
    Server Settings -> Roles rather than in this app, so the caller
    records the message instead of discarding it.
    """
    if not config.ROSTER_SQUAD_ROLE_ID:
        raise DiscordApiError("ROSTER_SQUAD_ROLE_ID isn't set")
    discord_api.put(
        f"/guilds/{config.DISCORD_GUILD_ID}/members/{user_id}"
        f"/roles/{config.ROSTER_SQUAD_ROLE_ID}"
    )


def announce_move(channel_id: str, embed: dict, *, mention_id: str | None = None,
                  components: list[dict] | None = None) -> str:
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
    if components:
        payload["components"] = components
    resp = discord_api.post(f"/channels/{channel_id}/messages", json=payload)
    return str(resp.json()["id"])
