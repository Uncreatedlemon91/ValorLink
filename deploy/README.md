# Hosting YeeHaw FC on a DigitalOcean droplet

One FastAPI app behind Caddy, plus a handful of oneshot systemd timers for
the things that need polling. No always-on bot process — every Discord
feature runs over REST and a signed interactions webhook.

| File | What it is |
|---|---|
| `yeehaw-fc.service` | the site (gunicorn + uvicorn workers on 127.0.0.1:8001) |
| `yeehaw-fc-backup.{service,timer}` | daily database backup |
| `proclubs-poll.{service,timer}` | hourly EA stats snapshot → `history.db`, and the league table |
| `proclubs-discord-events-poll.*` | Discord Scheduled Events → site fixtures, every 10 min |
| `proclubs-clips-poll.*` | Discord video posts → Clips page, every 30 min |
| `proclubs-reactions-poll.*` | reaction counts on article announcements, every 30 min |
| `proclubs-event-invites-poll.*` | staged event-thread invites, every 10 min |
| `proclubs-notify-poll.*` | match-week messages every 10 min: reminders, the post-match vote, availability nudges, milestones, Player of the Month |
| `Caddyfile` | reverse proxy + automatic HTTPS |
| `install.sh` | copies the units in, enables and starts them |
| `backup.sh` / `restore.sh` | database snapshot and restore |

The app runs as a `valorlink` system user out of `/opt/valorlink`. That
name is a leftover from a different project this repo used to hold; it is
kept on purpose, because renaming a live path and uid buys nothing and
risks an outage.

---

## 1. Create the droplet

DigitalOcean control panel → **Create → Droplets**.

- **Image:** Ubuntu 24.04 (LTS)
- **Type:** Basic → Regular. The **$6/mo** (1 GB RAM) size is comfortable;
  the $4/mo (512 MB) works but leaves little headroom for updates.
- **Authentication:** add your SSH key (not a password).
- Create it, and note the public IP.

## 2. Point the domain at it

Two DNS **A records**, both at the droplet's IP:

- `yeehaw-fc.club`
- `www.yeehaw-fc.club` — Caddy redirects www to the apex, but still needs
  the record to get a certificate for it.

The apex is the canonical host: `SITE_BASE_URL` and the Discord OAuth
redirect URI both name it, and Discord rejects a callback whose host
doesn't match the registered redirect exactly.

## 3. First login and firewall

```bash
ssh root@YOUR_DROPLET_IP

ufw allow OpenSSH
ufw allow 80
ufw allow 443
ufw --force enable
```

## 4. System packages

```bash
apt update && apt upgrade -y
apt install -y python3-venv python3-pip git sqlite3

# Caddy (official apt repo)
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
```

`sqlite3` is needed by `backup.sh`, which uses SQLite's online `.backup`.

## 5. App user and clone

```bash
# Clone first, then create the service user and hand it ownership.
git clone https://github.com/Uncreatedlemon91/ValorLink.git /opt/valorlink
useradd --system --home-dir /opt/valorlink --shell /usr/sbin/nologin valorlink
chown -R valorlink:valorlink /opt/valorlink
```

## 6. Virtualenv and dependencies

```bash
cd /opt/valorlink
sudo -u valorlink python3 -m venv proclubs/.venv
sudo -u valorlink proclubs/.venv/bin/pip install --upgrade pip
sudo -u valorlink proclubs/.venv/bin/pip install -r proclubs/requirements.txt
```

## 7. Configure

```bash
sudo -u valorlink cp proclubs/.env.example proclubs/.env
# A session secret to paste in:
openssl rand -hex 32
sudo -u valorlink nano proclubs/.env
chmod 600 proclubs/.env
```

`proclubs/.env.example` documents every setting inline.
`proclubs/README.md#configuring-a-fresh-deployment` explains where each
value comes from. The minimum for a working site is `SESSION_SECRET`,
`HTTPS_ONLY=1`, `SITE_BASE_URL`, the four Discord OAuth values, and the
club — which you don't type in. Look it up on EA and write it to `.env` in
one step:

```bash
cd /opt/valorlink/proclubs
sudo -u valorlink .venv/bin/python3 season.py find
sudo -u valorlink .venv/bin/python3 season.py switch
```

(See `proclubs/README.md#a-new-season`; the same command handles every
later season change.) Everything else switches on an optional feature.

The database needs no setup — it is created on first start.

## 8. Start it

```bash
sudo bash deploy/install.sh
```

This copies every unit into `/etc/systemd/system`, reloads systemd, and
enables and starts them. It also retires the old `valorlink-*` units if
the box still has them (see **Updating**, below).

## 9. Point Caddy at it

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Caddy fetches and renews the certificates itself. DNS has to resolve to
the droplet first, so if the first attempt fails, give it a few minutes
and reload again.

Visit `https://yeehaw-fc.club`, sign in with Discord, and check:

```bash
systemctl status yeehaw-fc
journalctl -u yeehaw-fc -f
```

---

## Updating

```bash
cd /opt/valorlink
sudo -u valorlink git pull
sudo -u valorlink proclubs/.venv/bin/pip install -r proclubs/requirements.txt
sudo bash deploy/install.sh
```

`install.sh` is safe to re-run; it reinstalls the units and restarts the
site. Run the `pip install` whenever `requirements.txt` changed — it is a
no-op otherwise.

**Coming from a droplet that ran the old bot:** this repo used to carry an
unrelated Discord bot and its web app, and the site itself ran under the
name `valorlink-proclubs`. `install.sh` stops, disables and removes
`valorlink-bot`, `valorlink-web`, `valorlink-proclubs` and
`valorlink-backup.*` before installing the new units — the site rename
especially, since both would otherwise fight over `127.0.0.1:8001` and
whichever lost would stay down. Nothing else is deleted; the bot's old
databases are simply left alone under `/opt/valorlink`, and you can remove
them by hand once you are sure you want to.

---

## Optional features

Each one is off until its settings are present, and the site works without
any of them. All need `sudo systemctl restart yeehaw-fc` after editing
`proclubs/.env`.

### History tracking and the league table

EA's API exposes only a rolling window of recent matches and no historical
data at all, so season-long trends have to be accumulated over time.
`proclubs-poll.timer` fires `poll.py` hourly, snapshotting our club
(`CLUB_ID` in `.env`) into `proclubs/data/history.db`. Run it once rather
than waiting an hour:

```bash
sudo -u valorlink /opt/valorlink/proclubs/.venv/bin/python3 /opt/valorlink/proclubs/poll.py
```

To snapshot extra clubs as well, list them in `proclubs/tracked_clubs.json`
(it ships empty); it takes effect on the next poll, no redeploy. History
only accumulates forward from when a club is added — there is no way to
backfill matches EA has already evicted.

A new game means a new club ID; `season.py switch` handles it and erases
the previous season's stats (`proclubs/README.md#a-new-season`).

The same run maintains `/league`, which builds itself from real opponents
rather than a curated list (see
`proclubs/README.md#the-league-table-auto-built-not-manually-curated`).
More distinct opponents means more EA calls per run, capped by
`LEAGUE_TABLE_MAX_TEAMS` (default 25).

### Discord: the bot token

Everything below needs `DISCORD_BOT_TOKEN` (Developer Portal → your app →
Bot → Reset Token). It is full bot access rather than a scoped secret, so
treat it as the most sensitive value in `.env`.

```bash
sudo -u valorlink nano /opt/valorlink/proclubs/.env
# DISCORD_BOT_TOKEN=...
sudo systemctl restart yeehaw-fc
```

### Scheduled Events sync

Events created in Discord (Server → Events → New Event) mirror in as site
fixtures. See `proclubs/README.md#mirrored-discord-scheduled-events` for
what does and doesn't sync. Needs only the bot token.

```bash
sudo -u valorlink /opt/valorlink/proclubs/.venv/bin/python3 /opt/valorlink/proclubs/discord_events_poll.py
```

### Clips sync

Video files posted in one Discord channel mirror onto the Clips page —
real video *attachments* only, not pasted links (see
`proclubs/README.md#clips-are-discord-only`). Set `CLIPS_CHANNEL_ID` (enable
Developer Mode, right-click the channel, "Copy Channel ID"). The bot needs
View Channel + Read Message History there.

```bash
sudo -u valorlink /opt/valorlink/proclubs/.venv/bin/python3 /opt/valorlink/proclubs/discord_clips_poll.py
```

### Announcements and reactions

With `NEWS_ANNOUNCE_CHANNEL_ID` and `SITE_BASE_URL` set, publishing an
article posts an embed to that channel. Reactions on it — any emoji, all
summed — come back as the article's like count, refreshed every 30 minutes
for the `DISCORD_REACTIONS_POLL_LIMIT` most recent (default 20).

```bash
sudo -u valorlink /opt/valorlink/proclubs/.venv/bin/python3 /opt/valorlink/proclubs/discord_reactions_poll.py
```

### Event sign-ups

Sign-up buttons on the Discord post come back to the site as signed
interactions, which needs `DISCORD_PUBLIC_KEY` (Developer Portal → General
Information → Public Key) and either `EVENTS_ANNOUNCE_CHANNEL_ID` or
`EVENT_THREAD_CHANNEL_ID`.

**Weekly AI-written article (optional).** Every Saturday at about 09:15
(server time), `proclubs-weekly-article.timer` (already installed by
`install.sh`) fires `weekly_article.py`: it gathers the past seven days from
`data/history.db` -- results, player totals, division/points movement,
league position, and signings/departures (the hourly poll diffs EA's member
list to spot those) -- has Claude write it up, and **publishes it live**
under the byline `WEEKLY_ARTICLE_AUTHOR`, announcing it to Discord like any
other article. A week with no matches and no squad changes is skipped.

It runs the Claude Code CLI on your Claude subscription rather than API
credits. One-time setup on the droplet:

```bash
# Install the CLI for the valorlink user (lands in /opt/valorlink/.local/bin)
sudo -u valorlink -H bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
# Create a long-lived token for your Claude account (opens a sign-in link)
sudo -u valorlink -H /opt/valorlink/.local/bin/claude setup-token
```

Put the printed token in `proclubs/.env` as `CLAUDE_CODE_OAUTH_TOKEN`, and
check `CLAUDE_BIN` points at the CLI. Then preview an article without
publishing anything:

```bash
sudo -u valorlink /opt/valorlink/proclubs/.venv/bin/python3 /opt/valorlink/proclubs/weekly_article.py --dry-run
```

If Claude can't be reached (say, the subscription's usage limit is hit),
the service retries hourly, up to six times. The token lasts about a year;
when it expires, `journalctl -u proclubs-weekly-article` shows the failure
and re-running `claude setup-token` fixes it. Signings only start being
tracked from the first poll after this ships -- that first poll records the
current squad as a baseline, so nobody already in it shows up as "new".

In the same portal, set **Interactions Endpoint URL** to
`https://yeehaw-fc.club/discord/interactions`. Discord verifies the URL as
you save it — it sends a signed PING and some deliberately-invalid ones,
and refuses the URL unless the bad ones come back 401 — so save it *after*
the site is deployed and reachable.

### Staged event threads

An announced event can open its own **private thread** and widen who can
see it as kick-off approaches (see
`proclubs/README.md#staged-thread-invites`):

```ini
EVENT_THREAD_CHANNEL_ID=<the parent channel's ID>
EVENT_INVITE_TIERS=create:<role>,48:<role>,24:<role>
```

**Needs the Server Members privileged intent** (Developer Portal → your app
→ Bot → Server Members Intent). Discord has no route to list a role's
members, and threads carry no permissions of their own, so people are added
one at a time — which means listing the guild. Without it each tier is
pinged but nobody gains access; `"added 0 member(s)"` in the log is that
signature.

```bash
sudo -u valorlink /opt/valorlink/proclubs/.venv/bin/python3 /opt/valorlink/proclubs/event_invites_poll.py
```

### Squad moves

`/roster` (staff only) lists the Discord server's members and publishes
offers and departures. Needs `ROSTER_ANNOUNCE_CHANNEL_ID` (falls back to
`NEWS_ANNOUNCE_CHANNEL_ID`), the **Server Members intent** as above, and
the interactions endpoint for the Accept / Decline buttons.

`ROSTER_SQUAD_ROLE_ID` is the role a player gets when they accept their own
offer — the only Discord role this app ever writes, and it only ever adds.
For that, the bot needs **Manage Roles**, *and* its own highest role must
sit **above** the squad role in Server Settings → Roles. Discord refuses
otherwise with a 403, which `/roster` shows against the acceptance. Leave
the setting blank and accepting is just recorded.

### Checking what's configured

```bash
cd /opt/valorlink/proclubs && sudo -u valorlink .venv/bin/python3 -c "
import sys; sys.path.insert(0, '/opt/valorlink/proclubs')
import config
print('roster moves missing:', config.roster_moves_missing() or 'nothing')
print('role grant enabled :', config.ROSTER_ROLE_GRANT_ENABLED)
print('event rsvp missing :', config.event_rsvp_missing() or 'nothing')
"
```

---

## Backups

Two SQLite files under `proclubs/data`:

- `site.db` — articles, events, sign-ups, squad moves, streamers, tactics
- `history.db` — accumulated EA stats. **This one cannot be rebuilt**, since
  EA evicts old matches from its own rolling window.

`install.sh` sets up a daily backup that snapshots both into one compressed
archive and keeps the last 14. It uses SQLite's online `.backup`, so it is
safe while the site is live (a plain `cp` of a database mid-write can
capture a torn file), and each snapshot is integrity-checked before it is
kept.

```bash
systemctl list-timers yeehaw-fc-backup.timer      # when it last/next runs
sudo -u valorlink bash deploy/backup.sh           # run one right now
ls -lh /opt/valorlink/backups                     # the archives
journalctl -u yeehaw-fc-backup                    # run logs
```

Archives are named `yeehaw-fc-<UTC-timestamp>.tar.gz`. Older
`valorlink-*.tar.gz` archives are left alone by the pruner and are still
restorable.

**Tune it** in `proclubs/.env`: `BACKUP_RETENTION` (how many to keep),
`BACKUP_DIR` (where they go), and `BACKUP_REMOTE` — an
[rclone](https://rclone.org/) target such as a DigitalOcean Spaces or S3
bucket. If it is set and `rclone` is installed (`apt install -y rclone` +
`rclone config`), every archive is also copied off-box, so a lost droplet
doesn't take the backups with it. Keeping at least one copy off the droplet
is strongly recommended.

**Restore** stops the site, moves the current databases aside as
`*.pre-restore-*` (never deletes them), lays the snapshot back down, and
starts the site again:

```bash
sudo bash deploy/restore.sh --list                # available archives
sudo bash deploy/restore.sh --latest              # restore the newest
sudo bash deploy/restore.sh /opt/valorlink/backups/yeehaw-fc-20260715-033000.tar.gz
```

DigitalOcean's own weekly droplet backups (a paid add-on) are a fine
belt-and-braces layer on top, but they're weekly and whole-disk; this is
daily, per-database, and restorable in place.

---

## Troubleshooting

- **Site up but sign-in fails** — `DISCORD_OAUTH_REDIRECT` must exactly
  match a redirect registered on the Discord application, down to the host
  and path, and the bot must be in the guild so it can read roles.
- **Certificate errors** — DNS must resolve to the droplet before Caddy can
  issue a cert. Give it a few minutes, then `systemctl reload caddy`.
- **A Discord feature does nothing** — it is almost always an unset value in
  `proclubs/.env`. Run the "what's configured" check above; the site also
  says which settings are missing where the feature would have appeared.
- **`"added 0 member(s)"`, or an empty Squad page** — the Server Members
  privileged intent is off.
- **An acceptance says the squad role wasn't added** — the bot's own role is
  below the squad role in Server Settings → Roles, or it lacks Manage Roles.
- **Site won't start after an update** — `journalctl -u yeehaw-fc -n 50`. A
  port conflict here means an old `valorlink-proclubs` is still running;
  `sudo systemctl disable --now valorlink-proclubs` and re-run `install.sh`.
