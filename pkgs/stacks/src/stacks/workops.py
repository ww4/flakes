"""Deleting and merging works, in exactly one place.

Three code paths used to re-implement "remove a work" by hand — the API
delete, list_split's parent-drop, and the enrich twin-merge — and each
forgot a different table. The 2026-08 audit found the consequences: deleting
a work a sale-doc want rule targeted was an opaque 500; enrich-merging a
previously-scanned duplicate raised IntegrityError mid-batch and killed the
whole run. An invariant that lives in three copies is not an invariant.

What points at a work, and what each operation does with it:

  table                  on delete                on merge into twin
  ---------------------  -----------------------  -------------------------
  editions, copies       deleted                  repointed
  loans (via copies)     deleted with their copy  follow their copy
  scan_events            matched_work_id -> NULL  repointed (audit trail
                         (what was scanned stays  stays true — it matched
                         true after the record    the same book, now under
                         it matched is gone)      its surviving id)
  want_rules.work_id     rule deleted (a rule     repointed (still the same
                         anchored to a record     book to look out for)
                         that no longer exists
                         matches nothing and
                         would sit active
                         forever)
  requests               deleted (NOT NULL fk,    repointed
                         meaningless without
                         the work)
  work_tags              DB cascade               repointed, deduplicated
                                                  (composite pk)
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from stacks.models import (
    Copy,
    Edition,
    Loan,
    Request,
    ScanEvent,
    WantRule,
    Work,
    WorkTag,
)


def delete_work(session: Session, work: Work) -> tuple[int, int]:
    """Delete ``work`` and everything under it. Returns (copies, editions)."""
    wid = work.id
    work.cover_edition_id = None
    session.flush()

    session.query(ScanEvent).filter(ScanEvent.matched_work_id == wid).update(
        {ScanEvent.matched_work_id: None}, synchronize_session=False
    )
    session.query(WantRule).filter(WantRule.work_id == wid).delete(
        synchronize_session=False
    )
    session.query(Request).filter(Request.work_id == wid).delete(
        synchronize_session=False
    )

    copy_ids = session.scalars(select(Copy.id).where(Copy.work_id == wid)).all()
    if copy_ids:
        session.query(Loan).filter(Loan.copy_id.in_(copy_ids)).delete(
            synchronize_session=False
        )
    n_copies = session.query(Copy).filter(Copy.work_id == wid).delete(
        synchronize_session=False
    )
    n_editions = session.query(Edition).filter(Edition.work_id == wid).delete(
        synchronize_session=False
    )
    session.delete(work)
    session.flush()
    return n_copies, n_editions


def merge_work_into(session: Session, work: Work, twin: Work) -> None:
    """Fold ``work`` into ``twin`` and delete the empty record.

    Everything that pointed at ``work`` points at ``twin`` afterwards; the
    books, scans, rules and requests all concerned the same physical title.
    """
    assert work.id != twin.id
    wid, tid = work.id, twin.id

    for model, col in (
        (Copy, Copy.work_id),
        (Edition, Edition.work_id),
        (ScanEvent, ScanEvent.matched_work_id),
        (WantRule, WantRule.work_id),
        (Request, Request.work_id),
    ):
        session.query(model).filter(col == wid).update(
            {col: tid}, synchronize_session=False
        )

    # work_tags has a composite (work_id, tag_id) primary key, so a blanket
    # UPDATE collides when both works carry the same tag. Move only the tags
    # the twin does not already have; the duplicates just die with the work.
    twin_tags = set(
        session.scalars(select(WorkTag.tag_id).where(WorkTag.work_id == tid)).all()
    )
    if twin_tags:
        session.query(WorkTag).filter(
            WorkTag.work_id == wid, WorkTag.tag_id.in_(twin_tags)
        ).delete(synchronize_session=False)
    session.query(WorkTag).filter(WorkTag.work_id == wid).update(
        {WorkTag.work_id: tid}, synchronize_session=False
    )

    work.cover_edition_id = None
    session.flush()
    session.delete(work)
    session.flush()
