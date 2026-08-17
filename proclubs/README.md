# YeeHaw FC — Pro Clubs team site

The team's public home: news/blog articles, an events calendar, a live
Twitch streamer showcase, and an EA Pro Clubs stats dashboard locked to our
own club. FastAPI + Jinja2 + SQLAlchemy, matching the main ValorLink
platform's stack.

This runs as its own independent service alongside the ValorLink bot/web app
-- separate venv, separate systemd unit, separate domain
(`yeehaw-fc.club`), separate `.env`, no shared database or code
with `web/`, `db/`, `tenancy/`, or `utils/`. See
[`../deploy/README.md`](../deploy/README.md) for the production deploy steps.

One deliberate exception: if `DISCORD_BOT_TOKEN` is set (for event
announcements and the Discord syncs below), it's a copy of the main bot's
actual token,
not a separately-registered credential. Everything else -- database,
session, OAuth client -- stays as described above.

## Permissions

Three tiers, all derived live from Discord at sign-in time (never stored):
everyone -- including signed-out visitors -- can read the site. Anyone
signed in with Discord **and a member of `DISCORD_GUILD_ID`** can comment
on and like news articles, staff or not -- see "Comments and likes" below.
**Staff** -- holders of `DISCORD_STAFF_ROLE_ID` in `DISCORD_GUILD_ID` -- can
write articles, create and edit events, mark attendance, and manage the
streamer list (and are always members too, since holding a guild role
requires being in the guild). A role change in Discord takes effect on that
person's next sign-in. Members who aren't staff can still sign up for
events -- see "Events and sign-ups" below.

Signing in with Discord doesn't by itself mean membership: OAuth just
proves "this is a real Discord account," and anyone can authorize the
app's login regardless of what servers they're in. Guild membership -- the
comment/like gate -- is a second, separate check against
`DISCORD_GUILD_ID` made during sign-in (`auth.py`'s OAuth callback already
had to fetch this to determine staff roles; it's the same lookup).

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

- **Our club** (`CLUB_PLATFORM` / `CLUB_ID`) -- this site shows one club's
  stats, configured once, not a search box. `tracked_clubs.json` (used by
  the history poller, see below) should reference the same club.
- **Discord OAuth2** -- reuses the same Discord application as the main
  ValorLink bot/web app; a Discord app supports multiple OAuth2 redirect
  URIs, so no second app is needed. On that existing application's OAuth2
  page at [discord.com/developers/applications](https://discord.com/developers/applications),
  add a redirect matching `DISCORD_OAUTH_REDIRECT`, then copy the same
  client ID/secret into this app's `.env`. (Sharing the OAuth app is just
  sharing an identity provider -- the `.env`, session, and database stay
  separate.) Then find the team's guild ID and the staff role's ID (enable
  Developer Mode in Discord, right-click the server/role, "Copy ID") --
  the role doesn't have to be the same one ValorLink treats as officer.
- **Twitch** -- register a free app at
  [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps). This
  site only uses the app-level client-credentials grant to check "is this
  channel live," never a user login, so any redirect URL satisfies
  registration. "Live" also means *playing our game* -- see
  `TWITCH_GAME_FILTER` in `.env.example`; a roster member streaming
  something else doesn't show up as live here. Its default
  (`EA Sports FC 26`) is Twitch's actual category name as of this writing,
  not a guess to double-check, but it does change with each yearly title.
- **`SESSION_SECRET`** -- a long random string (`openssl rand -hex 32`).
  Signs the session cookie; rotating it signs everyone out.
- **`DISCORD_BOT_TOKEN`** (optional) -- enables the Discord Scheduled
  Events sync and the Clips sync, below. Copy `DISCORD_BOT_TOKEN` from
  `/opt/valorlink/.env` (the main bot's own token) rather than registering
  a separate bot -- an accepted exception to this app's usual isolation,
  see above.
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
which is also what events mirrored in from Discord's Events tab get.

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

Events created in Discord's own **Events** tab still mirror in on a timer
(`proclubs-discord-events-poll.timer`) -- see below. That path is now one
way of getting an event in, not the only one.

### Mirrored Discord Scheduled Events

- Runs on a schedule (`proclubs-discord-events-poll.timer`, every 10
  minutes -- see `../deploy/README.md`), not instantly on creation. This
  app has no always-on bot/gateway connection, so polling Discord's REST
  API is the only way to notice a change made there.
- Discord is the source of truth for **title, description, and
  date/time** on every synced event -- each sync overwrites them.
  **Type** (Match/Scrim/Tournament/Community), **opponent**, and
  **result** are site-only fields Discord has no equivalent for; they're
  set to a sensible default (Type: Match, no opponent, no result) on
  first sync and never touched again by later syncs -- so a mirrored event
  keeps whatever staff set for those on the site, and only its title,
  description, and time follow Discord.
- If a Discord event is canceled or deleted, its mirrored site fixture is
  removed on the next sync too (as long as it's still in the future --
  past fixtures are left alone even if their Discord event ages out of
  Discord's own list).
- Synced events show a "Discord" pill next to the event type badge.

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
to several different shapes across the site -- a short wide banner in the
home hero, a 21:9 header on the article page, ~4:3/16:9 card thumbnails in
the news rail/grid -- and a plain center crop often cuts off the part that
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
announcement forever isn't useful). Shown on the article page next to the
site's own Like button, but kept visually and functionally separate from
it -- the site's Like button tracks a signed-in member's own toggle state,
while the Discord count is just a read-only aggregate with no identity
behind it, so merging them into one number would misrepresent both. An
article with no reactions (or that was never announced) shows no badge at
all, same "don't show a zero" pattern as the engagement badges on article
thumbnails.

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

## Important caveats about the EA stats dashboard

`/stats` has four reports: an **Overview** (a club scoreboard, headline KPIs,
a skill-rating trend, and a squad spotlight, with cards into the other
three), **Players** (the full roster, filterable/sortable, click through for
a per-player breakdown), **Matches** (result/shot/pass/tackle trends, click
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

## The league table (auto-built, not manually curated)

`/league` shows every club we've actually played that's currently in the
same division as us, sorted by points -- games played, points, squad size,
and a last-5-results form strip per row. See `db.py`'s `league_table()`,
`sync_league_roster()`, and `known_opponents()`.

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
  non-pinned member currently has the fewest points in its own latest
  snapshot -- see `sync_league_roster()`'s docstring for the exact tie-break
  rules.
- **Every club in the table gets polled like our own club does** --
  `poll.py`'s `sync_and_poll_league_table()` runs `poll_club()` (division,
  points, matches, and now squad size via `members/stats`) against each
  league-table member after syncing membership, so its own "last 5" form
  and points are real, current data, not just our record against them
  (that's still `rival_records()`, a different report on the Competition
  tab). This roughly triples-plus the number of EA API calls a poll run
  makes once the table has real members -- accepted cost of the 25-team
  cap, tune `LEAGUE_TABLE_MAX_TEAMS` down if that's too much load.
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
  discord_events.py      Discord Scheduled Events REST client (read-only)
  discord_events_poll.py Standalone poller, mirrors Discord events -> Event rows
  ea_client.py           EA Pro Clubs API client (curl_cffi, unrelated to the above)
  db.py                  Locally-accumulated EA stats history (own sqlite3 file)
  poll.py                Standalone poller for db.py, run by proclubs-poll.timer
  tracked_clubs.json     Clubs poll.py snapshots (just ours, normally)
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
| `/` | everyone | Hero, latest news, next event, featured live stream, stats teaser |
| `/news`, `/news/<slug>` | everyone (drafts: staff only) | Article list/detail |
| `/news/new`, `/news/<slug>/edit` | staff | Article form: rich-text (WYSIWYG) editor + optional cover image |
| `/events` | everyone | Upcoming + past events -- read-only, see below |
| `/streamers` (nav label: "Live") | everyone | Featured channel (embedded player) + the rest of the showcase, live status from Twitch |
| `/stats` | everyone | EA stats dashboard for our club |
| `/league` | everyone | Auto-built league table -- see below |
| `/tactics` | everyone (editing: staff only) | Drag-and-drop formation board -- see below |
| `/login`, `/logout` | everyone | Discord sign-in / dev sign-in |

`/api/overview`, `/api/standings`, `/api/members`, `/api/matches`,
`/api/history/division`, `/api/history/matches`, `/api/history/players`, and
`/api/streamers/live` back the `/stats` page's JS and are not meant to be
called directly, though they're unauthenticated (read-only, no secrets).
`/api/tactics` is the one write endpoint in this list -- staff-only, CSRF-
protected, see below.

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
