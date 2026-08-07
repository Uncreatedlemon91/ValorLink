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

Two tiers, both derived live from Discord roles at sign-in time (never
stored): everyone -- including signed-out visitors -- can read the site.
**Staff** -- holders of `DISCORD_STAFF_ROLE_ID` in `DISCORD_GUILD_ID` -- can
write articles, manage events, and manage the streamer list. A role change
in Discord takes effect on that person's next sign-in.

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
  Events sync, below. Copy `DISCORD_BOT_TOKEN` from `/opt/valorlink/.env`
  (the main bot's own token) rather than registering a separate bot -- an
  accepted exception to this app's usual isolation, see above.

Any of Discord OAuth, Twitch, or the Discord Events sync can be left
unconfigured -- the site degrades gracefully (sign-in shows "not
configured," the streamer showcase shows profiles without live status, no
events get auto-created) rather than erroring.

## Discord Scheduled Events sync

One-directional: create an event in Discord (Server → Events → New Event)
and it shows up as a site fixture automatically, no site action needed.
It is **not** the reverse -- creating a fixture on the site does not
create a Discord event.

- Runs on a schedule (`proclubs-discord-events-poll.timer`, every 10
  minutes -- see `../deploy/README.md`), not instantly on creation. This
  app has no always-on bot/gateway connection, so polling Discord's REST
  API is the only way to notice a change made there.
- Discord is the source of truth for **title, description, and
  date/time** on synced events -- each sync overwrites them from Discord.
  **Type** (Match/Scrim/Tournament/Community), **opponent**, and
  **result** are site-only fields Discord has no equivalent for; they're
  set to a sensible default on first sync and never touched again, so
  staff can fill them in on the site without the next sync reverting them.
- If a Discord event is canceled or deleted, its mirrored site fixture is
  removed on the next sync too (as long as it's still in the future --
  past fixtures are left alone even if their Discord event ages out of
  Discord's own list).
- Synced events show a "Discord" pill next to the event type badge, and
  the edit form explains what will and won't get overwritten.

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
  markdown_render.py    Article Markdown -> sanitized HTML
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
  tests/                 pytest suite
```

## Pages

| Path | Who | What |
|---|---|---|
| `/` | everyone | Hero, latest news, next event, featured live stream, stats teaser |
| `/news`, `/news/<slug>` | everyone (drafts: staff only) | Article list/detail |
| `/news/new`, `/news/<slug>/edit` | staff | Article form (Markdown + optional cover image) |
| `/events` | everyone | Upcoming + past events |
| `/events/new`, `/events/<id>/edit` | staff | Event form |
| `/streamers` (nav label: "Live") | everyone | Featured channel (embedded player) + the rest of the showcase, live status from Twitch |
| `/stats` | everyone | EA stats dashboard for our club |
| `/login`, `/logout` | everyone | Discord sign-in / dev sign-in |

`/api/overview`, `/api/standings`, `/api/members`, `/api/matches`,
`/api/history/division`, `/api/history/matches`, `/api/history/players`, and
`/api/streamers/live` back the `/stats` page's JS and are not meant to be
called directly, though they're unauthenticated (read-only, no secrets).
