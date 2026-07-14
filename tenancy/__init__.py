"""Multi-unit (multi-tenant) foundation for ValorLink.

A small **registry** database lists every unit and where its private database
lives; each unit has its own database with the normal ValorLink schema
(``db/models.py``). Resolve an incoming request — by web subdomain or by
Discord guild id — to the right unit, then open a session against that unit's
database.

Import from the submodules directly (``tenancy.registry``, ``tenancy.resolve``,
``tenancy.units``); this package deliberately does no work at import time so
environment variables are read only when those modules are first used.

Phase 1: this layer exists and is tested, but is not yet wired into the web
app or the bot, so a single-tenant deployment keeps working unchanged. See
``docs/MULTI_TENANT.md``.
"""
