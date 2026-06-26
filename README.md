# ValorLink

A Discord bot for managing a single War of Rights regiment: recruitment,
personnel records, rank/company structure, a live roster, drill/battle
attendance, and disciplinary records.

This bot is wired for **one regiment per deployment** — configuration lives
in `config.py` as plain constants, not a setup wizard. Fork/copy it per
server.

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

3. **Edit `config.py`**

   Replace every `PLACEHOLDER` role/channel ID with the real IDs from your
   server (enable Developer Mode in Discord, then right-click a role or
   channel → Copy ID). Edit `REGIMENT_NAME`, `REGIMENT_MOTTO`, `RANKS`, and
   `COMPANIES` to match your unit.

4. **Run database migrations**

   ```bash
   alembic upgrade head
   ```

   Whenever you change `db/models.py`, generate a new migration with
   `alembic revision --autogenerate -m "description"` and re-run
   `alembic upgrade head`.

5. **Run the bot**

   ```bash
   python3 bot.py
   ```

## Permission model

Commands are gated by the role IDs in `config.py`, not Discord's built-in
admin permission:

- `ADMIN_ROLE_ID` — full access to every command.
- `OFFICER_ROLE_ID` — promote/demote/assign/discipline/event commands.
- `RECRUITER_ROLE_ID` — recruitment commands only.

## Known limitations

- Interview and RSVP buttons are bound to in-memory views tied to a specific
  application/event. If the bot restarts while a candidate's interview
  thread or an event announcement is still open, those buttons stop
  responding until a fresh `/setup_recruitment` or `/event_create` is run.
  This mirrors the original prototype's behavior and is fine for typical
  same-day approval/RSVP windows.
- The inactivity sweep and roster refresh assume a single guild
  (`GUILD_ID`). Multi-guild support isn't implemented.
