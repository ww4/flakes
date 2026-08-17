"""Libib CSV import.

Libib's export is the pre-flood baseline: it asserts *we owned this in 2024*,
which is not the same as *this book is on a shelf*. Every copy created here
therefore lands as ``CopyStatus.unverified``. The physical sweep promotes rows
to ``present``; whatever is still unverified once a shelf is swept is a
candidate flood loss. Nobody has to remember what was destroyed.

**One export per collection, and collections overlap.** The library was exported
as 16 files, one per Libib collection, and a book catalogued in two collections
is one physical book listed twice — not two copies. So rows are aggregated
across *all* files by identity first, and only then turned into copies.

**Collections are provenance, not location.** Several are named for rooms
("Frankfort living room"), but the family moved out of the flooded house, boxed
everything, and moved back — nothing is where those names claim. Real locations
come from the physical sweep. Others are hauls ("November haul") or set-asides
("Pulled for Peter"), which are genuinely useful signals, so all of them are
preserved verbatim on the copy.

Export columns (superset of the documented import format):
    item_type, title, creators, first_name, last_name, collection,
    ean_isbn13, upc_isbn10, description, publisher, publish_date, group, tags,
    notes, price, ..., status, began, completed, added, copies
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stacks.models import (
    Author,
    Copy,
    CopyStatus,
    Edition,
    Provenance,
    Series,
    Work,
)
from stacks.normalize import (
    normalize_author,
    normalize_title,
    split_creators,
    to_isbn10,
    to_isbn13,
    year_from,
)

log = logging.getLogger(__name__)

csv.field_size_limit(10_000_000)


@dataclass
class ImportStats:
    files: int = 0
    rows: int = 0
    holdings: int = 0
    works_created: int = 0
    editions_created: int = 0
    copies_created: int = 0
    copies_already_present: int = 0
    series_created: int = 0
    with_isbn: int = 0
    without_isbn: int = 0
    bad_isbn: int = 0
    skipped_no_title: int = 0
    merged_across_collections: int = 0
    collections: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.files} files / {self.rows} rows -> {self.holdings} distinct holdings; "
            f"{self.works_created} works, {self.editions_created} editions, "
            f"{self.copies_created} copies, {self.series_created} series "
            f"({self.with_isbn} with ISBN, {self.without_isbn} without, "
            f"{self.bad_isbn} unparseable)"
        )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip()
    return v or None


def _int_or(value: str | None, default: int) -> int:
    try:
        n = int(str(value).strip())
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


def read_rows(path: Path) -> Iterator[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames:
            reader.fieldnames = [(f or "").strip().lower() for f in reader.fieldnames]
        yield from reader


def author_name_from(row: dict[str, str]) -> str | None:
    """Prefer the split name columns over the packed ``creators`` field.

    ``creators`` is ambiguous: a comma in "Le Guin, Ursula K." reverses a name,
    but a comma in "Gaiman, Neil, Pratchett, Terry" separates co-authors, and
    the string alone cannot tell you which. The export gives first_name and
    last_name separately, which removes the guess entirely.
    """
    last = _clean(row.get("last_name"))
    first = _clean(row.get("first_name"))
    if last:
        return f"{first} {last}".strip() if first else last
    creators = split_creators(row.get("creators"))
    return creators[0] if creators else None


@dataclass
class Holding:
    """One book, aggregated across every collection it appears in."""

    title: str
    author: str | None
    isbn13: str | None
    isbn10: str | None
    publisher: str | None
    publish_date: str | None
    description: str | None
    series: str | None
    collections: set[str] = field(default_factory=set)
    tags: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    copies: int = 1
    rows: int = 0


def aggregate(paths: Iterable[Path], stats: ImportStats) -> dict[tuple, Holding]:
    """Fold every row of every file into distinct holdings.

    Identity is the ISBN-13 when there is one, otherwise normalised
    title+author. That is deliberately conservative: two different printings of
    the same book stay distinct here, and Open Library enrichment merges them
    later by resolving both to one work.
    """
    holdings: dict[tuple, Holding] = {}

    for path in sorted(paths):
        stats.files += 1
        for row in read_rows(path):
            stats.rows += 1

            title = _clean(row.get("title"))
            if not title:
                stats.skipped_no_title += 1
                continue

            collection = _clean(row.get("collection")) or "(none)"
            stats.collections[collection] = stats.collections.get(collection, 0) + 1

            raw13 = _clean(row.get("ean_isbn13"))
            raw10 = _clean(row.get("upc_isbn10"))
            isbn13 = to_isbn13(raw13) or to_isbn13(raw10)
            if isbn13:
                stats.with_isbn += 1
            elif raw13 or raw10:
                stats.bad_isbn += 1
                stats.warnings.append(f"unparseable ISBN on {title!r}: {raw13 or raw10!r}")
            else:
                stats.without_isbn += 1

            author = author_name_from(row)
            key = (
                ("isbn", isbn13)
                if isbn13
                else ("ta", normalize_title(title), normalize_author(author))
            )

            h = holdings.get(key)
            if h is None:
                h = Holding(
                    title=title,
                    author=author,
                    isbn13=isbn13,
                    isbn10=to_isbn10(raw10),
                    publisher=_clean(row.get("publisher")),
                    publish_date=_clean(row.get("publish_date")),
                    description=_clean(row.get("description")),
                    series=_clean(row.get("group")),
                    copies=_int_or(row.get("copies"), 1),
                )
                holdings[key] = h
            else:
                stats.merged_across_collections += 1
                # A book listed in two collections is one book. Take the larger
                # declared copy count rather than summing.
                h.copies = max(h.copies, _int_or(row.get("copies"), 1))
                h.series = h.series or _clean(row.get("group"))
                h.description = h.description or _clean(row.get("description"))

            h.rows += 1
            h.collections.add(collection)
            for t in (row.get("tags") or "").split(","):
                if t.strip():
                    h.tags.add(t.strip())
            if n := _clean(row.get("notes")):
                h.notes.append(n)

    stats.holdings = len(holdings)
    return holdings


def _get_or_create_author(session: Session, name: str, cache: dict) -> Author:
    sort_name = normalize_author(name)
    if sort_name in cache:
        return cache[sort_name]
    existing = session.scalar(select(Author).where(Author.sort_name == sort_name))
    if existing is None:
        existing = Author(name=name, sort_name=sort_name)
        session.add(existing)
        session.flush()
    cache[sort_name] = existing
    return existing


def _get_or_create_series(session: Session, name: str, cache: dict, stats: ImportStats) -> Series:
    if name in cache:
        return cache[name]
    existing = session.scalar(select(Series).where(Series.name == name))
    if existing is None:
        existing = Series(name=name)
        session.add(existing)
        session.flush()
        stats.series_created += 1
    cache[name] = existing
    return existing


def import_libib_exports(
    session: Session,
    paths: Iterable[Path],
    owner_household_id: int | None = None,
    default_status: CopyStatus = CopyStatus.unverified,
) -> ImportStats:
    stats = ImportStats()
    holdings = aggregate(paths, stats)

    authors: dict[str, Author] = {}
    series_cache: dict[str, Series] = {}

    for h in holdings.values():
        author = _get_or_create_author(session, h.author, authors) if h.author else None
        series = (
            _get_or_create_series(session, h.series, series_cache, stats) if h.series else None
        )

        sort_title = normalize_title(h.title)
        stmt = select(Work).where(Work.sort_title == sort_title)
        if author is not None:
            stmt = stmt.where(Work.primary_author_id == author.id)
        work = session.scalar(stmt)
        if work is None:
            work = Work(
                title=h.title,
                sort_title=sort_title,
                description=h.description,
                primary_author_id=author.id if author else None,
                series_id=series.id if series else None,
            )
            session.add(work)
            session.flush()
            stats.works_created += 1
        elif series is not None and work.series_id is None:
            work.series_id = series.id

        edition: Edition | None = None
        if h.isbn13:
            edition = session.scalar(select(Edition).where(Edition.isbn13 == h.isbn13))
            if edition is None:
                edition = Edition(
                    work_id=work.id,
                    isbn13=h.isbn13,
                    isbn10=h.isbn10,
                    publisher=h.publisher,
                    publish_date=h.publish_date,
                    publish_year=year_from(h.publish_date),
                )
                session.add(edition)
                session.flush()
                stats.editions_created += 1
            elif edition.work_id != work.id:
                # The ISBN already belongs to another work's edition — common
                # after enrichment, when a differently-worded title resolved
                # elsewhere. The ISBN is the stronger identity: follow it.
                # The old behavior kept the title-matched work AND the foreign
                # edition, minting a copy whose work and edition.work
                # disagreed — silently breaking every "owned isbn" join
                # downstream (2026-08 audit M3).
                resolved = session.get(Work, edition.work_id)
                if resolved is not None:
                    stats.warnings.append(
                        f"{h.title!r}: ISBN {h.isbn13} already belongs to "
                        f"{resolved.title!r} — importing the copy there"
                    )
                    work = resolved

        blob_parts = list(h.notes)
        if h.tags:
            blob_parts.append("tags: " + ", ".join(sorted(h.tags)))
        blob = " | ".join(blob_parts) or None

        # Idempotent: a re-run must not double the library. Count the
        # libib-provenance copies this work already carries and only top up
        # the difference — re-importing the same exports is a no-op, and an
        # interrupted import resumes where it stopped (2026-08 audit M3).
        existing = session.scalar(
            select(func.count(Copy.id)).where(
                Copy.work_id == work.id,
                Copy.provenance == Provenance.libib_import,
            )
        ) or 0
        to_create = max(0, h.copies - existing)
        stats.copies_already_present += h.copies - to_create
        for _ in range(to_create):
            session.add(
                Copy(
                    work_id=work.id,
                    edition_id=edition.id if edition else None,
                    owner_household_id=owner_household_id,
                    status=default_status,
                    provenance=Provenance.libib_import,
                    source_collections=sorted(h.collections),
                    notes=blob,
                )
            )
            stats.copies_created += 1

    session.flush()
    return stats
