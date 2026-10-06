"""The match-week loop: availability, preferences, the team sheet, the
post-match vote, ratings, the report, and the bot's scheduled messages.

Run with: pytest proclubs/tests/test_matchweek.py
"""
import os
import re
import sys
import zlib
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import config  # noqa: E402
import database  # noqa: E402
import discord_notify  # noqa: E402
import matchweek as mw  # noqa: E402
import matchweek_routes  # noqa: E402
import notify_poll  # noqa: E402
import services  # noqa: E402
from models import Event  # noqa: E402


@pytest.fixture
def client():
    database.Base.metadata.drop_all(database.engine)
    with TestClient(appmod.app) as c:
        yield c


@pytest.fixture
def bot(monkeypatch):
    """Discord, recorded: channel posts and DMs."""
    sent = {"posts": [], "dms": []}
    monkeypatch.setattr(config, "NOTIFY_ENABLED", True)
    monkeypatch.setattr(config, "NOTIFY_DMS", True)
    monkeypatch.setattr(config, "MATCHDAY_CHANNEL_ID", "777")

    def post(channel_id, **kw):
        sent["posts"].append((channel_id, kw))
        return f"msg-{len(sent['posts'])}"

    monkeypatch.setattr(discord_notify, "post", post)
    monkeypatch.setattr(discord_notify, "send_dm", lambda uid, content, embeds=None: sent["dms"].append((str(uid), content)))
    return sent


def _id(name):
    return str(zlib.crc32(name.encode()))


def _login(client, name, *, staff=False):
    data = {"name": name, "member": "1"}
    if staff:
        data["staff"] = "1"
    client.post("/auth/dev", data=data, follow_redirects=False)
    return _id(name)


def _csrf(client, path):
    m = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert m, path
    return m.group(1)


def _event(*, hours=48, formation="4-3-3", event_type="Match", title="League Night") -> int:
    with database.get_session() as session:
        e = Event(title=title, event_type=event_type, opponent="Rivals FC", formation=formation,
                  scheduled_at=datetime.utcnow() + timedelta(hours=hours))
        session.add(e)
        session.commit()
        return e.id


def _contract(discord_id, name, position="Striker", status="Starter"):
    with database.get_session() as session:
        services.create_contract(session, discord_id=discord_id, display_name=name, avatar_url=None,
                                 position=position, squad_status=status, weeks=8, source="recorded",
                                 created_by_name="Boss")


def _signup(event_id, uid, name, status="going", slot=None):
    with database.get_session() as session:
        event = services.get_event(session, event_id)
        if slot:
            services.claim_slot(session, event, discord_user_id=int(uid), discord_name=name,
                                discord_avatar=None, slot_key=slot, source="site")
        else:
            services.set_signup(session, event, discord_user_id=int(uid), discord_name=name,
                                discord_avatar=None, status=status, source="site")


# --- Availability ---------------------------------------------------------------- #
def test_a_pattern_and_away_dates_decide_each_night(client):
    monday = date(2027, 3, 1)
    with database.get_session() as session:
        pattern = mw.set_pattern(session, "1", [0, 2])   # Mon, Wed
        away = mw.add_away(session, "1", date(2027, 3, 3), date(2027, 3, 3), today=monday)
        assert pattern.days == "1010000"
        assert mw.day_state(pattern, [away], monday) == "free"
        assert mw.day_state(pattern, [away], monday + timedelta(days=1)) == "busy"
        assert mw.day_state(pattern, [away], monday + timedelta(days=2)) == "away"
        assert mw.day_state(None, [], monday) == "unknown"


def test_away_dates_are_checked(client):
    today = date(2027, 3, 1)
    with database.get_session() as session:
        with pytest.raises(services.ServiceError, match="before the first"):
            mw.add_away(session, "1", date(2027, 3, 5), date(2027, 3, 4), today=today)
        with pytest.raises(services.ServiceError, match="already passed"):
            mw.add_away(session, "1", date(2027, 2, 1), date(2027, 2, 2), today=today)
        away = mw.add_away(session, "1", date(2027, 3, 5), date(2027, 3, 6), today=today)
        with pytest.raises(services.ServiceError, match="isn't yours"):
            mw.delete_away(session, away.id, "2")


def test_a_fixture_answer_overrides_the_pattern_on_its_night(client):
    _contract("10", "Ann")
    _contract("11", "Ben")
    eid = _event(hours=30)
    _signup(eid, "10", "Ann", "out")
    _signup(eid, "11", "Ben", "going")
    day = datetime.utcnow().date()
    with database.get_session() as session:
        for uid in ("10", "11"):
            mw.set_pattern(session, uid, list(range(7)))
        grid = mw.availability_grid(session, mw.squad_people(session), day)
        event_day = services.get_event(session, eid).scheduled_at.date()
    i = grid["days"].index(event_day)
    states = {r["name"]: r["cells"][i]["state"] for r in grid["rows"]}
    assert states == {"Ann": "out", "Ben": "going"}
    assert grid["counts"][i] == 1


def test_players_set_their_nights_on_the_site(client):
    uid = _login(client, "Self")
    token = _csrf(client, "/availability")
    r = client.post("/availability/pattern", data={"day": ["1", "3"], "note": "late kick-offs",
                                                   "csrf_token": token})
    assert r.status_code == 200 and "Your usual nights are saved." in r.text
    with database.get_session() as session:
        assert mw.get_pattern(session, uid).days == "0101000"


# --- Preferences ----------------------------------------------------------------- #
def test_preferences_need_a_sign_up_and_distinct_positions(client):
    eid = _event()
    with database.get_session() as session:
        event = services.get_event(session, eid)
        with pytest.raises(services.ServiceError, match="Sign up first"):
            mw.set_preferences(session, event, 1, ["Striker"])
    _signup(eid, "1", "Ann")
    with database.get_session() as session:
        event = services.get_event(session, eid)
        with pytest.raises(services.ServiceError, match="different position"):
            mw.set_preferences(session, event, 1, ["Striker", "Striker"])
        with pytest.raises(services.ServiceError, match="from the list"):
            mw.set_preferences(session, event, 1, ["Sweeper"])
        signup = mw.set_preferences(session, event, 1, ["Winger", "", "Striker"])
        assert mw.preferences(signup) == ["Winger", "Striker"]


# --- The team sheet ---------------------------------------------------------------- #
def test_the_draft_starts_from_claimed_shirts(client):
    eid = _event()
    _signup(eid, "1", "Ann", slot="ST")
    with database.get_session() as session:
        assert mw.draft_lineup(session, services.get_event(session, eid)) == {"ST": "1"}


def test_nobody_is_picked_twice(client):
    eid = _event()
    with database.get_session() as session:
        event = services.get_event(session, eid)
        with pytest.raises(services.ServiceError, match="picked twice"):
            mw.save_lineup(session, event, {"ST": "1", "GK": "1"}, {"1": "Ann"})
        with pytest.raises(services.ServiceError, match="isn't a position"):
            mw.save_lineup(session, event, {"XX": "1"}, {})
        saved = mw.save_lineup(session, event, {"ST": "1", "GK": "2", "CM1": ""}, {"1": "Ann", "2": "Kai"})
        assert set(saved) == {"ST", "GK"}


def test_publishing_posts_the_sheet_and_dms_each_shirt(client, bot):
    _contract("1", "Ann")
    _contract("2", "Kai", position="Goalkeeper")
    eid = _event()
    _signup(eid, "1", "Ann")
    _login(client, "Coach", staff=True)
    token = _csrf(client, f"/events/{eid}/teamsheet")
    r = client.post(f"/events/{eid}/teamsheet", data={
        "slot__ST": "1", "slot__SUB1": "2", "action": "publish", "csrf_token": token})
    assert r.status_code == 200 and "Team sheet published and sent to Discord." in r.text
    (channel, kw), = bot["posts"]
    assert channel == "777" and set(kw["mention_ids"]) == {"1", "2"}
    fields = {f["name"]: f["value"] for f in kw["embeds"][0]["fields"]}
    assert "<@1>" in fields["Starting XI"] and "<@2>" in fields["Bench"]
    dms = dict(bot["dms"])
    assert "starting at **ST**" in dms["1"] and "on the bench" in dms["2"]


def test_players_see_their_shirt_once_published(client, bot):
    uid = _login(client, "Ann")
    eid = _event()
    with database.get_session() as session:
        event = services.get_event(session, eid)
        mw.save_lineup(session, event, {"LW": uid}, {uid: "Ann"})
    assert "You're starting at" not in client.get(f"/events/{eid}").text
    with database.get_session() as session:
        mw.mark_lineup_published(session, services.get_event(session, eid), None)
    assert "You're starting at LW." in client.get(f"/events/{eid}").text


def test_members_cannot_pick_the_team(client):
    eid = _event()
    _login(client, "Fan")
    assert client.get(f"/events/{eid}/teamsheet").status_code == 403


# --- The vote, ratings and report ------------------------------------------------------ #
def _played_match(*players):
    """A match two and a half hours ago with these (id, name) as the
    published sheet, and its vote open."""
    eid = _event(hours=-2.5)
    with database.get_session() as session:
        event = services.get_event(session, eid)
        slots = ["ST", "LW", "RW", "GK"]
        mw.save_lineup(session, event, {slots[i]: uid for i, (uid, _) in enumerate(players)},
                       dict(players))
        mw.mark_lineup_published(session, event, None)
        mw.open_vote(session, event)
    return eid


def test_votes_are_for_teammates_who_played(client):
    eid = _played_match(("1", "Ann"), ("2", "Ben"), ("3", "Cal"))
    with database.get_session() as session:
        event = services.get_event(session, eid)
        with pytest.raises(services.ServiceError, match="yourself"):
            mw.cast_vote(session, event, "1", "1")
        with pytest.raises(services.ServiceError, match="in the squad"):
            mw.cast_vote(session, event, "9", "1")
        mw.cast_vote(session, event, "1", "2")
        mw.cast_vote(session, event, "3", "1")
        mw.cast_vote(session, event, "1", "3")       # a changed mind
        assert mw.motm_result(session, eid) == {"votes": {"1": 1, "3": 1}, "total": 2,
                                                "winners": ["1", "3"]}


def test_the_vote_isnt_open_before_full_time(client):
    eid = _event(hours=1)
    with database.get_session() as session:
        event = services.get_event(session, eid)
        assert mw.vote_state(event) == "not_yet"
        with pytest.raises(services.ServiceError, match="after kick-off"):
            mw.open_vote(session, event)


def test_publishing_the_report_closes_the_vote_and_posts_it(client, bot):
    eid = _played_match(("1", "Ann"), ("2", "Ben"))
    with database.get_session() as session:
        mw.cast_vote(session, services.get_event(session, eid), "1", "2")
    _login(client, "Coach", staff=True)
    token = _csrf(client, f"/events/{eid}/report")
    r = client.post(f"/events/{eid}/report", data={
        "notes": "Pressed high and it worked.",
        "clips": "https://example.com/goal", "rating__2": "8", "comment__2": "Ran the midfield.",
        "action": "publish", "csrf_token": token})
    assert "Report published and posted to Discord." in r.text
    with database.get_session() as session:
        event = services.get_event(session, eid)
        assert mw.vote_state(event) == "closed"
    embed = bot["posts"][-1][1]["embeds"][0]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Man of the Match"] == "Ben (1 vote)"
    assert "Pressed high and it worked." in embed["description"]


def test_a_coach_rating_is_seen_by_its_player_and_nobody_else(client, bot):
    ann = _id("Ann")
    eid = _played_match((ann, "Ann"), ("2", "Ben"))
    with database.get_session() as session:
        event = services.get_event(session, eid)
        mw.set_coach_ratings(session, event, {ann: ("7", "Good shape off the ball.")}, {ann: "Ann"}, "Gaffer")
        services.ensure_player(session, discord_id=ann, display_name="Ann")
    _login(client, "Ann")
    assert "Good shape off the ball." in client.get(f"/events/{eid}/report").text
    assert "Good shape off the ball." in client.get(f"/players/{ann}").text
    _login(client, "Ben")
    assert "Good shape off the ball." not in client.get(f"/events/{eid}/report").text
    assert "Good shape off the ball." not in client.get(f"/players/{ann}").text


def test_players_vote_and_rate_from_the_site(client):
    ann = _id("Ann")
    eid = _played_match((ann, "Ann"), ("2", "Ben"))
    _login(client, "Ann")
    token = _csrf(client, f"/events/{eid}/report")
    client.post(f"/events/{eid}/report/mine", data={"nominee_id": "2", "self_rating": "6",
                                                     "csrf_token": token})
    with database.get_session() as session:
        assert mw.my_vote(session, eid, ann) == "2"
        assert mw.ratings_for(session, eid)[ann].self_rating == 6


def test_the_discord_pickers_vote_and_rate_privately(client):
    eid = _played_match(("1", "Ann"), ("2", "Ben"))
    ann = {"id": 1, "name": "Ann", "avatar": None}
    r = matchweek_routes.handle_picker(f"motm:{eid}", ["2"], ann)
    assert r["data"]["flags"] == 64 and "Vote counted: **Ben**" in r["data"]["content"]
    r = matchweek_routes.handle_picker(f"selfrate:{eid}", ["7"], ann)
    assert "7/10" in r["data"]["content"]
    r = matchweek_routes.handle_picker(f"motm:{eid}", ["2"], {"id": 9, "name": "Nope", "avatar": None})
    assert "Only players who were in the squad" in r["data"]["content"]


def test_ea_matches_from_the_night_are_found():
    event = Event(title="x", event_type="Match", scheduled_at=datetime(2027, 3, 1, 20, 0))
    stamp = lambda h, m=0: int(datetime(2027, 3, 1, h, m).timestamp() - datetime(1970, 1, 1).timestamp())
    history = [{"played_at": stamp(18), "us_score": 9, "opp_score": 9},
               {"played_at": stamp(20, 25), "us_score": 2, "opp_score": 0},
               {"played_at": stamp(21, 10), "us_score": 1, "opp_score": 1}]
    near = mw.ea_matches_near(event, history)
    assert [(m["us_score"], m["opp_score"]) for m in near] == [(2, 0), (1, 1)]
    assert mw.result_text(2, 0) == "W 2-0" and mw.result_text(1, 1) == "D 1-1"


def test_the_night_is_a_record_and_a_goal_difference():
    assert mw.night_summary([]) is None
    night = mw.night_summary([{"us_score": 2, "opp_score": 0}, {"us_score": 1, "opp_score": 1},
                              {"us_score": 0, "opp_score": 3}, {"us_score": None, "opp_score": None}])
    assert (night["wins"], night["draws"], night["losses"]) == (1, 1, 1)
    assert night["gd"] == -1 and night["gd_text"] == "\u22121" and night["record"] == "1W 1D 1L"
    assert [g["letter"] for g in night["games"]] == ["W", "D", "L"]
    assert mw.night_summary([{"us_score": 4, "opp_score": 1}])["gd_text"] == "+3"


def test_the_report_adds_up_the_night_and_asks_for_no_score(client, bot, monkeypatch):
    eid = _played_match(("1", "Ann"))
    played = datetime.utcnow()
    near = [{"match_id": "a", "played": played, "us_score": 3, "opp_score": 0, "opp_name": "Rovers"},
            {"match_id": "b", "played": played, "us_score": 1, "opp_score": 2, "opp_name": "United"}]
    monkeypatch.setattr(matchweek_routes, "_ea_night", lambda event: (near, []))
    _login(client, "Coach", staff=True)
    html = client.get(f"/events/{eid}/report").text
    assert "Goal difference" in html and "+2" in html and "1W 1L" in html
    assert "vs Rovers" in html and "vs United" in html
    assert re.search(r'report-figure">4<span class="report-dash">:</span>2<', html)
    assert 'name="us_score"' not in html and "name=\"opp_score\"" not in html


def test_the_discord_report_carries_the_night():
    event = Event(id=1, title="League Night", event_type="Match", scheduled_at=datetime(2027, 3, 1, 20, 0))
    night = mw.night_summary([{"us_score": 2, "opp_score": 0}, {"us_score": 0, "opp_score": 1}])
    embed = discord_notify.report_embed(event, motm_names=[], motm_votes=0, top_rated=[], clips=[], night=night)
    assert embed["description"].startswith("**2–1**")
    assert {"name": "Match night", "inline": True, "value": "1W 1L · GD +1"} in embed["fields"]


# --- The scheduled messages ------------------------------------------------------------- #
def test_reminders_go_once_to_players_who_havent_answered(client, bot):
    _contract("1", "Ann")
    _contract("2", "Ben")
    eid = _event(hours=10)
    _signup(eid, "1", "Ann")
    now = datetime.utcnow()
    assert notify_poll.run(now)["reminders"] == 1
    assert notify_poll.run(now)["reminders"] == 0
    assert [uid for uid, _ in bot["dms"]] == ["2"]


def test_the_availability_nudge_is_a_monday_thing(client, bot):
    _contract("1", "Ann")
    monday = datetime(2027, 3, 1, 16, 0)
    assert notify_poll.run(monday - timedelta(days=1))["availability"] == 0
    assert notify_poll.run(monday)["availability"] == 1
    assert notify_poll.run(monday)["availability"] == 0
    with database.get_session() as session:
        mw.set_pattern(session, "1", [0])
    assert notify_poll.run(monday + timedelta(days=7))["availability"] == 0


def test_the_vote_opens_after_full_time_and_is_posted_once(client, bot):
    eid = _event(hours=-2.5)
    _signup(eid, "1", "Ann")
    _signup(eid, "2", "Ben")
    assert notify_poll.run()["votes"] == 1
    assert notify_poll.run()["votes"] == 0
    (channel, kw), = bot["posts"]
    pickers = [row["components"][0]["custom_id"] for row in kw["components"]]
    assert pickers == [f"motm:{eid}", f"selfrate:{eid}"]
    with database.get_session() as session:
        assert mw.vote_state(services.get_event(session, eid)) == "open"


def test_training_sessions_get_no_vote(client, bot):
    eid = _event(hours=-2.5, event_type="Community")
    _signup(eid, "1", "Ann")
    assert notify_poll.run()["votes"] == 0
