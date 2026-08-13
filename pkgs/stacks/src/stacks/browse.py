"""Shelves for browsing the library.

A catalog of three thousand books is unusable as a list. What makes it browsable
is the same thing that makes a video service browsable: a small number of rows,
each answering a question someone actually has.

The rows here are not generic. They are the questions this particular library
raises — what did the flood take, what has been replaced, what am I still
collecting — because those are the ones worth a horizontal scroll.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stacks.badges import compute as compute_badges
from stacks.badges import derive_status
from stacks.models import (
    Author,
    Copy,
    CopyStatus,
    Edition,
    Provenance,
    Series,
    Tag,
    WantRule,
    Work,
    WorkTag,
)

#: Books per row. Enough to feel deep, few enough to stay one query.
ROW_SIZE = 24


@dataclass
class ShelfItem:
    work_id: int
    title: str
    author: str | None
    isbn13: str | None
    status: str
    year: int | None = None
    cover_id: int | None = None
    badges: list[str] = field(default_factory=list)


@dataclass
class Shelf:
    key: str
    title: str
    subtitle: str | None = None
    total: int = 0
    items: list[ShelfItem] = field(default_factory=list)


def _status_of(present: int, unverified: int, lost: int, reacq: int) -> str:
    return derive_status(present, unverified, lost, reacq)


def _base_query():
    """Works with their holding counts and a representative ISBN."""
    return (
        select(
            Work.id,
            Work.title,
            Author.name,
            func.min(Edition.isbn13).label("isbn13"),
            func.max(Edition.publish_year).label("year"),
            # Prefer a printing we actually own; fall back to any with art.
            func.min(Edition.cover_id).filter(
                Edition.id.in_(select(Copy.edition_id).where(Copy.edition_id.is_not(None)))
            ).label("owned_cover_id"),
            func.min(Edition.cover_id).label("any_cover_id"),
            func.count(Copy.id).filter(Copy.status == CopyStatus.present).label("present"),
            func.count(Copy.id).filter(Copy.status == CopyStatus.unverified).label("unverified"),
            func.count(Copy.id).filter(Copy.status == CopyStatus.lost_flood).label("lost"),
            func.count(Copy.id)
            .filter(
                Copy.status == CopyStatus.unverified,
                Copy.provenance == Provenance.re_acquired,
            )
            .label("reacq"),
            # Wanted and assigned labels ride along so a tile can show the
            # strongest badge without a second query per book.
            func.bool_or(
                Work.primary_author_id.in_(
                    select(WantRule.author_id).where(WantRule.active.is_(True))
                ) | Work.series_id.in_(
                    select(WantRule.series_id).where(WantRule.active.is_(True))
                )
            ).label("wanted"),
            func.array_remove(func.array_agg(func.distinct(Tag.name)), None).label("tags"),
        )
        .select_from(Work)
        .outerjoin(Author, Author.id == Work.primary_author_id)
        .outerjoin(Edition, Edition.work_id == Work.id)
        .outerjoin(Copy, Copy.work_id == Work.id)
        .outerjoin(WorkTag, WorkTag.work_id == Work.id)
        .outerjoin(Tag, Tag.id == WorkTag.tag_id)
        .group_by(Work.id, Work.title, Author.name)
    )


def _rows_to_items(rows) -> list[ShelfItem]:
    return [
        ShelfItem(
            work_id=r[0], title=r[1], author=r[2], isbn13=r[3], year=r[4],
            # Owned printing first: the tile should look like the book indoors.
            cover_id=r[5] or r[6],
            status=_status_of(r[7], r[8], r[9], r[10]),
            badges=compute_badges(
                present=r[7], unverified=r[8], lost=r[9], reacq=r[10],
                wanted=bool(r[11]), assigned=list(r[12] or []),
            ).all,
        )
        for r in rows
    ]


def _shelf_by_status(
    session: Session, key: str, title: str, subtitle: str, having, limit: int = ROW_SIZE
) -> Shelf:
    q = _base_query().having(having).order_by(Work.title).limit(limit)
    items = _rows_to_items(session.execute(q).all())
    total = session.scalar(
        select(func.count()).select_from(
            _base_query().having(having).subquery()
        )
    ) or 0
    return Shelf(key=key, title=title, subtitle=subtitle, total=total, items=items)


def lost_shelf(session: Session, limit: int = ROW_SIZE) -> Shelf:
    return _shelf_by_status(
        session, "lost", "Lost in the flood",
        "Destroyed and not replaced — this is the replacement list",
        func.count(Copy.id).filter(Copy.status == CopyStatus.lost_flood) > 0,
        limit,
    )


def replaced_shelf(session: Session, limit: int = ROW_SIZE) -> Shelf:
    return _shelf_by_status(
        session, "replaced", "Replaced after the flood",
        "Bought again — not yet scanned back onto a shelf",
        func.count(Copy.id).filter(
            Copy.status == CopyStatus.unverified,
            Copy.provenance == Provenance.re_acquired,
        ) > 0,
        limit,
    )


def unconfirmed_shelf(session: Session, limit: int = ROW_SIZE) -> Shelf:
    return _shelf_by_status(
        session, "unconfirmed", "Catalogued before the flood",
        "In the 2023 export, unseen since — the sweep will settle these",
        func.count(Copy.id).filter(Copy.status == CopyStatus.unverified) > 0,
        limit,
    )


def series_shelves(session: Session, limit_rows: int = 8) -> list[Shelf]:
    """One row per series, biggest first."""
    rows = session.execute(
        select(Series.id, Series.name, func.count(Work.id).label("n"))
        .join(Work, Work.series_id == Series.id)
        .group_by(Series.id, Series.name)
        .order_by(func.count(Work.id).desc())
        .limit(limit_rows)
    ).all()

    out = []
    for sid, name, n in rows:
        q = _base_query().where(Work.series_id == sid).order_by(
            Work.series_position.nullslast(), Work.title
        ).limit(ROW_SIZE)
        out.append(
            Shelf(key=f"series:{sid}", title=name, subtitle=f"{n} in the catalog",
                  total=n, items=_rows_to_items(session.execute(q).all()))
        )
    return out


def author_shelves(session: Session, limit_rows: int = 8) -> list[Shelf]:
    rows = session.execute(
        select(Author.id, Author.name, func.count(Work.id).label("n"))
        .join(Work, Work.primary_author_id == Author.id)
        .group_by(Author.id, Author.name)
        .having(func.count(Work.id) >= 4)
        .order_by(func.count(Work.id).desc())
        .limit(limit_rows)
    ).all()

    out = []
    for aid, name, n in rows:
        q = _base_query().where(Work.primary_author_id == aid).order_by(Work.title).limit(ROW_SIZE)
        out.append(
            Shelf(key=f"author:{aid}", title=name, subtitle=f"{n} books",
                  total=n, items=_rows_to_items(session.execute(q).all()))
        )
    return out


def collection_shelves(session: Session, limit_rows: int = 8) -> list[Shelf]:
    """Rows by Libib collection — provenance, not location.

    The location-named collections are stale (everything is boxed), but they
    still say something true: which shelf a book was catalogued on in 2023, or
    which book sale it came home from.
    """
    rows = session.execute(
        select(
            func.unnest(Copy.source_collections).label("coll"),
            func.count(func.distinct(Copy.work_id)).label("n"),
        )
        .group_by("coll")
        .order_by(func.count(func.distinct(Copy.work_id)).desc())
        .limit(limit_rows)
    ).all()

    out = []
    for coll, n in rows:
        q = (
            _base_query()
            .where(Work.id.in_(
                select(Copy.work_id).where(Copy.source_collections.any(coll))
            ))
            .order_by(Work.title)
            .limit(ROW_SIZE)
        )
        out.append(
            Shelf(key=f"collection:{coll}", title=coll, subtitle=f"{n} books",
                  total=n, items=_rows_to_items(session.execute(q).all()))
        )
    return out


def wanted_shelf(session: Session, limit: int = ROW_SIZE) -> Shelf:
    """Books matching a standing want rule that we do not confirmably hold."""
    want_authors = select(WantRule.author_id).where(
        WantRule.active.is_(True), WantRule.author_id.is_not(None)
    )
    want_series = select(WantRule.series_id).where(
        WantRule.active.is_(True), WantRule.series_id.is_not(None)
    )
    q = (
        _base_query()
        .where(
            Work.primary_author_id.in_(want_authors) | Work.series_id.in_(want_series)
        )
        .order_by(Work.title)
        .limit(limit)
    )
    items = _rows_to_items(session.execute(q).all())
    return Shelf(key="wanted", title="On your want list",
                 subtitle="Authors and series you're collecting",
                 total=len(items), items=items)


def all_shelves(session: Session) -> list[Shelf]:
    """The browse page, in the order the questions actually get asked."""
    shelves = [
        lost_shelf(session),
        replaced_shelf(session),
        wanted_shelf(session),
        *series_shelves(session),
        *author_shelves(session),
        *collection_shelves(session),
        unconfirmed_shelf(session),
    ]
    return [s for s in shelves if s.items]


def shelf_by_key(session: Session, key: str, limit: int = 500) -> Shelf | None:
    """Resolve a shelf key to its full contents, for a page of its own.

    Keys match what :func:`all_shelves` emits, so a shelf heading can link
    straight to itself without the caller inventing a second vocabulary.
    """
    if key == "lost":
        return lost_shelf(session, limit)
    if key == "replaced":
        return replaced_shelf(session, limit)
    if key == "unconfirmed":
        return unconfirmed_shelf(session, limit)
    if key == "wanted":
        return wanted_shelf(session, limit)

    kind, _, value = key.partition(":")
    if not value:
        return None

    if kind == "series":
        row = session.execute(
            select(Series.name).where(Series.id == int(value))
        ).first()
        if row is None:
            return None
        q = (
            _base_query().where(Work.series_id == int(value))
            .order_by(Work.series_position.nullslast(), Work.title).limit(limit)
        )
        items = _rows_to_items(session.execute(q).all())
        return Shelf(key=key, title=row[0], subtitle=f"{len(items)} in the catalog",
                     total=len(items), items=items)

    if kind == "author":
        row = session.execute(
            select(Author.name).where(Author.id == int(value))
        ).first()
        if row is None:
            return None
        q = _base_query().where(Work.primary_author_id == int(value)) \
            .order_by(Work.title).limit(limit)
        items = _rows_to_items(session.execute(q).all())
        return Shelf(key=key, title=row[0], subtitle=f"{len(items)} books",
                     total=len(items), items=items)

    if kind == "collection":
        q = (
            _base_query()
            .where(Work.id.in_(
                select(Copy.work_id).where(Copy.source_collections.any(value))
            ))
            .order_by(Work.title).limit(limit)
        )
        items = _rows_to_items(session.execute(q).all())
        return Shelf(key=key, title=value, subtitle=f"{len(items)} books",
                     total=len(items), items=items)

    if kind == "tag":
        from stacks.models import Tag, WorkTag

        q = (
            _base_query()
            .where(Work.id.in_(
                select(WorkTag.work_id).join(Tag, Tag.id == WorkTag.tag_id)
                .where(Tag.name == value.upper())
            ))
            .order_by(Work.title).limit(limit)
        )
        items = _rows_to_items(session.execute(q).all())
        return Shelf(key=key, title=value.upper(), subtitle=f"{len(items)} books",
                     total=len(items), items=items)

    return None
