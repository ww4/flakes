"""Load the parsed flood document into the catalog.

Three things make this harder than a CSV import, and each is handled explicitly:

1. **The two halves overlap ~58%.** A title written in the hand-sorted section
   and again in the photo-transcribed section is *one* destroyed book. Records
   are folded before any copy is created, or ~170 books would be double-counted.

2. **Most of these were never catalogued.** Only ~31% of destroyed titles appear
   in the Libib export — the flood hit the uncatalogued children's readers
   hardest. So a flood record must *match into* the existing catalog where it
   can, and create a bare work (no ISBN, no edition) where it cannot.

3. **Blue does not mean one thing.** Per the owner: ordered and offered books
   should count as acquired, but must not enter the catalog as shelved until
   someone scans them. That is exactly the ``unverified`` / ``present`` split —
   they load as ``unverified`` with ``re_acquired`` provenance, so they stay off
   the wishlist while never claiming to be on a shelf.

Matching into the existing catalog uses pg_trgm similarity. The threshold is
deliberately conservative: many destroyed titles are generic children's readers
("Hidden treasure", "Silver sails"), and a false merge silently tells someone
they own a book they do not. Every match records its score for audit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stacks.importers.flood_doc import FloodRecord, Half, parse, titles_only
from stacks.models import Copy, CopyStatus, Provenance, Work
from stacks.normalize import normalize_title, numeric_conflict

log = logging.getLogger(__name__)

#: Below this trigram score we create a new work rather than merge into an
#: existing one. A false merge is worse than a duplicate: a duplicate shows up
#: as an extra wishlist row, a false merge marks a book you own as destroyed.
MATCH_THRESHOLD = 0.72


@dataclass
class FloodStats:
    records: int = 0
    folded: int = 0
    distinct: int = 0
    matched_existing: int = 0
    works_created: int = 0
    vetoed_numeric: int = 0
    merged_onto_work: int = 0
    converted_libib: int = 0
    lost: int = 0
    believed_acquired: int = 0
    not_lost: int = 0
    borderline: list[tuple[str, str, float]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.records} title lines -> {self.distinct} distinct books "
            f"({self.folded} folded across the two halves); "
            f"{self.matched_existing} matched the existing catalog, "
            f"{self.works_created} new works; "
            f"{self.lost} lost, {self.believed_acquired} believed re-acquired, "
            f"{self.not_lost} not actually lost; "
            f"{self.converted_libib} converted an existing Libib holding, "
            f"{self.merged_onto_work} merged as corroboration"
        )


def _status_for(rec: FloodRecord) -> tuple[CopyStatus, Provenance, str]:
    """Map the document's annotations onto a holding state.

    Returns (status, provenance, reason).
    """
    res = set(rec.resolutions)

    if "not_lost" in res:
        # "there but no pic" — it survived; it was only missing from the photos.
        return CopyStatus.unverified, Provenance.flood_doc, "survived (there but no pic)"

    if "ordered" in res or "offered_by_other" in res:
        # Treated as acquired, but not shelved until scanned.
        why = "ordered" if "ordered" in res else "offered by someone else"
        return CopyStatus.unverified, Provenance.re_acquired, why

    if rec.is_blue:
        # Blue with no other annotation: a replacement was found.
        return CopyStatus.unverified, Provenance.re_acquired, "replacement found (blue)"

    return CopyStatus.lost_flood, Provenance.flood_doc, "destroyed"


def _fold(records: list[FloodRecord]) -> dict[str, FloodRecord]:
    """Collapse the two halves onto one record per normalised title.

    Prefers whichever record carries annotations, since the sorted half holds
    all the colour and status information and the photos half holds none.
    """
    out: dict[str, FloodRecord] = {}
    for r in records:
        key = normalize_title(r.title)
        if not key or len(key) < 3:
            continue
        prev = out.get(key)
        if prev is None:
            out[key] = r
            continue
        # Keep the more informative of the two.
        prev_score = (prev.is_blue, bool(prev.resolutions), prev.half is Half.sorted_by_grade)
        this_score = (r.is_blue, bool(r.resolutions), r.half is Half.sorted_by_grade)
        if this_score > prev_score:
            out[key] = r
    return out


def load(session: Session, path: Path, threshold: float = MATCH_THRESHOLD) -> FloodStats:
    stats = FloodStats()
    records = titles_only(parse(path))
    stats.records = len(records)

    folded = _fold(records)
    stats.distinct = len(folded)
    stats.folded = stats.records - stats.distinct

    # Work id -> the copy this run already created or converted for it.
    #
    # Folding by normalised title is not enough. Two lines can differ in
    # wording ("Magic School Bus and the Butterfly Bunch" in the sorted half,
    # a shorter transcription in the photos half) yet resolve to the same
    # catalogue work, producing two loss records for one book. Dedupe on the
    # resolved target, which is the thing that actually has to be unique.
    handled: dict[int, Copy] = {}

    for norm_key, rec in folded.items():
        sim = func.similarity(Work.sort_title, norm_key)
        # Take several candidates: the top trigram hit may be vetoed by the
        # volume-number check, and the right book can be just behind it.
        candidates = session.execute(
            select(Work, sim.label("score"))
            .where(sim > threshold)
            .order_by(sim.desc())
            .limit(5)
        ).all()

        row = next(
            (c for c in candidates if not numeric_conflict(norm_key, c[0].sort_title)),
            None,
        )
        if row is None and candidates:
            stats.vetoed_numeric += 1

        if row is not None:
            work, score = row[0], float(row[1])
            stats.matched_existing += 1
            if score < threshold + 0.12:
                stats.borderline.append((rec.title, work.title, score))
        else:
            work = Work(
                title=rec.title,
                sort_title=norm_key,
                ol_work_keys=[],
            )
            session.add(work)
            session.flush()
            stats.works_created += 1
            score = 0.0

        status, provenance, reason = _status_for(rec)
        if status is CopyStatus.lost_flood:
            stats.lost += 1
        elif provenance is Provenance.re_acquired:
            stats.believed_acquired += 1
        else:
            stats.not_lost += 1

        note = f"flood doc [{rec.half.value} line {rec.index}]: {reason}"
        if rec.section:
            note += f" | section: {rec.section}"
        if rec.grade:
            note += f" | grade {rec.grade}"
        if rec.annotations:
            note += " | " + "; ".join(rec.annotations)
        if score:
            note += f" | matched existing work @ {score:.2f}"
        if rec.starred:
            note += " | starred (*) — meaning unknown"

        # Another line already accounted for this book. Record the corroboration
        # in the note rather than inventing a second lost copy.
        if work.id in handled:
            prior = handled[work.id]
            prior.notes = f"{prior.notes or ''} || also {note}"[:2000]
            stats.merged_onto_work += 1
            continue

        # A flood record is evidence about a book the Libib export may already
        # describe — the same physical object, not an extra one. Convert the
        # existing unverified holding rather than adding alongside it, so a book
        # that was catalogued and then destroyed reads as one book, lost.
        existing = session.scalar(
            select(Copy)
            .where(Copy.work_id == work.id, Copy.status == CopyStatus.unverified)
            .order_by(Copy.id)
            .limit(1)
        )
        if existing is not None:
            existing.status = status
            existing.provenance = provenance
            existing.notes = f"{existing.notes or ''} || {note}".strip(" |")[:2000]
            handled[work.id] = existing
            stats.converted_libib += 1
        else:
            fresh = Copy(
                work_id=work.id,
                status=status,
                provenance=provenance,
                notes=note[:2000],
            )
            session.add(fresh)
            session.flush()
            handled[work.id] = fresh

    session.flush()
    return stats
