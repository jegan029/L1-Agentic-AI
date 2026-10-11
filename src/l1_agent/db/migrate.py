"""Run schema migrations. Uses the migration role, never the application role."""

from __future__ import annotations

import re
from pathlib import Path

from alembic import command
from alembic.config import Config

from src.l1_agent.config.settings import ConfigError

DEFAULT_APP_ROLE = "l1_app"
_ROLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def alembic_config(url: str, app_role: str = DEFAULT_APP_ROLE) -> Config:
    if not url.startswith("postgresql+asyncpg://"):
        raise ConfigError("Migrations need a postgresql+asyncpg:// URL for the migration role")
    if not _ROLE_RE.match(app_role):
        raise ConfigError("The application role name must be a plain lower-case identifier")
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["app_role"] = app_role
    return cfg


def upgrade(url: str, app_role: str = DEFAULT_APP_ROLE, revision: str = "head") -> None:
    """Apply migrations. Synchronous: call it from a thread if a loop is running."""
    command.upgrade(alembic_config(url, app_role), revision)
