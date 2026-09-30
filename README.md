# YeeHaw FC

The club's website and Discord integration for EA Sports FC 27 Pro Clubs.

Everything lives in **[`proclubs/`](proclubs/)** — a FastAPI + Jinja2 +
SQLAlchemy app serving [yeehaw-fc.club](https://yeehaw-fc.club): news,
fixtures and sign-ups, squad moves, a clips page, a Twitch showcase, a
tactics board, and an EA Pro Clubs stats dashboard locked to our own club.

There is no separate bot process. Every Discord feature runs from the web
app plus a handful of systemd timers, over Discord's REST API and a signed
interactions webhook — no gateway connection, nothing to keep online.

```
proclubs/      the application  — see proclubs/README.md
deploy/        systemd units, Caddy config, install/backup/restore
design-fc27/   the broadcast design canvas the current look came from
```

## What it does

- **News** — rich-text articles with cover images, comments and likes,
  announced to Discord on publish; reactions there count toward the
  article's like total.
- **Events** — fixtures created on the site, posted to Discord as a staged
  private thread that widens its audience as kick-off approaches. Players
  pick a position from the site or from the Discord post; both stay in
  step. Times render in each viewer's own timezone.
- **Squad moves** — pick a member out of the Discord server, offer them a
  position or announce a departure. An offer carries a contract length in
  weeks and a squad status (Starter / Rotation / Reserve), with Accept /
  Decline buttons only that player can press; accepting adds the squad
  role, and staff then confirm the signing to publish the announcement and
  start the contract. Expired contracts are flagged for staff to renew
  (the player accepts again) or release.
- **Stats** — the club's EA figures, with a locally-accumulated history
  (EA only exposes a rolling window, so the trend data has to be collected
  over time).
- **Clips**, **Live** (Twitch) and a drag-and-drop **tactics board**.

## Running it

Local development and every setting are documented in
**[`proclubs/README.md`](proclubs/README.md)**; production deployment in
**[`deploy/README.md`](deploy/README.md)**.

```bash
cd proclubs
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # fill it in
.venv/bin/python3 -m uvicorn app:app --reload
```

Tests:

```bash
cd proclubs && python3 -m pytest tests/ -q
```

## A note on names

The deployment still lives at `/opt/valorlink` and runs as a `valorlink`
system user. That is the leftover of a different project this repo used to
hold, kept deliberately: renaming a live path and uid buys nothing and
risks an outage. The code, the units and the domain are all YeeHaw FC.

Unofficial fan-run project. Not affiliated with EA.
