"""Deleting and merging works must detach everything that points at them.

Three hand-rolled copies of this logic each forgot a different table: the API
delete forgot want_rules and requests (deleting a work a sale-doc rule
targeted was an opaque 500, and the work was undeletable from the UI); the
enrich twin-merge forgot scan history (merging a previously-scanned duplicate
raised IntegrityError and killed the whole batch run). workops is now the one
implementation, and these tests enumerate the pointing tables so a new FK has
a place to fail loudly.
"""

from __future__ import annotations

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
        s.rollback()


def _work(session, title):
    from stacks.models import Work

    w = Work(title=title, sort_title=title.lower(), ol_work_keys=[])
    session.add(w)
    session.flush()
    return w


def _full_web(session, work):
    """Hang one of everything off the work: the audit's failure inventory."""
    from stacks.models import (
        Copy,
        CopyStatus,
        Edition,
        Person,
        Provenance,
        Request,
        ScanEvent,
        Tag,
        WantRule,
        WantKind,
        WantSource,
        WorkTag,
    )

    e = Edition(work_id=work.id, isbn13=None)
    session.add(e)
    session.flush()
    c = Copy(work_id=work.id, edition_id=e.id,
             status=CopyStatus.present, provenance=Provenance.manual)
    person = Person(name=f"Requester {work.id}")
    session.add_all([c, person])
    session.flush()
    session.add_all([
        ScanEvent(scanned_code="0000000000000", matched_work_id=work.id,
                  verdict="SKIP_HAVE"),
        WantRule(kind=WantKind.work, source=WantSource.manual, work_id=work.id,
                 label=work.title, match_key=work.sort_title),
        Request(work_id=work.id, requester_id=person.id),
    ])
    tag = Tag(name=f"WORKOPS-{work.id}")
    session.add(tag)
    session.flush()
    session.add(WorkTag(work_id=work.id, tag_id=tag.id))
    session.flush()
    return tag


class TestDeleteWork:
    def test_a_fully_referenced_work_deletes_cleanly(self, session):
        from stacks.models import Request, ScanEvent, WantRule, Work
        from stacks.workops import delete_work

        w = _work(session, "Workops Delete Fixture")
        _full_web(session, w)
        wid = w.id

        n_copies, n_editions = delete_work(session, w)

        assert (n_copies, n_editions) == (1, 1)
        assert session.get(Work, wid) is None
        # The scan stays, detached — audit trail outlives the record.
        ev = session.query(ScanEvent).filter_by(scanned_code="0000000000000").all()
        assert ev and all(e.matched_work_id is None for e in ev)
        # Rules and requests anchored to the record die with it.
        assert not session.query(WantRule).filter_by(work_id=wid).all()
        assert not session.query(Request).filter_by(work_id=wid).all()

    def test_api_delete_survives_want_rule(self, session):
        """The endpoint-level regression: this exact call was a 500."""
        from fastapi.testclient import TestClient

        from stacks.api import app
        from stacks.models import WantKind, WantRule, WantSource

        w = _work(session, "Api Delete Fixture")
        session.add(WantRule(kind=WantKind.work, source=WantSource.manual,
                             work_id=w.id, label=w.title, match_key=w.sort_title))
        session.commit()

        with TestClient(app) as client:
            r = client.delete(
                f"/api/work/{w.id}", params={"confirm_title": "Api Delete Fixture"}
            )
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] is True


class TestMergeWorkInto:
    def test_everything_repoints_to_the_twin(self, session):
        from stacks.models import (
            Copy,
            Edition,
            Request,
            ScanEvent,
            WantRule,
            Work,
            WorkTag,
        )
        from stacks.workops import merge_work_into

        dupe = _work(session, "Merge Fixture (dupe)")
        twin = _work(session, "Merge Fixture (twin)")
        tag = _full_web(session, dupe)
        # The twin already carries the same tag — the composite-pk collision
        # case a blanket UPDATE dies on.
        session.add(WorkTag(work_id=twin.id, tag_id=tag.id))
        session.flush()
        d_id, t_id = dupe.id, twin.id

        merge_work_into(session, dupe, twin)

        assert session.get(Work, d_id) is None
        for model, col in ((Copy, Copy.work_id), (Edition, Edition.work_id),
                           (WantRule, WantRule.work_id), (Request, Request.work_id),
                           (ScanEvent, ScanEvent.matched_work_id)):
            assert not session.query(model).filter(col == d_id).all(), model
        assert session.query(Copy).filter(Copy.work_id == t_id).count() == 1
        assert session.query(ScanEvent).filter(
            ScanEvent.matched_work_id == t_id
        ).count() >= 1
        # One tag link, not a crashed UPDATE and not a duplicate.
        assert session.query(WorkTag).filter(
            WorkTag.work_id == t_id, WorkTag.tag_id == tag.id
        ).count() == 1
