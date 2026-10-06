"""squad.py and db.squad_usage: the squad screen's rules.

What's pinned: every formation slot maps to a contract position (so no
formation silently drops a row from the depth chart), depth counts
naturals and cover correctly, each flag fires on exactly the evidence it
names, and playing time is measured against the club's recent matches
case-insensitively by gamertag.

Run with: pytest proclubs/tests/test_squad.py
"""
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import db  # noqa: E402
import discord_roster  # noqa: E402
import squad  # noqa: E402
from formations import FORMATIONS  # noqa: E402


def _c(name, position, secondary=None, status="Starter", cid=None, discord_id=None):
    """A stand-in Contract row -- squad.py only reads attributes."""
    cid = cid or abs(hash(name)) % 10_000
    return SimpleNamespace(id=cid, display_name=name, position=position,
                           secondary_position=secondary, squad_status=status,
                           discord_id=str(discord_id or cid), weeks=8)


# --- Formation slots -> contract positions ---------------------------------- #
def test_every_formation_slot_maps_to_a_contract_position():
    """A formation added with a new slot label must fail here, not quietly
    vanish from the depth chart."""
    for name, slots in FORMATIONS.items():
        for key, meta in slots.items():
            assert meta["label"] in squad.SLOT_POSITION, f"{name}: {key} ({meta['label']})"
    assert set(squad.SLOT_POSITION.values()) <= set(discord_roster.PITCH_POSITIONS)


def test_a_433_needs_what_you_would_expect():
    assert squad.formation_needs(FORMATIONS["4-3-3"]) == {
        "Goalkeeper": 1, "Centre Back": 2, "Full Back": 2,
        "Centre Midfield": 3, "Winger": 2, "Striker": 1,
    }


# --- Depth ------------------------------------------------------------------- #
def _depth_for(contracts, formation="4-3-3"):
    return {r["position"]: r for r in squad.squad_depth(FORMATIONS[formation], contracts)["rows"]}


def test_a_lone_goalkeeper_has_no_backup():
    depth = _depth_for([_c("Keeper", "Goalkeeper")])
    assert depth["Goalkeeper"]["state"] == squad.DEPTH_THIN
    assert depth["Goalkeeper"]["naturals"] == ["Keeper"]


def test_no_goalkeeper_at_all_is_a_gap():
    assert _depth_for([_c("Striker", "Striker")])["Goalkeeper"]["state"] == squad.DEPTH_GAP


def test_a_secondary_position_counts_as_cover():
    """The point of the secondary position: it fills the depth chart."""
    depth = _depth_for([
        _c("A", "Goalkeeper"), _c("B", "Centre Back", secondary="Goalkeeper"),
    ])
    gk = depth["Goalkeeper"]
    assert gk["naturals"] == ["A"] and gk["cover"] == ["B"]
    assert gk["state"] == squad.DEPTH_OK


def test_cover_alone_can_fill_a_position_but_is_listed_as_cover():
    depth = _depth_for([_c("B", "Centre Back", secondary="Striker")])
    assert depth["Striker"]["naturals"] == [] and depth["Striker"]["cover"] == ["B"]
    assert depth["Striker"]["state"] == squad.DEPTH_THIN


def test_two_centre_backs_for_two_slots_have_no_backup():
    depth = _depth_for([_c("A", "Centre Back"), _c("B", "Centre Back")])
    assert depth["Centre Back"]["state"] == squad.DEPTH_THIN
    depth = _depth_for([_c("A", "Centre Back"), _c("B", "Centre Back"), _c("C", "Centre Back")])
    assert depth["Centre Back"]["state"] == squad.DEPTH_OK


def test_any_outfield_covers_outfield_positions_but_never_goal():
    depth = _depth_for([_c("Utility", "Any Outfield")])
    assert depth["Striker"]["cover"] == ["Utility"]
    assert depth["Full Back"]["cover"] == ["Utility"]
    assert depth["Goalkeeper"]["cover"] == []


def test_a_player_is_not_counted_twice_at_one_position():
    """Primary and secondary the same would be refused at entry, but an
    old contract shouldn't double-count either."""
    depth = _depth_for([_c("A", "Striker", secondary="Striker")])
    assert depth["Striker"]["naturals"] == ["A"] and depth["Striker"]["cover"] == []


def test_contracted_positions_the_formation_does_not_use_are_reported():
    result = squad.squad_depth(FORMATIONS["4-3-3"], [
        _c("A", "Wing Back"), _c("B", "Wing Back"), _c("C", "Defensive Midfield"),
    ])
    assert result["unused"] == {"Wing Back": 2, "Defensive Midfield": 1}


def test_depth_counts_gaps_and_thin_positions():
    result = squad.squad_depth(FORMATIONS["4-3-3"], [_c("Keeper", "Goalkeeper")])
    # GK thin; the other five positions are gaps.
    assert (result["gaps"], result["thin"]) == (5, 1)


# --- Flags ------------------------------------------------------------------- #
def _flags(**kw):
    defaults = dict(status="Starter", linked=True, usage=None, window=10,
                    attendance=None, contract_state="active", time_left="in 5 weeks")
    defaults.update(kw)
    return [f["text"] for f in squad.player_flags(**defaults)]


def test_an_unlinked_player_is_flagged_and_nothing_is_guessed():
    flags = _flags(linked=False, usage=None)
    assert flags == ["No gamertag linked, so playing time and form can't be tracked."]


def test_a_starter_who_isnt_playing_is_flagged():
    flags = _flags(status="Starter", usage={"apps_window": 2, "form": 6.5})
    assert "Starter, but has played 2 of the last 10 matches." in flags


def test_a_starter_playing_most_games_is_not_flagged():
    assert _flags(status="Starter", usage={"apps_window": 5, "form": 6.5}) == []


def test_rotation_expects_less_than_a_starter():
    assert _flags(status="Rotation", usage={"apps_window": 2, "form": 6.5}) == []
    assert "Rotation, but has played 1 of the last 10 matches." in \
        _flags(status="Rotation", usage={"apps_window": 1, "form": 6.5})


def test_a_reserve_is_promised_nothing():
    assert _flags(status="Substitute", usage={"apps_window": 0, "form": None}) == []


def test_a_linked_player_with_no_appearances_counts_as_zero():
    """Linked but absent from every recorded match: that IS the signal."""
    assert "Starter, but has played 0 of the last 10 matches." in _flags(usage=None)


def test_playing_time_is_not_judged_on_too_few_matches():
    assert _flags(status="Starter", usage={"apps_window": 0, "form": None}, window=3) == []


def test_a_squad_player_in_form_is_suggested_for_promotion():
    flags = _flags(status="Substitute", usage={"apps_window": 3, "form": 7.8})
    assert "Substitute, averaging 7.8 — worth a promotion?" in flags


def test_a_starter_in_form_is_not_offered_a_promotion():
    assert _flags(status="Starter", usage={"apps_window": 8, "form": 8.2}) == []


def _left(days):
    """The phrase the real page passes in, from the real function."""
    import services
    return services.contract_time_left(
        SimpleNamespace(expires_at=datetime.utcnow() + timedelta(days=days, hours=1)))


def test_expiring_and_expired_contracts_are_flagged_louder_for_form():
    assert _flags(contract_state="expiring", time_left=_left(4),
                  usage={"apps_window": 8, "form": 6.9}) == ["Contract expiring — 4 days left."]
    assert _flags(contract_state="expiring", time_left=_left(4),
                  usage={"apps_window": 8, "form": 7.9}) == \
        ["Contract expiring — 4 days left. In form at 7.9."]
    assert _flags(contract_state="expired", usage={"apps_window": 8, "form": 6.0}) == \
        ["Contract has run out — renew or release."]


def test_poor_attendance_is_flagged_only_once_it_is_a_real_rate():
    assert "Attended 40% of marked events." in _flags(
        usage={"apps_window": 6}, attendance={"rate": 40})
    # rate None = too few marked events to say (see MIN_EVENTS_FOR_RELIABILITY).
    assert _flags(usage={"apps_window": 6}, attendance={"rate": None}) == []
    assert _flags(usage={"apps_window": 6}, attendance={"rate": 80}) == []


# --- Rows -------------------------------------------------------------------- #
def _usage(**players):
    return {"window": 10, "players": {
        name.casefold(): {"name": name, "apps_window": apps, "apps_total": apps,
                          "goals": 0, "assists": 0, "mom": 0, "form": form, "positions": {}}
        for name, (apps, form) in players.items()}}


def test_rows_join_contract_to_usage_through_the_gamertag_case_insensitively():
    c = _c("Cap", "Striker", discord_id=42)
    rows = squad.squad_rows(
        contracts=[c], links={42: "CAPTAIN_9"}, usage=_usage(Captain_9=(7, 7.1)),
        attendance={}, contract_states={}, time_left=lambda c: "in 3 weeks",
    )
    assert rows[0]["gamertag"] == "CAPTAIN_9"
    assert rows[0]["usage"]["apps_window"] == 7


def test_rows_sort_by_status_then_position_then_name():
    contracts = [
        _c("Zed", "Striker", status="Substitute", discord_id=1),
        _c("Amy", "Striker", status="Starter", discord_id=2),
        _c("Bob", "Goalkeeper", status="Starter", discord_id=3),
        _c("Cal", "Striker", status="Rotation", discord_id=4),
    ]
    rows = squad.squad_rows(contracts=contracts, links={}, usage=_usage(), attendance={},
                            contract_states={}, time_left=lambda c: "")
    assert [r["contract"].display_name for r in rows] == ["Bob", "Amy", "Cal", "Zed"]


def test_uncontracted_regulars_are_listed():
    c = _c("Cap", "Striker", discord_id=42)
    usage = _usage(Cap_GT=(8, 7.0), Trialist=(4, 6.8), Cameo=(1, None))
    regulars = squad.uncontracted_regulars(usage, {42: "Cap_GT"}, [c])
    assert [u["name"] for u in regulars] == ["Trialist"]


def test_a_contracted_but_unlinked_players_gamertag_shows_as_a_regular():
    """That's how staff find out which gamertag to link."""
    c = _c("Cap", "Striker", discord_id=42)
    regulars = squad.uncontracted_regulars(_usage(Cap_GT=(8, 7.0)), {}, [c])
    assert [u["name"] for u in regulars] == ["Cap_GT"]


def test_a_link_belonging_to_somebody_not_under_contract_does_not_hide_them():
    regulars = squad.uncontracted_regulars(_usage(Ex=(5, 7.0)), {99: "Ex"}, [])
    assert [u["name"] for u in regulars] == ["Ex"]


# --- db.squad_usage ---------------------------------------------------------- #
@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "history.db")


def _record(match_id, ts, players):
    """One league match for club c1, `players` = {gamertag: (rating, goals, pos)}."""
    db.record_matches("common-gen5", "c1", "leagueMatch", [{
        "matchId": match_id, "timestamp": ts,
        "clubs": {"c1": {"goals": "1", "details": {"name": "Us"}},
                  "c2": {"goals": "0", "details": {"name": "Them"}}},
        "players": {"c1": {
            str(i): {"playername": name, "rating": str(r), "goals": str(g), "assists": "0",
                     "mom": "0", "pos": pos}
            for i, (name, (r, g, pos)) in enumerate(players.items())
        }},
    }])


def test_usage_counts_appearances_in_the_recent_window(history):
    for i in range(12):
        players = {"Regular": (7.0, 0, "midfielder")}
        if i < 2:
            players["Oldtimer"] = (6.0, 1, "forward")   # only in the two oldest
        _record(f"m{i}", 1_700_000_000 + i * 1000, players)
    usage = db.squad_usage("common-gen5", "c1", window=10)
    assert usage["window"] == 10
    assert usage["players"]["regular"]["apps_window"] == 10
    assert usage["players"]["regular"]["apps_total"] == 12
    assert usage["players"]["oldtimer"]["apps_window"] == 0
    assert usage["players"]["oldtimer"]["goals"] == 2


def test_form_is_the_average_of_the_most_recent_ratings(history):
    ratings = [5.0, 5.0, 5.0, 8.0, 8.0, 8.0, 8.0, 8.0]   # oldest first
    for i, r in enumerate(ratings):
        _record(f"m{i}", 1_700_000_000 + i * 1000, {"Riser": (r, 0, "forward")})
    usage = db.squad_usage("common-gen5", "c1", window=10, form_games=5)
    assert usage["players"]["riser"]["form"] == 8.0


def test_form_needs_a_minimum_of_appearances(history):
    _record("m1", 1_700_000_000, {"New": (9.0, 0, "forward")})
    _record("m2", 1_700_001_000, {"New": (9.0, 0, "forward")})
    assert db.squad_usage("common-gen5", "c1", min_form_apps=3)["players"]["new"]["form"] is None


def test_usage_records_where_people_actually_played(history):
    _record("m1", 1_700_000_000, {"Flex": (7.0, 0, "defender")})
    _record("m2", 1_700_001_000, {"Flex": (7.0, 0, "midfielder")})
    _record("m3", 1_700_002_000, {"Flex": (7.0, 0, "midfielder")})
    assert db.squad_usage("common-gen5", "c1")["players"]["flex"]["positions"] == \
        {"defender": 1, "midfielder": 2}


def test_no_window_counts_every_recorded_match(history):
    for i in range(30):
        _record(f"m{i}", 1_700_000_000 + i * 1000, {"Regular": (7.0, 0, "midfielder")})
    usage = db.squad_usage("common-gen5", "c1", window=None)
    assert usage["window"] == 30
    assert usage["players"]["regular"]["apps_window"] == 30


def test_the_flag_says_which_span_it_counted():
    assert squad.apps_phrase(3, 25, season=True) == "played 3 of this season's 25"
    assert squad.apps_phrase(3, 10) == "played 3 of the last 10"


def test_a_short_season_reports_the_real_window(history):
    _record("m1", 1_700_000_000, {"A": (7.0, 0, "forward")})
    assert db.squad_usage("common-gen5", "c1", window=10)["window"] == 1


def test_no_history_is_empty_not_an_error(history):
    assert db.squad_usage("common-gen5", "c1") == {"window": 0, "players": {}}


# --- The public page's position cards -------------------------------------- #
def test_each_line_gets_a_card_with_its_open_shirts_hollow():
    from formations import FORMATIONS
    slots = FORMATIONS["4-3-3"]
    depth = squad.squad_depth(slots, [_c("Keeper", "Goalkeeper")])
    cards = {c["key"]: c for c in squad.line_cards(slots, depth)}
    assert list(cards) == ["goalkeeping", "defence", "midfield", "attack"]
    assert cards["goalkeeping"]["open"] == []
    assert cards["defence"]["open"] == ["Centre Back", "Full Back"]
    # Eleven dots on every card; only the line's own are lit.
    assert all(len(c["dots"]) == 11 for c in cards.values())
    assert sum(d["mine"] for d in cards["midfield"]["dots"]) == 3
    # The 4-3-3 has no wing backs, so defence doesn't recruit one.
    assert "Wing Back" not in cards["defence"]["open"]


def test_a_line_the_formation_doesnt_use_is_said_so():
    slots = {"GK": {"label": "GK", "top": 92, "left": 50}}
    cards = {c["key"]: c for c in squad.line_cards(slots, squad.squad_depth(slots, []))}
    assert cards["goalkeeping"]["used"] and cards["goalkeeping"]["open"] == ["Goalkeeper"]
    assert not cards["attack"]["used"] and cards["attack"]["band"] is None


# --- The season, completed from EA's own counts ----------------------------------- #
def _snapshot(wins, ties, losses):
    db.record_snapshot("common-gen5", "c1", {"wins": str(wins), "ties": str(ties), "losses": str(losses)}, None)


def test_eas_match_total_is_wins_draws_and_losses(history):
    assert db.ea_match_total("common-gen5", "c1") is None
    _snapshot(60, 14, 20)
    assert db.ea_match_total("common-gen5", "c1") == 94


def test_member_totals_keep_the_latest_and_survive_a_departure(history):
    db.record_member_totals("common-gen5", "c1", [{"name": "A", "gamesPlayed": "10"}, {"name": "B", "gamesPlayed": "4"}])
    db.record_member_totals("common-gen5", "c1", [{"name": "A", "gamesPlayed": "12"}])
    totals = db.member_totals("common-gen5", "c1")
    assert totals["a"]["games_played"] == 12 and totals["b"]["games_played"] == 4


def test_a_short_history_is_completed_from_ea(history):
    for i in range(64):
        _record(f"m{i}", 1_700_000_000 + i * 1000, {"Cap_GT": (7.0, 1, "forward")})
    _snapshot(60, 14, 20)
    db.record_member_totals("common-gen5", "c1", [
        {"name": "Cap_GT", "gamesPlayed": "80", "goals": "90", "assists": "5", "manOfTheMatch": "3"},
        {"name": "Early_GT", "gamesPlayed": "12", "goals": "2"}])      # only played before tracking
    usage = squad.with_ea_totals(
        {**db.squad_usage("common-gen5", "c1", window=None), "recorded": 64},
        db.member_totals("common-gen5", "c1"), db.ea_match_total("common-gen5", "c1"))
    assert (usage["window"], usage["recorded"], usage["from_ea"]) == (94, 64, True)
    cap = usage["players"]["cap_gt"]
    assert (cap["apps_window"], cap["apps_recorded"], cap["goals"]) == (80, 64, 90)
    assert cap["form"] == 7.0                                          # still from recorded matches
    assert usage["players"]["early_gt"]["apps_window"] == 12


def test_a_complete_history_is_left_alone(history):
    for i in range(10):
        _record(f"m{i}", 1_700_000_000 + i * 1000, {"Cap_GT": (7.0, 1, "forward")})
    _snapshot(7, 1, 2)
    db.record_member_totals("common-gen5", "c1", [{"name": "Cap_GT", "gamesPlayed": "10"}])
    usage = squad.with_ea_totals({**db.squad_usage("common-gen5", "c1", window=None), "recorded": 10},
                                 db.member_totals("common-gen5", "c1"), 10)
    assert usage["from_ea"] is False and usage["window"] == 10
