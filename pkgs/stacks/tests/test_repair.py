"""Holdings that describe one book as several.

Reported from real use: "The Magic School Bus and the Butterfly Bunch" showed
three entries under "your copies" — one unverified from a Libib collection and
two flood losses — for a book there is very likely no copy of at all.

Two distinct causes:
  * the loss document names a book in both of its halves, and two differently
    worded lines resolve to the same catalogue work;
  * a book catalogued in Libib and later destroyed is one physical object, but
    was recorded as an owned copy *plus* a separate loss.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs a database"
)


@pytest.fixture
def session():
    from stacks.db import session_scope

    with session_scope() as s:
        yield s


@pytest.fixture
def work(session):
    from stacks.models import Work

    w = Work(title="Repair Fixture", sort_title="repair fixture", ol_work_keys=[])
    session.add(w)
    session.flush()
    yield w
    session.rollback()


def _copy(session, work, status, provenance, **kw):
    from stacks.models import Copy

    c = Copy(work_id=work.id, status=status, provenance=provenance, **kw)
    session.add(c)
    session.flush()
    return c


class TestCollapseDuplicateLosses:
    def test_two_losses_become_one(self, session, work):
        from stacks.models import Copy, CopyStatus, Provenance
        from stacks.repair import collapse_duplicate_losses

        _copy(session, work, CopyStatus.lost_flood, Provenance.flood_doc,
              notes="sorted half line 99")
        _copy(session, work, CopyStatus.lost_flood, Provenance.flood_doc,
              notes="photos half line 653")

        removed, works = collapse_duplicate_losses(session)
        assert removed >= 1
        remaining = session.query(Copy).filter(
            Copy.work_id == work.id, Copy.status == CopyStatus.lost_flood
        ).all()
        assert len(remaining) == 1
        # The corroborating line is kept as evidence, not thrown away.
        assert "653" in (remaining[0].notes or "")

    def test_single_loss_untouched(self, session, work):
        from stacks.models import Copy, CopyStatus, Provenance
        from stacks.repair import collapse_duplicate_losses

        _copy(session, work, CopyStatus.lost_flood, Provenance.flood_doc, notes="only one")
        collapse_duplicate_losses(session)
        assert session.query(Copy).filter(Copy.work_id == work.id).count() == 1


class TestAbsorbUnverified:
    def test_catalogued_then_destroyed_is_one_book(self, session, work):
        from stacks.models import Copy, CopyStatus, Provenance
        from stacks.repair import absorb_unverified_into_loss

        _copy(session, work, CopyStatus.unverified, Provenance.libib_import,
              source_collections=["November haul"], notes="tags: picture books")
        _copy(session, work, CopyStatus.lost_flood, Provenance.flood_doc, notes="destroyed")

        # The repair runs database-wide, so assert on this fixture's outcome
        # rather than the global counter.
        absorb_unverified_into_loss(session)

        remaining = session.query(Copy).filter(Copy.work_id == work.id).all()
        assert len(remaining) == 1
        assert remaining[0].status is CopyStatus.lost_flood
        # Where it used to live is real provenance and must survive.
        assert "November haul" in remaining[0].source_collections

    def test_unverified_alone_is_left_alone(self, session, work):
        from stacks.models import Copy, CopyStatus, Provenance
        from stacks.repair import absorb_unverified_into_loss

        _copy(session, work, CopyStatus.unverified, Provenance.libib_import)
        absorb_unverified_into_loss(session)
        assert session.query(Copy).filter(Copy.work_id == work.id).count() == 1

    def test_a_confirmed_copy_is_never_absorbed(self, session, work):
        """A book seen since the flood is real, whatever the document says."""
        from stacks.models import Copy, CopyStatus, Provenance
        from stacks.repair import absorb_unverified_into_loss

        _copy(session, work, CopyStatus.present, Provenance.re_acquired)
        _copy(session, work, CopyStatus.lost_flood, Provenance.flood_doc)
        absorb_unverified_into_loss(session)
        statuses = {
            c.status for c in session.query(Copy).filter(Copy.work_id == work.id).all()
        }
        assert CopyStatus.present in statuses


class TestIdempotence:
    def test_running_twice_changes_nothing(self, session, work):
        from stacks.models import Copy, CopyStatus, Provenance
        from stacks.repair import run

        _copy(session, work, CopyStatus.unverified, Provenance.libib_import)
        _copy(session, work, CopyStatus.lost_flood, Provenance.flood_doc)
        _copy(session, work, CopyStatus.lost_flood, Provenance.flood_doc)

        run(session)
        after_first = session.query(Copy).filter(Copy.work_id == work.id).count()
        stats = run(session)
        after_second = session.query(Copy).filter(Copy.work_id == work.id).count()

        assert after_first == after_second == 1
        assert stats.duplicate_losses_removed == 0
        assert stats.unverified_absorbed == 0
