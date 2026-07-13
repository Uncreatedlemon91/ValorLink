# ValorLink

A Discord bot for managing a single War of Rights regiment: recruitment,
personnel records, rank/company structure, a live roster, drill/battle
attendance, and disciplinary records.

This bot is wired for **one regiment per deployment**, but configuration is
entirely chat-driven — roles, channels, regiment identity, ranks, and
companies are all set live with `/config`, `/rank`, and `/company` commands.
No file edits or restarts required after the bot is online.

## Features

- **Onboarding** — auto-greets new joins, assigns a visitor role.
- **Recruitment** — persistent "Apply to Enlist" button, modal application,
  private interview thread, recruiter approve/deny workflow.
- **Personnel** — `/promote`, `/demote`, `/set_rank`, `/service_log`,
  `/record` against a configurable rank ladder.
- **Roster** — a live, auto-updating roster embed grouped by company and
  rank, `/assign_company`, and automatic inactivity flagging.
- **Events & Attendance** — `/event_create` posts an announcement with
  Accept/Tentative/Decline buttons; `/attendance_mark` and
  `/attendance_history` track actual turnout.
- **Moderation** — `/note`, `/warn`, `/strike` write to a member's
  disciplinary record and post to a mod-log channel.

## Setup

> **New to this project or setting it up for the first time?** See
> [`SETUP.md`](SETUP.md) for a full walkthrough — Discord application/bot
> creation, server roles & channels, local testing, and hosting options to
> keep it running 24/7. The quick version is below.

1. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

2. **Configure secrets**

   ```bash
   cp .env.example .env
   ```

   Fill in `DISCORD_BOT_TOKEN` and `GUILD_ID`. `DATABASE_URL` defaults to a
   local SQLite file and rarely needs changing for a single-server bot.

3. **Run database migrations**

   ```bash
   alembic upgrade head
   ```

   Whenever you change `db/models.py`, generate a new migration with
   `alembic revision --autogenerate -m "description"` and re-run
   `alembic upgrade head`.

4. **Run the bot**

   ```bash
   python3 bot.py
   ```

5. **Configure it from Discord**

   Everything else — regiment name/motto/color, admin/officer/recruiter/
   member/candidate/visitor/inactive roles, recruitment/roster/log/
   announcement channels, the rank ladder, and companies — is set live with
   `/config`, `/rank`, and `/company` commands. See
   [`SETUP.md`](SETUP.md#4-configure-valorlink-from-discord) for the full
   command list. The very first `/config set_role key:admin ...` call needs
   server Administrator permission, since no admin role exists yet on a
   fresh setup; every command after that can use the role you just
   configured.

## Web UI — Regimental Headquarters

A read-only companion website renders the same data the bot manages, styled
as an 1860s regimental order book: the roster, full muster roll, personnel
dossiers (service history, honors, conduct, attendance), muster calls
(events) with turnout, and the honors catalogue. It opens the **same
database** the bot uses (`DATABASE_URL`), so nothing extra needs wiring —
whatever happens in Discord shows up here. It never writes to the database.

```bash
pip install -r web/requirements.txt   # fastapi, uvicorn, jinja2
uvicorn web.app:app --reload          # then visit http://127.0.0.1:8000
```

Want to preview it without a live server behind it? Seed a throwaway
database with a plausible regiment first (it refuses to touch a database
that already holds members):

```bash
DATABASE_URL=sqlite:///demo.db python -m web.seed_demo
DATABASE_URL=sqlite:///demo.db uvicorn web.app:app --reload
```

The site picks up the regiment's name, motto, and brand colour from the
same `/config` values the bot uses, so the banner matches your Discord.

## Permission model

Commands are gated by role IDs stored in the database and set via
`/config set_role`, not Discord's built-in admin permission (except as a
bootstrap fallback — see above):

- **admin** — full access to every command, including `/config`, `/rank`,
  and `/company`.
- **officer** — promote/demote/assign/discipline/event commands.
- **recruiter** — recruitment commands only.

Each entry in the rank ladder and each company can optionally carry a
Discord role (`/rank set_role`, `/company set_role`). When set, the bot
keeps that role in sync automatically: it's swapped on promotion/demotion/
`/set_rank`/`/assign_company` and applied on enlistment approval. Leave a
rank/company without a role to skip sync for it. For this to work, the
bot's own role must sit above every rank/company role in the server's role
list, and the bot needs the **Manage Roles** and **Manage Nicknames**
permissions — on rank changes it also rewrites the member's nickname to
`[Abbreviation] Callsign`. If permissions or hierarchy aren't right, sync
calls silently no-op rather than erroring out commands.

## Known limitations

- Interview and RSVP buttons are bound to in-memory views tied to a specific
  application/event. If the bot restarts while a candidate's interview
  thread or an event announcement is still open, those buttons stop
  responding until a fresh `/setup_recruitment` or `/event_create` is run.
  This mirrors the original prototype's behavior and is fine for typical
  same-day approval/RSVP windows.
- The inactivity sweep and roster refresh assume a single guild
  (`GUILD_ID`). Multi-guild support isn't implemented.
