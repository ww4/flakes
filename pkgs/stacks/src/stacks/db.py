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


def run_migrations() -> None:
    """Bring the database to the latest revision. The supported path.

    Alembic, not ``create_all``: ``create_all`` cannot alter an existing
    Postgres enum, so adding a value to CopyStatus or Provenance silently fails
    against a live database and only shows up as an insert error later.
    """
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
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
