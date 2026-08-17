"""In-place repairs for holdings that describe one book as several.

The importers are the real fix — these routines exist so an already-populated
database can be corrected without discarding hours of Open Library enrichment
that lives on works and editions rather than on copies.

Everything here is idempotent: running it twice changes nothing the second time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stacks.models import Copy, CopyStatus, Provenance

log = logging.getLogger(__name__)


@dataclass
class RepairStats:
    duplicate_losses_removed: int = 0
    works_with_duplicate_losses: int = 0
    unverified_absorbed: int = 0
    works_absorbed: int = 0

    def summary(self) -> str:
        return (
            f"collapsed {self.duplicate_losses_removed} duplicate loss records "
            f"across {self.works_with_duplicate_losses} works; "
            f"absorbed {self.unverified_absorbed} pre-flood catalog entries into "
            f"the loss record on {self.works_absorbed} works"
        )


def collapse_duplicate_losses(session: Session) -> tuple[int, int]:
    """One book destroyed once is one loss record.

    The flood document names many books twice — in the hand-sorted section and
    again in the photo transcription — and two differently-worded lines can
    resolve to the same catalogue work. That produced several ``lost_flood``
    rows for a single physical book.
    """
    work_ids = [
        wid for (wid,) in session.execute(
            select(Copy.work_id)
            .where(Copy.status == CopyStatus.lost_flood)
            .group_by(Copy.work_id)
            .having(func.count(Copy.id) > 1)
        ).all()
    ]

    removed = 0
    for wid in work_ids:
        copies = session.scalars(
            select(Copy)
            .where(Copy.work_id == wid, Copy.status == CopyStatus.lost_flood)
            .order_by(Copy.id)
        ).all()
        keeper, rest = copies[0], copies[1:]
        for extra in rest:
            if extra.notes:
                keeper.notes = f"{keeper.notes or ''} || also {extra.notes}".strip(" |")[:2000]
            session.delete(extra)
            removed += 1

    session.flush()
    return removed, len(work_ids)


def absorb_unverified_into_loss(session: Session) -> tuple[int, int]:
    """A book in the loss record is the copy the catalog already knew about.

    A Libib row and a flood record for the same title describe one physical
    object: the book that was catalogued before the flood and then destroyed.
    Keeping both makes a destroyed book look like a book you still own plus a
    separate loss, and shows three "copies" of something you have none of.

    The pre-flood collection is preserved on the surviving record — it is real
    provenance, and losing it would erase where the book used to live.

    ``re_acquired`` copies are NOT absorbed. A loss plus a replacement is two
    copies of one book — list_split._attach creates exactly that pair on
    purpose, because collapsing them loses the fact that a replacement exists.
    This routine once merged them anyway: after ``stacks repair-copies`` a
    re-bought flood loss showed BUY_REPLACE ("The flood took this — not
    replaced yet"), inviting a third copy — the exact failure the verdict
    engine exists to prevent.
    """
    work_ids = [
        wid for (wid,) in session.execute(
            select(Copy.work_id)
            .group_by(Copy.work_id)
            .having(
                func.count(Copy.id).filter(Copy.status == CopyStatus.lost_flood) > 0,
                func.count(Copy.id).filter(Copy.status == CopyStatus.unverified) > 0,
            )
        ).all()
    ]

    absorbed = 0
    for wid in work_ids:
        loss = session.scalar(
            select(Copy)
            .where(Copy.work_id == wid, Copy.status == CopyStatus.lost_flood)
            .order_by(Copy.id)
            .limit(1)
        )
        if loss is None:
            continue
        unverified = session.scalars(
            select(Copy)
            .where(
                Copy.work_id == wid,
                Copy.status == CopyStatus.unverified,
                Copy.provenance != Provenance.re_acquired,
            )
            .order_by(Copy.id)
        ).all()

        for u in unverified:
            merged = {*(loss.source_collections or []), *(u.source_collections or [])}
            loss.source_collections = sorted(merged)
            if u.edition_id and not loss.edition_id:
                loss.edition_id = u.edition_id
            if u.notes:
                loss.notes = f"{loss.notes or ''} || catalogued: {u.notes}".strip(" |")[:2000]
            session.delete(u)
            absorbed += 1

    session.flush()
    return absorbed, len(work_ids)


def run(session: Session) -> RepairStats:
    stats = RepairStats()
    stats.duplicate_losses_removed, stats.works_with_duplicate_losses = (
        collapse_duplicate_losses(session)
    )
    stats.unverified_absorbed, stats.works_absorbed = absorb_unverified_into_loss(session)
    return stats
