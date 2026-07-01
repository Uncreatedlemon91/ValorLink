"""Central access point for everything that used to be hardcoded in
config.py. Roles, channels, regiment identity, and behavior tunables are
now stored in the `guild_config` singleton row and edited live via the
/config command -- no file edits or bot restarts required.

Secrets (bot token, guild id, database url) still come from config.py /
environment variables, since the bot needs them before it can even open a
database connection.
"""
from db.models import Company, GuildConfig

DEFAULT_REGIMENT_NAME = "Unconfigured Regiment"
DEFAULT_BRAND_COLOR = 0x2F3136
DEFAULT_INACTIVITY_DAYS = 30

# Maps the short keys used in /config commands to GuildConfig column names.
ROLE_KEYS = {
    "admin": "admin_role_id",
    "officer": "officer_role_id",
    "recruiter": "recruiter_role_id",
    "member": "member_role_id",
    "candidate": "candidate_role_id",
    "visitor": "visitor_role_id",
    "inactive": "inactive_role_id",
}

CHANNEL_KEYS = {
    "recruitment": "recruitment_channel_id",
    "personnel_forum": "personnel_forum_id",
    "roster": "roster_channel_id",
    "mod_log": "mod_log_channel_id",
    "admin_log": "admin_log_channel_id",
    "announcements": "announcements_channel_id",
    "welcome": "welcome_channel_id",
    "billboard": "billboard_channel_id",
}


def get_config(session) -> GuildConfig:
    cfg = session.get(GuildConfig, 1)
    if cfg is None:
        cfg = GuildConfig(
            id=1,
            regiment_name=DEFAULT_REGIMENT_NAME,
            brand_color=DEFAULT_BRAND_COLOR,
            inactivity_days_threshold=DEFAULT_INACTIVITY_DAYS,
        )
        session.add(cfg)
        session.commit()
    return cfg


def list_companies(session) -> list[Company]:
    return session.query(Company).order_by(Company.name).all()


def company_by_name(session, name: str) -> Company | None:
    if not name:
        return None
    return session.query(Company).filter(Company.name == name).one_or_none()


def default_company(session) -> Company | None:
    company = session.query(Company).filter(Company.is_default.is_(True)).first()
    if company:
        return company
    return session.query(Company).order_by(Company.id).first()


def default_company_name(session) -> str:
    """Company assigned on enlistment -- the company flagged as default, or
    a placeholder if none has been configured yet via /company_add."""
    company = default_company(session)
    return company.name if company else "Unassigned"
