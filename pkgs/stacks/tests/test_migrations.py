"""The migration chain must replay — in both directions.

The 2026-08 audit reproduced a broken round trip: the initial migration's
downgrade dropped every table but none of the enum types, so
downgrade → upgrade died on DuplicateObject. This suite replays the chain on
a THROWAWAY database created on the devdb server for the occasion, so it can
never touch the populated catalog and needs no rollback trickery.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs a database"
)

ROOT = Path(__file__).resolve().parents[1]
THROWAWAY = "stacks_migration_replay"


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, DATABASE_URL=url)
    return subprocess.run(
        ["python", "-m", "alembic", *args],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )


@pytest.fixture
def throwaway_url():
    """A fresh database on the same server, dropped afterwards."""
    from sqlalchemy import create_engine, text

    base = os.environ["DATABASE_URL"]
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {THROWAWAY}"))
        # template0: immune to the template1 collation-version drift a devdb
        # accumulates when the nix devshell's glibc moves under it.
        c.execute(text(f"CREATE DATABASE {THROWAWAY} TEMPLATE template0"))
    # postgresql+psycopg://postgres@/stacks?host=...&port=... — swap the db
    # name (the path component between the host-less // and the ?).
    head, _, query = base.partition("?")
    url = head.rsplit("/", 1)[0] + f"/{THROWAWAY}" + ("?" + query if query else "")
    yield url
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {THROWAWAY} WITH (FORCE)"))
    admin.dispose()


class TestMigrationReplay:
    def test_up_down_up(self, throwaway_url):
        """empty → head → base → head. The last leg is the one that broke."""
        r = _alembic(throwaway_url, "upgrade", "head")
        assert r.returncode == 0, f"first upgrade failed:\n{r.stderr}"
        r = _alembic(throwaway_url, "downgrade", "base")
        assert r.returncode == 0, f"downgrade failed:\n{r.stderr}"
        r = _alembic(throwaway_url, "upgrade", "head")
        assert r.returncode == 0, (
            "re-upgrade after downgrade failed — orphaned enum types or "
            f"other residue:\n{r.stderr}"
        )
