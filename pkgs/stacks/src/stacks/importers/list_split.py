"""Split loss-document lines that name several books into one record each.

The document has lines like ``Cam janson had: corn popper, camp mystery, it's
a raid, ...``. The parser made each one a single work with a 200-character
title. That is not a book: it cannot be scanned, it cannot be matched at a
sale, and the twenty real books it records are invisible. Splitting them is
data recovery, not tidying.

Which line names which books is human judgement and lives in
``data/list-records.toml``. This module only applies it.

The fragments are verbatim from the document and mostly partial — "corn
popper", "zoo note". Rather than expand them by hand, which would invent
titles, each is resolved against the catalog, which already holds twelve Cam
Jansen books and eleven Boxcar Children from the Libib export. A fragment that
matches lands the loss on the real book. One that does not becomes its own
record, named for what the document actually said, so nothing is lost.

Matching is deliberately two-stage, because the obvious instrument is the
wrong one. Trigram similarity scores shared trigrams over total length, so a
short fragment against a long title scores low however exactly it appears
inside it — every Cam Jansen fragment fell below the threshold while the book
sat in the catalog. Within a known series the fragment is a *piece* of the
title, so containment is the signal that applies. Only when the series scope
finds nothing does it fall back to whole-catalog trigram at the flood
importer's threshold.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stacks.models import Copy, CopyStatus, Provenance, Work
from stacks.normalize import normalize_title, numeric_conflict

#: Whole-catalog fallback, matching the flood importer.
GLOBAL_THRESHOLD = 0.72

#: Runs of two or more single letters, left by punctuation stripping.
_INITIALS = re.compile(r"\b(?:[a-z] ){1,}[a-z]\b")


@dataclass
class SplitStats:
    parents: int = 0
    deleted: int = 0
    children: int = 0
    matched_existing: int = 0
    works_created: int = 0
    volumes_recorded: int = 0
    #: (fragment, matched title, score) — worth a human glance.
    matches: list[tuple[str, str, float]] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.parents} list records split into {self.children} books "
            f"({self.matched_existing} matched an existing work, "
            f"{self.works_created} created, {self.volumes_recorded} recorded by "
            f"volume number); {self.deleted} deleted as junk"
        )


def _match_key(text: str) -> str:
    """Normalise for comparison, then close up initialisms.

    ``normalize_title`` drops punctuation, so "U.F.O." becomes "u f o" while
    the catalog holds "UFO". Collapsing runs of single letters makes those the
    same string. Only runs of two or more collapse, so "I have to go" is left
    alone.
    """
    s = normalize_title(text)
    return _INITIALS.sub(lambda m: m.group(0).replace(" ", ""), s)


def _resolve(
    session: Session, series: str, fragment: str, exclude: set[int]
) -> tuple[Work | None, float]:
    """Find the work a fragment names, preferring candidates inside the series.

    Within a series the fragment is a piece of a longer title — "zoo note" out
    of "Young Cam Jansen and the Zoo Note Mystery" — and raw trigram similarity
    is the wrong instrument for that: it scores by shared trigrams over total
    length, so a short fragment against a long title scores low no matter how
    exactly it appears inside it. Every Cam Jansen fragment fell below the
    threshold while the book sat in the catalog. Containment is the signal that
    actually applies; similarity only breaks ties.
    """
    key = _match_key(fragment)
    series_key = _match_key(series)

    candidates = session.scalars(
        select(Work).where(Work.sort_title.contains(normalize_title(series)))
    ).all()

    best: tuple[Work, float] | None = None
    for cand in candidates:
        if cand.id in exclude:
            continue
        cand_key = _match_key(cand.sort_title)
        if key not in cand_key:
            continue
        if numeric_conflict(key, cand.sort_title):
            continue
        # Prefer the tightest container: "baseball" is inside both "the
        # baseball mystery" and "the babe ruth baseball mystery", and the
        # shorter one is the better reading of a bare "baseball".
        score = len(key) / max(len(cand_key.replace(series_key, "").strip()), len(key))
        if best is None or score > best[1]:
            best = (cand, min(score, 1.0))
    if best is not None:
        return best

    sim = func.similarity(Work.sort_title, normalize_title(fragment))
    loose = session.execute(
        select(Work, sim.label("score"))
        .where(sim > GLOBAL_THRESHOLD, Work.id.notin_(exclude) if exclude else sim > 0)
        .order_by(sim.desc())
        .limit(5)
    ).all()
    row = next(
        (c for c in loose if not numeric_conflict(normalize_title(fragment), c[0].sort_title)),
        None,
    )
    if row is not None:
        return row[0], float(row[1])
    return None, 0.0


def _parent_state(session: Session, work: Work) -> tuple[CopyStatus, Provenance, str]:
    """What the parent line asserted, so every child inherits it.

    The flood importer already decided whether the line was a loss or a set of
    replacements — "had:" against "have:" — and encoded it in the copy. Reading
    it back is more reliable than re-deriving it from the title.
    """
    copy = session.scalar(
        select(Copy).where(Copy.work_id == work.id).order_by(Copy.id).limit(1)
    )
    if copy is None:
        return CopyStatus.unverified, Provenance.flood_doc, ""
    return copy.status, copy.provenance, copy.notes or ""


def load(session: Session, path: Path) -> SplitStats:
    stats = SplitStats()
    spec = tomllib.loads(path.read_text())

    # Every parent is itself a work with a title full of the words we are about
    # to search for, so "gold coins" would match the very blob it came out of.
    # Collect them all up front: a later record's fragment can otherwise match
    # an earlier record's parent that has not been removed yet.
    parents = {r["work_id"] for r in spec.get("record", [])}

    for rec in spec.get("record", []):
        parent = session.get(Work, rec["work_id"])
        if parent is None:
            continue

        if rec.get("delete"):
            _drop_work(session, parent)
            stats.deleted += 1
            continue

        stats.parents += 1
        status, provenance, parent_note = _parent_state(session, parent)
        series = rec["series"]
        origin = parent_note.split("|")[0].strip() or f"work {parent.id}"

        for fragment in rec.get("fragments", []):
            work, score = _resolve(session, series, fragment, parents)
            if work is not None and work.id != parent.id:
                stats.matched_existing += 1
                stats.matches.append((fragment, work.title, score))
            else:
                title = _new_title(fragment, series, rec.get("is_author", False))
                work = Work(title=title, sort_title=normalize_title(title), ol_work_keys=[])
                session.add(work)
                session.flush()
                stats.works_created += 1
                stats.unmatched.append(title)
                score = 0.0

            note = f"split from {origin} — document listed '{fragment}' under {series}"
            if score:
                note += f" | matched existing work @ {score:.2f}"
            _attach(session, work, status, provenance, note)
            stats.children += 1

        volumes = rec.get("volumes", []) + rec.get("special_volumes", [])
        for vol in volumes:
            title = f"{series} #{vol}"
            work = session.scalar(
                select(Work).where(Work.sort_title == normalize_title(title))
            )
            if work is None:
                work = Work(title=title, sort_title=normalize_title(title), ol_work_keys=[])
                session.add(work)
                session.flush()
                stats.works_created += 1
            note = (
                f"split from {origin} — document listed volume {vol} of {series}. "
                "Title not resolved: Open Library cannot look a series volume up "
                "by number reliably."
            )
            _attach(session, work, status, provenance, note)
            stats.children += 1
            stats.volumes_recorded += 1

        # The parent said "these several books"; the children now say it one book
        # at a time. Leaving it would double-count every one of them.
        _drop_work(session, parent)

    session.flush()
    return stats


def _new_title(fragment: str, series: str, is_author: bool) -> str:
    """Name a book the catalog does not already know.

    Series entries are prefixed unconditionally, even when the fragment reads
    like a complete title. That is partly accuracy — this really is the
    Bullseye printing of *The Three Musketeers* — but mostly it is what makes
    the next fragment findable. The document lists the same Cam Jansen books
    twice, once as lost and once as replaced, in different words each time
    ("Mystery of the Stolen Corn Popper" and "corn popper"). The second only
    finds the first if the first is filed under the series, because that is
    what the series-scoped search looks for. Without the prefix the two land
    on separate records and the book is silently counted twice.

    An author is not a series, so those keep the fragment when it already
    stands as a title.
    """
    if _match_key(series) in _match_key(fragment):
        return fragment
    if is_author:
        words = fragment.split()
        complete = len(words) >= 3 and (
            fragment[:1].isupper() or words[0].lower() in {"the", "a", "an"}
        )
        if complete:
            return fragment
    return f"{series}: {fragment}"


def _attach(
    session: Session, work: Work, status: CopyStatus, provenance: Provenance, note: str
) -> None:
    """Record the holding without double-counting or flattening it.

    Two different things can look alike here and must not be merged:

    * A Libib row plus a flood loss is **one** book that was catalogued and
      then destroyed, so the loss converts the existing holding — the flood
      importer's rule, kept.
    * A loss plus a replacement is **two** copies of one book: the original the
      water took, and the one they bought again. The document says both, on
      separate lines ("had:" and "have:"), and collapsing them loses the fact
      that a replacement exists. An early version converted here too and turned
      every re-bought Cam Jansen into a plain loss.

    Anything already asserted with the same status and provenance is a repeat
    of the same claim — the document lists some books twice — and only adds a
    note.
    """
    same = session.scalar(
        select(Copy)
        .where(
            Copy.work_id == work.id,
            Copy.status == status,
            Copy.provenance == provenance,
        )
        .limit(1)
    )
    if same is not None:
        same.notes = f"{same.notes or ''} || also {note}".strip(" |")[:2000]
        return

    if status is CopyStatus.lost_flood:
        catalogued = session.scalar(
            select(Copy)
            .where(
                Copy.work_id == work.id,
                Copy.status == CopyStatus.unverified,
                Copy.provenance == Provenance.libib_import,
            )
            .order_by(Copy.id)
            .limit(1)
        )
        if catalogued is not None:
            catalogued.status = status
            catalogued.provenance = provenance
            catalogued.notes = f"{catalogued.notes or ''} || {note}".strip(" |")[:2000]
            return

    session.add(Copy(work_id=work.id, status=status, provenance=provenance, notes=note[:2000]))
    session.flush()


def _drop_work(session: Session, work: Work) -> None:
    """Remove a work and everything that points at it.

    Delegates to workops.delete_work — the one shared implementation — after
    this file's copy was found missing want_rules/requests detachment (the
    same class of forgotten-table bug in three hand-rolled copies).
    """
    from stacks import workops

    workops.delete_work(session, work)
