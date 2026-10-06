# YeeHaw FC — Pro Clubs team site

The team's public home: news/blog articles, an events calendar, a live
Twitch streamer showcase, and an EA Pro Clubs stats dashboard locked to our
own club. FastAPI + Jinja2 + SQLAlchemy.

This directory is the whole application. It runs as one systemd unit
(`yeehaw-fc`) on its own domain (`yeehaw-fc.club`), with its own venv,
`.env` and databases. See [`../deploy/README.md`](../deploy/README.md) for
the production deploy steps.

The repo used to carry an unrelated Discord bot and web app alongside this
one, which is where the isolation described throughout these notes came
from; that code is gone, and the isolation is simply how the site is
built.

## Navigation

The site is grouped into sections by what people come to do, defined once
in `navigation.py`. The sidebar, every section's tab bar, the shortcut
URLs and the tests all read that one list:

| Section | Tabs | Shortcut |
|---|---|---|
| Home | -- | `/` |
| News | -- | `/news` |
| Matchday | Fixtures & sign-ups (`/events`) · Training (`/training`) · Availability (`/availability`) · Tactics (`/tactics`) · Set pieces (`/set-pieces`) | `/matchday` |
| Squad | Players (`/players`) · Overview (`/squad`, staff) · Planner (`/squad/planner`, staff) · Recruitment (`/recruitment`, staff) · Moves & contracts (`/roster`, management) | -- |
| Club | Stats (`/stats`) · League table (`/league`) · Club profile (`/club-profile`, management) | `/club` |
| Media | Clips (`/clips`) · Live (`/streamers`) | `/media` |

A section is one place in the navigation, with the same header and tab bar
on every tab, but **each tab keeps its own URL**. Links people have saved
keep working, the back button behaves, and a page loads only its own
scripts: the tactics board and the stats dashboard are both script-heavy
and have nothing to share. The shortcuts redirect (302, not 301, since
which tab comes first is a choice that may change) to a section's first
tab. Each tab names the access level it needs (see "Permissions"); a
viewer sees only the tabs they can open, and a section disappears when
that's none of them. The routes are gated regardless.

To add a page to a section, add a `Tab` in `navigation.py`; a test fails
if a tab points at a route that doesn't exist.

## The design system

The site is styled as the **club's own records office**, in **FC Dallas
colours** (red, navy, white): navy panels over a floodlit pitch, a
red-hooped crest, and every page a "file" with
a short code above its title (`YFC / FIXTURES & SIGN-UPS`). The layout is
still a management tool -- a sidebar of sections, a strip of tabs, panels
and compact tables -- built to be scanned before a match rather than to
put on a show. The tokens live in `static/css/site.css`'s `:root`; the
theme layer ("Club file theme") is the last block in that file and
restyles the components above it. The rules worth knowing before adding
anything:

- **One accent.** `--accent` (khaki) marks the primary action and where
  you are. Every other colour has a meaning: `--accent-2` brass is staff
  and club roles, `--amber` orange needs attention, `--live` red is a
  stream on air or a destructive action. If a colour isn't saying one of
  those things, it should be grey-olive.
- **The ground.** The page background is fixed chalk pitch markings
  (`static/img/pitch.svg`) over mown stripes and a fine grain
  (`static/img/grain.svg`); panels are slightly translucent over it.
  Borders do the separating, and a panel's meaning is a 2px line across
  its top (`.panel-amber`, `.panel-staff`, `.panel-live`).
- **Two voices of type.** Inter is for reading and for every figure.
  IBM Plex Mono is for labels: kickers, table heads, chips, the
  breadcrumb, always small and tracked. Buttons and tabs are sentence
  case in Inter.
- **Kickers come from the shell.** `base.html` sets `--kicker` on `<main>`
  from the current section and tab, and `.page-head` draws it above the
  title, so no template repeats it. `.kicker` is the same style for
  anywhere else.
- **The crest** is an inline SVG macro (`templates/_crest.html`): a
  silver-rimmed navy shield with red hoops, a red chief with three stars,
  a ball and the club's initials -- FC Dallas's colours, not its badge -- so it needs no artwork and scales from the sidebar to
  the splash page. Replace the macro's body to use a real badge.
- **The shell.** A sidebar (crest, motto, sections, your account) that
  collapses to icons with the button in the top bar (remembered per
  browser, `static/js/shell.js`), and a top bar with the breadcrumb, a
  UTC clock -- fixture times are stored in UTC -- and your player file.
  `SITE_SHORT` (default `YFC`) and `SITE_MOTTO` set the code and the line
  under the crest.
- **On a phone** the sidebar folds into a top bar with the sections in a
  scrollable row, and tables scroll inside their panel rather than
  pushing the page sideways.
- **`--series-*` is the validated categorical chart palette**, read
  directly by `charts.js`; the `--status-*` colours are for win/draw/loss
  and health states only. The tactics pitch stays grass green.

The home page is a dashboard of panels: next match and club standing
across the top, the news down the main column (a lead story, then a
compact list), and squad moves, transfers and the Discord invite down the
side. "Live Now" appears only while somebody is actually streaming.

## Player cards

The Players tab renders the roster as cards rather than a table
(`playerCardHtml` in `static/js/app.js`, `.player-card` in `site.css`).

- **Not an EA Ultimate Team card.** That layout is EA's own branded design;
  this is the same job -- identity, rating, a few numbers -- done in this
  site's own design language: an OVR block, a name bar, three stat cells.
- **Every value is a real API field**: `proOverall`, `favoritePosition`,
  `ratingAve`, `winRate`, `manOfTheMatch`, `cleanSheetsGK`, `gamesPlayed`,
  `goals`, `assists`. There is deliberately no pace/dribbling attribute
  row -- EA's Pro Clubs API exposes no per-attribute ratings, and inventing
  or modelling them would make the card lie. A missing `proOverall` shows
  `--`, not a zero.
- **Tier colour is derived, not assigned.** `playerTier()` maps
  `proOverall` to elite (amber, `>= TIER_ELITE`), squad (green,
  `>= TIER_SQUAD`) or rotation (steel), so it stays current as ratings
  move. If most of the roster comes out amber, raise `TIER_ELITE`. Rotation
  is deliberately unglamorous but never punitive: no red, no arrows.
- **Keepers swap the win rate for clean sheets**, which is the number that
  actually says something about them.
- **Sorting moved from column headers to a control.** Cards have no headers
  to click, so `.player-sort` drives the same `sortBy()`/`PLAYER_COLUMNS`
  comparator the sortable `<th>`s used to. Every column that was sortable
  still is; picking one still starts it in its useful direction (names
  A-Z, counts biggest-first) and the toggle flips that.
- The card is a `<button>`, so Enter/Space activation and focus come for
  free -- unlike the table rows it replaced, which needed an explicit
  `tabindex` and keydown handler. Clicking one opens the same dashboard
  drawer as before, now a full-width panel spanning the grid
  (`.member-detail-row`).

## Performance

Two things made the site slow, and both are handled in ways worth knowing
before changing them.

**Upstream latency used to be page latency.** The home page reads the
club's standing from EA and who's live from Twitch. `cache.py` is a
stale-while-revalidate cache: a value past its fresh window is served
immediately while a background thread refreshes it, a failed refresh keeps
the last good value, and `app._warm_home_caches()` fills it at startup so
the first visitor after a restart isn't the one who waits.

The case that still bit was a **cold** cache: if EA is down when the app
starts, the warm fails, nothing is cached, and every visitor then pays the
full 10-second timeout -- twice, for the two calls behind the standing
band. So the home page reads with `blocking=False`
(`SwrCache.get_if_cached`, threaded through `ea_client` as a keyword-only
flag): a cold miss returns `None` and fetches behind the request instead of
in front of it. The band is missing for a few seconds after a restart
rather than the page hanging. `_start_refresh` dedupes, so a burst on a
cold key starts one fetch, not one per visitor.

Use `blocking=True` (the default) anywhere an empty answer is a failure
rather than a cosmetic gap -- the pollers and the `/api/*` stats routes all
keep it.

**Images used to be inlined.** Uploads were stored as base64 data URIs and
pasted into the markup of every page that showed them, which made the home
page ~10MB of HTML and the news index ~23MB -- none of it cacheable, since
a data URI lives inside the document. `images.py` re-encodes uploads to
WebP at two sizes (a display variant and a thumbnail), and they're served
from `/media/...` with an ETag and a year-long immutable cache. The URL
carries a version token derived from the row's `updated_at`, which is what
makes that cache safe: editing an article changes the URL.

Measured on a seeded copy with eight articles and real cover images: the
home page is **11.6 KB of HTML**, 11 requests, ~473 KB total, FCP under
300ms. A 2 MB upload becomes a 509 KB display variant and a 4 KB thumbnail.

## Permissions

The site is for the club. **Only the splash page is public** -- `/` for
anybody who isn't a member, and `/welcome` for everybody (so members can
preview it). Everything else needs signing in with Discord as a member of
`DISCORD_GUILD_ID`. The gate is a middleware in `app.py` (`_login_gate`)
and it's **default-deny**: a new route is members-only unless it's added
to the public list, not public until somebody remembers to protect it.
The public list is the splash page, sign-in/out, static files, Discord's
button presses (`/discord/interactions`, which checks its own signature)
and article cover images (Discord fetches those to show in an
announcement, and can't sign in). A guest asking for a page is sent to
sign in and then returned to it; the JSON API answers 401 instead.

Access levels, lowest first (`roles.py`):

| Level | Who | Can |
|---|---|---|
| Guest | signed out, or not in the Discord server | the splash page |
| Member | in the Discord server | the whole site: every player's profile and match stats, fixtures and sign-ups, tactics, news, comments |
| Staff | **Coach** | matchday: events, tactics, attendance, news, streamers, the squad overview, coach notes |
| Management | **Club President**, **Head Coach**, or `DISCORD_STAFF_ROLE_ID` in Discord | everything: offers, contracts, releases, who holds which club role, the club profile |

**Club roles are assigned on the site**, stored on the player's record
(`Player.club_role`), and read on every request, so a promotion takes
effect on the next page load rather than the next sign-in. Two ways in:
a staff offer the person accepts and management confirms (see "Staff
roles"), or management picking the role on the player's file. Nobody can
change their own role. A departure ("Let Go") ends any club role.

**The Discord staff role counts as management.** That's how the site
worked before club roles existed, and it means the club can't lock itself
out: whoever holds that role in Discord can always fix the roles here.
Being in the Discord server and holding that role are checked at sign-in,
so a change *in Discord* still takes effect at the next sign-in.

Signing in with Discord doesn't by itself mean membership: OAuth just
proves "this is a real Discord account," and anyone can authorize the
app's login regardless of what servers they're in. Guild membership is a
second, separate check against `DISCORD_GUILD_ID` made during sign-in.

## Player files

Every person has one record (`Player`, keyed on their Discord ID) that
the rest of the site hangs off: contracts, squad moves, sign-ups and the
gamertag link all store the same Discord ID. A member gets a record the
first time they sign in; anybody put under contract gets one then; and
at startup every existing contract is given one (`services.backfill_players`),
so a squad carried over from last season appears with no manual step.

`/players` is the squad list: staff by role, then the players under
contract grouped as **Starting**, **Rotation** and **Substitute** players,
each with appearances, form, goals, assists and MOTM. `/players/<id>` is a
player's file and `/players/me` is your own.

| On a player's file | Them | Other members | Staff |
|---|---|---|---|
| Profile, positions, gamertag, foot, build, bio | yes | yes | yes |
| Match stats and last 10 matches | yes | yes | yes |
| Contract, past contracts, attendance, squad-move history | yes | -- | yes |
| Coach notes | **never** | -- | yes (not on their own file) |

Players write their own preferred foot, build and bio, and link their own
gamertag. **Coach notes** are kept by staff, deleted by their author or
management, and never shown to the player they're about -- including a
coach who also plays, on their own file. Like trial notes, each one is also
posted to the staff channel in Discord (`coach_notes.py`; the recruitment
channel unless `COACH_NOTES_CHANNEL_ID` names another, `off` to stop),
pinging nobody, and deleting a note on the site deletes its Discord copy.
That channel has to be staff-only.

Squad statuses are **Starter**, **Rotation** and **Substitute**, shown as
Starting / Rotation / Substitute Player. Contracts stored with the old
"Reserve" are renamed to "Substitute" at startup.

## The public splash page

`welcome.html` is the club's shop window: a crest banner, the motto
and the club's own words, three ways in (join, the player portal, how we
play), a card per line of the team -- each drawing the current formation
with that line lit and any shirt nobody covers hollow -- then club
operations (squad, staff, how the next fixture is filling) beside the
live standing from EA. The words come from the **Club
profile** (`/club-profile`, management): headline, about, when we play,
region, platform, how we play, and a recruiting note; a blank field is
left off the page. The rest is live: skill rating, record and win rate
from EA, the last five results, how many players are signed, the
formation, the staff, and the positions **nobody** in the squad covers in
that formation (from the same depth rules as the squad screen), shown as
"We're recruiting".

## Local dev

```bash
cd proclubs
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill in at least SESSION_SECRET; see below
DEV_LOGIN=1 .venv/bin/uvicorn app:app --reload
```

Then open http://localhost:8000. With `DEV_LOGIN=1` set, `/login` offers a
"Dev sign in" shortcut that acts as any name, staff or not -- no Discord app
needed for local development. Never set `DEV_LOGIN` in production.

Run the tests with:

```bash
.venv/bin/pip install pytest
.venv/bin/pytest tests/
```

## Configuring a fresh deployment

All configuration lives in `.env` (see `.env.example` for the full list).
The pieces that need real setup:

- **Our club** (`CLUB_NAME` / `CLUB_PLATFORM` / `CLUB_ID`) -- this site
  shows one club's stats, configured once, not a search box. Don't type
  the ID in: `python season.py find` looks the club up on EA by name, and
  `python season.py switch` writes it to `.env`. See "A new season" below.
- **Discord OAuth2** -- on the club's application's OAuth2 page at
  [discord.com/developers/applications](https://discord.com/developers/applications),
  add a redirect matching `DISCORD_OAUTH_REDIRECT`, then copy the client
  ID and secret into `.env`. Discord rejects a callback whose host doesn't
  match a registered redirect exactly, so the two must agree character for
  character. Then find the team's guild ID and the staff role's ID (enable
  Developer Mode in Discord, right-click the server/role, "Copy ID").
- **Twitch** -- register a free app at
  [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps). This
  site only uses the app-level client-credentials grant to check "is this
  channel live," never a user login, so any redirect URL satisfies
  registration. "Live" also means *playing our game* -- see
  `TWITCH_GAME_FILTER` in `.env.example`; a roster member streaming
  something else doesn't show up as live here. Its default
  (`EA Sports FC 27`) has *not* been checked against Twitch's live
  category page -- if the roster reads as offline while streaming, check
  that first. It changes with each yearly title.
- **`SESSION_SECRET`** -- a long random string (`openssl rand -hex 32`).
  Signs the session cookie; rotating it signs everyone out.
- **`DISCORD_BOT_TOKEN`** (optional) -- enables every Discord feature
  here: the Clips sync, article and squad-move
  announcements, and event sign-ups. Developer Portal -> your app -> Bot
  -> Reset Token. This is full bot access rather than a scoped secret, so
  it is the most sensitive value in `.env`.
- **`CLIPS_CHANNEL_ID`** (optional) -- enables the Clips page sync, below.
  The ID of the Discord channel to pull video clips from (enable Developer
  Mode in Discord, right-click the channel, "Copy Channel ID"). Needs
  `DISCORD_BOT_TOKEN` too, and the bot needs View Channel + Read Message
  History in that channel.

Any of Discord OAuth, Twitch, or the Discord Events/Clips syncs can be left
unconfigured -- the site degrades gracefully (sign-in shows "not
configured," the streamer showcase shows profiles without live status, no
events get auto-created, the Clips page shows "not configured yet") rather
than erroring.

## Events and sign-ups

Staff create events on the site (`/events/new`), optionally picking a
**formation**. Players sign up from either surface -- the event page, or
the controls on the event's Discord post -- and both write the same row, so
answering in one place updates the other.

**With a formation, the team sheet is the sign-up sheet.** The event page
shows the pitch and players click an open shirt to take it; the Discord
post carries a position picker listing whatever is still open. Claiming a
position *is* signing up -- there's deliberately no separate "Going"
button next to the picker, because offering both would let someone be down
as going with no position and believe they'd picked one. One player per
shirt: a formation has exactly one GK, so a second claimant is refused
rather than quietly sharing. Moving position releases the old one, and
answering Maybe or Can't make it frees the shirt, since holding a position
you can't fill would block a slot nobody can see is open. Changing an
event's formation releases every claim (slot names differ between shapes),
leaving those players signed up but needing to re-pick.

**Without a formation** it's the plain **Going / Maybe / Can't make it**,
which is what an event gets when it's made without one.

- **The Discord post is not polled.** Discord delivers each button press
  straight to `POST /discord/interactions` as a signed HTTPS request, so
  it lands instantly. This still needs no always-on bot process: an
  interaction is an ordinary webhook, not a gateway connection.
- **Every interaction request is signature-checked** (Ed25519, against
  `DISCORD_PUBLIC_KEY`) before it is even JSON-decoded. That check is
  load-bearing security, not a formality -- the endpoint is public by
  necessity, so without it anyone who learned the URL could sign up, or
  un-sign-up, anyone they liked. Discord also probes the endpoint with
  deliberately-invalid signatures when you save the URL and refuses it
  unless they are rejected with a 401.
- **A sign-up made on the site edits the Discord post** so both rosters
  agree. If that edit fails, the sign-up is still saved and the site says
  so -- the post catches up on the next change.
- **The Tactics board is the fallback, not the truth.** A shirt claimed for
  a specific event wins over that player's usual spot on the board -- the
  board is the default lineup, the claim is what they signed up to play
  here. Players with no claim (or on an event with no formation) still show
  their board position, via a one-time gamertag link.
  The board stores EA gamertags (that's what EA's roster gives us) while a
  sign-up knows only a Discord account, so a member picks their own
  gamertag once and the site joins the two from then on. No link, no
  fallback position -- a normal state for a new member, not an error.
- **"Turns up" is measured, not claimed.** Staff mark who actually
  attended after the event; the percentage is presents over presents plus
  absents. An unmarked event counts as no evidence rather than an absence,
  `excused` is excluded from both halves of the ratio (so telling staff in
  advance never costs you), and no percentage is shown at all below three
  marked events -- the raw record is shown instead, since a two-event
  sample swings 50 points per event.
- **Staff can close sign-ups** without deleting the event. The buttons come
  off the Discord post at the same time, rather than being left there to
  fail.

### Times are local at both ends

Event times are still **stored** as UTC -- one instant, no ambiguity -- but
neither end of the site shows UTC to a person any more.

- **Entering one.** The kick-off field is the author's own local time. An
  `<input type="datetime-local">` submits bare wall-clock digits with no
  zone, so the form sends a hidden `tz_offset` alongside:
  JavaScript's `getTimezoneOffset()` **for the instant picked**, not for
  today. That last part is what stops a November fixture booked in August
  landing an hour out, and the field shows what it is about to save
  ("Saving as Sat, Nov 14, 8:00 PM EST") so the conversion is visible
  before you commit to it. Editing an event shifts the stored UTC back into
  the author's zone first, so what you see is what you set.
- **Reading one.** `app._localtime()` emits
  `<time datetime="...Z" data-localtime="FORMAT">` and
  `static/js/localtime.js` rewrites the text in the reader's zone via
  `Intl.DateTimeFormat`, including the zone abbreviation so it is never
  ambiguous. Dates are localised too, not just clocks: 23:30 UTC on the
  20th is the 21st in Sydney, and a date badge disagreeing with the time
  beside it is worse than either alone.
- **With JavaScript off**, neither conversion happens: the server-rendered
  UTC text stands (labelled UTC), and the kick-off field means UTC, which
  is what it meant before. The label and a `<noscript>` note both say so,
  rather than the field silently changing meaning.
- **A junk offset is ignored**, not applied -- real ones run UTC-12..UTC+14
  and shifting a fixture by a nonsense amount is worse than treating the
  entry as UTC.
- The Discord post was already per-member local (`<t:epoch:F>`) and is
  unchanged.

### Staged thread invites

With `EVENT_THREAD_CHANNEL_ID` set, announcing an event opens a **thread**
for that fixture instead of posting loose in a channel. With
`EVENT_INVITE_TIERS` set the thread is **private** and the ladder widens who
can see it as kick-off approaches; without a ladder it's **public** (anyone
who can see the parent channel), because a private thread nobody is added
to is invisible to everyone but the bot.

```
EVENT_INVITE_TIERS=create:<role>,48:<role>,24:<role>
```

The first rung fires the moment staff announce (not on the timer -- waiting
ten minutes to tell the people with first pick would make "first pick" mean
very little); the rest fire once kick-off is that many hours away. Each
rung adds its role's members to the thread, then pings the role *in* the
thread, so the notification is one tap from the position picker.

- **Why members, not a role.** Threads carry no permission overwrites of
  their own -- they inherit the parent channel's -- so a role cannot be
  granted access to a thread. Members are added individually
  (`PUT /channels/<thread>/thread-members/<user>`), which is also why the
  thread is private: a public one is visible to everyone who can see the
  parent channel, handing the whole ladder its access on day one.
- **This needs the Server Members privileged intent.** Discord has no
  "list a role's members" route, so `discord_rsvp.role_member_ids` pages
  the guild's member list and filters. Turn it on at Developer Portal ->
  your application -> Bot -> Server Members Intent. Without it every tier
  still gets pinged but nobody gains access; the poller says so, and the
  `member_count` recorded against each fired tier is 0, which is the
  signature to look for.
- **A tier is due once its moment has *passed***, not during a window. An
  event announced 12 hours before kick-off owes its 48h and 24h rungs
  immediately rather than never, and a poller that was down over a rung's
  moment catches up on its next run.
- **Each rung fires once**, enforced by a unique constraint on
  (event, tier) in `event_tier_invites` -- so a double run is a database
  error rather than a second ping to the same people. A rung whose Discord
  call fails is deliberately left unrecorded, so the next run retries it:
  a late ping is recoverable, a rung recorded as done having told nobody
  is not.
- **Adding somebody already in the thread is a no-op success**, so re-runs
  and overlapping tiers cost API calls and change nothing.

### Push to Discord

Staff always have a Discord button on an event page. Before the event is
posted it's **Post to Discord** (and, if Discord isn't configured, says
which setting is missing). After, it's **Push to Discord**, for when the
post can't be found (`discord_rsvp.push`):

- the thread was **archived** (Discord hides one after 7 quiet days): it's
  reopened and the post refreshed;
- the **post** was deleted: it's posted again in the same thread;
- the **thread or channel** was deleted, or it's a **private thread with no
  invite ladder** (from before threads went public without one): it's
  posted afresh in a new thread, the ladder starts again from its first
  rung, and the old hidden thread is deleted if the bot may.

The **Discord ↗** badge on the event links straight to the post.

`EVENT_THREAD_CHANNEL_ID` is enough on its own -- sign-ups need a bot
token, a public key, and *a* channel to post in, and either setting
satisfies the last of those. Leave it blank and everything above is
skipped: announcements go into `EVENTS_ANNOUNCE_CHANNEL_ID` as before.

Discord's own **Events** tab is not read at all. An event someone makes
there stays in Discord -- it never appears on the site, and the people who
press **Interested** on it are not signed up for anything here. Site
events are the only sign-up sheets, and they're made on the site.

## The match week

One loop each week, for players and coaches alike (`matchweek.py` holds
the rules, `matchweek_routes.py` the pages, `discord_notify.py` what the
bot says, `notify_poll.py` when it says it):

1. **Availability** (`/availability`, Matchday tab). Players tick their
   usual nights once and add any dates they're away. Everyone sees the
   squad's grid for the next two weeks; a fixture's own answers override
   the pattern on its night, and the bottom row counts who's free or going.
2. **Sign-up with positions.** Signing up for a match also asks for your
   top three pitch positions, best first.
3. **The team sheet** (`/events/<id>/teamsheet`, staff). Pick the XI and
   bench from a list that shows each player's answer, preferences, natural
   positions, form, appearances and whether they're free that night. The
   shirts players claimed when signing up are filled in to start. Nobody
   can be picked twice. **Publish** shows it on the event page ("You're
   starting at LW"), posts it to Discord with each player mentioned, and
   DMs every player their shirt.
4. **After the match** (`/events/<id>/report`). Two hours after kick-off
   the bot opens the vote and posts it: a Man of the Match picker and a
   self-rating out of ten, answered privately. Only players in the squad
   (the published sheet, else everyone who went) can vote, never for
   themselves; votes can be changed until the vote closes, 48 hours later
   or when the report is published. Coaches write the report: the score
   (prefilled from EA's record of that night), key moments, clips, and a
   coach rating and comment per player. **Each player sees their own coach
   rating, on the report and on their file, and nobody else's.**
   Publishing closes the vote and posts the report -- score, the
   player-voted Man of the Match, EA's top ratings that night, and clips.

**Scheduled messages** (`notify_poll.py`, every 10 minutes via
`proclubs-notify-poll.timer`), each sent once and logged in
`notifications`:

| When | What |
|---|---|
| Mondays from 15:00 UTC | DM to contracted players who've never set their usual nights |
| The 24 hours before a fixture | DM to contracted players who haven't answered it |
| Two hours after kick-off | The vote, posted where the match was announced |

Channel posts go in the fixture's own Discord thread when it has one,
otherwise `MATCHDAY_CHANNEL_ID` (falling back to the events, then the
news channel). `NOTIFY_DMS=0` turns the DMs off and keeps the posts. A
player with DMs from server members turned off simply doesn't get one;
that's counted, never retried in a loop.

## Training sessions

In-game practice and theory are events of type **Training** or **Theory**
(`training.py`, `training_routes.py`), so they have the same sign-ups,
formation, attendance and Discord thread as a match. On top:

- **A plan** (`/events/<id>/plan`, staff): a one-line objective, the plan
  in order, and a **role brief per position** of the session's formation.
  The event page shows each player their own brief first ("Your brief ·
  ST"), from their place on a published team sheet or the shirt they
  claimed. The Discord announcement carries the objective, plan and
  briefs, so the theory talk in the thread starts from them; saving the
  plan updates an already-posted announcement.
- **A review** after the session: what worked, written by staff and shown
  on the session page and the Training tab.
- **The Training tab** (`/training`): upcoming sessions with their
  objectives, recent ones with their reviews, and **"What should we work
  on?"** -- players' suggestions, seen by staff and by their author only.
  Staff mark each New / Planned / Worked on / Not now with a reply the
  player sees.

Sessions get the 24-hour reminder like any fixture, but no team-sheet
DMs, vote or match report.

## Recognition

`recognition.py`, announced by the bot from `notify_poll.py`:

- **Milestones** -- a debut, 10/25/50/100/200 appearances, a first goal
  and 10/25/50/100 goals, 10/25/50 assists, 5/10/25 clean sheets (all from
  EA, through the player's linked gamertag), and 1/5/10/25 Man of the
  Match awards from the squad's votes. Each is awarded once and posted in
  the matchday channel with the players mentioned. The first evaluation
  ever records what players had already reached without posting it --
  history from before the feature isn't news -- and after that every new
  one is announced, a club's very first debut included.
- **Player of the Month** -- most Man of the Match *votes* across the
  month's matches (so three second places can beat one lucky win); level
  on votes, more wins takes it; still level, they share it. Awarded and
  posted from the 1st of the next month.
- **Leaderboards** on the Players page: form, goals, assists, squad Man of
  the Match awards and attendance, top five each, with the latest Player
  of the Month above them.
- **Honours** on each player file: their milestones and months won.

## Development goals and reviews

On each player file, for the player and the staff only (`development.py`,
`development_routes.py`) -- unlike coach notes, this is the part of a
player's development they're meant to see and work on:

- **Goals.** A coach sets one -- text, an area (Attacking, Defending,
  Positioning, Teamwork, Technique, Mentality) and an optional target
  date -- and the player gets a DM. Up to three open at once. The player
  updates their progress (0/25/50/75/100%) and a note as they go; staff
  mark it achieved or drop it, and can reopen it.
- **Monthly reviews.** A short write-up of a one-to-one, against the
  goals. One per player per month; saving again replaces it.

## Staff tools

`staff_tools.py`, `recruitment.py`, `setpieces.py`, pages in
`staff_routes.py`:

- **Action inbox** -- "Needs a decision" at the top of the dashboard's
  side column, for staff. Worked out from the data each time, so it
  can't go stale: contracts run out or running out and accepted offers
  waiting to be confirmed (management only), matches in the next 72
  hours without a team sheet, players who haven't answered a fixture in
  the next 48 hours, last week's matches without a report, players with
  no gamertag linked or no usual nights set, unanswered training
  suggestions, and players with no development goal. Orange items need
  doing soon; grey ones are housekeeping. Each links to where it's done.
- **Squad planner** (`/squad/planner`) -- a what-if: change statuses,
  release players, add up to four signings you're considering, and see
  the status balance, whether the starters alone can field the
  formation, and the depth chart that would leave. It saves nothing;
  contracts still change through offers and renewals.
- **Recruitment** (`/recruitment`) -- how somebody goes from joining the
  Discord server to settled in the squad:
  1. **Joins the server.** "New in the server" lists members who joined
     in the last 30 days and nobody has dealt with yet (no file, contract,
     offer or club role). Staff start a file in one click, or dismiss
     somebody who isn't here to play. Needs the bot and its Server
     Members intent, like the `/roster` picker.
  2. **Prospect.** One file per Discord account: positions, gamertag,
     where they came from -- editable on the file.
  3. **On trial.** A note after each session, with an optional rating
     out of ten, tied to the match or session (a note from a match moves
     a prospect to "On trial"). **Every note is posted to the recruitment
     channel** (`RECRUITMENT_CHANNEL_ID`, by default `1546267802791452772`)
     as an embed linking back to the file; nobody is pinged. A failed post
     is flashed and the note still saves.
  4. **Offered.** Management send the contract offer from the file (or
     from Moves & contracts -- the file follows either way) and the file
     shows whether it's waiting, accepted or declined.
  5. **Signed.** Confirming the signing (on the file or on `/roster`)
     starts the contract and moves the file to Signed.
  6. **Settling in.** A checklist on the file: contract started, gamertag
     linked, usual nights set and a first goal tick themselves from the
     data; the EA club invite and the welcome message are ticked by hand,
     and a button DMs the newcomer the links they'll need (which ticks
     the welcome). Unfinished checklists, and trialists with no feedback,
     show in the staff inbox.
- **Set pieces** (`/set-pieces`, Matchday tab) -- the club's routines by
  kind (corner, free kick, penalty, throw-in, kick-off): name, side,
  taker, the routine and its targets. Every member reads it; staff write
  it.

## Managed Discord roles

The site manages these Discord roles (`role_sync.py`): when someone's
record changes here, their role follows -- added when they should hold
it, removed when they no longer should.

| Role | Held by |
|---|---|
| Starting players | a live contract at **Starter** |
| Rotation players | a live contract at **Rotation** |
| Substitute players | a live contract at **Substitute** |
| Squad | any live contract, or an accepted offer awaiting confirmation |
| Club President / Head Coach / Coach | that club role on their player file |
| Trialists | a prospect **On trial** or **Offered**, not yet under contract |

**Which Discord role is which** is set by management on **Squad → Discord
roles** (`/discord-roles`, `role_settings.py`), picked from the server's
own role list, and stored in `club_settings` under `discord_role:<key>`.
"Not managed" leaves that role alone. The page refuses @everyone, roles
that belong to a bot or integration, the same role for two keys, and
`DISCORD_STAFF_ROLE_ID`; it flags any role sitting above the bot's own,
which Discord would refuse to hand out. Swapping a role stops the site
managing the old one but doesn't take it off anybody. A key never saved
on the page falls back to its old `.env` variable (`ROLE_STARTER_ID`,
`ROLE_TRIALIST_ID`, ..., `ROSTER_SQUAD_ROLE_ID`), so existing installs
keep working until the page is saved once. The bot token, guild ID and
staff role stay in `.env`. Changes that trigger a sync:
confirming a signing or an appointment, recording, renewing (when the
player accepts) or releasing a contract, Let Go, giving or clearing a
club role, and moving a prospect through the pipeline. If Discord refuses,
the change on the site still stands and the reason is flashed.

**The guardrails.** Only the roles in that table are ever added or
removed; any other role a member holds is never touched.
`DISCORD_STAFF_ROLE_ID` -- the way into the site's management -- is
excluded even if it's listed, so the site can never lock anyone out.
Somebody who holds a managed role in Discord but has no record here is
only ever changed from **Squad → Discord roles** (`/discord-roles`,
management), which lists every member out of step and what they'd gain or
lose, and changes nothing until **Apply all** is pressed. Run it once
after deploying, to bring the server in line with the contracts already
recorded -- and record contracts for anyone who should keep their role
first.

The bot needs **Manage Roles**, and its own highest role must sit **above**
every managed role in Server Settings -> Roles; the server page also needs
the Server Members Intent to list members.

## Clips are Discord-only

`/clips` is **read-only** -- no upload UI on the site. Post a video directly in the configured Discord channel (an actual
file attachment, not a link) and it shows up on the site's Clips page
automatically.

- **Scope, deliberately narrow:** only video *files* uploaded straight to
  Discord (`content_type` starting `video/`) are picked up. A pasted
  YouTube/Twitch/Streamable link shows up in Discord as a rich embed, not
  a file attachment, and isn't turned into a clip here -- reliably
  converting an arbitrary link into an embeddable player is a bigger job
  than this first pass covers. If a message has more than one video
  attached, only the first is used.
- **The displayed title comes from the clip's filename, not the Discord
  message's text.** Whatever caption (if any) someone typed alongside the
  upload is ignored -- it's often blank, unrelated chat, or just an emoji.
  The filename (what the console/game capture named the file) is cleaned
  up instead (extension stripped, underscores/dashes turned into spaces --
  see `services._title_from_filename`), and refreshed on every sync the
  same way `video_url` is.
- Runs on a schedule (`proclubs-clips-poll.timer`, every 30 minutes -- see
  `../deploy/README.md`), same polling reasoning as the Events sync: no
  always-on bot/gateway connection here, so REST polling is the only way
  to notice a new clip.
- **Video URLs expire and get refreshed, not the messages themselves.**
  Discord's attachment CDN URLs are signed and valid roughly 24h, reissued
  fresh on every fetch; each sync updates `video_url` for any clip whose
  message is still within the polled window (the most recent 50 messages).
  A clip that scrolls out of that window keeps whatever URL it last had,
  which will eventually go stale -- every clip also stores a permanent
  Discord "jump" link (`discord.com/channels/...`) as a fallback that never
  expires, shown under the player.
- Unlike events, a clip that ages out of the polled window is **not**
  deleted from the site -- there's no equivalent of Discord "canceling" a
  clip, so old clips just stop refreshing rather than disappearing.

**Clips can be embedded in an article.** The article editor's "Insert
Clip" button (only shown when `CLIPS_SYNC_ENABLED`) opens a picker over
`GET /api/clips` and drops the chosen clip into the body. What actually
gets stored is a placeholder -- `<clip-embed data-clip-id="N">` -- never
the clip's `video_url` itself, because that URL is the same signed,
~24h-expiring Discord CDN link described above; baking it into an
article's stored HTML at save time would go stale even while
`sync_clips` keeps the underlying `Clip` row's URL fresh. Instead,
`services.render_clip_embeds()` resolves each placeholder to a live
`<video>` on every view of `/news/<slug>`, reading whatever `video_url`
is currently in the `Clip` table -- so an embed keeps working for as
long as its clip stays in the synced window, exactly like `/clips`
itself, and shows "This clip is no longer available" instead of a dead
player if the clip row is gone. `html_sanitize.py` allows the
`clip-embed` tag with only its `data-clip-id` attribute -- nothing else
about the embed is staff-controlled HTML.

## The article editor

`/news/new` and `/news/<slug>/edit` use [Quill](https://quilljs.com) as a
proper rich-text (WYSIWYG) editor -- headings, bold/italic/underline/
strike, blockquotes, code blocks, lists, links, and inline images -- not
raw Markdown. A few things worth knowing:

- **Vendored, not a CDN.** `static/vendor/quill/` is Quill's own unmodified
  build, checked into the repo (BSD-3-Clause, see the LICENSE file there)
  rather than loaded from jsdelivr/unpkg/cdnjs. That's deliberate: this
  site otherwise avoids third-party script origins (the one exception is
  Google Fonts, disclosed in base.html), and a vendored copy keeps working
  even if a CDN is down or blocked.
- **The toolbar is deliberately narrow.** Every button maps to something
  `html_sanitize.py` actually allows through (see below); options Quill
  supports beyond that -- text color, fonts, alignment -- are left off
  rather than offered and then silently stripped on save. The one
  exception is Discord clips: not a stock Quill format, but a custom
  "Insert Clip" button and blot (see "Clips are Discord-only" above).
- **Inline images are embedded as data URIs**, the same "no upload
  endpoint, just embed it" pattern as the cover image and streamer
  avatars elsewhere on this site -- capped client-side at 3MB per image.
  A long article with several photos can get large; `MAX_BODY_LENGTH` in
  html_sanitize.py caps the total stored size as a backstop.
- **Still sanitized server-side**, same as the old Markdown pipeline was --
  the editor's output is HTML reaching every visitor's browser unescaped,
  so a compromised staff account or a bug in Quill's own JS shouldn't
  turn into stored XSS. One accepted tradeoff: allowing `data:` image
  sources through the sanitizer also permits a `data:` link (nh3 applies
  its URL-scheme allowlist to every URL attribute uniformly, not per-tag)
  -- modern browsers already refuse top-level navigation to a cross-origin
  `data:` URL, so the realistic risk is low, but it's a real tradeoff, not
  an oversight. See the comment in html_sanitize.py.

**The cover image has a focal point.** The same cover photo gets cropped
to several different shapes across the site -- the lead story on the home
page, a 21:9 header on the article page, small thumbnails in the home news
list and 16:9 cards in the grids -- and a plain center crop often cuts off the part that
actually matters (a face at the edge of the frame, for instance). On the
article form, clicking the cover preview sets `Article.cover_focal_x`/`
cover_focal_y` (percentages, defaulting to 50/50 -- dead center), which
every template that renders that cover image reads back via `app.py`'s
`focal_position()` helper and applies as `object-position`/
`background-position`. Repositioning doesn't require re-uploading the
image -- the two fields save independently of the file input.

## Publishing announces to Discord

Set `NEWS_ANNOUNCE_CHANNEL_ID` (and `SITE_BASE_URL`) and the site posts a
rich embed to that channel the moment an article goes live -- title,
summary, category-colored accent, cover image, and a link back to the
article. See `discord_announce.py`.

- **Fires on publish, not on save.** A brand-new article published
  immediately announces; so does a draft the first time it's published.
  Re-saving an article that was *already* published does not -- otherwise
  every typo fix would repost it. The check is a simple before/after
  comparison of `Article.published` in `app.py`'s `news_new`/`news_edit`
  routes, no extra column needed.
- **Reuses `DISCORD_BOT_TOKEN`** (see "Clips are Discord-only" above for
  the sharing tradeoff) and `discord_api.py`'s POST-with-429-retry helper.
  Synchronous and one-directional (site -> Discord) -- unlike the
  events/clips sync, there's nothing to poll for, so it's a plain API call
  made right when `services.create_article`/`update_article` publishes.
- **The cover image needs a real URL, not the data: URI it's stored as.**
  Discord's embed API can't fetch a `data:` URI, so
  `GET /news/<slug>/cover-image` serves the stored image's decoded bytes
  at an actual endpoint (`services.decode_data_uri`), and the embed points
  there instead. Articles with no cover image just get an embed with no
  thumbnail.
- **A Discord hiccup never blocks publishing.** `announce()`'s failure is
  caught in `app.py` and turned into an error-styled flash message ("...but
  the Discord announcement failed to send") rather than raised -- the
  article is already live on the site either way.
- **`SITE_BASE_URL` is required** because an embed's `url`/`image.url`
  fields must be absolute, and this deploy doesn't configure uvicorn/
  gunicorn to trust Caddy's proxy headers, so a request's own scheme can't
  be trusted to say `https`. Missing it doesn't block publishing either --
  same flash-and-continue treatment, just naming the actual problem.

**Reactions on that Discord message show up on the article as a heart.**
`announce()` returns the new message's id, saved as
`Article.discord_message_id`. `proclubs-reactions-poll.timer` (every 30
minutes, see `../deploy/README.md`) runs `discord_reactions_poll.py`,
which re-fetches that message and sums every reaction on it -- any emoji,
not just ❤️, all counted together (`discord_announce.fetch_reaction_count`)
-- into `Article.discord_reaction_count`, capped to the
`DISCORD_REACTIONS_POLL_LIMIT` most-recently-announced articles per run
(default 20; reactions settle quickly after posting, so checking an old
announcement forever isn't useful). **Added into the article's like
count**, not shown as a separate badge -- see `services.combined_like_count`
and "Comments and likes" below for what that total does and doesn't mean.

## Squad moves: contracts, staff roles, departures

`/roster` (Squad → Moves & contracts, management only) lists everyone in the Discord
server with their Discord avatar. Below the picker are three separate
panels, one per kind of move. Each posts its own fields and only the
pressed panel's are read, so a half-filled contract can't leak into a
staff offer. See `discord_roster.py`.

- **Player contract** -- **Offer Contract** publishes an offer the player
  answers themselves, carrying a primary and optional secondary position,
  a length in weeks and a squad status (see "Contracts" below).
- **Staff role** -- **Offer Staff Role** offers a role such as Assistant
  Manager. Same Accept / Decline / confirm flow, but no contract and
  **no Discord role** (see "Staff roles" below).
- **Departure** -- **Let Go** ends their contract, if any, and publishes
  a departure.

The player-contract flow:

```
staff picks a member  ->  OFFER posted to Discord with Accept / Decline
                             |
             player presses  |  (only the player the offer names can)
                    +--------+--------+
                    |                 |
                ACCEPT            DECLINE
                    |                 |
      squad role added        nothing changes
      offer edited in place   offer edited in place
                    |
      staff press "Confirm signing" on /roster
                    |
      SIGNING announcement posted -- the celebration
      CONTRACT starts: N weeks from the confirmation
```

### Contracts

Every contract offer asks staff for a **primary position**, an optional
**secondary position**, a **contract length** (1-52 whole weeks) and a
**squad status** -- *Starter*, *Rotation* or *Substitute*, after Football
Manager's squad statuses. All of them show on the offer in Discord so the
player sees the terms before pressing, and on the signing announcement.

- **Positions come from a fixed list** of pitch positions
  (`discord_roster.PITCH_POSITIONS`) rather than free text: a contract is a
  record of what was agreed, "Stirker" on one is a real mistake, and "the
  secondary must differ from the primary" can only be checked against
  known values. Staff titles like Manager are not on it; they're offered
  from the Staff role panel instead.
- A renewal restates both positions as a pair, so dropping the secondary
  on renewal really drops it. A contract recorded before the fixed list
  existed keeps its old primary position through a renewal.

- **A contract starts when staff confirm the signing**, not when the
  player presses Accept -- that's the moment it's official. Offers made
  before contracts existed have no terms, so confirming one starts
  nothing.
- **Record Contract** is for players who were in the squad before the
  site tracked this: it saves terms directly, with no offer, no
  announcement and no role change.
- **One live contract per person.** Offering or recording for somebody
  already under contract is refused -- renew the one they have.
- **Nothing happens by itself when a contract runs out.** Expiry is
  computed from `Contract.expires_at`, not stored, so there's no timer. The
  Contracts panel on `/roster` lists every live contract soonest-first,
  marks the last 7 days as *Expiring soon* and anything past its date as
  *Expired*, and says at the top how many need a decision. Staff then:
  - **Renew** -- posts a renewal to Discord with the same Accept /
    Decline buttons, only the player can press. Accepting adds the new
    weeks to the **current end date** (renewing early never costs the
    player time), or to today if it had already lapsed, and applies the
    new status and position. Declining changes nothing; the contract runs
    out as it was going to. One open renewal per contract at a time, and
    no role is written either way.
  - **Release** -- ends the contract and publishes a departure, exactly
    like Let Go (and, like it, never removes a role). A renewal left
    unanswered can't be accepted after a release.
- Renewals never reach the public home page -- a contract negotiation
  isn't news (`services.public_roster_moves`).

### Staff roles

A staff appointment is its own kind of move (`staff_offer`), not a
"position" on a playing contract. It's offered from the Staff role panel
for one of the three club roles -- Club President, Head Coach, Coach --
answered with the same Accept / Decline buttons, and confirmed by
management with **Confirm appointment**, which publishes an appointment
announcement in the palette's blue rather than the signing green, and
**gives the person that club role on the site** (see "Permissions").

- **No contract.** Squad status and contract length mean nothing for a
  coach.
- **No Discord role on Accept, even with a Squad role set.**
  The squad role isn't theirs by default, since a coach needn't be a
  player. The site access comes from the club role, and only when
  management confirm -- never from the person's own button press.
- **A player under contract can still be offered a staff role.** A
  player-coach is normal, and "already under contract" only guards against
  a second *playing* contract.
- A confirmed appointment shows on the home page as *Appointed*; pending,
  unconfirmed and declined staff offers stay private, the same rule as
  player offers.

**The role rule.** Squad moves themselves only ever *grant*:
the Squad role (set on Discord roles), when the player presses Accept on
their own contract offer (`grant_squad_role`, the only role write in
`discord_roster.py`). Every other role
change -- including every removal -- goes through `role_sync.py`, which
keeps the managed roles in step with the site (see "Managed Discord
roles" below).

**The acceptance and the role grant are recorded separately**, because
they can disagree. If Discord refuses the role write -- the bot lacking
Manage Roles, or its highest role sitting below the squad role -- the
acceptance still stands (it is the player's, and our permissions problem
is no reason to pretend they didn't answer). The failure is stored on the
row and shown on `/roster` with what to go and fix, rather than leaving
an acceptance that silently granted nothing.

**Confirming is a second, human step.** The player accepting is them
agreeing; the club announcing a signing is the club's own act, and there
is usually paperwork between the two. So an accepted offer sits on
`/roster` as *Accepted* with a **Confirm signing** button, and only that
publishes the celebration. It goes back to the channel the offer went to
(`RosterMove.discord_channel_id`), not wherever `ROSTER_ANNOUNCE_CHANNEL_ID`
points today -- the two can differ if the setting changed in between.

**Refusals on a button press are ephemeral**, so only the presser sees
them and the post stays as it was for everyone else: an offer that isn't
theirs, one already answered, one that no longer exists. A settled offer
is re-rendered with its buttons removed, since dead controls only invite
presses that can't be honoured.

**The interactions endpoint is shared** with event sign-ups. The
`roster:` custom_id prefix is checked before either side tries to parse
an id that isn't theirs (see `app.py`'s `discord_interactions`).

**Picking somebody.** The member list comes from
`GET /guilds/<id>/members`, which needs the bot's privileged
**GUILD_MEMBERS** intent (Developer Portal -> your app -> Bot -> Server
Members Intent). Without it Discord answers 403; the page shows that error
and names the intent rather than rendering an empty list, because the fix
is a checkbox on Discord's side and nothing in this repo hints at it
otherwise. That same walk is what `discord_rsvp.role_member_ids` uses for
staged event invites -- one implementation, two callers.

- **Bots are filtered out** and members are sorted case-insensitively.
- **Avatars** follow Discord's own precedence: per-server avatar, then
  account avatar, then the default art (which has two schemes, one for
  legacy discriminator accounts and one for the new username system).
  Animated avatars are requested as `.png`, which the CDN serves as a
  still frame.
- **The list is cached for 60s** (`cache.SwrCache`), and dropped after an
  announcement, since a role change usually follows within a minute.
- **The picker is a radio group**, not a JS widget, so choosing a person
  works with scripting off; `roster.js` only adds the search filter and
  the confirmation dialog.
- **The posted id is re-resolved** against the live member list before
  anything is published, so a stale tab or a hand-edited form can't
  announce a position for somebody who isn't in the server.

**The announcement** is an embed in the site's palette -- green for an
offer or renewal, amber for a departure -- with the member's avatar as its
thumbnail, the position and contract terms as fields, and an optional note
from staff. The player is
mentioned in the message body as well as inside the embed, because a
mention *inside* an embed renders as a link and notifies nobody.
`allowed_mentions` is always explicit and lists only that one user, so an
`@everyone` typed into the staff note can never go out for real.

**Every attempt is recorded**, delivered or not (`models.RosterMove`,
shown under "Recently announced" with its state -- *Awaiting answer*,
*Accepted*, *Declined*, *Signed*). A post that fails writes a row with a
null `discord_message_id` and the page marks it *not delivered to
Discord* -- otherwise staff see a success redirect, assume the club
announced something, and never find out it didn't. The row snapshots the
name and avatar at announcement time, since somebody who was let go is
likely to leave the server.

**The home page shows a "Squad Moves" band**, and what it leaves out is
the point (`services.public_roster_moves`). Only two things are public:

- a **departure**, which was announced the moment it was made;
- a **signing** -- an offer the player accepted *and* staff confirmed.

Everything else stays on the staff page. A **pending offer** hasn't been
answered, so putting it on the front page announces it over the player's
head. An **accepted but unconfirmed** offer is exactly what the confirm
step exists to hold back; leaking it here would make that step
ornamental. A **declined** offer is never shown at all -- publishing that
somebody turned the club down is unkind, and isn't the club's news to
tell. Tests in `test_app.py` pin each of those exclusions.

Ordered by when each became public (`confirmed_at` where there is one,
`announced_at` otherwise), so a signing confirmed today leads even if the
offer went out last week. Capped at six, and the whole section is absent
when nothing qualifies.

Set `ROSTER_ANNOUNCE_CHANNEL_ID` to choose the channel (falls back to
`NEWS_ANNOUNCE_CHANNEL_ID`); the role an acceptance grants is the Squad
role on Discord roles. The Accept/Decline buttons ride on the same signed
interactions webhook as event sign-ups, so they need `DISCORD_PUBLIC_KEY`
and the Interactions Endpoint URL that those already require.

## The squad screen

`/squad` (Squad → Overview, staff only) is
Football Manager's squad view built from data the site already has. It
joins each contract to what EA recorded through the player's linked
gamertag. See `squad.py` for the rules and `db.squad_usage` for the query.

**How far back.** The hourly poll keeps every match it sees in
`data/history.db` for the whole season (`season.py` clears it at the next
one), so nothing is capped at EA's 10. Squad, Players and each player file
have a **This season / Last 20 / Last 10** picker (`?window=`,
`squad.WINDOWS`) for "Played", the flags below and the matches a file
lists; it defaults to the whole season. Goals, assists and MOTM are always
season totals, and attendance always counts every marked event. The team
sheet's appearance counts stay on the last 10, as recent form for picking a
side.

**When the history is short of EA's count.** EA only ever lists a club's
latest ~10 matches, so any played before tracking began, or while the poll
was down, never reach `matches` -- the history might hold 64 of a season's
94. The poll also stores EA's own per-player season totals
(`members/stats` -> `member_totals`), and EA's club record gives the
season's real total (wins + draws + losses on the latest snapshot). So in
the **This season** view, when the history is short, Played, goals,
assists and MOTM come from EA's totals out of EA's match count
(`squad.with_ea_totals`), and the page says so. Form and the match lists
still come from the recorded matches, since EA keeps no per-match detail
beyond the last few. The Last 20 / Last 10 views are always fully recorded.

**Passing, defending and shooting.** Every recorded match keeps each
player's passes made / attempted, tackles won / attempted and shots
(`match_players`), and the poll stores EA's season figures alongside
(`member_totals`: passes, pass success %, tackles, tackle success %, shot
success %, average rating, clean sheets, red cards). The Squad overview
has Pass % and Tkl % columns, and each player file a row of passing,
tackling and shooting tiles plus Passes / Tackles / Shots in its match
list (`squad.detail_stats`). Over Last 20 / Last 10 they're summed from the
recorded matches (conversion = goals / shots); in the season view with
EA's totals in use they're EA's own season figures, which count the
matches the history missed. EA's API has nothing finer -- no key passes,
interceptions, crosses, dribbles, distance or xG.

**Players.** One row per player under contract: squad status, primary /
secondary position, appearances over the picked span, form
(average rating over their last 5 appearances, shown once they have 3),
goals · assists · Man of the Match, attendance, and time left on the
contract. Under a row go the things worth acting on, each naming the
evidence it's based on:

- **Playing time against squad status.** A *Starter* who has played in
  fewer than half of the picked span's matches, or a *Rotation* player in
  fewer than a fifth.
  A *Substitute* is promised nothing. Not judged until the club has 5
  recorded matches.
- **Promotion candidates.** A Rotation or Substitute player averaging 7.5 or
  better.
- **Contracts** expiring or expired, louder when the player is in form.
- **Attendance** under 60%, once there are enough marked events for a rate
  (`services.attendance_record`; excused absences don't count against them).
- **No gamertag linked**, so nothing about them can be measured.

Thresholds are named constants at the top of `squad.py`.

**"Played" means appearances, not starts.** EA doesn't record who started
and who came off the bench, so the page says "played 2 of the last 10"
(or "of this season's 26")
and never claims to count starts. Only league and playoff matches count,
since those are all `poll.py` records.

**Depth.** For the formation currently on the tactics board, each position
it uses, how many of it the formation needs, and who covers it. A
player's primary position makes them a **natural** there and their
secondary makes them **cover**; "Any Outfield" covers every outfield
position but never goal. Each position is *Uncovered* (can't be filled),
*No backup* (filled with nobody spare) or *Covered*. Contracted positions
the formation doesn't use are listed under it, which is worth knowing
before offering another one. Every slot label in `formations.py` must map
to a contract position (`squad.SLOT_POSITION`); a test enforces it, so a
new formation can't quietly drop a row.

**Playing without a contract** lists gamertags that have played at least
3 of the last 10 but aren't linked to anybody under contract. Each one is
either somebody who needs a contract recorded, or a contracted player
whose gamertag isn't linked yet.

### Gamertag links

Everything above depends on knowing which gamertag is whose, and linking
used to be left to each member. Now:

- **Confirm signing takes the gamertag**, linked before anything is posted,
  so a gamertag another member already holds stops the signing with
  nothing announced. It's optional, because a new signing may not have
  joined the EA club yet. Confirming without one still signs them, and
  says plainly that they still need linking.
- **Staff can link from the squad screen** for anybody under contract,
  using a field that suggests every gamertag seen in a recorded match plus
  the live EA roster if it's cached. Linking somebody who isn't under
  contract is refused, since members still link their own from an event
  page. A gamertag already claimed is refused there too.
- Suggestions never wait on EA: the roster is read non-blocking, so a slow
  or down API leaves just the recorded names.

## The AI-written roundup

Every 2 days (`ROUNDUP_DAYS`), `weekly_article.py` has Claude write a
roundup of those days and publishes it live -- no staff review step -- under the byline
`WEEKLY_ARTICLE_AUTHOR` (default `"<SITE_NAME> Desk"`), announced to Discord
like any other article. Staff can edit or unpublish it afterwards like any
other post.

- **What it's written from:** only what `data/history.db` holds for the last
  `ROUNDUP_DAYS` days -- results, per-player totals, division/points movement, league
  table position, form, and signings/departures. Claude is told to use those
  facts and nothing else, and runs with every tool disabled.
- **Signings** come from `poll.py` diffing EA's member list for our own club
  each hour (`squad_members` / `squad_moves` in `db.py`). The first poll only
  records a baseline, so tracking starts from deploy day; an empty member
  list from EA is ignored rather than read as the whole squad leaving.
- **Category:** "Match Highlight" if there were matches, otherwise
  "Transfer". A stretch with neither is skipped (and the next run tries
  again the following morning).
- **Billing:** it shells out to the Claude Code CLI on a Claude subscription
  (`CLAUDE_CODE_OAUTH_TOKEN`), not API credits. Setup is in
  [`../deploy/README.md`](../deploy/README.md). Preview without publishing
  with `python weekly_article.py --dry-run`.

## Comments and likes

Any signed-in Discord user who's also a member of `DISCORD_GUILD_ID` (see
"Permissions" above) can comment on and like news articles -- not staff-only,
unlike everything else that writes to this site.

- **Comments are plain text**, not rich text -- rendered through Jinja's
  normal HTML auto-escaping, no markup story here (unlike article bodies,
  which go through html_sanitize.py). Capped at 2000 characters.
- **Deleting a comment**: its author can delete their own, and staff can
  delete anyone's (moderation). There's no edit -- delete and re-post is
  the only path, keeping the write surface small.
- **Likes are a simple toggle**, one per (article, Discord user) enforced
  by a database unique constraint -- clicking again un-likes. No "who
  liked this" list, just a count.
- **The count shown is site likes plus Discord reactions**, summed by
  `services.combined_like_count` and used identically on the article page
  and on article thumbnails, so a card and its page never disagree. These
  were once two separate badges, because they do measure different things:
  the site's is a per-member toggle whose identity we know, Discord's is an
  anonymous aggregate. That difference is still honoured where it matters
  -- **the heart's filled/empty state reflects only the viewer's own like**,
  since there's nothing on the Discord side to toggle -- but two numbers
  side by side read as a puzzle rather than a signal, so the figure itself
  is the sum.
- **What inflates that total**: Discord's half counts *every* emoji on the
  announcement, not just hearts, and one person adding three reactions
  counts three times (see `discord_announce.fetch_reaction_count`). It also
  lags by up to the reactions-poll interval. So the number is a reasonable
  measure of engagement, not a headcount.
- **Signed in but not a member** (someone who authorized the site's
  Discord login without being in our server) can still read everything,
  they just see a prompt instead of the comment box, and the like button
  renders as inert text instead of a clickable one.
- Deleting an article deletes its comments and likes with it
  (`services.delete_article`) -- they're plain `article_id` columns, not a
  real foreign key (matching this app's existing no-ORM-relationships
  style), so nothing cascades automatically without that explicit cleanup.
- **Getting people from "not a member" to "member"**: a site-wide banner
  (every page, `base.html`) points anyone who isn't a guild member --
  signed out, or signed in without being in the server -- at
  `DISCORD_INVITE_URL`. The home page also has a standalone "Connect with
  us" button to the same invite, shown to everyone regardless of sign-in
  state. Not a secret, so it's fine to ship a real default in
  `config.py`/`.env.example`; override `DISCORD_INVITE_URL` if the invite
  link ever needs to be regenerated.

## A new season

EA issues a **brand-new club ID every title**. The FC 26 club and the FC 27
club are different clubs to the API even under the same name -- nothing
carries over, not matches, not records, not skill rating. So each new
game means pointing the site at a different club, and the stats collected
for the old one stop meaning anything.

`season.py` does the switch in one command:

```bash
cd /opt/valorlink/proclubs
sudo -u valorlink .venv/bin/python3 season.py find      # search EA for CLUB_NAME
sudo -u valorlink .venv/bin/python3 season.py switch    # take the one exact match
# or, if the search is ambiguous or can't find it yet:
sudo -u valorlink .venv/bin/python3 season.py switch --club-id 1234567
```

`switch` verifies the club against EA, erases the stats history, writes
`CLUB_ID` and `CLUB_PLATFORM` into `.env` (every other line kept as it
was), and prints the restart command. It resolves the club *before*
erasing anything, so if EA can't be reached, nothing changes.

- **It never guesses between clubs.** EA's search is a substring match and
  names aren't unique, so it proceeds on its own only with exactly one
  *exact*-name match. Two clubs called "Yeehaw FC" is a question for a
  human; it lists them and asks for `--club-id`.
- **A brand-new club may not show up in `find` yet.** EA's search runs over
  its all-time leaderboard, which lags behind. `--club-id` looks the club
  up directly and works regardless. The ID is in the URL of the club's page
  on EA's Pro Clubs site.
- **What's erased:** `data/history.db` -- snapshots, matches, per-player
  lines, and the league table built from that season's opponents.
- **What's kept:** everything in `site.db`. News, events and sign-ups,
  squad moves, clips, streamers and the tactics board are the club's own
  content, not the game's. Gamertag links carry over too -- they're EA
  account names, not per-title.
- **The old stats are archived, not destroyed**, into the backups directory
  as `history-<old club id>-<timestamp>.db`, with the command to delete it
  printed alongside. That data can't be re-fetched once EA evicts it, and
  the step most likely to go wrong is picking the club. `--no-archive`
  deletes outright.

`season.py reset` erases the stats without changing the club.

`CLUB_NAME` is the club's name as spelled in-game and is what `find`
searches for. It's separate from `SITE_NAME`, the site's own branding,
which doesn't have to match EA's casing.

## Important caveats about the EA stats dashboard

`/stats` has four reports: an **Overview** (a club scoreboard, headline KPIs,
a skill-rating trend, and a squad spotlight, with cards into the other
three), **Players** (the full roster as player cards, filterable/sortable, click
through for a per-player breakdown -- see "Player cards" below), **Matches** (result/shot/pass/tackle trends, click
a match for a team-vs-team comparison plus both full rosters), and
**Competition** (our own divisional progress and a head-to-head record
against every club we've played).

- The EA API (`proclubs.ea.com/api/fc`) is **not official**. It's the same
  undocumented endpoint the proclubs.ea.com website itself calls, and EA can
  change or break it without notice. It also doesn't distinguish shots on
  target from total shots -- only a total `shots` count is available, so
  that's all this dashboard can show.
- EA does not expose a full league table, or any way to look up another
  club's results -- **Competition**'s "Head-to-Head Record" isn't from EA at
  all; it's aggregated from our own tracked match history (`db.rival_records`),
  built the same way the division/skill-rating trends are (see below). The
  division ladder assumes EA Sports FC Pro Clubs' current 10-division
  structure (undocumented, so treated as a reasonable default, not a fact --
  it extends past 10 automatically rather than truncate if a club's data
  ever reports higher).
- EA's API only returns a rolling window of recent matches and no historical
  division data at all. `proclubs-poll.timer` (see `../deploy/README.md`)
  snapshots our club hourly into `data/history.db` so the skill-rating trend,
  cumulative win rate, head-to-head record, and a player's full-career rating
  trend (in their Players-tab detail drawer) all have something to show
  beyond that rolling window; history only accumulates from whenever polling
  started, never backfilled.

## The site doesn't show a division, on purpose

There is no division anywhere on this site -- not on the home standing
band, not on the stats dashboard, not in `/api/standings`. That's a
deliberate removal, not an oversight:

- `clubs/overallStats` has **no division field at all**. Its `bestDivision`
  and `finishesInDivision*` fields are legacy values that stopped tracking
  reality (a club with 7 promotions reported zero finishes).
- `allTimeLeaderboard/search` does return `currentDivision`, and it's a
  frozen all-time snapshot. Measured against `overallStats` for our own
  club it reported **74 games played and Division 10** while `overallStats`
  reported **185 games** -- 111 matches out of date, and a division the
  club had long since climbed out of. Its `points`, `wins` and
  `promotions` are stale by the same margin. This is what used to be shown,
  and it's why the site claimed Division 10 while the club was in Division 2.
- **Skill rating is the one live standing number**, and it does *not*
  determine the division: promotion and relegation do, and EA publishes no
  rating-to-division thresholds. So there's nothing to derive it from.

That leaves three options -- show a stale number, have someone retype the
real one after every promotion, or show none. The site shows none. Skill
rating leads instead (live, moves with results, and what the league table
already ranks on), backed by the season record and win rate.

If EA ever exposes a real one, `app._standing_teaser` and `/api/standings`
are the two places to put it back.

`ea_client.division_stats()` still exists, with a docstring carrying all of
the above, because `poll.py` uses its tier to bucket the league table (see
below). Don't reach for it for anything the site presents as "now."

## The league table (auto-built, not manually curated)

`/league` shows every club we've actually played that's currently in the
same division as us, ranked by **skill rating** -- games played, skill
rating, squad size, and a last-5-results form strip per row. See `db.py`'s
`league_table()`, `sync_league_roster()`, and `known_opponents()`.

- **Why rating and not points.** Points only ever accumulate, so they
  measure how much a club has played as much as how well: a club grinding
  twice as many matches outranks a better one that played fewer. This
  table's members have wildly different match counts by construction (they
  join it whenever we happen to face them), which makes points a
  particularly bad sort here. Skill rating is EA's own strength number and
  moves both ways. Points are still recorded and still break rating ties;
  a club whose snapshot has no rating sorts last, since unknown strength
  isn't zero strength.

- **There's no roster to maintain.** EA's API has no region/league concept
  to query (see the caveats above -- it can't even list every club in a
  division, let alone a community-defined group like "NA East 2"), so
  instead of a hand-edited file the table builds itself from real match
  history: `record_matches()` already sees each opponent's real club ID
  inside the raw match payload (`db.py`'s `matches.opp_club_id` column),
  it's just never been persisted before this. Every poll, any newly-seen
  opponent gets folded into the table.
- **Capped at `LEAGUE_TABLE_MAX_TEAMS`** (default 25, `.env`-configurable)
  so poll runtime and EA API load stay bounded no matter how many
  different clubs get faced over a season. Our own club is pinned and
  never evicted; once full, a newly-discovered opponent replaces whichever
  non-pinned member currently has the lowest skill rating in its own latest
  snapshot -- the same ranking the table displays by, so the club dropped is
  the one shown at the bottom. See `sync_league_roster()`'s docstring for
  the exact tie-break rules.
- **Every club in the table gets polled like our own club does** --
  `poll.py`'s `sync_and_poll_league_table()` runs `poll_club()` (division,
  rating, points, matches, and squad size via `members/stats`) against each
  league-table member after syncing membership, so its own "last 5" form
  and rating are real, current data, not just our record against them
  (that's still `rival_records()`, a different report on the Competition
  tab). This roughly triples-plus the number of EA API calls a poll run
  makes once the table has real members -- accepted cost of the 25-team
  cap, tune `LEAGUE_TABLE_MAX_TEAMS` down if that's too much load.
- **The tier used to group the table is stale, and that's tolerable.**
  Every club's division here comes from the same all-time leaderboard
  snapshot described above, so it lags live play badly. It's compared
  stale-against-stale, which is self-consistent enough to bucket a
  comparable set of opponents, and every number actually shown in the table
  (rating, record, form) is current. The page says so rather than implying
  the tier is where anyone sits right now.
- **"Same division" is the closest available proxy for a real bracket.**
  EA's division number is a skill tier that moves independently per club
  (see above), not a fixed league assignment -- filtering the table to
  "whoever's currently in our division" is an approximation, not a
  guarantee those clubs are in the same actual competition as us.
- **No retroactive backfill.** A match recorded before `opp_club_id`
  existed doesn't have one and is excluded from opponent discovery --
  coverage starts building from whenever this shipped, same limitation as
  every other tracked-history feature in this app.

## Project layout

```
proclubs/
  app.py               FastAPI routes: pages, staff CRUD forms, /api/* stats proxy
  auth.py               Discord OAuth2, staff-role gating, CSRF helpers
  config.py             All env-var configuration
  database.py            SQLAlchemy engine/session for the site's own content DB
  models.py              Article / Event / Streamer
  services.py            CRUD + validation for articles/events/streamers
  html_sanitize.py       Sanitizes the rich-text editor's HTML before it's stored
  twitch_client.py       Twitch Helix: is-this-channel-live, with a short cache
  discord_roster.py      Guild member list (with avatars) + squad-move announcements
  ea_client.py           EA Pro Clubs API client (curl_cffi, unrelated to the above)
  db.py                  Locally-accumulated EA stats history (own sqlite3 file)
  poll.py                Standalone poller for db.py, run by proclubs-poll.timer
  season.py              New-season switch: find the club on EA, erase old stats
  squad.py               Squad screen rules: depth by position, playing-time flags
  navigation.py          Sections and tabs: the sidebar, tab bars and shortcut URLs
  tracked_clubs.json     Extra clubs for poll.py to snapshot (ours comes from .env)
  templates/             Jinja2 templates
  static/css/site.css    Design system (also read by charts.js as CSS vars)
  static/js/app.js       Stats dashboard UI (fetches /api/*)
  static/js/charts.js    Dependency-free SVG charts
  static/js/article-editor.js  Wires up the Quill rich-text article editor
  static/vendor/quill/   Vendored Quill 2.x build (no CDN dependency -- see its README)
  tests/                 pytest suite
```

## Pages

| Path | Who | What |
|---|---|---|
| `/` | everyone | Dashboard: next match, club standing, news, squad moves, transfers, who's live |
| `/news`, `/news/<slug>` | everyone (drafts: staff only) | Article list/detail |
| `/news/new`, `/news/<slug>/edit` | staff | Article form: rich-text (WYSIWYG) editor + optional cover image |
| `/events` | everyone | Upcoming + past events -- read-only, see below |
| `/streamers` (Media › Live) | everyone | Featured channel (embedded player) + the rest of the showcase, live status from Twitch |
| `/stats` | everyone | EA stats dashboard for our club |
| `/league` | everyone | Auto-built league table -- see below |
| `/tactics` | everyone (editing: staff only) | Drag-and-drop formation board -- see below |
| `/squad` (Squad › Overview) | staff | Squad screen: contracts beside appearances, form and attendance, plus depth for the current formation -- see below |
| `/roster` (Squad › Moves & contracts) | staff | pick a Discord member, offer a contract or staff role, or publish a departure -- see below |
| `/login`, `/logout` | everyone | Discord sign-in / dev sign-in |

`/api/overview`, `/api/standings`, `/api/members`, `/api/matches`,
`/api/history/division`, `/api/history/matches`, `/api/history/players`, and
`/api/streamers/live` back the `/stats` page's JS and are not meant to be
called directly, though they're unauthenticated (read-only, no secrets).
`/api/tactics` is the one write endpoint in this list -- staff-only, CSRF-
protected, see below.

## Player cards

The Players tab renders the roster as cards rather than the eight-column
table it used to be (`playerCardHtml` in `static/js/app.js`, `.player-card`
in `site.css`).

- **Not an EA Ultimate Team card.** That layout is EA's own branded
  design; this is the same job -- identity, rating, a few numbers -- done
  in this site's own design language: an OVR block, a name bar, three
  stat cells.
- **Every value is a real API field.** `proOverall`, `favoritePosition`,
  `ratingAve`, `winRate`, `manOfTheMatch`, `cleanSheetsGK`, `gamesPlayed`,
  `goals`, `assists` -- all straight from `/api/members`. There is
  deliberately no pace/dribbling/passing attribute row: EA's Pro Clubs API
  doesn't expose per-attribute ratings, and inventing or modelling them
  would make the card lie. A missing `proOverall` shows `--`, not a zero.
- **Tier colour is derived, not assigned.** `playerTier()` maps
  `proOverall` to elite (amber, `>= TIER_ELITE`), squad (green,
  `>= TIER_SQUAD`) or rotation (steel). It keeps itself current as ratings
  move, and the thresholds are a judgement call about what should feel
  rare -- if most of the roster comes out amber, raise `TIER_ELITE`.
  Rotation is deliberately unglamorous but never punitive: no red, no
  downward arrows.
- **Keepers get a different third stat** -- clean sheets instead of win
  rate, since the outfield framing says nothing useful about them.
- **There's no player photography** in Pro Clubs to draw on, so the
  oversized position code (`.pc-watermark`, 5.5% white) is what gives each
  card its own silhouette.
- The card is a `<button>`, so Enter/Space activation and focus come for
  free -- unlike the table rows it replaced, which needed an explicit
  `tabindex` and keydown handler. Clicking one opens the same full
  breakdown as before, now as a full-width drawer spanning the grid
  (`.member-detail-row`).

## The tactics board

`/tactics` is a drag-and-drop formation board: staff drag names from the
live EA roster (the same `/api/members` the Players tab uses) onto pitch
slots, then hit Save. Everyone else sees the saved result, read-only. See
`app.py`'s `FORMATIONS` dict, `services.py`'s tactics functions, and
`static/js/tactics.js`.

- **Fully manual placement, no Discord-role automation.** A formation slot
  can hold exactly one person; two people sharing a broad role (e.g. both
  tagged "Midfielder" in Discord) can't be resolved into "who plays CM vs
  CDM" without a human decision, so staff makes that call directly by
  dragging rather than the site guessing from roles or stats.
- **The formation list is FC 26's.** All 20 shapes were taken from FC 26
  and have *not* been re-checked against FC 27 (released 25 Sep 2026) --
  EA hadn't published its formation list when this shipped. If FC 27 adds,
  drops or renames a shape, `FORMATIONS` in `app.py` and
  `test_formations_cover_all_fc26_shapes` both need updating; nothing
  breaks in the meantime, the board just offers last year's set.
- **Several common formations, each remembered independently.** Switching
  the formation dropdown doesn't discard what's set up for the others --
  each (formation, slot) pair is its own saved row (`TacticsSlot`), so
  4-3-3 and 4-4-2 can both have a saved lineup at the same time. Whichever
  formation was active at last save is what loads by default.
- **Player names are free text, not linked to any roster row.** There's no
  local "players" table to foreign-key against -- the bench list is
  populated live from EA on page load, but what actually gets saved is
  just the name string that was dragged. A typo'd or since-renamed name
  doesn't break anything, it just won't highlight against the current
  bench.
- **Whole-board save, not per-drag.** Staff can rearrange several names
  before saving; "Save Lineup" sends the entire slot map for the current
  formation in one request (`POST /api/tactics`, staff-only + CSRF), which
  replaces that formation's saved slots outright -- a slot missing from
  the request is now empty, not left over from before.
- **Plain HTML5 drag-and-drop**, no external library, matching this app's
  "no external chart library" precedent in `charts.js`. Click a filled
  slot to clear it -- the fallback for touch devices without real drag
  support.
