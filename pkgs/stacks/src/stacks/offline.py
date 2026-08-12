"""The offline set — what the phone carries to a book sale.

A book sale happens in a church basement or a gym with no usable signal, and the
one question that matters ("do we already own this?") must be answerable in the
half-second between picking a book up and putting it back. So the whole answer
travels with the phone.

Because enrichment expands every owned work into all of its known editions, the
set below already contains every ISBN that means "a book we own", including
printings we have never held. That turns the sale-day check into an exact set
membership test — no fuzzy matching, no network, no latency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stacks.models import Author, Copy, CopyStatus, Edition, Provenance, WantRule, Work

#: Bump when the payload shape changes so cached clients refetch.
SCHEMA_VERSION = 3


@dataclass(slots=True)
class OfflineSet:
    version: int
    generated_at: str
    isbns: dict[str, dict]
    works: dict[str, dict]
    want_authors: list[str]
    want_series: list[str]

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "generated_at": self.generated_at,
            "isbns": self.isbns,
            "works": self.works,
            "want_authors": self.want_authors,
            "want_series": self.want_series,
        }


def build(session: Session) -> OfflineSet:
    """Assemble the payload.

    Keys are deliberately short. This ships tens of thousands of entries to a
    phone over whatever connection it had before leaving the house, and the
    difference between ``"present"`` and ``"p"`` across 100k rows is real.
    """
    holdings = {
        wid: {"p": p, "u": u, "l": lost, "r": reacq}
        for wid, p, u, lost, reacq in session.execute(
            select(
                Copy.work_id,
                func.count(Copy.id).filter(Copy.status == CopyStatus.present),
                func.count(Copy.id).filter(Copy.status == CopyStatus.unverified),
                func.count(Copy.id).filter(Copy.status == CopyStatus.lost_flood),
                # Re-acquired copies must travel too: without this the phone
                # would tell someone to replace a book they already re-bought.
                func.count(Copy.id).filter(
                    Copy.status == CopyStatus.unverified,
                    Copy.provenance == Provenance.re_acquired,
                ),
            ).group_by(Copy.work_id)
        ).all()
    }

    # Only works we have some holding record for are worth shipping; a work
    # that exists purely as an enrichment artefact tells the scanner nothing.
    work_rows = session.execute(
        select(Work.id, Work.title, Work.desired_copies, Author.name)
        .outerjoin(Author, Author.id == Work.primary_author_id)
        .where(Work.id.in_(holdings.keys()))
    ).all()

    works: dict[str, dict] = {}
    for wid, title, desired, author in work_rows:
        h = holdings.get(wid, {})
        works[str(wid)] = {
            "t": title,
            "a": author or "",
            "d": desired,
            "p": h.get("p", 0),
            "u": h.get("u", 0),
            "l": h.get("l", 0),
            "r": h.get("r", 0),
        }

    isbns: dict[str, int] = {}
    for isbn, wid in session.execute(
        select(Edition.isbn13, Edition.work_id)
        .where(Edition.isbn13.is_not(None), Edition.work_id.in_(holdings.keys()))
    ).all():
        isbns[isbn] = wid

    want_authors = [
        name
        for (name,) in session.execute(
            select(Author.name)
            .join(WantRule, WantRule.author_id == Author.id)
            .where(WantRule.active.is_(True))
            .distinct()
        ).all()
        if name
    ]
    want_series = [
        label
        for (label,) in session.execute(
            select(WantRule.label).where(
                WantRule.active.is_(True), WantRule.series_id.is_not(None)
            ).distinct()
        ).all()
    ]

    return OfflineSet(
        version=SCHEMA_VERSION,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        isbns={k: {"w": v} for k, v in isbns.items()},
        works=works,
        want_authors=sorted(want_authors),
        want_series=sorted(want_series),
    )
