# Pr1mE6ers — Pro Clubs team site

The team's public home: news/blog articles, an events calendar, a live
Twitch streamer showcase, and an EA Pro Clubs stats dashboard locked to our
own club. FastAPI + Jinja2 + SQLAlchemy, matching the main ValorLink
platform's stack.

This runs as its own independent service alongside the ValorLink bot/web app
-- separate venv, separate systemd unit, separate subdomain
(`proclubs.apps.valorlink.co`), separate `.env`, no shared database or code
with `web/`, `db/`, `tenancy/`, or `utils/`. See
[`../deploy/README.md`](../deploy/README.md) for the production deploy steps.

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
- **Discord OAuth2** -- register a new application at
  [discord.com/developers/applications](https://discord.com/developers/applications)
  (separate from the main ValorLink bot's app). Add an OAuth2 redirect
  matching `DISCORD_OAUTH_REDIRECT`, and copy the client ID/secret. Then
  find the team's guild ID and the staff role's ID (enable Developer Mode
  in Discord, right-click the server/role, "Copy ID").
- **Twitch** -- register a free app at
  [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps). This
  site only uses the app-level client-credentials grant to check "is this
  channel live," never a user login, so any redirect URL satisfies
  registration.
- **`SESSION_SECRET`** -- a long random string (`openssl rand -hex 32`).
  Signs the session cookie; rotating it signs everyone out.

Any of Discord OAuth or Twitch can be left unconfigured -- the site
degrades gracefully (sign-in shows "not configured," the streamer showcase
shows profiles without live status) rather than erroring.

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
| `/` | everyone | Hero, latest news, next event, live streamers, stats teaser |
| `/news`, `/news/<slug>` | everyone (drafts: staff only) | Article list/detail |
| `/news/new`, `/news/<slug>/edit` | staff | Article form (Markdown + optional cover image) |
| `/events` | everyone | Upcoming + past events |
| `/events/new`, `/events/<id>/edit` | staff | Event form |
| `/streamers` | everyone | Showcase, live status from Twitch |
| `/stats` | everyone | EA stats dashboard for our club |
| `/login`, `/logout` | everyone | Discord sign-in / dev sign-in |

`/api/overview`, `/api/standings`, `/api/members`, `/api/matches`,
`/api/history/division`, `/api/history/matches`, `/api/history/players`, and
`/api/streamers/live` back the `/stats` page's JS and are not meant to be
called directly, though they're unauthenticated (read-only, no secrets).
