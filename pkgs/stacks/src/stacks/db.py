"""Engine/session helpers and schema bootstrap."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from stacks.config import get_settings
from stacks.models import Base

_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _Session
    if _engine is None:
        url = get_settings().database_url
        if not url:
            raise RuntimeError(
                "DATABASE_URL is not set. Enter the devshell (nix develop) or set it "
                "explicitly."
            )
        _engine = create_engine(url, future=True, pool_pre_ping=True)
        _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _Session is not None
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def migrations_dir() -> Path:
    """Where the migration scripts live.

    Beside the package, always. Deriving this from the repository root worked
    in a checkout and failed in the Nix store — the installed service
    crash-looped 1797 times on "Path doesn't exist: .../lib/python3.13/
    migrations" because there is no repo root there. Anchoring to the package
    means the scripts are wherever the code is.
    """
    return Path(__file__).resolve().parent / "migrations"


def run_migrations() -> None:
    """Bring the database to the latest revision. The supported path.

    Alembic, not ``create_all``: ``create_all`` cannot alter an existing
    Postgres enum, so adding a value to CopyStatus or Provenance silently fails
    against a live database and only shows up as an insert error later.

    The config is built in code rather than read from alembic.ini, so nothing
    outside the installed package has to exist for a deploy to migrate.
    """
    from alembic import command
    from alembic.config import Config

    scripts = migrations_dir()
    if not scripts.is_dir():
        raise RuntimeError(f"migration scripts missing from the package: {scripts}")

    cfg = Config()
    cfg.set_main_option("script_location", str(scripts))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
    command.upgrade(cfg, "head")


def init_schema() -> None:
    """Create extensions + tables directly from the models.

    For tests and throwaway databases only — it bypasses migration history, so
    a database built this way has no ``alembic_version`` and cannot be upgraded.
    Use :func:`run_migrations` for anything that holds real data.
    """
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS unaccent"))
    Base.metadata.create_all(engine)
