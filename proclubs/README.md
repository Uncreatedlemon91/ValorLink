# YeeHaw FC — Pro Clubs team site

The team's public home: news/blog articles, an events calendar, a live
Twitch streamer showcase, and an EA Pro Clubs stats dashboard locked to our
own club. FastAPI + Jinja2 + SQLAlchemy, matching the main ValorLink
platform's stack.

This runs as its own independent service alongside the ValorLink bot/web app
-- separate venv, separate systemd unit, separate subdomain
(`proclubs.apps.valorlink.co`), separate `.env`, no shared database or code
with `web/`, `db/`, `tenancy/`, or `utils/`. See
[`../deploy/README.md`](../deploy/README.md) for the production deploy steps.

One deliberate exception: if `DISCORD_BOT_TOKEN` is set (for the Discord
Scheduled Events sync, below), it's a copy of the main bot's actual token,
not a separately-registered credential. Everything else -- database,
session, OAuth client -- stays as described above.

## Permissions

Three tiers, all derived live from Discord at sign-in time (never stored):
everyone -- including signed-out visitors -- can read the site. Anyone
signed in with Discord **and a member of `DISCORD_GUILD_ID`** can comment
on and like news articles, staff or not -- see "Comments and likes" below.
**Staff** -- holders of `DISCORD_STAFF_ROLE_ID` in `DISCORD_GUILD_ID` -- can
write articles and manage the streamer list (and are always members too,
since holding a guild role requires being in the guild). A role change in
Discord takes effect on that person's next sign-in. Events are the one
exception: there's no staff editing UI for them at all -- see "Events are
Discord-only" below.

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
  registration.
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

## Events are Discord-only

`/events` is **read-only** on the site -- there is no create/edit/delete
UI, for anyone, staff included. Events exist only via the one-directional
Discord Scheduled Events sync: create an event in Discord (Server →
Events → New Event) and it shows up as a site fixture automatically. It
is **not** the reverse -- there's no way to create a fixture from the
site that pushes back to Discord.

- Runs on a schedule (`proclubs-discord-events-poll.timer`, every 10
  minutes -- see `../deploy/README.md`), not instantly on creation. This
  app has no always-on bot/gateway connection, so polling Discord's REST
  API is the only way to notice a change made there.
- Discord is the source of truth for **title, description, and
  date/time** on every synced event -- each sync overwrites them.
  **Type** (Match/Scrim/Tournament/Community), **opponent**, and
  **result** are site-only fields Discord has no equivalent for; they're
  set to a sensible default (Type: Match, no opponent, no result) on
  first sync and never touched again by later syncs -- but since there's
  no site UI to change them either, they stay at that default unless set
  by hand directly in the database.
- If a Discord event is canceled or deleted, its mirrored site fixture is
  removed on the next sync too (as long as it's still in the future --
  past fixtures are left alone even if their Discord event ages out of
  Discord's own list).
- Synced events show a "Discord" pill next to the event type badge.

## Clips are Discord-only

`/clips` is **read-only** too, same idea as events -- no upload UI on the
site. Post a video directly in the configured Discord channel (an actual
file attachment, not a link) and it shows up on the site's Clips page
automatically.

- **Scope, deliberately narrow:** only video *files* uploaded straight to
  Discord (`content_type` starting `video/`) are picked up. A pasted
  YouTube/Twitch/Streamable link shows up in Discord as a rich embed, not
  a file attachment, and isn't turned into a clip here -- reliably
  converting an arbitrary link into an embeddable player is a bigger job
  than this first pass covers. If a message has more than one video
  attached, only the first is used.
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
  supports beyond that -- text color, fonts, alignment, embedded video --
  are left off rather than offered and then silently stripped on save.
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

- The EA API (`proclubs.ea.com/api/fc`) is **not official**. It's the same
  undocumented endpoint the proclubs.ea.com website itself calls, and EA can
  change or break it without notice.
- EA does not expose a full league table. "Standings" means our own club's
  divisional progress (current division, promotions/relegations, skill
  rating) -- not a table of other clubs.
- EA's API only returns a rolling window of recent matches and no historical
  division data at all. `proclubs-poll.timer` (see `../deploy/README.md`)
  snapshots our club hourly into `data/history.db` so the "History" tab has
  something to show beyond that window; history only accumulates from
  whenever polling started.

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
| `/login`, `/logout` | everyone | Discord sign-in / dev sign-in |

`/api/overview`, `/api/standings`, `/api/members`, `/api/matches`,
`/api/history/division`, `/api/history/matches`, `/api/history/players`, and
`/api/streamers/live` back the `/stats` page's JS and are not meant to be
called directly, though they're unauthenticated (read-only, no secrets).
