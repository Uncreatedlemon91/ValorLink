"""Content models for the team site: news articles, events, and the
streamer showcase. Permissions aren't modelled here at all -- "can edit"
is derived live from the signed-in user's Discord roles (see auth.py), not
stored, so a role change in Discord takes effect immediately everywhere.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Column, Date, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import query_expression

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
    # Both data URIs (see images.py), served as cacheable URLs rather
    # than inlined into the page -- cover_thumb is the small variant the
    # card/rail grids use, so a page showing a dozen covers doesn't pull
    # a dozen hero-sized images. Null on rows saved before thumbnails
    # existed; those fall back to cover_image.
    cover_image = Column(Text, nullable=True)
    cover_thumb = Column(Text, nullable=True)
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

    # Populated by list queries (see services.list_articles), which defer
    # the cover columns rather than dragging a megabyte of base64 per row
    # into a page that only needs to know whether to render an <img> at
    # all. Templates showing a list test this instead of cover_image;
    # touching cover_image there would undo the deferral one lazy load at
    # a time. None on a fully-loaded row, where cover_image is there to
    # read directly.
    has_cover = query_expression()


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
    # Set only on events once mirrored in from Discord's Scheduled Events.
    # That sync is gone (Discord's Events tab is no longer read); the column
    # stays because this schema is add-only, and nothing reads it now.
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
    # When set, signing up means claiming a named position in this formation
    # rather than answering a flat yes/no -- the team sheet IS the sign-up
    # sheet. NULL keeps the plain Going/Maybe/Out behaviour.
    formation = Column(String, nullable=True)

    # --- The match-week loop (see matchweek.py) ---------------------------
    # The team sheet: picked by staff (EventLineup rows), then published --
    # posted to Discord and DMed to each player. Unset until then.
    lineup_published_at = Column(DateTime, nullable=True)
    lineup_message_id = Column(String, nullable=True)
    # After the match: the score (prefilled from EA's record of it), what
    # happened, and clips -- published as the match report.
    us_score = Column(Integer, nullable=True)
    opp_score = Column(Integer, nullable=True)
    review_notes = Column(Text, nullable=True)
    review_clips = Column(Text, nullable=True)          # one URL per line
    review_published_at = Column(DateTime, nullable=True)
    review_published_by = Column(String, nullable=True)
    # The post-match vote: opened by the notifier after full time (or by
    # staff), closed when the report is published or the window ends.
    vote_opened_at = Column(DateTime, nullable=True)
    vote_closes_at = Column(DateTime, nullable=True)
    vote_message_id = Column(String, nullable=True)

    # --- Training sessions (see training.py) -------------------------------
    session_objective = Column(String, nullable=True)
    session_plan = Column(Text, nullable=True)
    # JSON: slot_key -> what that position does in this session.
    role_briefs = Column(Text, nullable=True)
    session_review = Column(Text, nullable=True)
    session_reviewed_by = Column(String, nullable=True)


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
    # The position claimed on the event's formation ("GK", "CM2", "SUB1"),
    # for events that have one. Only ever set alongside status "going" --
    # you cannot hold a shirt and also be out. At most one player per slot
    # per event; that's enforced in services.claim_slot rather than by a
    # DB constraint, because this app's schema is create_all-managed with
    # no migration tool, so a UniqueConstraint added here would apply only
    # to databases created after it and silently not to existing ones.
    slot_key = Column(String, nullable=True)
    source = Column(String, nullable=False, default="site")     # site|discord
    responded_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    # Filled in by staff afterwards -- NULL means "not marked", which is
    # different from absent and is excluded from the reliability figure.
    attendance = Column(String, nullable=True)                  # see ATTENDANCE_STATUSES
    attendance_marked_by = Column(String, nullable=True)
    attendance_marked_at = Column(DateTime, nullable=True)

    # Where the player would like to play this match, best first -- pitch
    # positions (discord_roster.PITCH_POSITIONS), not slots, so the coach
    # sees "Striker, then Winger" rather than one claimed shirt.
    pref_1 = Column(String, nullable=True)
    pref_2 = Column(String, nullable=True)
    pref_3 = Column(String, nullable=True)


class EventTierInvite(Base):
    """One staged invite that has already been sent for one event.

    The invite ladder (config.EVENT_INVITE_TIERS) widens a fixture's
    audience as it approaches: the first tier is pinged when the event is
    announced, later tiers at a set number of hours before kick-off. This
    table is what stops the poller re-pinging a tier on every run -- a row
    here means "that tier has had its turn for this event", and the unique
    constraint makes a double-fire a database error rather than a second
    notification.

    Kept as its own table rather than a column on Event because the ladder
    is configurable: how many tiers exist, and their keys, can change
    between deployments and between edits to .env, and a row per fired tier
    survives that without a schema change.
    """

    __tablename__ = "event_tier_invites"
    __table_args__ = (UniqueConstraint("event_id", "tier_key", name="uq_tier_event_key"),)

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, nullable=False, index=True)
    # The tier's key from the config ladder: "create", or the hours-before
    # as written ("48"). Stored as text because it is the config's own
    # identifier for that rung, not a number we do arithmetic on.
    tier_key = Column(String, nullable=False)
    role_id = Column(String, nullable=False)
    invited_at = Column(DateTime, default=_utcnow)
    # How many members were actually added to the thread. Recorded because
    # "we pinged the role but added nobody" is the signature of a missing
    # GUILD_MEMBERS intent, and that is otherwise invisible after the fact.
    member_count = Column(Integer, nullable=False, default=0)


class RosterMove(Base):
    """One squad announcement that has been published to Discord.

    Kept because the announcement itself lives in Discord, where it
    scrolls away: without a row here, "did we ever announce that?" is a
    question only answerable by scrolling a channel, and a staff member
    who reloads the page after a flaky post has no way to tell whether it
    went out. The /roster page reads these back as a short history.

    Not a roster table. This records what was *announced*, not who is in
    the squad -- membership is still Discord roles, which this app reads
    and never writes (see discord_roster.py). A row here is a press
    release, so it is never edited or back-dated; a mistake is corrected
    by publishing the opposite move, exactly as it would be in the
    channel.
    """

    __tablename__ = "roster_moves"

    id = Column(Integer, primary_key=True)
    # Text, not BigInteger: this is an opaque Discord snowflake used to
    # build a mention and match against the picker's values, never
    # arithmetic. (Article.author_discord_id predates that reasoning.)
    discord_id = Column(String, nullable=False, index=True)
    # Snapshotted at announcement time rather than looked up on render:
    # someone who was let go is likely to leave the server, and the
    # history should still say who the move was about.
    display_name = Column(String, nullable=False)
    avatar_url = Column(String, nullable=True)
    kind = Column(String, nullable=False)     # see discord_roster.MOVE_KINDS
    position = Column(String, nullable=True)
    note = Column(Text, nullable=True)
    announced_by_name = Column(String, nullable=True)
    announced_by_discord_id = Column(BigInteger, nullable=True)
    announced_at = Column(DateTime, default=_utcnow)
    # Null when the post itself failed -- the row is still written so the
    # attempt is visible, and the page marks it as not delivered.
    discord_message_id = Column(String, nullable=True)
    # Where that message lives, so the confirmation announcement lands in
    # the same channel even if ROSTER_ANNOUNCE_CHANNEL_ID is changed
    # between the offer and the signing.
    discord_channel_id = Column(String, nullable=True)

    # --- The offer's own lifecycle (offers only; a departure has none) ---
    # An offer is a question, so it has an answer: null while nobody has
    # pressed, then "accepted" or "declined" (see
    # discord_roster.OFFER_RESPONSES). Only the person the offer names can
    # set it, and only once.
    response = Column(String, nullable=True)
    responded_at = Column(DateTime, nullable=True)
    # Whether accepting actually put the squad role on them. Separate from
    # `response` because the two can disagree: the acceptance is theirs and
    # always stands, while the role write can fail on its own (bot missing
    # Manage Roles, or its highest role sitting below the squad role).
    # role_error carries why, so /roster can say what to fix instead of
    # showing an acceptance that silently granted nothing.
    role_granted = Column(Boolean, nullable=False, default=False)
    role_error = Column(String, nullable=True)
    # Staff's confirmation of an accepted offer, and the celebratory
    # announcement it published. Deliberately a second, human step: the
    # player accepting is them agreeing, not the club announcing.
    confirmed_at = Column(DateTime, nullable=True)
    confirmed_by_name = Column(String, nullable=True)
    confirm_message_id = Column(String, nullable=True)

    # --- Contract terms (offers and renewals) ---
    # What the player is being asked to agree to. Carried on the move, not
    # only the Contract, because an offer's terms exist before any
    # contract does -- the contract is only written when staff confirm
    # the signing. Null on departures, and on offers made before
    # contracts existed.
    contract_weeks = Column(Integer, nullable=True)
    squad_status = Column(String, nullable=True)    # see discord_roster.SQUAD_STATUSES
    # The contract's second position, alongside `position` (the primary).
    # Optional -- not everybody has one -- and null on staff offers, where
    # `position` holds the staff role instead of a pitch position.
    secondary_position = Column(String, nullable=True)
    # A renewal's contract. Null on everything else: an offer's contract
    # points back at the offer instead (Contract.signing_move_id).
    contract_id = Column(Integer, nullable=True, index=True)


class Contract(Base):
    """How long somebody is signed for, and at what squad status.

    Written when staff confirm a signing, or recorded directly for a
    player who was already in the squad before the site tracked this.
    Unlike RosterMove this row IS edited: a renewal the player accepts
    extends it in place, so there is only ever one live contract per
    person and "when is X's deal up?" has one answer.

    Nothing happens by itself when a contract runs out. Expiry is
    computed (expires_at in the past, ended_at unset) and shown to staff,
    who then Renew or Release -- the same rule as the rest of squad
    moves, where nothing public happens and no role changes without a
    person deciding it.
    """

    __tablename__ = "contracts"

    id = Column(Integer, primary_key=True)
    discord_id = Column(String, nullable=False, index=True)
    # Snapshotted like RosterMove's, for the same reason: somebody whose
    # contract ran out may well have left the server.
    display_name = Column(String, nullable=False)
    avatar_url = Column(String, nullable=True)
    position = Column(String, nullable=True)            # primary
    secondary_position = Column(String, nullable=True)  # optional
    squad_status = Column(String, nullable=False)   # see discord_roster.SQUAD_STATUSES
    # The length of the current term -- the last renewal's, once renewed.
    weeks = Column(Integer, nullable=False)
    starts_at = Column(DateTime, nullable=False, default=_utcnow)
    expires_at = Column(DateTime, nullable=False, index=True)
    # "signing" (an offer staff confirmed) or "recorded" (entered directly
    # for an existing squad member). Only for the staff page to say where
    # the contract came from.
    source = Column(String, nullable=False, default="signing")
    signing_move_id = Column(Integer, nullable=True)
    created_by_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    renewal_count = Column(Integer, nullable=False, default=0, server_default="0")
    last_renewed_at = Column(DateTime, nullable=True)
    # Set when staff release the player. A released contract is history:
    # it stays listed for the record but can't be renewed.
    ended_at = Column(DateTime, nullable=True)
    ended_by_name = Column(String, nullable=True)


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
    avatar_thumb = Column(Text, nullable=True)         # small variant, see images.py
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


class Player(Base):
    """One row per person: the personnel file everything else hangs off.

    Keyed on the Discord ID, which contracts, squad moves, sign-ups and
    gamertag links already store -- so those tables point here without a
    migration that rewrites them. Created the first time a guild member
    signs in, or when somebody is put under contract (see
    services.ensure_player), and backfilled from existing contracts at
    startup, so a squad carried over from FC 26 appears on day one.

    Squad status and positions are NOT here: they're terms of the
    contract and live on it. This holds who the person is, what they hold
    at the club, and what they've told us about themselves.
    """

    __tablename__ = "players"

    id = Column(Integer, primary_key=True)
    discord_id = Column(String, nullable=False, unique=True, index=True)
    display_name = Column(String, nullable=False)
    avatar_url = Column(String, nullable=True)
    # Club President / Head Coach / Coach (roles.CLUB_ROLES), or null.
    # Carries site permissions, so only management can set it.
    club_role = Column(String, nullable=True)
    club_role_since = Column(DateTime, nullable=True)
    # Filled in by the player themselves.
    preferred_foot = Column(String, nullable=True)
    archetype = Column(String, nullable=True)
    bio = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    last_seen_at = Column(DateTime, nullable=True)


class CoachNote(Base):
    """A note staff keep about a player. Staff-only, including from the
    player it's about: the one thing in a player's file they don't see."""

    __tablename__ = "coach_notes"

    id = Column(Integer, primary_key=True)
    discord_id = Column(String, nullable=False, index=True)
    body = Column(Text, nullable=False)
    author_name = Column(String, nullable=False)
    author_discord_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    # The copy posted to the staff channel (coach_notes.py), null when it
    # wasn't posted.
    discord_channel_id = Column(String, nullable=True)
    discord_message_id = Column(String, nullable=True)


class ClubSetting(Base):
    """Key/value text the club edits about itself -- what the public
    splash page says (see services.CLUB_PROFILE_FIELDS)."""

    __tablename__ = "club_settings"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    updated_by_name = Column(String, nullable=True)



# --- The match-week loop ----------------------------------------------------- #
class AvailabilityPattern(Base):
    """The nights a player can usually play, set once.

    `days` is seven characters, Monday first: "1" free, "0" not -- e.g.
    "1010100" is Monday, Wednesday and Friday. Evenings are implied: that's
    when the club plays.
    """

    __tablename__ = "availability_patterns"

    discord_id = Column(String, primary_key=True)
    days = Column(String, nullable=False, default="0000000")
    note = Column(String, nullable=True)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class AvailabilityAway(Base):
    """An exception to the usual pattern: away from these dates, inclusive."""

    __tablename__ = "availability_away"

    id = Column(Integer, primary_key=True)
    discord_id = Column(String, nullable=False, index=True)
    starts_on = Column(Date, nullable=False)
    ends_on = Column(Date, nullable=False)
    note = Column(String, nullable=True)
    created_at = Column(DateTime, default=_utcnow)


class EventLineup(Base):
    """Who staff picked for one slot of one event's team sheet."""

    __tablename__ = "event_lineups"
    __table_args__ = (UniqueConstraint("event_id", "slot_key", name="uq_lineup_event_slot"),)

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, nullable=False, index=True)
    slot_key = Column(String, nullable=False)
    discord_user_id = Column(BigInteger, nullable=False)
    display_name = Column(String, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class MatchRating(Base):
    """One player's ratings for one match: their own, out of ten, and the
    coach's. The coach's rating and comment are shown to that player and
    to staff, nobody else."""

    __tablename__ = "match_ratings"
    __table_args__ = (UniqueConstraint("event_id", "discord_id", name="uq_rating_event_player"),)

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, nullable=False, index=True)
    discord_id = Column(String, nullable=False, index=True)
    display_name = Column(String, nullable=False)
    self_rating = Column(Integer, nullable=True)
    coach_rating = Column(Integer, nullable=True)
    coach_comment = Column(Text, nullable=True)
    coach_name = Column(String, nullable=True)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class MotmVote(Base):
    """One player's Man of the Match vote. One per voter per match; voting
    again changes it."""

    __tablename__ = "motm_votes"
    __table_args__ = (UniqueConstraint("event_id", "voter_id", name="uq_motm_event_voter"),)

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, nullable=False, index=True)
    voter_id = Column(String, nullable=False)
    nominee_id = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class Notification(Base):
    """A message the bot has already sent, so the notifier sends each one
    once however often it runs. `key` says what it was about, e.g.
    "remind:12:4031" -- event 12's reminder to that player."""

    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("kind", "key", name="uq_notification_kind_key"),)

    id = Column(Integer, primary_key=True)
    kind = Column(String, nullable=False)
    key = Column(String, nullable=False)
    sent_at = Column(DateTime, default=_utcnow)
    ok = Column(Boolean, nullable=False, default=True)
    detail = Column(String, nullable=True)



class TrainingSuggestion(Base):
    """A player's suggestion for what training should work on next. Seen
    by staff and by the player who made it, nobody else."""

    __tablename__ = "training_suggestions"

    id = Column(Integer, primary_key=True)
    discord_id = Column(String, nullable=False, index=True)
    display_name = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    status = Column(String, nullable=False, default="open")   # see training.SUGGESTION_STATUSES
    staff_note = Column(String, nullable=True)
    handled_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)



# --- Recognition (see recognition.py) ------------------------------------------ #
class Milestone(Base):
    """A milestone a player has reached -- 25 appearances, first goal, five
    Man of the Match awards. Awarded once, announced once."""

    __tablename__ = "milestones"
    __table_args__ = (UniqueConstraint("discord_id", "key", name="uq_milestone_player_key"),)

    id = Column(Integer, primary_key=True)
    discord_id = Column(String, nullable=False, index=True)
    key = Column(String, nullable=False)
    label = Column(String, nullable=False)
    achieved_at = Column(DateTime, default=_utcnow)
    # Unset until the bot has posted it. Milestones found on the very first
    # run are recorded as already announced: history from before this
    # feature existed isn't news.
    announced_at = Column(DateTime, nullable=True)


class PlayerOfTheMonth(Base):
    """The month's award, from the squad's post-match votes. A tie names
    everyone level (comma-separated Discord IDs)."""

    __tablename__ = "player_of_the_month"

    month = Column(String, primary_key=True)          # "2026-10"
    discord_ids = Column(String, nullable=False)
    names = Column(String, nullable=False)
    votes = Column(Integer, nullable=False, default=0)
    awarded_at = Column(DateTime, default=_utcnow)
    announced_at = Column(DateTime, nullable=True)



# --- Development (see development.py) -------------------------------------------- #
class DevGoal(Base):
    """A goal a coach sets a player. The player updates progress and a
    note; staff close it. Seen by the player and staff only."""

    __tablename__ = "dev_goals"

    id = Column(Integer, primary_key=True)
    discord_id = Column(String, nullable=False, index=True)
    text = Column(String, nullable=False)
    area = Column(String, nullable=True)                 # see development.GOAL_AREAS
    due_on = Column(Date, nullable=True)
    status = Column(String, nullable=False, default="open")   # open | achieved | dropped
    progress = Column(Integer, nullable=False, default=0, server_default="0")
    player_note = Column(String, nullable=True)
    set_by_name = Column(String, nullable=False)
    set_by_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    closed_at = Column(DateTime, nullable=True)
    closed_by_name = Column(String, nullable=True)


class DevReview(Base):
    """A monthly one-to-one, written up. One per player per month."""

    __tablename__ = "dev_reviews"
    __table_args__ = (UniqueConstraint("discord_id", "month", name="uq_review_player_month"),)

    id = Column(Integer, primary_key=True)
    discord_id = Column(String, nullable=False, index=True)
    month = Column(String, nullable=False)              # "2026-10"
    summary = Column(Text, nullable=False)
    by_name = Column(String, nullable=False)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


# --- Staff tools: recruitment and set pieces ---------------------------------------- #
class Prospect(Base):
    """Somebody the club is looking at: Prospect -> Trial -> Offered ->
    Signed (or Not for us), then settling in. Staff only."""

    __tablename__ = "prospects"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    discord_id = Column(String, nullable=True, index=True)
    gamertag = Column(String, nullable=True)
    position = Column(String, nullable=True)
    secondary_position = Column(String, nullable=True)
    stage = Column(String, nullable=False, default="prospect")   # see recruitment.STAGES
    source = Column(String, nullable=True)
    added_by = Column(String, nullable=True)
    # When the club confirmed their signing; the settling-in checklist
    # (recruitment.ONBOARDING) runs from here.
    signed_at = Column(DateTime, nullable=True)
    # The checklist steps staff have ticked by hand, comma-separated keys.
    # The rest are worked out from the data (contract, gamertag, nights).
    onboarding = Column(String, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class ProspectNote(Base):
    """An assessment: from a trial match (event_id), or general."""

    __tablename__ = "prospect_notes"

    id = Column(Integer, primary_key=True)
    prospect_id = Column(Integer, nullable=False, index=True)
    event_id = Column(Integer, nullable=True)
    rating = Column(Integer, nullable=True)
    body = Column(Text, nullable=False)
    author = Column(String, nullable=False)
    created_at = Column(DateTime, default=_utcnow)
    # The copy posted to the recruitment channel (config.RECRUITMENT_CHANNEL_ID),
    # null when it wasn't posted.
    discord_message_id = Column(String, nullable=True)


class SetPiece(Base):
    """One routine in the set-piece book."""

    __tablename__ = "set_pieces"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    kind = Column(String, nullable=False)          # see setpieces.KINDS
    side = Column(String, nullable=True)
    taker_id = Column(String, nullable=True)
    taker_name = Column(String, nullable=True)
    routine = Column(Text, nullable=True)
    targets = Column(String, nullable=True)
    position = Column(Integer, nullable=False, default=0)
    updated_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
