"""Manage units (tenants) from the command line.

    python -m tenancy.manage init
    python -m tenancy.manage create --slug 5thva --name "5th Virginia Volunteers" --guild 123456789
    python -m tenancy.manage list
    python -m tenancy.manage adopt-default --name "5th Virginia Volunteers"

Until the self-serve "register your unit" flow exists (Phase 4), this is how
you add a unit: it inserts the registry row and provisions the unit's private
database. The unit owner then invites the bot and configures roles/channels.
"""
import argparse
import sys

from tenancy.registry import Tenant, init_registry, registry_session
from tenancy.resolve import ensure_default_tenant, tenant_by_slug
from tenancy.units import provision_unit_db, unit_db_url_for_slug


def cmd_init(_args):
    init_registry()
    print(f"Registry ready at {Tenant.__table__.name!r}.")


def cmd_create(args):
    init_registry()
    from tenancy.provision import ProvisionError, create_unit

    try:
        db_url = create_unit(
            args.slug, args.name, guild_id=args.guild,
            motto=args.motto, blurb=args.blurb, db_url=args.db_url,
        )
    except ProvisionError as exc:
        sys.exit(str(exc))
    print(f"Created unit '{args.slug}' ({args.name})")
    print(f"  database: {db_url}")
    if args.guild:
        print(f"  guild:    {args.guild}")


def cmd_remove(args):
    init_registry()
    from tenancy.provision import ProvisionError, delete_unit

    try:
        result = delete_unit(args.slug, purge=args.purge)
    except ProvisionError as exc:
        sys.exit(str(exc))
    print(f"Removed unit '{result['slug']}' ({result['name']}).")
    if result["purged"]:
        print("  database deleted.")
    elif result["archived_to"]:
        print(f"  database archived to: {result['archived_to']}")
    else:
        print(f"  database left in place: {result['db_url']}")


def cmd_list(_args):
    init_registry()
    with registry_session() as session:
        tenants = session.query(Tenant).order_by(Tenant.slug).all()
        if not tenants:
            print("No units registered yet.")
            return
        for t in tenants:
            flags = []
            if t.is_default:
                flags.append("default")
            if not t.recruiting_open:
                flags.append("recruiting closed")
            if not t.listed:
                flags.append("unlisted")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            guild = f" guild={t.discord_guild_id}" if t.discord_guild_id else ""
            print(f"{t.slug:16} {t.name}{guild}{suffix}")
            print(f"                 {t.db_url}")


def cmd_adopt_default(args):
    """Register the current single-tenant deployment as the default unit."""
    init_registry()
    with registry_session() as session:
        tenant = ensure_default_tenant(session, name=args.name)
        print(f"Default unit is '{tenant.slug}' ({tenant.name}) -> {tenant.db_url}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manage ValorLink units (tenants).")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the registry database").set_defaults(func=cmd_init)

    c = sub.add_parser("create", help="register and provision a new unit")
    c.add_argument("--slug", required=True, help="subdomain label, e.g. 5thva")
    c.add_argument("--name", required=True, help="display name")
    c.add_argument("--guild", type=int, help="Discord guild id")
    c.add_argument("--motto")
    c.add_argument("--blurb", help="public directory description")
    c.add_argument("--db-url", help="override the unit database URL")
    c.set_defaults(func=cmd_create)

    r = sub.add_parser("remove", help="remove a unit (keeps its database unless --purge)")
    r.add_argument("--slug", required=True, help="the unit's subdomain handle")
    r.add_argument("--purge", action="store_true", help="also delete the unit's database file")
    r.set_defaults(func=cmd_remove)

    sub.add_parser("list", help="list registered units").set_defaults(func=cmd_list)

    a = sub.add_parser("adopt-default", help="register the current deployment as the default unit")
    a.add_argument("--name", default="Headquarters")
    a.set_defaults(func=cmd_adopt_default)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
