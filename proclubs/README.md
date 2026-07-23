# Pro Clubs Tracker

A Flask site that pulls club data from EA's **unofficial** Pro Clubs API
(`proclubs.ea.com/api/fc`) and displays it in a simple dashboard: club
overview, recent matches, member stats, and divisional standings.

This runs as its own independent service alongside the ValorLink bot/web app
-- separate venv, separate systemd unit, separate subdomain
(`proclubs.valorlink.co`), no shared database or code. See
[`../deploy/README.md`](../deploy/README.md) for the production deploy steps.

## Important caveats

- This is **not** an official EA API. It's the same undocumented endpoint the
  proclubs.ea.com website itself calls. EA can change or break it at any time
  without notice (it has gone down before -- see EA forum threads about
  outages).
- EA does not expose a full league table. "Standings" here means your own
  club's divisional progress (current division, promotions/relegations,
  skill rating) -- not a table of other clubs.
- The `clubs/matches` response shape isn't documented anywhere public, so the
  match list renderer is best-effort; if EA's field names don't match what's
  expected, it falls back to showing the raw JSON so you can see what came
  back.

## Local dev

```bash
cd proclubs
pip install -r requirements.txt
python app.py
```

Then open http://localhost:5000

## Platforms

EA groups platforms into a few buckets rather than one per console:

| Value          | Covers                    |
|----------------|---------------------------|
| `common-gen5`  | PC, PS5, Xbox Series X/S  |
| `common-gen4`  | PS4, Xbox One             |
| `nx`           | Legacy value, rarely used |

Since your club has members on PC and consoles, `common-gen5` is the right
choice for anyone on PC or current-gen -- Pro Clubs crossplay pools those
together under one platform bucket. Members still on PS4/Xbox One would show
up under `common-gen4` instead if the club also has a presence registered
there.

## Project layout

```
proclubs/
  app.py         Flask routes
  ea_client.py   EA API client (requests, caching, error handling)
  templates/index.html
  static/css/style.css
  static/js/app.js
  static/js/charts.js
```

## API endpoints (this app)

- `GET /api/clubs/search?name=&platform=`
- `GET /api/clubs/<club_id>/overview?platform=`
- `GET /api/clubs/<club_id>/standings?platform=`
- `GET /api/clubs/<club_id>/members?platform=`
- `GET /api/clubs/<club_id>/matches?platform=&matchType=leagueMatch|playoffMatch|friendlyMatch`
