# Multi-unit (multi-tenant) architecture

ValorLink began as a **single-regiment** deployment: one Discord guild, one
database, a `GuildConfig` singleton. This document describes the agreed plan
to grow it into a **platform** that hosts many units — each with its own
portal and its own database — plus a public directory where people can
discover units and apply to them.

## Decisions

| Question | Decision |
|---|---|
| Bot model | **One central bot.** Units invite it to their Discord server; the platform operates it. |
| Data isolation | **One SQLite database per unit.** Strong isolation, cheap, one box. |
| Portal addressing | **Subdomain per unit** — `slug.valorlink.co`. |

## The core idea: a thin registry + a private DB per unit

A directory that lists every unit can't be built from fully-siloed databases
— a unit's database knows nothing about the others. So we split data into
two planes:

- **Registry (control plane)** — one small shared database. Holds only the
  cross-cutting facts: the list of units, their public info (name, motto,
  blurb, brand colour, recruiting status), which Discord guild they map to,
  and where their private database lives. This powers the top bar, the
  public directory, and routing.
- **Unit databases (data plane)** — one private SQLite file per unit,
  holding that unit's roster, records, events, awards, config — exactly the
  schema that exists today (`db/models.py`), unchanged.

```
                 valorlink.co (public directory + global login)
                        │  reads
                        ▼
                 ┌──────────────┐
                 │  REGISTRY DB │  tenants: slug, name, guild_id, db_url, public info
                 └──────┬───────┘
        slug/guild →    │ resolve
        ┌───────────────┼────────────────┐
        ▼               ▼                 ▼
   5thva.db        1sttx.db          ...unit.db      (private, per-unit)
   ▲                    ▲
   │ subdomain          │ guild_id
 5thva.valorlink.co   one central bot (in every unit's server)
```

## How requests resolve to a unit

- **Web**: the `Host` header gives the subdomain label → `slug`. The registry
  maps `slug → db_url`; the request opens a session against that unit's
  database. The apex (`valorlink.co`) and `www` serve the public directory,
  not a unit.
- **Bot**: an interaction arrives with a `guild_id`. The registry maps
  `guild_id → db_url`; the handler uses that unit's database. (One bot,
  many guilds.)

Engines/sessionmakers are cached per `db_url`, so repeated requests reuse
connections.

## Global identity vs. per-unit standing

Users sign in **once** with Discord — that's their platform identity. Their
**permission tier inside a given unit** is still read from *that unit's*
Discord roles against *that unit's* configured admin/officer/recruiter role
IDs, exactly as today. Being an admin of one unit grants nothing in another.

## What changes in the current code

The single-tenant assumptions to unwind (in later phases):

1. **`db/base.py`** — today one global engine from `DATABASE_URL`. The
   tenancy layer (`tenancy/`) adds per-unit engines/sessions; the web and bot
   resolve the right one per request. `db/base.py` stays as the *default
   tenant* for backward compatibility.
2. **`config.GUILD_ID`** — the bot is wired to one guild. Becomes multi-guild:
   look up the unit for each interaction's guild.
3. **`GuildConfig` singleton (`id=1`)** — already per-database, so it becomes
   per-unit automatically once each unit has its own DB.
4. **New public surfaces** — directory, per-unit public pages, global
   sign-up, and the cross-unit apply flow.

## Provisioning a unit

Creating a unit:
1. Insert a registry row (`slug`, `name`, `discord_guild_id`, `db_url`).
2. Create the unit's SQLite file and build the schema (`create_all`), then
   stamp it at the current Alembic head so future migrations apply.
3. The unit's owner invites the bot and configures roles/channels from their
   portal's Command Tent (or Discord `/config`).

## Apply flow (cross-unit)

A signed-in user browses the directory → opens a unit's public page → clicks
**Apply**. That writes a `Candidacy` into *that unit's* database and pings
*that unit's* Discord recruitment channel via the bot. Recruiters approve it
from their own portal, just like today.

## Hosting

Still one small droplet for a long time: each unit is a small SQLite file,
and one bot process can sit in many Discord servers. Subdomains use a
**wildcard DNS record** (`*.valorlink.co → droplet`) and Caddy's on-demand
TLS to issue certificates per subdomain automatically. Scale-out later means
moving unit databases to Postgres schemas — a connection-string change, not a
rewrite.

## Phased rollout

- **Phase 1 — tenant-aware foundation** *(this PR)*: the registry database +
  `Tenant` model, a per-unit engine/session manager, unit-DB provisioning,
  and `Host`/`guild_id` resolvers. A management CLI to create/list units. The
  existing regiment is representable as the default tenant. **Not yet wired
  into the web or bot**, so the live single-tenant deployment is unaffected.
- **Phase 2 — subdomain portals**: the web app resolves the tenant per
  request and serves each unit's portal at its subdomain; wildcard TLS.
- **Phase 3 — public directory + apply**: the apex directory/top bar, per-unit
  public pages, global sign-up, and the cross-unit apply flow.
- **Phase 4 — multi-guild bot + self-serve provisioning**: the bot resolves
  the unit per guild, and a "register your unit" flow provisions everything.

Each phase is backward compatible: the single regiment keeps working
throughout.
