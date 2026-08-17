"""Holding counts must count copies, not join products.

_base_query outer-joins Edition, Copy and WorkTag; a bare count(Copy.id)
tallied copies x editions x tags, so a work with 40 printings and one
present copy counted present=40. Nothing visible broke while the counts fed
only booleans — but they ship to the client as numbers, and the first
numeric consumer would have inherited the inflation silently.
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


def test_counts_survive_edition_and_tag_fanout(session):
    from stacks import browse
    from stacks.models import (
        Copy,
        CopyStatus,
        Edition,
        Provenance,
        Tag,
        Work,
        WorkTag,
    )

    w = Work(title="Fanout Fixture", sort_title="fanout fixture", ol_work_keys=[])
    session.add(w)
    session.flush()
    for i in range(3):
        session.add(Edition(work_id=w.id, isbn13=None, publish_year=2000 + i))
    session.flush()
    session.add(Copy(work_id=w.id, status=CopyStatus.present,
                     provenance=Provenance.manual))
    session.add(Copy(work_id=w.id, status=CopyStatus.lost_flood,
                     provenance=Provenance.flood_doc))
    for name in ("FanoutA", "FanoutB"):
        t = Tag(name=name)
        session.add(t)
        session.flush()
        session.add(WorkTag(work_id=w.id, tag_id=t.id))
    session.flush()

    from stacks.models import Work as W

    row = session.execute(
        browse._base_query().where(W.id == w.id)
    ).one()
    # 3 editions x 2 tags = 6-way fan-out; a bare count() returned 6 and 6.
    assert row.present == 1
    assert row.lost == 1
    assert sorted(row.tags) == ["FanoutA", "FanoutB"]
