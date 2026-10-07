"""Tests for weekly_article.py and the squad tracking it reads
(db.record_squad / db.squad_moves) and Squad Moves (services.public_roster_moves),
which the article's transfer news comes from. The Claude CLI is never actually run --
subprocess.run is faked.

Run with: pytest proclubs/tests/test_weekly_article.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="proclubs-weekly-article-")
os.environ["SITE_DB_PATH"] = os.path.join(_TMP, "site.db")

import pytest  # noqa: E402

import config  # noqa: E402
import database  # noqa: E402
import db  # noqa: E402
import discord_announce  # noqa: E402
import services  # noqa: E402
import weekly_article  # noqa: E402

PLATFORM, CLUB = "common-gen5", "c1"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "history.db")
    database.Base.metadata.drop_all(database.engine)
    database.init_db()
    monkeypatch.setattr(config, "WEEKLY_ARTICLE_ENABLED", True)
    monkeypatch.setattr(config, "CLUB_PLATFORM", PLATFORM)
    monkeypatch.setattr(config, "CLUB_ID", CLUB)
    monkeypatch.setattr(config, "WEEKLY_ARTICLE_AUTHOR", "YeeHaw FC Desk")
    monkeypatch.setattr(config, "NEWS_ANNOUNCE_ENABLED", False)


def _match(match_id, *, us=3, them=1, ts=None, players=None):
    ts = ts or int(time.time()) - 3600
    win = "1" if us > them else "0"
    loss = "1" if us < them else "0"
    return {
        "matchId": match_id,
        "timestamp": ts,
        "clubs": {
            CLUB: {"goals": str(us), "wins": win, "losses": loss, "winnerByDnf": "0", "date": str(ts)},
            "c2": {"goals": str(them), "details": {"name": "Rivals FC"}, "winnerByDnf": "0"},
        },
        "players": {CLUB: players or {
            "1": {"playername": "Striker9", "pos": "forward", "rating": "8.5", "goals": "2", "assists": "0", "mom": "1"},
            "2": {"playername": "Mid10", "pos": "midfielder", "rating": "7.0", "goals": "1", "assists": "2", "mom": "0"},
        }},
    }


def _fake_cli(monkeypatch, reply, *, returncode=0, is_error=False, calls=None):
    def run(cmd, input, **kwargs):
        if calls is not None:
            calls.append({"cmd": cmd, "input": input})
        out = json.dumps({"type": "result", "is_error": is_error, "result": reply})
        return subprocess.CompletedProcess(cmd, returncode, stdout=out, stderr="")
    monkeypatch.setattr(weekly_article.subprocess, "run", run)


GOOD_REPLY = json.dumps({"title": "Striker9 Fires YeeHaw to Victory", "summary": "A 3-1 win.",
                         "body_html": "<p>What a week.</p><script>alert(1)</script>"})


# --- Squad tracking ----------------------------------------------------------- #
def test_first_squad_record_is_a_baseline_not_signings():
    assert db.record_squad(PLATFORM, CLUB, ["A", "B"]) == ([], [])
    assert db.squad_moves(PLATFORM, CLUB, 0) == []


def test_squad_changes_are_logged_as_moves():
    db.record_squad(PLATFORM, CLUB, ["A", "B"])
    assert db.record_squad(PLATFORM, CLUB, ["B", "C"]) == (["C"], ["A"])
    moves = {(m["player_name"], m["move"]) for m in db.squad_moves(PLATFORM, CLUB, 0)}
    assert moves == {("C", "joined"), ("A", "left")}


def test_empty_member_list_is_ignored_not_a_mass_departure():
    db.record_squad(PLATFORM, CLUB, ["A", "B"])
    assert db.record_squad(PLATFORM, CLUB, []) == ([], [])
    db.record_squad(PLATFORM, CLUB, ["A", "B"])
    assert db.squad_moves(PLATFORM, CLUB, 0) == []


def test_player_who_leaves_and_returns_is_a_new_signing():
    db.record_squad(PLATFORM, CLUB, ["A", "B"])
    db.record_squad(PLATFORM, CLUB, ["B"])
    assert db.record_squad(PLATFORM, CLUB, ["A", "B"]) == (["A"], [])


def _squad_move(kind, name, *, position=None, response=None, confirmed=False, days_ago=0):
    """A Squad Moves row, as the staff page would leave it."""
    with database.get_session() as session:
        move = services.record_roster_move(
            session, discord_id=name, display_name=name, avatar_url=None, kind=kind,
            position=position, note=None, announced_by_name="Coach",
            announced_by_discord_id=1, discord_message_id="m",
        )
        if response:
            services.record_offer_response(session, move, response=response,
                                           role_granted=False, role_error=None)
        if confirmed:
            services.confirm_roster_move(session, move, confirmed_by_name="Coach",
                                         confirm_message_id="c")
        when = datetime.utcnow() - timedelta(days=days_ago)
        move.announced_at = when
        if confirmed:
            move.confirmed_at = when
        session.commit()


# --- Gathering facts ------------------------------------------------------------ #
def test_gather_period_covers_results_players_and_signings(monkeypatch):
    monkeypatch.setattr(config, "ROUNDUP_DAYS", 2)
    db.record_matches(PLATFORM, CLUB, "leagueMatch", [_match("m1"), _match("old", ts=int(time.time()) - 3 * 86400)])
    _squad_move("offer", "NewGuy", position="ST", response="accepted", confirmed=True)

    facts = weekly_article.gather_period(PLATFORM, CLUB, int(time.time()))

    assert [m["score"] for m in facts["matches"]] == ["3-1"]  # the 3-day-old match is excluded
    assert facts["period"]["days"] == 2
    assert facts["record"] == {"played": 1, "won": 1, "drawn": 0, "lost": 0, "goals_for": 3, "goals_against": 1}
    top = facts["players"][0]
    assert (top["player_name"], top["goals"], top["mom"]) == ("Striker9", 2, 1)
    assert [(m["name"], m["position"]) for m in facts["signings"]] == [("NewGuy", "ST")]
    assert weekly_article.has_news(facts)
    assert weekly_article.category_for(facts) == "Match Highlight"
    json.dumps(facts)  # must be serializable for the prompt


def test_transfer_news_comes_from_squad_moves_not_ea_member_diffs(monkeypatch):
    monkeypatch.setattr(config, "ROUNDUP_DAYS", 2)
    _squad_move("offer", "Signed", response="accepted", confirmed=True)
    _squad_move("staff_offer", "Coach2", position="Coach", response="accepted", confirmed=True)
    _squad_move("release", "Gone")
    _squad_move("release", "LongGone", days_ago=5)  # before the period
    # Not news yet / never news -- the same rules as the home page's band.
    _squad_move("offer", "Pending")
    _squad_move("offer", "Unconfirmed", response="accepted")
    _squad_move("offer", "Declined", response="declined")
    # EA's member list changing is no longer a source.
    db.record_squad(PLATFORM, CLUB, ["A"])
    db.record_squad(PLATFORM, CLUB, ["A", "EaOnly"])

    facts = weekly_article.gather_period(PLATFORM, CLUB, int(time.time()))

    assert [m["name"] for m in facts["signings"]] == ["Signed"]
    assert [m["name"] for m in facts["staff_appointments"]] == ["Coach2"]
    assert [m["name"] for m in facts["departures"]] == ["Gone"]


def test_dates_are_in_the_article_timezone_not_utc(monkeypatch):
    # Sat 2026-10-03 01:30 UTC is Fri 2026-10-02 21:30 EDT.
    friday_night = 1790991000
    monkeypatch.setattr(config, "ARTICLE_TIMEZONE", "America/New_York")
    assert weekly_article._iso(friday_night) == "Fri 02 Oct 2026"
    monkeypatch.setattr(config, "ARTICLE_TIMEZONE", "UTC")
    assert weekly_article._iso(friday_night) == "Sat 03 Oct 2026"


def test_squad_news_alone_is_a_transfer_article():
    _squad_move("release", "B")
    facts = weekly_article.gather_period(PLATFORM, CLUB, int(time.time()))
    assert weekly_article.has_news(facts)
    assert weekly_article.category_for(facts) == "Transfer"


# --- Parsing Claude's reply ------------------------------------------------------ #
def test_parse_article_tolerates_code_fences():
    parsed = weekly_article.parse_article("```json\n" + GOOD_REPLY + "\n```")
    assert parsed["title"] == "Striker9 Fires YeeHaw to Victory"


@pytest.mark.parametrize("reply", ["no json here", '{"title": "x"}', '{"title": "", "body_html": "<p>x</p>"}', "{bad json}"])
def test_parse_article_rejects_unusable_replies(reply):
    with pytest.raises(weekly_article.ArticleError):
        weekly_article.parse_article(reply)


def test_cli_runs_with_every_tool_disabled(monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_BIN", "/x/claude")
    cmd = weekly_article.build_command()
    assert cmd[0] == "/x/claude" and "-p" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""


# --- main() ---------------------------------------------------------------------- #
def test_main_publishes_sanitized_article(monkeypatch):
    db.record_matches(PLATFORM, CLUB, "leagueMatch", [_match("m1")])
    calls = []
    _fake_cli(monkeypatch, GOOD_REPLY, calls=calls)

    assert weekly_article.main([]) == 0

    assert "Striker9" in calls[0]["input"]
    with database.get_session() as session:
        [article] = services.list_articles(session)
        assert article.published
        assert article.author_name == "YeeHaw FC Desk"
        assert article.category == "Match Highlight"
        assert "<script>" not in article.body_html


def test_main_does_not_post_twice_in_one_period(monkeypatch):
    db.record_matches(PLATFORM, CLUB, "leagueMatch", [_match("m1")])
    _fake_cli(monkeypatch, GOOD_REPLY)
    weekly_article.main([])
    weekly_article.main([])
    with database.get_session() as session:
        assert len(services.list_articles(session)) == 1


@pytest.mark.parametrize("hours_ago, posts_again", [(24, False), (42, True), (49, True)])
def test_main_posts_again_once_the_period_is_up(monkeypatch, hours_ago, posts_again):
    # The timer fires daily: with ROUNDUP_DAYS=2 the day in between is
    # skipped, and a run that starts a little later than last time (a
    # systemd retry, a slow boot) still goes out.
    monkeypatch.setattr(config, "ROUNDUP_DAYS", 2)
    db.record_matches(PLATFORM, CLUB, "leagueMatch", [_match("m1")])
    _fake_cli(monkeypatch, GOOD_REPLY)
    weekly_article.main([])
    with database.get_session() as session:
        [article] = services.list_articles(session)
        article.published_at = datetime.utcnow() - timedelta(hours=hours_ago)
        session.commit()
    weekly_article.main([])
    with database.get_session() as session:
        assert len(services.list_articles(session)) == (2 if posts_again else 1)


def test_main_skips_a_quiet_period(monkeypatch, capsys):
    _fake_cli(monkeypatch, GOOD_REPLY, calls=(calls := []))
    assert weekly_article.main([]) == 0
    assert calls == []
    assert "skipping" in capsys.readouterr().out


def test_main_disabled_without_token(monkeypatch):
    monkeypatch.setattr(config, "WEEKLY_ARTICLE_ENABLED", False)
    _fake_cli(monkeypatch, GOOD_REPLY, calls=(calls := []))
    assert weekly_article.main([]) == 0
    assert calls == []


def test_main_fails_so_systemd_retries_when_claude_errors(monkeypatch):
    db.record_matches(PLATFORM, CLUB, "leagueMatch", [_match("m1")])
    _fake_cli(monkeypatch, "usage limit reached", returncode=1, is_error=True)
    assert weekly_article.main([]) == 1
    with database.get_session() as session:
        assert services.list_articles(session) == []


def test_main_dry_run_publishes_nothing(monkeypatch, capsys):
    db.record_matches(PLATFORM, CLUB, "leagueMatch", [_match("m1")])
    _fake_cli(monkeypatch, GOOD_REPLY)
    assert weekly_article.main(["--dry-run"]) == 0
    assert "Striker9 Fires" in capsys.readouterr().out
    with database.get_session() as session:
        assert services.list_articles(session) == []


def test_main_announces_to_discord(monkeypatch):
    db.record_matches(PLATFORM, CLUB, "leagueMatch", [_match("m1")])
    _fake_cli(monkeypatch, GOOD_REPLY)
    monkeypatch.setattr(config, "NEWS_ANNOUNCE_ENABLED", True)
    monkeypatch.setattr(config, "NEWS_ANNOUNCE_CHANNEL_ID", "555")
    monkeypatch.setattr(config, "SITE_BASE_URL", "https://example.test")
    sent = []
    monkeypatch.setattr(discord_announce, "announce", lambda channel_id, embed: sent.append(embed) or "42")

    assert weekly_article.main([]) == 0

    assert sent[0]["url"].startswith("https://example.test/news/")
    assert sent[0]["footer"] == {"text": "Posted by YeeHaw FC Desk"}
    with database.get_session() as session:
        assert services.list_articles(session)[0].discord_message_id == "42"
