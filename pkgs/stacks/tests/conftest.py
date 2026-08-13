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


@pytest.fixture(scope="session", autouse=True)
def _rollback_everything():
    """Bind the app's session factory to a transaction we control."""
    if not NEEDS_DB:
        yield
        return

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
