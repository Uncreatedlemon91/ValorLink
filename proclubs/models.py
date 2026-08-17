"""Content models for the team site: news articles, events, and the
streamer showcase. Permissions aren't modelled here at all -- "can edit"
is derived live from the signed-in user's Discord roles (see auth.py), not
stored, so a role change in Discord takes effect immediately everywhere.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Float, Integer, String, Text, UniqueConstraint

from database import Base

# What kind of post this is, for the front page's category badges and the
# /news filter -- a fixed, small set rather than free-text tags, matching
# how a real club site sections its news.
ARTICLE_CATEGORIES = ["News", "Transfer", "Match Highlight"]


def _utcnow() -> datetime:
    return datetime.utcnow()


class Article(Base):
    """A news/blog post, written with a rich-text (WYSIWYG) editor -- the
    editor produces HTML directly, so body_html is both the editable
    source (loaded back into the editor) and what gets rendered; see
    html_sanitize.py for why it's still sanitized rather than trusted."""

    __tablename__ = "articles"

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    slug = Column(String, nullable=False, unique=True, index=True)
    category = Column(String, nullable=False, server_default="News")  # see ARTICLE_CATEGORIES
    summary = Column(String, nullable=True)          # dek shown in list views
    body_html = Column(Text, nullable=False)
    cover_image = Column(Text, nullable=True)         # data URI
    # Where the cover image should stay centered when it's cropped narrower
    # than its native shape -- the home hero, the article header, and card
    # thumbnails all crop it to a different aspect ratio (see focal_position
    # in app.py). Percentages, 0-100; (50, 50) is a plain center crop.
    cover_focal_x = Column(Float, nullable=False, server_default="50")
    cover_focal_y = Column(Float, nullable=False, server_default="50")
    author_discord_id = Column(BigInteger, nullable=True)
    author_name = Column(String, nullable=False)
    author_avatar = Column(String, nullable=True)
    published = Column(Boolean, nullable=False, default=True)
    published_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    # The Discord announcement message this article got posted as (see
    # discord_announce.py / app.py's news_new+news_edit), so
    # discord_reactions_poll.py knows which message to re-check for
    # reactions. Null for a draft, an article published before this
    # existed, or one whose announcement failed to send.
    discord_message_id = Column(String, nullable=True)
    # Cached total reaction count (every emoji summed, not just one) on
    # that message -- refreshed periodically by discord_reactions_poll.py,
    # never fetched live on a page view (see discord_announce.py).
    discord_reaction_count = Column(Integer, nullable=True)


class Event(Base):
    """A team event: an upcoming match, scrim, tournament, or community
    event. ``result`` is filled in after the fact; blank means not played
    yet (or not a competitive fixture at all)."""

    __tablename__ = "events"

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    event_type = Column(String, nullable=False, default="Match")  # Match|Scrim|Tournament|Community
    opponent = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    scheduled_at = Column(DateTime, nullable=False)
    image = Column(Text, nullable=True)                # data URI
    result = Column(String, nullable=True)              # e.g. "W 4-1", "L 1-2"
    created_by_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    # Set only for events mirrored in from Discord's Scheduled Events (see
    # discord_events.py) -- lets the poller find its own rows again to
    # update or remove them without duplicating on every run. Null for
    # events created directly on the site.
    discord_event_id = Column(String, nullable=True, index=True)

    # The RSVP announcement this event posted to Discord, if any. Signing up
    # on the site has to edit that message so both surfaces show the same
    # roster, so we keep the coordinates needed to PATCH it. Null until the
    # event is announced (see discord_rsvp.announce).
    discord_channel_id = Column(String, nullable=True)
    discord_message_id = Column(String, nullable=True)
    # Staff can close sign-ups without deleting the event (e.g. once the
    # squad is picked). Closed events still show their roster, read-only.
    signups_open = Column(Boolean, nullable=False, default=True, server_default="1")


# How a player answers the sign-up question. Deliberately three states, not
# a yes/no: "maybe" is the honest answer often enough that forcing it into
# yes or no is what makes a roster untrustworthy.
SIGNUP_STATUSES = ["going", "maybe", "out"]
SIGNUP_LABELS = {"going": "Going", "maybe": "Maybe", "out": "Can't make it"}

# What actually happened, recorded by staff after the event. Only these
# feed the reliability figure -- an unmarked event is not evidence.
ATTENDANCE_STATUSES = ["present", "absent", "excused"]


class EventSignup(Base):
    """One player's answer for one event, from either surface.

    Keyed by Discord user ID because that's the one identity both surfaces
    share: the site knows it from OAuth, Discord knows it from the button
    press. ``source`` records which surface the answer came from -- purely
    informational, since an answer means the same thing either way, but it
    makes "the Discord buttons stopped working" diagnosable.
    """

    __tablename__ = "event_signups"
    __table_args__ = (UniqueConstraint("event_id", "discord_user_id", name="uq_signup_event_user"),)

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, nullable=False, index=True)
    discord_user_id = Column(BigInteger, nullable=False, index=True)
    # Denormalized on purpose: this site has no user table, and a roster
    # from six months ago should still render the name it was signed with
    # even if that person has since left the guild.
    discord_name = Column(String, nullable=False)
    discord_avatar = Column(String, nullable=True)
    status = Column(String, nullable=False, default="going")   # see SIGNUP_STATUSES
    source = Column(String, nullable=False, default="site")     # site|discord
    responded_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    # Filled in by staff afterwards -- NULL means "not marked", which is
    # different from absent and is excluded from the reliability figure.
    attendance = Column(String, nullable=True)                  # see ATTENDANCE_STATUSES
    attendance_marked_by = Column(String, nullable=True)
    attendance_marked_at = Column(DateTime, nullable=True)


class PlayerLink(Base):
    """Ties a Discord account to the EA gamertag it plays under.

    The Tactics board stores plain gamertags (they come from EA's club
    roster, which knows nothing about Discord), while a sign-up knows only
    a Discord user. Without this table there is no way to answer "what
    position is this person on the team sheet". Self-service and one-time:
    a member picks their own gamertag from the club roster once.
    """

    __tablename__ = "player_links"

    discord_user_id = Column(BigInteger, primary_key=True)
    player_name = Column(String, nullable=False, index=True)
    linked_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class Comment(Base):
    """A comment on a news article, left by a signed-in Discord user who's a
    member of DISCORD_GUILD_ID (see auth.require_member) -- not staff-only,
    unlike everything else that writes to this site. Plain text: rendered
    through Jinja's normal auto-escaping, no rich-text/HTML story here."""

    __tablename__ = "comments"

    id = Column(Integer, primary_key=True)
    article_id = Column(Integer, nullable=False, index=True)
    author_discord_id = Column(BigInteger, nullable=False)
    author_name = Column(String, nullable=False)
    author_avatar = Column(String, nullable=True)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=_utcnow)


class Like(Base):
    """One row per (article, Discord user) that has liked it -- existence is
    the like, nothing to update, so unliking just deletes the row. The
    unique constraint is what makes "toggle" safe against double-clicks."""

    __tablename__ = "likes"
    __table_args__ = (UniqueConstraint("article_id", "user_discord_id", name="uq_like_article_user"),)

    id = Column(Integer, primary_key=True)
    article_id = Column(Integer, nullable=False, index=True)
    user_discord_id = Column(BigInteger, nullable=False)
    created_at = Column(DateTime, default=_utcnow)


class Clip(Base):
    """A video clip, mirrored in from a Discord channel (see
    discord_clips.py / services.sync_clips). Read-only from the site, same
    as Event -- there's no create/edit UI, only what's synced from Discord.

    video_url is Discord's signed CDN URL, which expires (Discord issues a
    fresh one on every fetch, valid roughly 24h) -- refreshed on every
    sync a clip's message is still within the polled window. jump_url is a
    permanent link to the message itself, which never expires, used as a
    fallback once video_url is too stale to still work."""

    __tablename__ = "clips"

    id = Column(Integer, primary_key=True)
    discord_message_id = Column(String, nullable=False, unique=True, index=True)
    title = Column(String, nullable=True)               # message content, if any
    video_url = Column(Text, nullable=False)
    filename = Column(String, nullable=True)
    author_name = Column(String, nullable=True)
    jump_url = Column(Text, nullable=False)
    posted_at = Column(DateTime, nullable=False)
    synced_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class Streamer(Base):
    """A team member (or the team's own channel) featured in the "live now"
    showcase. Live status itself is never stored -- it's checked against
    Twitch on read (see twitch_client.py) so it's never stale."""

    __tablename__ = "streamers"

    id = Column(Integer, primary_key=True)
    display_name = Column(String, nullable=False)
    twitch_login = Column(String, nullable=False, unique=True)
    avatar = Column(Text, nullable=True)               # data URI, optional override
    position = Column(Integer, nullable=False, default=0)   # display order
    featured = Column(Boolean, nullable=False, server_default="0")  # gets the embedded player on Live/Home
    added_by_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=_utcnow)


class TacticsBoard(Base):
    """Singleton settings row (always id=1) for the /tactics page -- just
    which formation is currently the active/shown one. Each formation's
    own slot assignments live independently in TacticsSlot, so switching
    formations here doesn't lose what staff set up for the others."""

    __tablename__ = "tactics_board"

    id = Column(Integer, primary_key=True)
    active_formation = Column(String, nullable=False, default="4-3-3")
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    updated_by_name = Column(String, nullable=True)


class TacticsSlot(Base):
    """Who staff placed in one position slot of one formation -- see
    app.py's FORMATIONS for the valid (formation, slot_key) pairs and
    their pitch coordinates. player_name is free text, not a foreign key
    to any roster table: the pool offered in the UI comes live from EA
    (see /api/members), but there's no local "players" table to reference,
    and a name typed in by staff shouldn't be blocked by not matching it
    exactly. Null/absent means that slot is empty."""

    __tablename__ = "tactics_slots"
    __table_args__ = (UniqueConstraint("formation", "slot_key", name="uq_tactics_slot"),)

    id = Column(Integer, primary_key=True)
    formation = Column(String, nullable=False)
    slot_key = Column(String, nullable=False)
    player_name = Column(String, nullable=True)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
