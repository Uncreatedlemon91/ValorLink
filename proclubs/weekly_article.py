"""One-shot job: have Claude write up the past week and publish it.

Run every Saturday via systemd (see deploy/proclubs-weekly-article.service
+ .timer). Gathers the week's facts from data/history.db -- results, player
totals, division/points movement, league position, signings and departures
(see db.record_squad) -- hands them to the Claude Code CLI in headless mode,
and publishes what comes back as a live article, announced to Discord the
same way a staff-written one is.

The CLI rather than the API SDK so the writing is billed to a Claude
subscription (CLAUDE_CODE_OAUTH_TOKEN, see config.py), not API credits.
It's run with every tool disabled: this is a pure "facts in, article out"
call, and the facts include EA-controlled strings (player and club names)
that have no business steering anything. The output is sanitized like any
staff article (services.create_article -> html_sanitize).

Exits non-zero when Claude can't be reached or returns something unusable,
so systemd's Restart=on-failure retries later -- e.g. after a subscription
usage limit resets. A week with nothing to report is a clean skip, not a
failure.

Run it manually to test: python weekly_article.py [--dry-run]
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

import config
import db
import discord_announce
import services
from database import get_session, init_db
from models import Article

WEEK_SECONDS = 7 * 24 * 3600
CLAUDE_TIMEOUT_SECONDS = 600
# A timer that fires late (Persistent=true after downtime) or a manual
# re-run shouldn't publish a second roundup for the same week.
REPOST_GUARD = timedelta(days=6)

SYSTEM_PROMPT = """You are the staff writer for {site}, an EA Sports FC Pro Clubs team. \
You write the club's weekly roundup for its website: energetic, broadcast-style \
football journalism, written for the squad and its fans.

Rules:
- Use ONLY the facts in the JSON you are given. Never invent scores, scorers, \
quotes, opponents, fixtures or events. If a number isn't there, don't state one.
- Player and club names are gamertags; reproduce them exactly.
- Cover, where the data has them: the results and the story of the week, \
standout players, league/division standing and how it moved, and squad news \
(signings and departures). Skip any section the data has nothing for.
- 300-600 words.

Reply with a single JSON object and nothing else -- no code fences, no commentary:
{{"title": "...", "summary": "one-sentence dek, under 160 characters", \
"body_html": "the article as HTML"}}
body_html may use only <p>, <h2>, <h3>, <strong>, <em>, <ul>, <ol>, <li>, \
<blockquote>, and <table>/<thead>/<tbody>/<tr>/<th>/<td>. Don't repeat the title in it."""


class ArticleError(Exception):
    pass


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%a %d %b %Y") if ts else None


def gather_week(platform: str, club_id: str, now: int) -> dict:
    """Everything the article may say, as plain JSON-able data."""
    since = now - WEEK_SECONDS
    matches = [m for m in db.match_history(platform, club_id) if (m["played_at"] or 0) >= since]
    counted = [m for m in matches if not m["forfeit"]]

    latest = db.latest_snapshot(platform, club_id)
    week_start = db.snapshot_at(platform, club_id, since)
    standing = None
    if latest:
        standing = {"division": latest["division"], "points": db._num(latest["points"]),
                    "skill_rating": db._num(latest["skill_rating"])}
        if week_start:
            standing["division_a_week_ago"] = week_start["division"]
            standing["points_a_week_ago"] = db._num(week_start["points"])

    table = db.league_table(platform, club_id)
    league_position = next(
        ({"position": i + 1, "of": len(table)} for i, row in enumerate(table) if row["is_us"]), None,
    )

    moves = db.squad_moves(platform, club_id, since)
    return {
        "club": config.SITE_NAME,
        "week": {"from": _iso(since), "to": _iso(now)},
        "record": {
            "played": len(counted),
            "won": sum(m["outcome"] == "W" for m in counted),
            "drawn": sum(m["outcome"] == "D" for m in counted),
            "lost": sum(m["outcome"] == "L" for m in counted),
            "goals_for": sum(m["us_score"] or 0 for m in counted),
            "goals_against": sum(m["opp_score"] or 0 for m in counted),
        },
        "matches": [
            {"date": _iso(m["played_at"]), "competition": "playoff" if m["match_type"] == "playoffMatch" else "league",
             "opponent": m["opp_name"], "score": f"{m['us_score']}-{m['opp_score']}", "result": m["outcome"],
             "forfeit": bool(m["forfeit"])}
            for m in matches
        ],
        "players": db.player_totals(platform, club_id, since),
        "standing": standing,
        "league_table_position": league_position,
        "form_last_5": db.recent_form(platform, club_id),
        "signings": [m["player_name"] for m in moves if m["move"] == "joined"],
        "departures": [m["player_name"] for m in moves if m["move"] == "left"],
    }


def has_news(facts: dict) -> bool:
    return bool(facts["matches"] or facts["signings"] or facts["departures"])


def category_for(facts: dict) -> str:
    return "Match Highlight" if facts["matches"] else "Transfer"


def build_command() -> list[str]:
    cmd = [
        config.CLAUDE_BIN, "-p",
        "--output-format", "json",
        "--tools", "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--system-prompt", SYSTEM_PROMPT.format(site=config.SITE_NAME),
    ]
    if config.CLAUDE_MODEL:
        cmd += ["--model", config.CLAUDE_MODEL]
    return cmd


def ask_claude(facts: dict) -> dict:
    """Runs the CLI with the facts on stdin and returns the parsed
    {title, summary, body_html}. Raises ArticleError on any failure."""
    prompt = "Write this week's roundup from these facts:\n\n" + json.dumps(facts, indent=2)
    try:
        # A scratch cwd so the CLI never picks up this repo's CLAUDE.md or settings.
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(
                build_command(), input=prompt, capture_output=True, text=True,
                timeout=CLAUDE_TIMEOUT_SECONDS, cwd=cwd,
            )
    except FileNotFoundError:
        raise ArticleError(f"Claude CLI not found at {config.CLAUDE_BIN!r} -- set CLAUDE_BIN")
    except subprocess.TimeoutExpired:
        raise ArticleError("Claude CLI timed out")

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise ArticleError(f"Claude CLI failed (exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()[:500]}")
    if proc.returncode != 0 or envelope.get("is_error"):
        raise ArticleError(f"Claude CLI reported an error: {str(envelope.get('result'))[:500]}")
    return parse_article(envelope.get("result") or "")


def parse_article(text: str) -> dict:
    # Tolerate a stray code fence or preamble around the object.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ArticleError("Claude's reply had no JSON object in it")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ArticleError(f"Claude's reply wasn't valid JSON: {exc}")
    if not all(isinstance(data.get(k), str) and data[k].strip() for k in ("title", "body_html")):
        raise ArticleError("Claude's reply is missing a title or body")
    return {"title": data["title"].strip()[:200], "summary": str(data.get("summary") or "").strip()[:300],
            "body_html": data["body_html"]}


def already_posted_this_week(session) -> bool:
    cutoff = datetime.utcnow() - REPOST_GUARD
    return session.scalar(
        select(Article.id).where(
            Article.author_name == config.WEEKLY_ARTICLE_AUTHOR, Article.published_at >= cutoff,
        ).limit(1)
    ) is not None


def announce(session, article) -> None:
    """Best-effort, like app.py's _announce_article: the article is already
    live, so a Discord failure is logged, not fatal (and not retried --
    a retry would re-publish nothing, only re-fail)."""
    if not config.NEWS_ANNOUNCE_ENABLED:
        return
    if not config.SITE_BASE_URL:
        print("SITE_BASE_URL isn't configured -- skipped the Discord announcement")
        return
    try:
        article.discord_message_id = discord_announce.announce_article(
            article, channel_id=config.NEWS_ANNOUNCE_CHANNEL_ID, base_url=config.SITE_BASE_URL,
        )
    except discord_announce.DiscordApiError as exc:
        print(f"published, but the Discord announcement failed: {exc}")
        return
    session.commit()


def main(argv: list[str] | None = None) -> int:
    dry_run = "--dry-run" in (argv if argv is not None else sys.argv[1:])
    if not config.WEEKLY_ARTICLE_ENABLED:
        print("CLAUDE_CODE_OAUTH_TOKEN not set -- weekly article disabled")
        return 0
    if not config.CLUB_ID:
        print("CLUB_ID not set -- nothing to write about")
        return 0

    init_db()
    with get_session() as session:
        if not dry_run and already_posted_this_week(session):
            print("this week's article is already up -- skipping")
            return 0

    facts = gather_week(config.CLUB_PLATFORM, str(config.CLUB_ID), int(time.time()))
    if not has_news(facts):
        print("no matches or squad changes this week -- skipping")
        return 0

    try:
        written = ask_claude(facts)
    except ArticleError as exc:
        print(f"could not write the article: {exc}")
        return 1

    if dry_run:
        print(json.dumps(written, indent=2))
        return 0

    with get_session() as session:
        try:
            article = services.create_article(
                session, title=written["title"], summary=written["summary"], body_html=written["body_html"],
                cover_image=None, published=True, category=category_for(facts),
                author={"id": None, "name": config.WEEKLY_ARTICLE_AUTHOR, "avatar": None},
            )
        except services.ServiceError as exc:
            print(f"Claude's article was rejected: {exc}")
            return 1
        print(f"published /news/{article.slug}")
        announce(session, article)
    return 0


if __name__ == "__main__":
    sys.exit(main())
