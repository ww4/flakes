"""The Libib importer — idempotency and identity.

Untested since it shipped (the audit found the fixture CSV orphaned — no
test ever read it). Two behaviors matter enough to pin:

* Re-running the import must be a no-op. It used to re-create every copy —
  a second run silently doubled the library.
* An ISBN that already belongs to another work's edition means the ISBN
  resolves the book, not the title match. The old code kept the
  title-matched work and the foreign edition on one copy, so
  copy.work != copy.edition.work and every "owned isbn" join downstream
  silently misfired.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs a database"
)

FIXTURE = Path(__file__).parent / "fixtures" / "libib-sample.csv"


@pytest.fixture
def session():
    from stacks.db import session_scope

    with session_scope() as s:
        yield s
        s.rollback()


def _libib_copies(session):
    from stacks.models import Copy, Provenance

    return session.query(Copy).filter(
        Copy.provenance == Provenance.libib_import
    ).count()


class TestIdempotency:
    def test_a_second_run_creates_nothing(self, session):
        from stacks.importers.libib import import_libib_exports

        before = _libib_copies(session)
        first = import_libib_exports(session, [FIXTURE])
        assert first.copies_created > 0
        after_first = _libib_copies(session)
        assert after_first == before + first.copies_created

        second = import_libib_exports(session, [FIXTURE])
        assert second.copies_created == 0, "re-run doubled the library"
        # Against a populated catalog a fixture row can title-match a work
        # that already holds libib copies, so the first run itself may skip
        # some — the invariant is on the total requested, not on created.
        assert (second.copies_already_present
                == first.copies_created + first.copies_already_present)
        assert _libib_copies(session) == after_first

    def test_multi_copy_rows_survive(self, session):
        """The Hobbit row carries copies=2 — both must exist after one run,
        and still exactly two after a second."""
        from stacks.importers.libib import import_libib_exports
        from stacks.models import Copy, Work

        import_libib_exports(session, [FIXTURE])
        import_libib_exports(session, [FIXTURE])
        hobbit = session.query(Work).filter(Work.title == "The Hobbit").one()
        assert session.query(Copy).filter(Copy.work_id == hobbit.id).count() == 2


class TestIsbnIdentityWins:
    def test_copy_follows_the_existing_edition_to_its_work(self, session):
        """Pre-create the ISBN under a differently-titled work (what
        enrichment does), then import: the copy must land on THAT work."""
        from stacks.importers.libib import import_libib_exports
        from stacks.models import Copy, Edition, Work

        w = Work(title="Dune (Ace mass market)", sort_title="dune ace mass market",
                 ol_work_keys=[])
        session.add(w)
        session.flush()
        e = Edition(work_id=w.id, isbn13="9780441172719")
        session.add(e)
        session.flush()

        stats = import_libib_exports(session, [FIXTURE])
        assert any("9780441172719" in warn for warn in stats.warnings), stats.warnings

        copies = session.query(Copy).join(
            Edition, Copy.edition_id == Edition.id
        ).filter(Edition.isbn13 == "9780441172719").all()
        assert copies, "the Dune row imported no copy"
        for c in copies:
            assert c.work_id == w.id, (
                "copy.work and copy.edition.work disagree — the split-identity "
                "bug the audit flagged"
            )
