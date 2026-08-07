"""Content models for the team site: news articles, events, and the
streamer showcase. Permissions aren't modelled here at all -- "can edit"
is derived live from the signed-in user's Discord roles (see auth.py), not
stored, so a role change in Discord takes effect immediately everywhere.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, String, Text

from database import Base

# What kind of post this is, for the front page's category badges and the
# /news filter -- a fixed, small set rather than free-text tags, matching
# how a real club site sections its news.
ARTICLE_CATEGORIES = ["News", "Transfer", "Match Highlight"]


def _utcnow() -> datetime:
    return datetime.utcnow()


class Article(Base):
    """A news/blog post. Markdown source is what's stored and edited;
    rendered HTML is cached alongside it so a read doesn't re-render."""

    __tablename__ = "articles"

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    slug = Column(String, nullable=False, unique=True, index=True)
    category = Column(String, nullable=False, server_default="News")  # see ARTICLE_CATEGORIES
    summary = Column(String, nullable=True)          # dek shown in list views
    body_md = Column(Text, nullable=False)
    body_html = Column(Text, nullable=False)
    cover_image = Column(Text, nullable=True)         # data URI
    author_discord_id = Column(BigInteger, nullable=True)
    author_name = Column(String, nullable=False)
    author_avatar = Column(String, nullable=True)
    published = Column(Boolean, nullable=False, default=True)
    published_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


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
