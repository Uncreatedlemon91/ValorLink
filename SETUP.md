# ValorLink Setup & Go-Live Guide

This walks you through everything between "empty Discord server" and "ValorLink
running 24/7 for your regiment" — creating the bot application, wiring up
roles/channels with chat commands, and hosting it so it stays online.

Estimated time: 30–45 minutes for first-time setup.

---

## 1. Create the Discord application & bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
   and click **New Application**. Name it (e.g. "ValorLink").
2. In the left sidebar, open **Bot**.
   - Click **Reset Token** / **Copy** to get your bot token. **Save this
     somewhere safe — you can't view it again, only regenerate it.** This is
     your `DISCORD_BOT_TOKEN`.
   - Turn **on** these two Privileged Gateway Intents (required, the bot
     won't start without them):
     - **Server Members Intent** — needed for `on_member_join`, roster
       lookups, and role sync.
     - **Message Content Intent** — needed for the activity-tracking
       listener in `cogs/roster.py`.
3. In the left sidebar, open **OAuth2 → URL Generator**.
   - Under **Scopes**, check `bot` and `applications.commands`.
   - Under **Bot Permissions**, check:
     - Manage Roles
     - Manage Nicknames
     - Send Messages
     - Create Public Threads / Create Private Threads
     - Send Messages in Threads
     - Embed Links
     - Read Message History
     - View Channel
   - Copy the generated URL at the bottom, open it in your browser, and
     invite the bot to your server.
4. **Role hierarchy matters.** In your server's **Server Settings → Roles**,
   drag the bot's own role **above every rank role and company role** it
   will need to assign. If the bot's role sits below a role it's trying to
   grant, Discord silently blocks it and ValorLink's role-sync just no-ops.

---

## 2. Prep your Discord server

Enable **Developer Mode** first so you can copy IDs: User Settings →
Advanced → Developer Mode.

### Roles to create
| Role | Used for |
|---|---|
| Admin (or reuse an existing staff role) | full bot control |
| Officer | promote/demote/assign/discipline/event commands |
| Recruiter | processes applications |
| Member | base "enlisted" tag, granted on approval |
| Candidate | applied, awaiting interview |
| Visitor | auto-tagged on join, before applying |
| Inactive | auto-tagged when flagged for inactivity |
| One role per rank (Private, Corporal, Sergeant, …) | rank display, optional auto-sync |
| One role per company (Company A, Company B, …) | unit display, optional auto-sync |

### Channels to create
| Channel | Used for |
|---|---|
| `#recruitment` | hosts the persistent "Apply to Enlist" button |
| `#roster` | live auto-updating roster embed |
| `#mod-log` | warn/note/strike audit trail |
| `#admin-log` | enlistment/denial audit trail |
| `#announcements` | `/event_create` posts |
| `#welcome` | onboarding greeting message |
| A **Forum channel** for personnel dossiers (optional — only needed if you want per-member dossier threads) |

You don't need to copy any IDs by hand — once the bot is running, you'll
configure all of this in Discord with `/config`, `/rank`, and `/company`
commands (step 4), which let you pick roles/channels straight from Discord's
own UI.

---

## 3. Get the code running locally (recommended before going live)

```bash
git clone <your fork of this repo>
cd ValorLink
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Create your `.env`:

```bash
cp .env.example .env
```

Edit `.env`:

```bash
DISCORD_BOT_TOKEN=<the token from step 1>
GUILD_ID=<your server's ID — right-click the server icon → Copy Server ID>
DATABASE_URL=sqlite:///valorlink.db   # fine for a single-server bot
```

---

## 4. Configure ValorLink from Discord

Nothing to edit in source files for this part — everything below is a slash
command, run once the bot is online (see step 6 for starting it). The very
first command must come from an account with server **Administrator**
permission, since no admin role exists yet on a fresh setup; every command
afterward can use the admin role you configure.

**Identity:**

```
/config set_name name:1st Virginia Regiment
/config set_motto motto:Death Before Dishonor
/config set_color hex_color:#2F3136
/config set_inactivity_days days:30
```

**Roles** — pick the actual role from Discord's UI, no copying IDs:

```
/config set_role key:admin role:@Admin
/config set_role key:officer role:@Officer
/config set_role key:recruiter role:@Recruiter
/config set_role key:member role:@Member
/config set_role key:candidate role:@Candidate
/config set_role key:visitor role:@Visitor
/config set_role key:inactive role:@Inactive
```

**Channels** — same idea, pick the channel:

```
/config set_channel key:recruitment channel:#recruitment
/config set_channel key:personnel_forum channel:#personnel-forum   (skip if you didn't create one)
/config set_channel key:roster channel:#roster
/config set_channel key:mod_log channel:#mod-log
/config set_channel key:admin_log channel:#admin-log
/config set_channel key:announcements channel:#announcements
/config set_channel key:welcome channel:#welcome
```

Run `/config show` any time to see the current configuration.

**Rank ladder** — add lowest to highest; each `/rank add` appends to the top:

```
/rank add name:Private abbreviation:Pvt tier:Enlisted role:@Private
/rank add name:Corporal abbreviation:Cpl tier:NCO role:@Corporal
/rank add name:Sergeant abbreviation:Sgt tier:NCO role:@Sergeant
/rank add name:Lieutenant abbreviation:Lt tier:Officer role:@Lieutenant
/rank add name:Captain abbreviation:Capt tier:Officer role:@Captain
```

Binding a role is optional — leave it off to skip role sync for that rank.
Use `/rank list` to check order, `/rank move` to reorder, `/rank set_role`
to attach/change a role after the fact, and `/rank remove` to delete one
(members holding it keep the label until reassigned).

**Companies:**

```
/company add name:Company A role:@Company A is_default:true
/company add name:Company B role:@Company B
/company add name:Headquarters role:@Headquarters
```

`is_default` marks which company new enlistees get assigned to. Use
`/company list`, `/company set_role`, `/company set_default`, and
`/company remove` the same way as ranks.

Every one of these can be changed again later from chat — no restarts, no
file edits, no redeploys.

---

## 5. Initialize the database

```bash
alembic upgrade head
```

This creates `valorlink.db` (or whatever `DATABASE_URL` points to) with all
tables. Re-run this any time you pull an update that adds a new migration.

---

## 6. Test run

```bash
python3 bot.py
```

You should see log lines for each cog loading, then `<Bot Name> is online
as <Bot Name>#0000`. In Discord:

1. Run through the `/config`, `/rank`, and `/company` commands from step 4
   (your account needs Administrator permission for the very first
   `/config set_role key:admin ...` call).
2. Run `/setup_recruitment` (as a Recruiter/Officer/Admin) in your
   recruitment channel — this posts the persistent Apply button.
3. Click **Apply to Enlist**, fill out the modal, confirm a private
   interview thread is created.
4. Approve the application from another (recruiter) account and confirm:
   - The roster embed appears/updates in your roster channel.
   - The new member's rank role, company role, and nickname (`[Pvt]
     Callsign`) are applied.
5. Run `/roster` and `/record` to sanity-check the embeds render correctly.

If commands don't show up in Discord, wait a minute (slash command sync can
lag) or restart the bot — `bot.py` syncs commands to `GUILD_ID` on every
startup, which is near-instant for a single guild.

Stop the test run with `Ctrl+C` once you're satisfied.

---

## 7. Go live: keep it running 24/7

A bot process running in your terminal dies when you close the terminal or
your machine sleeps. Pick one of these to keep it running continuously.

### Option A — VPS with systemd (recommended, full control)

Any small VPS (DigitalOcean, Hetzner, a home server, etc.) works. After
cloning the repo and completing steps 3–5 on the server:

Create `/etc/systemd/system/valorlink.service`:

```ini
[Unit]
Description=ValorLink Discord Bot
After=network.target

[Service]
Type=simple
User=<your-linux-user>
WorkingDirectory=/path/to/ValorLink
ExecStart=/path/to/ValorLink/.venv/bin/python3 bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now valorlink
sudo systemctl status valorlink   # confirm it's "active (running)"
journalctl -u valorlink -f        # tail logs
```

It now survives reboots and auto-restarts on crash.

### Option B — Managed host (Railway, Render, Fly.io, etc.)

These platforms run a process for you without managing a VPS yourself.
General steps (specifics vary by provider):

1. Push this repo to GitHub.
2. Create a new "worker"/"background service" (not a web service — this
   bot doesn't listen on a port) pointed at your repo.
3. Set the start command to `python3 bot.py` (after a build step that runs
   `pip install -r requirements.txt`).
4. Set `DISCORD_BOT_TOKEN`, `GUILD_ID`, and `DATABASE_URL` as environment
   variables in the platform's dashboard — don't commit `.env`.
5. **Persistence matters**: SQLite needs a persistent disk/volume so your
   roster data survives redeploys. If the platform only offers ephemeral
   storage, attach a volume and point `DATABASE_URL` at a path inside it,
   or switch to a managed Postgres instance and update `DATABASE_URL`
   accordingly (SQLAlchemy/Alembic both support it with no code changes).

### Option C — Quick & dirty (tmux/screen on any always-on machine)

Fine for testing, not recommended for a production regiment bot since it
won't survive a reboot or crash without manual intervention:

```bash
tmux new -s valorlink
source .venv/bin/activate
python3 bot.py
# Ctrl+B then D to detach; `tmux attach -t valorlink` to reattach
```

---

## 8. Post-launch checklist

- [ ] Bot shows **online** in your member list.
- [ ] `/setup_recruitment` posted and the Apply button works end-to-end.
- [ ] A test enlistment correctly applies rank role, company role, and
      nickname.
- [ ] Roster embed in `#roster` updates after the test enlistment.
- [ ] `/promote`, `/demote`, `/assign_company` swap roles correctly.
- [ ] `/warn`, `/note`, `/strike` post to `#mod-log`.
- [ ] `/event_create` posts to `#announcements` and RSVP buttons work.
- [ ] Inactivity sweep role and threshold (`/config set_role key:inactive`,
      `/config set_inactivity_days`) are correct for your regiment's
      activity expectations.
- [ ] You have a backup plan for `valorlink.db` (see below).

### Back up your database

SQLite is a single file. Back it up regularly, e.g. a daily cron job:

```bash
0 3 * * * cp /path/to/ValorLink/valorlink.db /path/to/backups/valorlink-$(date +%F).db
```

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Bot won't start, `SystemExit: DISCORD_BOT_TOKEN is not set` | `.env` missing or not in the working directory `bot.py` runs from |
| Bot starts but slash commands never appear | Wait ~1 min, or confirm `GUILD_ID` in `.env` matches your server; restart the bot to force a re-sync |
| Role/nickname sync silently does nothing | Bot's role is below the target role in **Server Settings → Roles**, or bot is missing Manage Roles/Manage Nicknames permission |
| `on_member_join` / activity tracking doesn't fire | Server Members Intent or Message Content Intent not enabled in the Developer Portal (step 1) |
| Interview/RSVP buttons stop responding after a restart | Known limitation — those views are tied to in-memory state for a specific application/event. Re-run `/setup_recruitment` or `/event_create` for any threads/posts still open across a restart |
| `alembic` errors about "Target database is not up to date" | Run `alembic upgrade head` once to stamp the existing DB before generating a new migration |

For anything not covered here, see the **Known limitations** section in
`README.md`.
