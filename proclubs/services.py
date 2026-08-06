"""CRUD helpers for articles, events, and streamers.

Kept separate from app.py so the routes stay thin (parse request -> call
service -> render/redirect), matching the ValorLink web app's own
app.py/services.py split.
"""
from __future__ import annotations

import base64
import re
from datetime import datetime

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

import markdown_render
from models import ARTICLE_CATEGORIES, Article, Event, Streamer

_MAX_IMAGE_BYTES = 2 * 1024 * 1024  # 2MB, generous enough for a cover photo


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


def _normalize_category(category: str) -> str:
    category = (category or "").strip()
    return category if category in ARTICLE_CATEGORIES else ARTICLE_CATEGORIES[0]


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


def create_article(session: Session, *, title: str, summary: str, body_md: str,
                    cover_image: str | None, published: bool, author: dict,
                    category: str = "News") -> Article:
    title = title.strip()
    if not title:
        raise ServiceError("Give the article a title.")
    if not body_md.strip():
        raise ServiceError("The article needs some body text.")
    article = Article(
        title=title,
        slug=unique_slug(session, title),
        category=_normalize_category(category),
        summary=summary.strip() or None,
        body_md=body_md,
        body_html=markdown_render.render(body_md),
        cover_image=cover_image,
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
                    body_md: str, cover_image: str | None, published: bool,
                    category: str | None = None) -> Article:
    title = title.strip()
    if not title:
        raise ServiceError("Give the article a title.")
    if not body_md.strip():
        raise ServiceError("The article needs some body text.")
    if title != article.title:
        article.slug = unique_slug(session, title, exclude_id=article.id)
    article.title = title
    article.category = _normalize_category(category if category is not None else article.category)
    article.summary = summary.strip() or None
    article.body_md = body_md
    article.body_html = markdown_render.render(body_md)
    if cover_image is not None:
        article.cover_image = cover_image
    if published and not article.published:
        article.published_at = datetime.utcnow()
    article.published = published
    session.commit()
    session.refresh(article)
    return article


def delete_article(session: Session, article: Article) -> None:
    session.delete(article)
    session.commit()


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


def get_event(session: Session, event_id: int) -> Event | None:
    return session.get(Event, event_id)


def create_event(session: Session, *, title: str, event_type: str, opponent: str,
                  description: str, scheduled_at: datetime, image: str | None,
                  result: str, author_name: str) -> Event:
    title = title.strip()
    if not title:
        raise ServiceError("Give the event a title.")
    event = Event(
        title=title,
        event_type=event_type or "Match",
        opponent=opponent.strip() or None,
        description=description.strip() or None,
        scheduled_at=scheduled_at,
        image=image,
        result=result.strip() or None,
        created_by_name=author_name,
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def update_event(session: Session, event: Event, *, title: str, event_type: str,
                  opponent: str, description: str, scheduled_at: datetime,
                  image: str | None, result: str) -> Event:
    title = title.strip()
    if not title:
        raise ServiceError("Give the event a title.")
    event.title = title
    event.event_type = event_type or "Match"
    event.opponent = opponent.strip() or None
    event.description = description.strip() or None
    event.scheduled_at = scheduled_at
    if image is not None:
        event.image = image
    event.result = result.strip() or None
    session.commit()
    session.refresh(event)
    return event


def delete_event(session: Session, event: Event) -> None:
    session.delete(event)
    session.commit()


# --- Streamers ------------------------------------------------------------ #
def list_streamers(session: Session) -> list[Streamer]:
    return list(session.execute(select(Streamer).order_by(Streamer.position.asc(), Streamer.id.asc())).scalars())


def get_streamer(session: Session, streamer_id: int) -> Streamer | None:
    return session.get(Streamer, streamer_id)


def create_streamer(session: Session, *, display_name: str, twitch_login: str,
                     avatar: str | None, author_name: str) -> Streamer:
    display_name = display_name.strip()
    twitch_login = twitch_login.strip().lower().lstrip("@")
    if not display_name or not twitch_login:
        raise ServiceError("Give the streamer a display name and Twitch username.")
    if session.execute(select(Streamer.id).where(Streamer.twitch_login == twitch_login)).first():
        raise ServiceError(f"{twitch_login} is already on the showcase.")
    next_position = (
        session.execute(select(Streamer.position).order_by(Streamer.position.desc())).scalars().first() or 0
    ) + 1
    streamer = Streamer(
        display_name=display_name,
        twitch_login=twitch_login,
        avatar=avatar,
        position=next_position,
        added_by_name=author_name,
    )
    session.add(streamer)
    session.commit()
    session.refresh(streamer)
    return streamer


def delete_streamer(session: Session, streamer: Streamer) -> None:
    session.delete(streamer)
    session.commit()
