from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

import config

connect_args = {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(config.DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


def db_session():
    """Open a session against the unit database bound to the current context,
    or the default ``DATABASE_URL`` when none is bound (single-guild mode).

    Cogs and utils use this instead of ``SessionLocal`` directly so that the
    bot, serving many guilds, reads and writes the right unit's database.
    """
    from db.context import current_db_url

    url = current_db_url()
    if not url:
        return SessionLocal()
    from tenancy.units import sessionmaker_for

    return sessionmaker_for(url)()
