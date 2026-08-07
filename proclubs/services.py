"""CRUD helpers for articles and streamers, plus read/sync for events.

Events are read-only from the site's own UI -- they exist only via the
Discord Scheduled Events sync (see sync_discord_events below); there's no
create/update/delete path left for staff to use directly, by design.

Kept separate from app.py so the routes stay thin (parse request -> call
service -> render/redirect), matching the ValorLink web app's own
app.py/services.py split.
"""
from __future__ import annotations

import base64
import binascii
import re
from datetime import datetime, timezone
from html import escape as _escape_html

from fastapi import UploadFile
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

import discord_clips as discord_clips_mod
import discord_events as discord_events_mod
import html_sanitize
from models import ARTICLE_CATEGORIES, Article, Clip, Comment, Event, Like, Streamer

_MAX_IMAGE_BYTES = 2 * 1024 * 1024  # 2MB, generous enough for a cover photo
_MAX_COMMENT_LENGTH = 2000


class ServiceError(Exception):
    """A user-facing validation failure."""


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "article"


def unique_slug(session: Session, title: str, *, exclude_id: int | None = None) -> str:
    base = slugify(title)
    slug = base
    n = 2
    while True:
        query = select(Article.id).where(Article.slug == slug)
        if exclude_id is not None:
            query = query.where(Article.id != exclude_id)
        if session.execute(query).first() is None:
            return slug
        slug = f"{base}-{n}"
        n += 1


async def image_to_data_uri(upload: UploadFile | None) -> str | None:
    if upload is None or not upload.filename:
        return None
    content_type = upload.content_type or ""
    if not content_type.startswith("image/"):
        raise ServiceError("That file doesn't look like an image.")
    data = await upload.read()
    if not data:
        return None
    if len(data) > _MAX_IMAGE_BYTES:
        raise ServiceError("Images must be under 2MB.")
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


_DATA_URI_RE = re.compile(r"^data:([^;]+);base64,(.+)$", re.S)


def decode_data_uri(data_uri: str | None) -> tuple[str, bytes] | tuple[None, None]:
    """Splits a `data:<mime>;base64,<...>` URI (see image_to_data_uri above)
    back into (content_type, raw bytes) -- used to serve a stored cover
    image at a real fetchable URL (see GET /news/<slug>/cover-image),
    since e.g. Discord's embed API needs a URL it can fetch, not a data:
    URI baked into the embed JSON."""
    match = _DATA_URI_RE.match(data_uri or "")
    if not match:
        return None, None
    content_type, encoded = match.groups()
    try:
        return content_type, base64.b64decode(encoded)
    except (binascii.Error, ValueError):
        return None, None


def _normalize_category(category: str) -> str:
    category = (category or "").strip()
    return category if category in ARTICLE_CATEGORIES else ARTICLE_CATEGORIES[0]


def _is_meaningfully_empty(html: str) -> bool:
    """The rich-text editor's "empty" state is still markup (Quill always
    keeps a trailing <p><br></p>), so a plain truthiness/strip() check on
    the raw HTML doesn't catch it -- strip tags and check what's left. An
    image-only body (no text) still counts as real content."""
    html = html or ""
    if "<img" in html:
        return False
    return not re.sub(r"<[^>]+>", "", html).strip()


# --- Articles ---------------------------------------------------------- #
def list_articles(session: Session, *, include_drafts: bool = False,
                   category: str | None = None, limit: int | None = None) -> list[Article]:
    query = select(Article).order_by(Article.published_at.desc())
    if not include_drafts:
        query = query.where(Article.published.is_(True))
    if category:
        query = query.where(Article.category == category)
    if limit:
        query = query.limit(limit)
    return list(session.execute(query).scalars())


def get_article(session: Session, slug: str) -> Article | None:
    return session.execute(select(Article).where(Article.slug == slug)).scalar_one_or_none()


def get_article_by_id(session: Session, article_id: int) -> Article | None:
    return session.get(Article, article_id)


def _clamp_focal(value) -> float:
    """A focal-point coordinate is a percentage into the image (0-100);
    anything unparseable or out of range just falls back to a plain center
    crop rather than erroring the whole save over a cosmetic field."""
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return 50.0


def create_article(session: Session, *, title: str, summary: str, body_html: str,
                    cover_image: str | None, published: bool, author: dict,
                    category: str = "News", cover_focal_x=50, cover_focal_y=50) -> Article:
    title = title.strip()
    if not title:
        raise ServiceError("Give the article a title.")
    if len(body_html or "") > html_sanitize.MAX_BODY_LENGTH:
        raise ServiceError("That article is too long (likely too many embedded images).")
    if _is_meaningfully_empty(body_html):
        raise ServiceError("The article needs some body text.")
    article = Article(
        title=title,
        slug=unique_slug(session, title),
        category=_normalize_category(category),
        summary=summary.strip() or None,
        body_html=html_sanitize.sanitize(body_html),
        cover_image=cover_image,
        cover_focal_x=_clamp_focal(cover_focal_x),
        cover_focal_y=_clamp_focal(cover_focal_y),
        author_discord_id=author.get("id") or None,
        author_name=author.get("name", "Staff"),
        author_avatar=author.get("avatar"),
        published=published,
        published_at=datetime.utcnow(),
    )
    session.add(article)
    session.commit()
    session.refresh(article)
    return article


def update_article(session: Session, article: Article, *, title: str, summary: str,
                    body_html: str, cover_image: str | None, published: bool,
                    category: str | None = None, cover_focal_x=50, cover_focal_y=50) -> Article:
    title = title.strip()
    if not title:
        raise ServiceError("Give the article a title.")
    if len(body_html or "") > html_sanitize.MAX_BODY_LENGTH:
        raise ServiceError("That article is too long (likely too many embedded images).")
    if _is_meaningfully_empty(body_html):
        raise ServiceError("The article needs some body text.")
    if title != article.title:
        article.slug = unique_slug(session, title, exclude_id=article.id)
    article.title = title
    article.category = _normalize_category(category if category is not None else article.category)
    article.summary = summary.strip() or None
    article.body_html = html_sanitize.sanitize(body_html)
    if cover_image is not None:
        article.cover_image = cover_image
    article.cover_focal_x = _clamp_focal(cover_focal_x)
    article.cover_focal_y = _clamp_focal(cover_focal_y)
    if published and not article.published:
        article.published_at = datetime.utcnow()
    article.published = published
    session.commit()
    session.refresh(article)
    return article


def delete_article(session: Session, article: Article) -> None:
    # Comments/likes reference article_id as a plain column, not a real FK
    # (matching this app's existing no-ORM-relationships style), so nothing
    # cascades automatically -- clean them up by hand.
    session.execute(delete(Comment).where(Comment.article_id == article.id))
    session.execute(delete(Like).where(Like.article_id == article.id))
    session.delete(article)
    session.commit()


# --- Comments -------------------------------------------------------------- #
def list_comments(session: Session, article: Article) -> list[Comment]:
    return list(session.execute(
        select(Comment).where(Comment.article_id == article.id).order_by(Comment.created_at.asc())
    ).scalars())


def get_comment(session: Session, comment_id: int) -> Comment | None:
    return session.get(Comment, comment_id)


def add_comment(session: Session, article: Article, *, author: dict, body: str) -> Comment:
    body = (body or "").strip()
    if not body:
        raise ServiceError("Say something first.")
    if len(body) > _MAX_COMMENT_LENGTH:
        raise ServiceError(f"Comments are limited to {_MAX_COMMENT_LENGTH} characters.")
    comment = Comment(
        article_id=article.id,
        author_discord_id=author["id"],
        author_name=author.get("name", "Fan"),
        author_avatar=author.get("avatar"),
        body=body,
    )
    session.add(comment)
    session.commit()
    session.refresh(comment)
    return comment


def delete_comment(session: Session, comment: Comment) -> None:
    session.delete(comment)
    session.commit()


def comment_counts_for(session: Session, article_ids: list[int]) -> dict[int, int]:
    """article_id -> comment count, for a batch of articles in one query --
    for a listing page (home, /news) showing counts on every card, not one
    round-trip per card."""
    if not article_ids:
        return {}
    rows = session.execute(
        select(Comment.article_id, func.count(Comment.id))
        .where(Comment.article_id.in_(article_ids))
        .group_by(Comment.article_id)
    ).all()
    return {article_id: count for article_id, count in rows}


# --- Likes ------------------------------------------------------------------ #
def count_likes(session: Session, article: Article) -> int:
    return len(list(session.execute(select(Like.id).where(Like.article_id == article.id)).scalars()))


def like_counts_for(session: Session, article_ids: list[int]) -> dict[int, int]:
    """Same idea as comment_counts_for, for likes."""
    if not article_ids:
        return {}
    rows = session.execute(
        select(Like.article_id, func.count(Like.id))
        .where(Like.article_id.in_(article_ids))
        .group_by(Like.article_id)
    ).all()
    return {article_id: count for article_id, count in rows}


def has_liked(session: Session, article: Article, user_id: int) -> bool:
    return session.execute(
        select(Like.id).where(Like.article_id == article.id, Like.user_discord_id == user_id)
    ).first() is not None


def toggle_like(session: Session, article: Article, user_id: int) -> bool:
    """Adds or removes the like, returning the new state (True == now
    liked). The unique (article_id, user_discord_id) constraint is what
    keeps this safe if the same click somehow lands twice."""
    existing = session.execute(
        select(Like).where(Like.article_id == article.id, Like.user_discord_id == user_id)
    ).scalar_one_or_none()
    if existing is not None:
        session.delete(existing)
        session.commit()
        return False
    session.add(Like(article_id=article.id, user_discord_id=user_id))
    session.commit()
    return True


# --- Events -------------------------------------------------------------- #
def list_events(session: Session, *, upcoming_only: bool = False, limit: int | None = None) -> list[Event]:
    query = select(Event)
    if upcoming_only:
        query = query.where(Event.scheduled_at >= datetime.utcnow()).order_by(Event.scheduled_at.asc())
    else:
        query = query.order_by(Event.scheduled_at.desc())
    if limit:
        query = query.limit(limit)
    return list(session.execute(query).scalars())


def _parse_discord_time(value: str) -> datetime:
    """Discord's timestamps are ISO 8601 with an explicit offset (or "Z").
    Normalize to a naive UTC datetime -- the same shape scheduled_at is
    stored in everywhere else on this model."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def sync_discord_events(session: Session, discord_events: list[dict]) -> dict:
    """Mirrors Discord's Scheduled Events into Event rows. Discord is the
    source of truth for title/description/scheduled_at/image on these rows
    -- each sync overwrites them. event_type/opponent/result have no
    Discord equivalent and no site UI to set them either; they're only
    defaulted on first creation (Match / no opponent / no result) and never
    touched again here.

    Events this function previously created that Discord no longer lists
    as upcoming (canceled, or the event itself deleted) are removed, so a
    canceled Discord event doesn't linger as a fixture on the site.
    """
    seen_ids = set()
    created = updated = 0
    for de in discord_events:
        if not discord_events_mod.is_upcoming(de):
            continue
        discord_id = de["id"]
        seen_ids.add(discord_id)
        title = de.get("name") or "Discord Event"
        description = de.get("description")
        scheduled_at = _parse_discord_time(de["scheduled_start_time"])
        image = discord_events_mod.cover_image_url(de)

        event = session.execute(
            select(Event).where(Event.discord_event_id == discord_id)
        ).scalar_one_or_none()
        if event is None:
            session.add(Event(
                discord_event_id=discord_id, title=title, event_type="Match",
                description=description, scheduled_at=scheduled_at, image=image,
                created_by_name="Discord sync",
            ))
            created += 1
        else:
            event.title = title
            event.description = description
            event.image = image
            event.scheduled_at = scheduled_at
            updated += 1

    removed = 0
    synced_upcoming = session.execute(
        select(Event).where(Event.discord_event_id.is_not(None))
                     .where(Event.scheduled_at >= datetime.utcnow())
    ).scalars()
    for event in synced_upcoming:
        if event.discord_event_id not in seen_ids:
            session.delete(event)
            removed += 1

    session.commit()
    return {"created": created, "updated": updated, "removed": removed}


# --- Clips ------------------------------------------------------------------ #
def list_clips(session: Session, *, limit: int | None = None) -> list[Clip]:
    query = select(Clip).order_by(Clip.posted_at.desc())
    if limit:
        query = query.limit(limit)
    return list(session.execute(query).scalars())


def sync_clips(session: Session, channel_id: str, messages: list[dict]) -> dict:
    """Mirrors video attachments from a Discord channel's recent messages
    into Clip rows. Every sync refreshes video_url for a clip whose message
    is still within the polled window, since Discord's attachment URLs are
    signed and expire (~24h) -- see discord_clips.py.

    Unlike events, a message no longer in the polled window isn't treated
    as deleted (older messages just age out of the default fetch -- they
    aren't "canceled" the way a Discord event can be): existing Clip rows
    are never removed here, only added to or refreshed.
    """
    created = updated = 0
    for message in messages:
        videos = discord_clips_mod.video_attachments(message)
        if not videos:
            continue
        attachment = videos[0]  # one clip per message, first video wins
        message_id = message["id"]
        posted_at = _parse_discord_time(message["timestamp"])
        author = message.get("author") or {}
        author_name = author.get("global_name") or author.get("username")

        clip = session.execute(
            select(Clip).where(Clip.discord_message_id == message_id)
        ).scalar_one_or_none()
        if clip is None:
            session.add(Clip(
                discord_message_id=message_id,
                title=(message.get("content") or "").strip() or None,
                video_url=attachment.get("url"),
                filename=attachment.get("filename"),
                author_name=author_name,
                jump_url=discord_clips_mod.jump_url(channel_id, message_id),
                posted_at=posted_at,
            ))
            created += 1
        else:
            clip.video_url = attachment.get("url")
            updated += 1

    session.commit()
    return {"created": created, "updated": updated}


_CLIP_EMBED_RE = re.compile(r'<clip-embed[^>]*\bdata-clip-id="(\d+)"[^>]*>.*?</clip-embed>', re.S)


def render_clip_embeds(session: Session, body_html: str) -> str:
    """Resolves <clip-embed data-clip-id="N"> placeholders (see the "Insert
    Clip" button in article-editor.js) into a live <video> embed, using
    each Clip's CURRENT video_url.

    Deliberately not done at save time: Discord's attachment URL is signed
    and expires (~24h, see discord_clips.py), so baking it into the stored
    body_html would go stale even though sync_clips keeps refreshing the
    Clip row itself. Resolving on every render instead means an embed
    keeps working for as long as the clip stays in the synced window,
    exactly like the /clips page.
    """
    if "<clip-embed" not in body_html:
        return body_html

    ids = {int(m.group(1)) for m in _CLIP_EMBED_RE.finditer(body_html)}
    if not ids:
        return body_html
    clips = {c.id: c for c in session.execute(select(Clip).where(Clip.id.in_(ids))).scalars()}

    def _replace(match: re.Match) -> str:
        clip = clips.get(int(match.group(1)))
        if clip is None:
            return '<p class="clip-embed-missing">This clip is no longer available.</p>'
        title = f' title="{_escape_html(clip.title)}"' if clip.title else ""
        return (
            '<div class="clip-embed-card">'
            f'<video class="clip-video" controls preload="metadata" src="{_escape_html(clip.video_url)}"{title}></video>'
            f'<a class="clip-embed-jump" href="{_escape_html(clip.jump_url)}" target="_blank" rel="noopener noreferrer">View in Discord</a>'
            '</div>'
        )

    return _CLIP_EMBED_RE.sub(_replace, body_html)


# --- Streamers ------------------------------------------------------------ #
def list_streamers(session: Session) -> list[Streamer]:
    return list(session.execute(select(Streamer).order_by(Streamer.position.asc(), Streamer.id.asc())).scalars())


def get_streamer(session: Session, streamer_id: int) -> Streamer | None:
    return session.get(Streamer, streamer_id)


def get_featured_streamer(session: Session) -> Streamer | None:
    return session.execute(select(Streamer).where(Streamer.featured.is_(True))).scalar_one_or_none()


def create_streamer(session: Session, *, display_name: str, twitch_login: str,
                     avatar: str | None, author_name: str, featured: bool = False) -> Streamer:
    display_name = display_name.strip()
    twitch_login = twitch_login.strip().lower().lstrip("@")
    if not display_name or not twitch_login:
        raise ServiceError("Give the streamer a display name and Twitch username.")
    if session.execute(select(Streamer.id).where(Streamer.twitch_login == twitch_login)).first():
        raise ServiceError(f"{twitch_login} is already on the showcase.")
    next_position = (
        session.execute(select(Streamer.position).order_by(Streamer.position.desc())).scalars().first() or 0
    ) + 1
    if featured:
        session.execute(update(Streamer).values(featured=False))
    streamer = Streamer(
        display_name=display_name,
        twitch_login=twitch_login,
        avatar=avatar,
        position=next_position,
        featured=featured,
        added_by_name=author_name,
    )
    session.add(streamer)
    session.commit()
    session.refresh(streamer)
    return streamer


def set_featured_streamer(session: Session, streamer: Streamer) -> None:
    """Only one streamer is ever featured -- setting one clears the rest,
    like a radio button, so there's always at most one embedded player."""
    session.execute(update(Streamer).values(featured=False))
    streamer.featured = True
    session.commit()


def delete_streamer(session: Session, streamer: Streamer) -> None:
    session.delete(streamer)
    session.commit()
