"""CRUD helpers for articles, streamers, and events.

Events are created and edited on the site by staff, and players sign up
from either surface -- the site or the Discord announcement's buttons (see
discord_rsvp.py). The older Discord Scheduled Events mirror still runs
alongside that (sync_discord_events below), so an event someone makes in
Discord's own Events tab still appears here; it just isn't the only way in
any more.

Kept separate from app.py so the routes stay thin (parse request -> call
service -> render/redirect), matching the ValorLink web app's own
app.py/services.py split.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from html import escape as _escape_html

from fastapi import UploadFile
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, defer, with_expression

import discord_clips as discord_clips_mod
import discord_events as discord_events_mod
import discord_roster
import html_sanitize
import images
from formations import BENCH_SLOTS, FORMATIONS
from models import (ARTICLE_CATEGORIES, ATTENDANCE_STATUSES, SIGNUP_STATUSES, Article, Clip,
                    Comment, Event, EventSignup, EventTierInvite, Like, PlayerLink, RosterMove,
                    Streamer, TacticsBoard, TacticsSlot)

EVENT_TYPES = ["Match", "Scrim", "Tournament", "Community"]

# Below this many marked events, a reliability percentage is noise dressed
# up as data -- two events is a 50% swing per event. The UI shows the raw
# record instead until there's enough to average.
MIN_EVENTS_FOR_RELIABILITY = 3

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


async def process_image_upload(upload: UploadFile | None) -> tuple[str, str] | tuple[None, None]:
    """(display, thumbnail) data URIs for an uploaded image, or (None, None)
    if nothing was uploaded. Both are downscaled and re-encoded rather than
    stored verbatim -- see images.py for why, and app.py for how they're
    then served as cacheable URLs instead of pasted into the markup."""
    if upload is None or not upload.filename:
        return None, None
    content_type = upload.content_type or ""
    if not content_type.startswith("image/"):
        raise ServiceError("That file doesn't look like an image.")
    data = await upload.read()
    if not data:
        return None, None
    if len(data) > _MAX_IMAGE_BYTES:
        raise ServiceError("Images must be under 2MB.")
    try:
        return images.render_variants(data)
    except images.ImageError as exc:
        raise ServiceError(str(exc)) from exc


# Re-exported so callers that only need to turn a stored image back into
# bytes don't have to reach past the service layer.
decode_data_uri = images.decode_data_uri


def article_image(session: Session, *, article_id: int | None = None,
                   slug: str | None = None, variant: str = "cover") -> str | None:
    """One article's stored cover image, as a data URI, selected on its own
    -- these rows are the biggest thing in the database and there's no
    reason to hydrate a whole Article (body_html included) to serve one
    picture.

    "thumb" falls back to the full-size cover for rows saved before
    thumbnails existed, so old articles keep showing a cover rather than a
    hole while only new uploads get the smaller variant."""
    column = Article.cover_thumb if variant == "thumb" else Article.cover_image
    query = select(column, Article.cover_image)
    if article_id is not None:
        query = query.where(Article.id == article_id)
    elif slug is not None:
        query = query.where(Article.slug == slug)
    else:
        return None
    row = session.execute(query).first()
    if row is None:
        return None
    return row[0] or row[1]


def streamer_avatar(session: Session, streamer_id: int, *, variant: str = "thumb") -> str | None:
    """Same idea as article_image, for a streamer's uploaded avatar."""
    column = Streamer.avatar_thumb if variant == "thumb" else Streamer.avatar
    row = session.execute(
        select(column, Streamer.avatar).where(Streamer.id == streamer_id)
    ).first()
    if row is None:
        return None
    return row[0] or row[1]


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
# The columns no list view ever renders: the full article body, and the two
# base64 cover images. Left in the SELECT they dominate every listing query
# -- a home page of 17 cards was reading tens of megabytes out of SQLite to
# render a few hundred kilobytes of markup. Deferred instead, with a cheap
# boolean standing in for "is there a cover" so templates never have to
# touch the blob to find out (see Article.has_cover).
_LIST_DEFERRED = (Article.body_html, Article.cover_image, Article.cover_thumb)


def _list_options():
    return [defer(column) for column in _LIST_DEFERRED] + [
        with_expression(Article.has_cover, Article.cover_image.is_not(None)),
    ]


def list_articles(session: Session, *, include_drafts: bool = False,
                   category: str | None = None, limit: int | None = None) -> list[Article]:
    """Articles for a listing page. Cover images and bodies are NOT loaded
    -- see _LIST_DEFERRED. Use get_article() for a page that renders one
    article in full."""
    query = select(Article).options(*_list_options()).order_by(Article.published_at.desc())
    if not include_drafts:
        query = query.where(Article.published.is_(True))
    if category:
        query = query.where(Article.category == category)
    if limit:
        query = query.limit(limit)
    return list(session.execute(query).scalars())


def articles_with_discord_message(session: Session, limit: int) -> list[Article]:
    """Most-recently-published articles that were actually announced to
    Discord (have a message to check) -- see discord_reactions_poll.py.
    Bounded, not "every article ever announced": reactions settle quickly
    after posting, so checking an old announcement forever is pointless
    upkeep, not a real feature gap."""
    query = (
        select(Article)
        .where(Article.discord_message_id.is_not(None))
        .order_by(Article.published_at.desc())
        .limit(limit)
    )
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
                    category: str = "News", cover_focal_x=50, cover_focal_y=50,
                    cover_thumb: str | None = None) -> Article:
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
        cover_thumb=cover_thumb,
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
                    category: str | None = None, cover_focal_x=50, cover_focal_y=50,
                    cover_thumb: str | None = None) -> Article:
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
        article.cover_thumb = cover_thumb
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
def combined_like_count(article: Article, site_likes: int) -> int:
    """One reaction figure for an article: likes given on the site plus
    reactions on its Discord announcement.

    These used to be shown as two separate badges, on the reasoning that
    they measure different things -- the site's is a per-member toggle we
    know the identity behind, Discord's is an anonymous aggregate of any
    emoji. That's still true, and it's why the button's filled/empty state
    is driven by the viewer's own like alone (see has_liked) rather than by
    this total. But two numbers side by side read as a puzzle rather than a
    signal, so the count itself is the sum.

    A caveat worth knowing when the number looks high: Discord's side counts
    every emoji on the announcement, not just hearts, and one person
    reacting three times counts three times -- see
    discord_announce.fetch_reaction_count. It also lags by up to the
    reactions poll interval."""
    return site_likes + (article.discord_reaction_count or 0)


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


def get_event(session: Session, event_id: int) -> Event | None:
    return session.get(Event, event_id)


def _validated_formation(formation: str | None) -> str | None:
    formation = (formation or "").strip()
    if not formation:
        return None
    if formation not in FORMATIONS:
        raise ServiceError(f"Unknown formation {formation!r}.")
    return formation


def event_slots(event: Event) -> dict[str, str]:
    """slot_key -> display label for everything claimable on this event:
    the formation's eleven, then the bench. Empty for an event with no
    formation, which is what makes "does this event use positions" a single
    truthy check everywhere else."""
    if not event.formation:
        return {}
    slots = {key: meta["label"] for key, meta in FORMATIONS[event.formation].items()}
    slots.update({key: meta["label"] for key, meta in BENCH_SLOTS.items()})
    return slots


def claimed_slots(session: Session, event_id: int) -> dict[str, EventSignup]:
    """slot_key -> the sign-up holding it. Only "going" rows can hold a
    slot, so this is also the starting XI as it currently stands."""
    return {
        s.slot_key: s
        for s in session.execute(
            select(EventSignup).where(
                EventSignup.event_id == event_id,
                EventSignup.slot_key.isnot(None),
            )
        ).scalars()
    }


def _validated_event_fields(*, title: str, event_type: str, scheduled_at: datetime | None) -> tuple[str, str]:
    title = (title or "").strip()
    if not title:
        raise ServiceError("Give the event a title.")
    if event_type not in EVENT_TYPES:
        raise ServiceError(f"Unknown event type {event_type!r}.")
    if scheduled_at is None:
        raise ServiceError("Give the event a date and time.")
    return title, event_type


def create_event(session: Session, *, title: str, event_type: str, scheduled_at: datetime,
                 opponent: str | None, description: str | None, image: str | None,
                 staff_name: str, formation: str | None = None) -> Event:
    title, event_type = _validated_event_fields(
        title=title, event_type=event_type, scheduled_at=scheduled_at)
    event = Event(
        title=title, event_type=event_type, scheduled_at=scheduled_at,
        opponent=(opponent or "").strip() or None,
        description=(description or "").strip() or None,
        image=image, created_by_name=staff_name,
        formation=_validated_formation(formation),
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def update_event(session: Session, event: Event, *, title: str, event_type: str,
                 scheduled_at: datetime, opponent: str | None, description: str | None,
                 image: str | None, result: str | None, formation: str | None = None) -> Event:
    title, event_type = _validated_event_fields(
        title=title, event_type=event_type, scheduled_at=scheduled_at)
    formation = _validated_formation(formation)
    if formation != event.formation:
        # Slot keys are formation-specific, so a claimed CDM2 is meaningless
        # once the shape becomes 4-3-3. Release every claim rather than
        # leaving players holding positions that no longer exist -- they
        # stay signed up as going, they just have to re-pick a shirt.
        session.execute(
            update(EventSignup).where(EventSignup.event_id == event.id).values(slot_key=None)
        )
        event.formation = formation
    event.title = title
    event.event_type = event_type
    event.scheduled_at = scheduled_at
    event.opponent = (opponent or "").strip() or None
    event.description = (description or "").strip() or None
    event.result = (result or "").strip() or None
    if image is not None:
        event.image = image
    session.commit()
    session.refresh(event)
    return event


def delete_event(session: Session, event: Event) -> None:
    """Removes the event and every sign-up for it. Sign-ups have no
    meaning without their event, and this app manages its schema with
    create_all rather than a migration tool, so the cascade is done here
    explicitly rather than relying on a DB-level ON DELETE."""
    session.execute(delete(EventSignup).where(EventSignup.event_id == event.id))
    session.delete(event)
    session.commit()


def set_signups_open(session: Session, event: Event, *, open_: bool) -> Event:
    event.signups_open = open_
    session.commit()
    session.refresh(event)
    return event


def set_event_announcement(session: Session, event: Event, *, channel_id: str, message_id: str) -> None:
    event.discord_channel_id = str(channel_id)
    event.discord_message_id = str(message_id)
    session.commit()


# --- Staged thread invites -------------------------------------------------- #
def events_awaiting_invites(session: Session) -> list[Event]:
    """Announced, still-upcoming events whose thread can still be widened.

    Past events are excluded: a tier that comes due after kick-off has
    missed its purpose, and pinging people to a fixture that has already
    been played is worse than staying quiet. Events that were never
    announced have no thread to add anyone to.
    """
    return list(session.execute(
        select(Event)
        .where(Event.discord_channel_id.is_not(None))
        .where(Event.scheduled_at >= datetime.utcnow())
        .order_by(Event.scheduled_at)
    ).scalars())


def invited_tier_keys(session: Session, event_id: int) -> set[str]:
    return set(session.execute(
        select(EventTierInvite.tier_key).where(EventTierInvite.event_id == event_id)
    ).scalars())


def due_invite_tiers(session: Session, event: Event, tiers: list[dict],
                     now: datetime | None = None) -> list[dict]:
    """Which rungs of the invite ladder this event owes right now.

    A tier is due when its moment has passed and it has not already fired.
    `create` is due the instant the event is announced; an hours-before
    tier is due once kick-off is that close.

    Deliberately "has passed", not "is within a window": an event announced
    12 hours before kick-off owes its 48h and 24h tiers immediately rather
    than never, and a poller that was down over a tier's moment still
    catches up on its next run instead of silently skipping it.
    """
    now = now or datetime.utcnow()
    already = invited_tier_keys(session, event.id)
    due = []
    for tier in tiers:
        if tier["key"] in already:
            continue
        hours = tier["hours_before"]
        if hours is not None and (event.scheduled_at - now).total_seconds() > hours * 3600:
            continue
        due.append(tier)
    return due


def record_tier_invite(session: Session, event: Event, tier: dict, member_count: int) -> None:
    """Marks a tier as fired for this event. The unique constraint makes a
    concurrent double-fire an error rather than a second ping."""
    session.add(EventTierInvite(
        event_id=event.id, tier_key=tier["key"],
        role_id=str(tier["role_id"]), member_count=member_count,
    ))
    session.commit()


# --- Sign-ups --------------------------------------------------------------- #
def list_signups(session: Session, event_id: int) -> list[EventSignup]:
    """Everyone who has answered, ordered going -> maybe -> out and then by
    when they answered, so the roster reads as a squad list rather than in
    click order."""
    order = {status: i for i, status in enumerate(SIGNUP_STATUSES)}
    rows = list(session.execute(
        select(EventSignup).where(EventSignup.event_id == event_id)
    ).scalars())
    return sorted(rows, key=lambda s: (order.get(s.status, 99), s.responded_at or datetime.min))


def get_signup(session: Session, event_id: int, discord_user_id: int) -> EventSignup | None:
    return session.execute(
        select(EventSignup).where(
            EventSignup.event_id == event_id,
            EventSignup.discord_user_id == discord_user_id,
        )
    ).scalars().first()


def signup_counts(session: Session, event_id: int) -> dict[str, int]:
    counts = {status: 0 for status in SIGNUP_STATUSES}
    for status, total in session.execute(
        select(EventSignup.status, func.count(EventSignup.id))
        .where(EventSignup.event_id == event_id).group_by(EventSignup.status)
    ).all():
        if status in counts:
            counts[status] = total
    return counts


def set_signup(session: Session, event: Event, *, discord_user_id: int, discord_name: str,
               discord_avatar: str | None, status: str, source: str = "site") -> EventSignup:
    """Records (or changes) one player's answer. Idempotent per player --
    answering again updates the existing row rather than stacking, which is
    what makes the two surfaces safe to use interchangeably."""
    if status not in SIGNUP_STATUSES:
        raise ServiceError(f"Unknown sign-up status {status!r}.")
    if not event.signups_open:
        raise ServiceError("Sign-ups for this event are closed.")

    signup = get_signup(session, event.id, discord_user_id)
    if signup is None:
        signup = EventSignup(
            event_id=event.id, discord_user_id=discord_user_id, discord_name=discord_name,
            discord_avatar=discord_avatar, status=status, source=source,
        )
        session.add(signup)
    else:
        signup.status = status
        signup.source = source
        # Refresh the display name -- people rename themselves, and a stale
        # name on a live roster is worse than no name.
        signup.discord_name = discord_name
        signup.discord_avatar = discord_avatar
    if status != "going":
        # Answering maybe/out frees the shirt for someone else. Holding a
        # position while saying you can't make it would quietly block a slot
        # nobody can see is free.
        signup.slot_key = None
    session.commit()
    session.refresh(signup)
    return signup


def claim_slot(session: Session, event: Event, *, discord_user_id: int, discord_name: str,
               discord_avatar: str | None, slot_key: str, source: str = "site") -> EventSignup:
    """Takes a shirt. Claiming a position IS signing up -- it sets status
    "going" as well, because picking where you'll play and saying you'll be
    there are the same statement.

    One player per slot: a formation has exactly one GK, so a second
    claimant is refused rather than silently sharing. A player moving
    between slots releases their old one first, so nobody can hold two.
    """
    if not event.signups_open:
        raise ServiceError("Sign-ups for this event are closed.")
    slots = event_slots(event)
    if not slots:
        raise ServiceError("This event doesn't use positions.")
    if slot_key not in slots:
        raise ServiceError(f"{slot_key} isn't a position in this event's formation.")

    holder = claimed_slots(session, event.id).get(slot_key)
    if holder is not None and holder.discord_user_id != discord_user_id:
        raise ServiceError(f"{slots[slot_key]} is already taken by {holder.discord_name}.")

    signup = get_signup(session, event.id, discord_user_id)
    if signup is None:
        signup = EventSignup(
            event_id=event.id, discord_user_id=discord_user_id, discord_name=discord_name,
            discord_avatar=discord_avatar, status="going", slot_key=slot_key, source=source,
        )
        session.add(signup)
    else:
        signup.status = "going"
        signup.slot_key = slot_key
        signup.source = source
        signup.discord_name = discord_name
        signup.discord_avatar = discord_avatar
    session.commit()
    session.refresh(signup)
    return signup


def release_slot(session: Session, event: Event, *, discord_user_id: int) -> EventSignup | None:
    """Gives up the shirt but stays signed up as going -- "I'll be there,
    just not in that position" is a normal thing to want to say."""
    signup = get_signup(session, event.id, discord_user_id)
    if signup is not None and signup.slot_key is not None:
        signup.slot_key = None
        session.commit()
        session.refresh(signup)
    return signup


def mark_attendance(session: Session, signup: EventSignup, *, attendance: str | None,
                    staff_name: str) -> EventSignup:
    """Records what actually happened. Passing None clears the mark, which
    returns the event to "not evidence" rather than counting as absent."""
    if attendance is not None and attendance not in ATTENDANCE_STATUSES:
        raise ServiceError(f"Unknown attendance status {attendance!r}.")
    signup.attendance = attendance
    signup.attendance_marked_by = staff_name if attendance else None
    signup.attendance_marked_at = datetime.utcnow() if attendance else None
    session.commit()
    session.refresh(signup)
    return signup


# --- Attendance reliability ------------------------------------------------- #
def attendance_record(session: Session, discord_user_id: int) -> dict:
    """How often this player actually turned up, across every event where
    staff marked them.

    Only marked events count. "excused" is deliberately excluded from both
    halves of the ratio rather than counted as a miss -- an approved
    absence says nothing about reliability, and counting it as a no-show
    would punish people for telling staff in advance, which is the exact
    behaviour we want to encourage.
    """
    rows = list(session.execute(
        select(EventSignup.attendance).where(
            EventSignup.discord_user_id == discord_user_id,
            EventSignup.attendance.isnot(None),
        )
    ).scalars())
    present = sum(1 for a in rows if a == "present")
    absent = sum(1 for a in rows if a == "absent")
    excused = sum(1 for a in rows if a == "excused")
    counted = present + absent
    rate = round(100 * present / counted) if counted else None
    return {
        "present": present, "absent": absent, "excused": excused,
        "counted": counted,
        # None means "not enough history to say" -- render the raw record
        # instead of a number the sample can't support.
        "rate": rate if counted >= MIN_EVENTS_FOR_RELIABILITY else None,
        "has_history": counted > 0,
    }


def attendance_records_for(session: Session, discord_user_ids: list[int]) -> dict[int, dict]:
    """attendance_record() for a whole roster in one query, so an event
    page with 20 sign-ups doesn't fire 20 round trips."""
    if not discord_user_ids:
        return {}
    tally: dict[int, dict[str, int]] = {
        uid: {"present": 0, "absent": 0, "excused": 0} for uid in discord_user_ids
    }
    for user_id, attendance, total in session.execute(
        select(EventSignup.discord_user_id, EventSignup.attendance, func.count(EventSignup.id))
        .where(EventSignup.discord_user_id.in_(discord_user_ids),
               EventSignup.attendance.isnot(None))
        .group_by(EventSignup.discord_user_id, EventSignup.attendance)
    ).all():
        if user_id in tally and attendance in tally[user_id]:
            tally[user_id][attendance] = total

    out = {}
    for user_id, counts in tally.items():
        counted = counts["present"] + counts["absent"]
        rate = round(100 * counts["present"] / counted) if counted else None
        out[user_id] = {
            **counts, "counted": counted,
            "rate": rate if counted >= MIN_EVENTS_FOR_RELIABILITY else None,
            "has_history": counted > 0,
        }
    return out


# --- Gamertag links and Tactics roles --------------------------------------- #
def get_player_link(session: Session, discord_user_id: int) -> PlayerLink | None:
    return session.get(PlayerLink, discord_user_id)


def set_player_link(session: Session, *, discord_user_id: int, player_name: str) -> PlayerLink:
    player_name = (player_name or "").strip()
    if not player_name:
        raise ServiceError("Pick the gamertag you play under.")
    taken = session.execute(
        select(PlayerLink).where(
            func.lower(PlayerLink.player_name) == player_name.lower(),
            PlayerLink.discord_user_id != discord_user_id,
        )
    ).scalars().first()
    if taken is not None:
        raise ServiceError(f"{player_name} is already claimed by another member.")

    link = session.get(PlayerLink, discord_user_id)
    if link is None:
        link = PlayerLink(discord_user_id=discord_user_id, player_name=player_name)
        session.add(link)
    else:
        link.player_name = player_name
    session.commit()
    session.refresh(link)
    return link


def clear_player_link(session: Session, discord_user_id: int) -> None:
    link = session.get(PlayerLink, discord_user_id)
    if link is not None:
        session.delete(link)
        session.commit()


def player_links_for(session: Session, discord_user_ids: list[int]) -> dict[int, str]:
    if not discord_user_ids:
        return {}
    return {
        row.discord_user_id: row.player_name
        for row in session.execute(
            select(PlayerLink).where(PlayerLink.discord_user_id.in_(discord_user_ids))
        ).scalars()
    }


def tactics_roles_for(session: Session, discord_user_ids: list[int],
                      slot_labels: dict[str, str]) -> dict[int, str]:
    """discord_user_id -> the position they hold on the current team sheet.

    Two hops: Discord user -> gamertag (PlayerLink) -> slot on the active
    formation (TacticsSlot). Anyone missing either hop simply has no role,
    which is a normal state (a new member, or a squad player not in the
    current XI) rather than an error. `slot_labels` maps a slot key to its
    display position ("CM1" -> "CM") and comes from app.py's FORMATIONS,
    which is the authority on what a formation's slots are called.
    """
    links = player_links_for(session, discord_user_ids)
    if not links:
        return {}
    slots = get_tactics_slots(session, get_active_formation(session))
    by_player = {name.lower(): slot_key for slot_key, name in slots.items()}
    roles = {}
    for user_id, player_name in links.items():
        slot_key = by_player.get(player_name.lower())
        if slot_key:
            roles[user_id] = slot_labels.get(slot_key, slot_key)
    return roles


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


_FILENAME_STEM_RE = re.compile(r"[_\-]+")
_FILENAME_WHITESPACE_RE = re.compile(r"\s+")


def _title_from_filename(filename: str | None) -> str | None:
    """A clip's title, derived from its own filename rather than whatever
    (if anything) someone typed as a Discord chat message alongside it --
    that's often blank, unrelated banter, or just an emoji, while
    console/game capture uploads name the file itself (e.g.
    "EA SPORTS FC 25 2027-06-01 20-15-30.mp4"), which is the closer thing
    to an actual title a clip has."""
    if not filename:
        return None
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    cleaned = _FILENAME_WHITESPACE_RE.sub(" ", _FILENAME_STEM_RE.sub(" ", stem)).strip()
    return cleaned or None


def sync_clips(session: Session, channel_id: str, messages: list[dict]) -> dict:
    """Mirrors video attachments from a Discord channel's recent messages
    into Clip rows. Every sync refreshes video_url (and title -- see
    _title_from_filename) for a clip whose message is still within the
    polled window, since Discord's attachment URLs are signed and expire
    (~24h) -- see discord_clips.py.

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
        title = _title_from_filename(attachment.get("filename"))

        clip = session.execute(
            select(Clip).where(Clip.discord_message_id == message_id)
        ).scalar_one_or_none()
        if clip is None:
            session.add(Clip(
                discord_message_id=message_id,
                title=title,
                video_url=attachment.get("url"),
                filename=attachment.get("filename"),
                author_name=author_name,
                jump_url=discord_clips_mod.jump_url(channel_id, message_id),
                posted_at=posted_at,
            ))
            created += 1
        else:
            clip.video_url = attachment.get("url")
            clip.title = title
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


# --- Squad moves ------------------------------------------------------------ #
def record_roster_move(session: Session, *, discord_id: str, display_name: str,
                       avatar_url: str | None, kind: str, position: str | None,
                       note: str | None, announced_by_name: str | None,
                       announced_by_discord_id: int | None,
                       discord_message_id: str | None) -> RosterMove:
    """Writes the history row for one published squad announcement.

    Called after the Discord post is attempted, not before, so
    discord_message_id records what actually happened -- a null there is
    the honest record of a post that failed, and the page says so rather
    than implying the club announced something it didn't.
    """
    move = RosterMove(
        discord_id=str(discord_id), display_name=display_name, avatar_url=avatar_url,
        kind=kind, position=(position or "").strip() or None,
        note=(note or "").strip() or None,
        announced_by_name=announced_by_name,
        announced_by_discord_id=announced_by_discord_id,
        discord_message_id=discord_message_id,
    )
    session.add(move)
    session.commit()
    session.refresh(move)
    return move


def set_roster_move_message(session: Session, move_id: int, *, channel_id: str,
                            message_id: str) -> None:
    """Backfills where the announcement landed.

    Split from record_roster_move because an offer's buttons carry its row
    id, so the row has to exist before the message can be built -- the id
    only comes back once Discord has accepted the post.
    """
    move = session.get(RosterMove, move_id)
    if move is None:
        return
    move.discord_channel_id = str(channel_id)
    move.discord_message_id = str(message_id)
    session.commit()


def get_roster_move(session: Session, move_id: int) -> RosterMove | None:
    return session.get(RosterMove, move_id)


def record_offer_response(session: Session, move: RosterMove, *, response: str,
                          role_granted: bool, role_error: str | None) -> RosterMove:
    """Stores the player's own answer to their offer.

    The acceptance and the role write are recorded separately because
    they can disagree: the press is theirs and always stands, while
    granting the role can fail on Discord's side (see
    discord_roster.grant_squad_role). Swallowing that would leave staff
    looking at an acceptance that quietly granted nothing.
    """
    move.response = response
    move.responded_at = datetime.utcnow()
    move.role_granted = role_granted
    move.role_error = role_error
    session.commit()
    session.refresh(move)
    return move


def confirm_roster_move(session: Session, move: RosterMove, *,
                        confirmed_by_name: str | None,
                        confirm_message_id: str | None) -> RosterMove:
    """Marks an accepted offer as confirmed by staff.

    Like record_roster_move, written after the announcement is attempted
    so a null confirm_message_id is the honest record of a celebration
    that didn't send.
    """
    move.confirmed_at = datetime.utcnow()
    move.confirmed_by_name = confirmed_by_name
    move.confirm_message_id = confirm_message_id
    session.commit()
    session.refresh(move)
    return move


def offer_is_open(move: RosterMove) -> bool:
    """An offer still waiting on the player. Departures are never open --
    there is nothing to accept about being let go."""
    return move.kind == discord_roster.MOVE_OFFER and move.response is None


def offer_awaits_confirmation(move: RosterMove) -> bool:
    """Accepted by the player, not yet confirmed by staff -- the state the
    /roster page turns into a Confirm signing button."""
    return (move.kind == discord_roster.MOVE_OFFER
            and move.response == discord_roster.RESPONSE_ACCEPTED
            and move.confirmed_at is None)


def recent_roster_moves(session: Session, limit: int = 15) -> list[RosterMove]:
    """Newest first, bounded -- this is a "what did we just publish"
    panel on the staff page, not an archive anyone pages through."""
    return list(session.execute(
        select(RosterMove).order_by(RosterMove.announced_at.desc(), RosterMove.id.desc())
        .limit(limit)
    ).scalars())


# --- Streamers ------------------------------------------------------------ #
def list_streamers(session: Session) -> list[Streamer]:
    return list(session.execute(select(Streamer).order_by(Streamer.position.asc(), Streamer.id.asc())).scalars())


def get_streamer(session: Session, streamer_id: int) -> Streamer | None:
    return session.get(Streamer, streamer_id)


def get_featured_streamer(session: Session) -> Streamer | None:
    return session.execute(select(Streamer).where(Streamer.featured.is_(True))).scalar_one_or_none()


def create_streamer(session: Session, *, display_name: str, twitch_login: str,
                     avatar: str | None, author_name: str, featured: bool = False,
                     avatar_thumb: str | None = None) -> Streamer:
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
        avatar_thumb=avatar_thumb,
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


# --- Tactics board ---------------------------------------------------------- #
def get_active_formation(session: Session) -> str:
    """The formation currently shown on /tactics -- creates the singleton
    settings row with a sane default on first use rather than requiring a
    migration/seed step."""
    board = session.get(TacticsBoard, 1)
    if board is None:
        return "4-3-3"
    return board.active_formation


def get_tactics_slots(session: Session, formation: str) -> dict[str, str]:
    """slot_key -> player_name for every filled slot of one formation.
    Empty slots aren't real rows -- absence from this dict means empty,
    same as a row with player_name=None."""
    rows = session.execute(
        select(TacticsSlot.slot_key, TacticsSlot.player_name).where(TacticsSlot.formation == formation)
    ).all()
    return {slot_key: name for slot_key, name in rows if name}


def get_all_tactics_slots(session: Session, formations: list[str]) -> dict[str, dict[str, str]]:
    """get_tactics_slots() for every formation at once -- lets the page
    embed every formation's saved lineup on load, so switching formations
    in the UI is instant (no round trip) instead of a fetch per switch."""
    return {formation: get_tactics_slots(session, formation) for formation in formations}


def save_tactics_lineup(session: Session, *, formation: str, slots: dict[str, str | None],
                         valid_slot_keys: set[str], staff_name: str) -> None:
    """Replaces a formation's entire slot assignment in one shot (the UI
    saves the whole board on "Save Lineup", not per-drag) and marks this
    formation as the active one. `valid_slot_keys` is the caller's
    authority on what this formation actually has slots for (see app.py's
    FORMATIONS) -- an unknown key is a bug or a tampered request, not
    something to silently store."""
    unknown = set(slots) - valid_slot_keys
    if unknown:
        raise ServiceError(f"Unknown slot(s) for {formation}: {', '.join(sorted(unknown))}")

    existing = {
        row.slot_key: row
        for row in session.execute(select(TacticsSlot).where(TacticsSlot.formation == formation)).scalars()
    }
    for slot_key in valid_slot_keys:
        name = (slots.get(slot_key) or "").strip() or None
        row = existing.get(slot_key)
        if row is None:
            session.add(TacticsSlot(formation=formation, slot_key=slot_key, player_name=name))
        else:
            row.player_name = name

    board = session.get(TacticsBoard, 1)
    if board is None:
        board = TacticsBoard(id=1, active_formation=formation, updated_by_name=staff_name)
        session.add(board)
    else:
        board.active_formation = formation
        board.updated_by_name = staff_name
    session.commit()
