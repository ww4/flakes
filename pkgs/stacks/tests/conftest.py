"""Test isolation.

The suite runs against the real development database — the one holding the
imported catalog, the flood record, and by now Chris's own confirmations from
scanning books. Without this, every run WRITES to it: earlier runs created a
Gatsby record, confirmed books present, and added copies. Tests that mutate the
data they are asserting against are both unreliable and destructive.

Everything is wrapped in one outer transaction that is rolled back when the
session ends. The app's own sessions are bound to that connection, so their
commits are real to the code under test and invisible afterwards — the
documented "joining an external transaction" pattern.
"""

from __future__ import annotations

import os

import pytest

NEEDS_DB = bool(os.environ.get("DATABASE_URL"))


#: The devdb runs on a Unix socket inside the checkout; its URL always names
#: the .devdb-run socket dir. The production service uses the system postgres
#: via a plain local socket. This is the discriminator the guard keys on.
_DEVDB_MARKER = ".devdb-run"


@pytest.fixture(scope="session", autouse=True)
def _rollback_everything():
    """Bind the app's session factory to a transaction we control.

    Refuses to engage for anything that is not the devdb. The rollback below
    is sound, but it is the ONLY thing between the mutating API suite and
    whatever DATABASE_URL points at — and pytest run with the production env
    (DATABASE_URL=postgresql+psycopg:///stacks) would happily exercise
    confirm/delete/rename against the live catalog protected by nothing but
    this transaction holding. Live data has been corrupted by an unisolated
    suite once already; a structural refusal is cheaper than a second time.
    Set STACKS_TEST_DB_OK=1 to override deliberately (e.g. a throwaway CI
    database with a different socket path).
    """
    if not NEEDS_DB:
        yield
        return

    url = os.environ["DATABASE_URL"]
    if _DEVDB_MARKER not in url and not os.environ.get("STACKS_TEST_DB_OK"):
        pytest.exit(
            f"refusing to run DB tests against {url!r}: it does not look like "
            f"the devdb (no {_DEVDB_MARKER!r} in the URL). This suite mutates "
            "whatever it is pointed at and relies on one rollback for safety. "
            "Use scripts/devdb.sh, or set STACKS_TEST_DB_OK=1 if this really "
            "is a disposable database.",
            returncode=2,
        )

    from sqlalchemy.orm import sessionmaker

    from stacks import db

    engine = db.get_engine()
    connection = engine.connect()
    transaction = connection.begin()

    original = db._Session
    # create_savepoint, explicitly. The default join mode lets a session's
    # commit reach through to the outer transaction, and one write escaped that
    # way — a savepoint per session keeps every commit undoable.
    db._Session = sessionmaker(
        bind=connection,
        expire_on_commit=False,
        future=True,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield
    finally:
        db._Session = original
        # Undo everything the suite did, including anything the app committed.
        transaction.rollback()
        connection.close()
