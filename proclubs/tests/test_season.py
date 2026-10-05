"""season.py: finding the new season's club, erasing the old season's
stats, and pointing .env at the new one.

The rules worth pinning are the ones that protect irreplaceable data: the
erase only touches history.db, it never guesses between two clubs with the
same name, a failure to reach EA changes nothing, and .env keeps every
other line (secrets included) exactly as it was.

Run with: pytest proclubs/tests/test_season.py
"""
import os
import sqlite3
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402
import ea_client  # noqa: E402
import season  # noqa: E402


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """history.db, the archive dir and .env all under a temp dir."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "history.db")
    monkeypatch.setattr(season, "ARCHIVE_DIR", tmp_path / "backups")
    env = tmp_path / ".env"
    env.write_text(
        "# Our club\n"
        "CLUB_PLATFORM=common-gen5\n"
        "CLUB_ID=8481799\n"
        "\n"
        "# Secrets below must survive untouched\n"
        "DISCORD_BOT_TOKEN=abc.def.ghi\n"
        "SESSION_SECRET=s3cr3t\n"
    )
    env.chmod(0o600)
    monkeypatch.setattr(season, "ENV_PATH", env)
    monkeypatch.setattr(config, "CLUB_ID", "8481799")
    monkeypatch.setattr(config, "CLUB_PLATFORM", "common-gen5")
    monkeypatch.setattr(config, "CLUB_NAME", "Yeehaw FC")
    return tmp_path


def _seed_fc26_history():
    """A history.db that looks like a season's worth of FC 26 data."""
    db.record_snapshot("common-gen5", "8481799", {"wins": "40"}, {"currentDivision": "2"})
    db.record_matches("common-gen5", "8481799", "leagueMatch", [{
        "matchId": "m1", "timestamp": 1700000000,
        "clubs": {"8481799": {"goals": "3", "details": {"name": "YeeHaw FC"}},
                  "222": {"goals": "1", "details": {"name": "Rivals FC"}}},
        "players": {},
    }])


def _row_count(path, table):
    con = sqlite3.connect(path)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def _search_returns(monkeypatch, by_platform):
    def fake(platform, name):
        return by_platform.get(platform, [])
    monkeypatch.setattr(ea_client, "search_club", fake)


# --- Finding the club ------------------------------------------------------- #
def test_find_flags_the_exact_name_among_lookalikes(monkeypatch):
    """EA's search is a substring match; the club itself has to stand out
    from "Yeehaw FC Reserves" and friends."""
    _search_returns(monkeypatch, {"common-gen5": [
        {"clubId": "1", "name": "Yeehaw FC"},
        {"clubId": "2", "name": "Yeehaw FC Reserves"},
    ]})
    clubs = season.find_clubs("Yeehaw FC", ("common-gen5",))
    assert [(c["clubId"], c["exact"]) for c in clubs] == [("1", True), ("2", False)]


def test_exact_match_ignores_case_and_edge_spaces(monkeypatch):
    """The site brands itself "YeeHaw FC"; EA holds whatever was typed
    in-game. Casing mustn't make the club unfindable."""
    _search_returns(monkeypatch, {"common-gen5": [{"clubId": "1", "name": " YEEHAW fc "}]})
    assert season.find_clubs("Yeehaw FC", ("common-gen5",))[0]["exact"]


def test_a_unique_exact_match_is_resolved(monkeypatch):
    _search_returns(monkeypatch, {"common-gen5": [
        {"clubId": "9001", "name": "Yeehaw FC"},
        {"clubId": "2", "name": "Yeehaw FC Reserves"},
    ]})
    club = season.resolve_club("Yeehaw FC", None, None)
    assert (club["clubId"], club["platform"]) == ("9001", "common-gen5")


def test_two_clubs_with_the_same_name_are_never_guessed_between(monkeypatch):
    """Picking the wrong one would erase a season's stats and point the
    site at a stranger's club."""
    _search_returns(monkeypatch, {
        "common-gen5": [{"clubId": "9001", "name": "Yeehaw FC"}],
        "common-gen4": [{"clubId": "9002", "name": "Yeehaw FC"}],
    })
    with pytest.raises(season.SeasonError, match="--club-id"):
        season.resolve_club("Yeehaw FC", None, None)


def test_no_exact_match_explains_the_search_lag(monkeypatch):
    """A club created days ago may not be in EA's leaderboard search yet --
    the error has to say so, or it reads as 'your club doesn't exist'."""
    _search_returns(monkeypatch, {"common-gen5": [{"clubId": "2", "name": "Yeehaw FC Reserves"}]})
    with pytest.raises(season.SeasonError, match="lags behind"):
        season.resolve_club("Yeehaw FC", None, None)


def test_an_explicit_club_id_is_verified_against_ea(monkeypatch):
    monkeypatch.setattr(ea_client, "club_info", lambda p, c: {"name": "Yeehaw FC"})
    club = season.resolve_club("Yeehaw FC", "9001", "common-gen5")
    assert club == {"clubId": "9001", "name": "Yeehaw FC", "platform": "common-gen5"}


def test_an_explicit_club_id_ea_does_not_know_is_refused(monkeypatch):
    monkeypatch.setattr(ea_client, "club_info", lambda p, c: None)
    with pytest.raises(season.SeasonError, match="no club 9001"):
        season.resolve_club("Yeehaw FC", "9001", "common-gen5")


def test_ea_being_unreachable_is_an_error_not_an_empty_result(monkeypatch):
    """Otherwise an outage reads as 'no such club', and someone goes
    hunting for an ID that was never the problem."""
    def down(platform, name):
        raise ea_client.EAApiError("could not reach EA's API")
    monkeypatch.setattr(ea_client, "search_club", down)
    with pytest.raises(season.SeasonError, match="couldn't search EA"):
        season.find_clubs("Yeehaw FC")


# --- Erasing the old season ------------------------------------------------- #
def test_erase_leaves_an_empty_history_with_the_schema_in_place(sandbox):
    _seed_fc26_history()
    assert _row_count(db.DB_PATH, "matches") == 1
    season.erase_history(archive=True, label="8481799")
    for table in ("club_snapshots", "matches", "match_players", "league_clubs"):
        assert _row_count(db.DB_PATH, table) == 0, table


def test_erase_archives_the_old_stats_rather_than_destroying_them(sandbox):
    """History.db can't be re-fetched -- EA evicts old matches -- so the
    default keeps a copy, named for the club it belonged to."""
    _seed_fc26_history()
    archived = season.erase_history(archive=True, label="8481799")
    assert archived is not None and archived.exists()
    assert "8481799" in archived.name
    assert archived.parent == sandbox / "backups"
    assert _row_count(archived, "matches") == 1, "the archive must be the real data"


def test_no_archive_deletes_outright(sandbox):
    _seed_fc26_history()
    assert season.erase_history(archive=False) is None
    assert not (sandbox / "backups").exists()
    assert _row_count(db.DB_PATH, "matches") == 0


def test_erase_removes_wal_and_shm_sidecars(sandbox):
    """A leftover WAL would replay the old season's writes onto the new,
    empty database the next time SQLite opened it."""
    _seed_fc26_history()
    for suffix in ("-wal", "-shm"):
        (db.DB_PATH.parent / (db.DB_PATH.name + suffix)).write_text("stale")
    season.erase_history(archive=False)
    for suffix in ("-wal", "-shm"):
        assert not (db.DB_PATH.parent / (db.DB_PATH.name + suffix)).exists()


def test_erase_with_no_history_yet_is_fine(sandbox):
    assert season.erase_history(archive=True) is None
    assert db.DB_PATH.exists()


# --- Pointing .env at the new club ------------------------------------------ #
def test_env_values_are_replaced_and_everything_else_kept(sandbox):
    before = season.ENV_PATH.read_text()
    season.set_env_values({"CLUB_ID": "9001"})
    after = season.ENV_PATH.read_text()
    assert "CLUB_ID=9001\n" in after and "8481799" not in after
    # Every other line -- comments, blank lines, secrets -- byte for byte.
    assert after.replace("CLUB_ID=9001", "CLUB_ID=8481799") == before


def test_a_missing_key_is_appended(sandbox):
    season.set_env_values({"CLUB_NAME": "Yeehaw FC"})
    assert season.ENV_PATH.read_text().endswith("CLUB_NAME=Yeehaw FC\n")


def test_a_commented_out_key_is_not_mistaken_for_the_live_one(sandbox):
    season.ENV_PATH.write_text("# CLUB_ID=111\nCLUB_ID=222\n")
    season.set_env_values({"CLUB_ID": "9001"})
    assert season.ENV_PATH.read_text() == "# CLUB_ID=111\nCLUB_ID=9001\n"


def test_env_permissions_survive_the_rewrite(sandbox):
    """It holds the bot token. A rewrite that quietly made it world-readable
    would be a real leak."""
    season.set_env_values({"CLUB_ID": "9001"})
    assert stat.S_IMODE(season.ENV_PATH.stat().st_mode) == 0o600


def test_no_env_file_is_a_clear_error(sandbox):
    season.ENV_PATH.unlink()
    with pytest.raises(season.SeasonError, match=".env.example"):
        season.set_env_values({"CLUB_ID": "9001"})


# --- The whole switch ------------------------------------------------------- #
def test_switch_erases_fc26_stats_and_points_at_the_fc27_club(sandbox, monkeypatch, capsys):
    _seed_fc26_history()
    _search_returns(monkeypatch, {"common-gen5": [{"clubId": "9001", "name": "Yeehaw FC"}]})

    assert season.main(["switch", "--yes"]) == 0

    env = season.ENV_PATH.read_text()
    assert "CLUB_ID=9001\n" in env and "CLUB_PLATFORM=common-gen5\n" in env
    assert "DISCORD_BOT_TOKEN=abc.def.ghi" in env
    assert _row_count(db.DB_PATH, "matches") == 0
    out = capsys.readouterr().out
    assert "systemctl restart yeehaw-fc" in out
    assert "rm " in out, "the archive's delete command must be printed"


def test_a_failed_lookup_changes_nothing(sandbox, monkeypatch):
    """Resolve first, erase second: if EA can't be reached, the old
    season's stats and CLUB_ID must both still be there."""
    _seed_fc26_history()
    env_before = season.ENV_PATH.read_text()

    def down(platform, name):
        raise ea_client.EAApiError("could not reach EA's API")
    monkeypatch.setattr(ea_client, "search_club", down)

    assert season.main(["switch", "--yes"]) == 1
    assert season.ENV_PATH.read_text() == env_before
    assert _row_count(db.DB_PATH, "matches") == 1


def test_declining_the_prompt_changes_nothing(sandbox, monkeypatch):
    _seed_fc26_history()
    env_before = season.ENV_PATH.read_text()
    _search_returns(monkeypatch, {"common-gen5": [{"clubId": "9001", "name": "Yeehaw FC"}]})
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    assert season.main(["switch"]) == 1
    assert season.ENV_PATH.read_text() == env_before
    assert _row_count(db.DB_PATH, "matches") == 1


def test_switching_to_the_club_already_configured_erases_nothing(sandbox, monkeypatch):
    """Re-running the switch by mistake mustn't wipe the new season's stats."""
    _seed_fc26_history()
    monkeypatch.setattr(ea_client, "club_info", lambda p, c: {"name": "Yeehaw FC"})
    assert season.main(["switch", "--club-id", "8481799", "--yes"]) == 0
    assert _row_count(db.DB_PATH, "matches") == 1


def test_reset_erases_stats_but_leaves_club_id(sandbox):
    _seed_fc26_history()
    env_before = season.ENV_PATH.read_text()
    assert season.main(["reset", "--yes"]) == 0
    assert season.ENV_PATH.read_text() == env_before
    assert _row_count(db.DB_PATH, "matches") == 0


def test_the_site_database_is_never_touched(sandbox, monkeypatch):
    """News, events, squad moves, gamertags -- the club's own content, not
    the game's -- live in site.db, which this tool has no business with."""
    site_db = sandbox / "data" / "site.db"
    site_db.parent.mkdir(parents=True, exist_ok=True)
    site_db.write_bytes(b"club content")
    _seed_fc26_history()
    _search_returns(monkeypatch, {"common-gen5": [{"clubId": "9001", "name": "Yeehaw FC"}]})
    season.main(["switch", "--yes"])
    assert site_db.read_bytes() == b"club content"
