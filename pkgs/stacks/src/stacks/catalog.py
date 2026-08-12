"""The whole catalog, small enough to hand over.

Measured: 3,045 works with descriptions, 55,285 editions, 3,103 copies and the
want rules come to **1.2 MB gzipped**. That is smaller than one photograph, so
there is no reason for the phone to hold a lean summary and go asking for the
rest. It holds everything.

The server stays authoritative while there is a connection — one search
ranking, one shelf builder, one place for bugs. This payload is what makes the
offline case *rich* rather than merely functional: with no signal you still get
titles, authors, series, descriptions, every printing, and what you own.

Keys are single letters throughout. Across 55,000 editions the difference
between ``"publisher"`` and ``"p"`` is most of a megabyte.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from stacks.models import (
    Author,
    Copy,
    Edition,
    Provenance,
    Series,
    WantRule,
    Work,
)

#: Bump when the shape changes so cached clients refetch rather than
#: misinterpret. The client refuses a payload whose version it does not know.
SCHEMA_VERSION = 4


def build(session: Session) -> dict:
    authors = {a.id: a.name for a in session.scalars(select(Author))}
    series = {s.id: s.name for s in session.scalars(select(Series))}

    works: dict[str, dict] = {}
    for w in session.scalars(select(Work)):
        works[str(w.id)] = {
            "t": w.title,
            "a": authors.get(w.primary_author_id),
            "s": series.get(w.series_id),
            "n": w.series_position,
            "d": w.desired_copies,
            "x": w.description,
            # Holding counts are filled in below rather than joined, because
            # the client needs them on every verdict and a nested lookup on a
            # phone is slower than four integers sitting right here.
            "p": 0, "u": 0, "l": 0, "r": 0,
        }

    isbns: dict[str, int] = {}
    editions: dict[str, list] = {}
    for e in session.scalars(select(Edition)):
        key = str(e.work_id)
        if e.isbn13:
            isbns[e.isbn13] = e.work_id
        editions.setdefault(key, []).append({
            "n": e.isbn13, "p": e.publisher, "y": e.publish_year,
            "b": e.binding, "c": e.cover_id,
        })

    copies: dict[str, list] = {}
    edition_isbn = {
        eid: isbn for eid, isbn in session.execute(
            select(Edition.id, Edition.isbn13).where(Edition.isbn13.is_not(None))
        ).all()
    }
    for c in session.scalars(select(Copy)):
        key = str(c.work_id)
        copies.setdefault(key, []).append({
            "s": c.status.value,
            "v": c.provenance.value,
            "c": list(c.source_collections or []),
            "n": edition_isbn.get(c.edition_id),
            "o": (c.notes or "")[:160] or None,
        })
        w = works.get(key)
        if w is None:
            continue
        if c.status.value == "present":
            w["p"] += 1
        elif c.status.value == "unverified":
            w["u"] += 1
            if c.provenance is Provenance.re_acquired:
                w["r"] += 1
        elif c.status.value == "lost_flood":
            w["l"] += 1

    want_authors = sorted({
        authors[a] for (a,) in session.execute(
            select(WantRule.author_id).where(
                WantRule.active.is_(True), WantRule.author_id.is_not(None)
            )
        ).all() if a in authors
    })
    want_series = sorted({
        series[s] for (s,) in session.execute(
            select(WantRule.series_id).where(
                WantRule.active.is_(True), WantRule.series_id.is_not(None)
            )
        ).all() if s in series
    })

    return {
        "version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "works": works,
        "isbns": isbns,
        "editions": editions,
        "copies": copies,
        "want_authors": want_authors,
        "want_series": want_series,
    }
