"""Things worth fixing — the catalog's own to-do list.

This data came from a Libib export, a hand-written loss document parsed by
heuristics, and a crowd-edited database. Every one of those leaves debris, and
debris that nobody can see never gets fixed. Each check below corresponds to a
real defect found by hand during the build; the point is to stop finding them by
accident.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from stacks.models import Author, Copy, Edition, Work


@dataclass
class Issue:
    work_id: int
    title: str
    author: str | None = None
    detail: str | None = None


@dataclass
class IssueGroup:
    key: str
    title: str
    why: str
    #: What to do about it, so the list is actionable rather than accusatory.
    fix: str
    total: int = 0
    items: list[Issue] = field(default_factory=list)


LIMIT = 60


def _rows(session: Session, where, limit: int = LIMIT):
    q = (
        select(Work.id, Work.title, Author.name)
        .outerjoin(Author, Author.id == Work.primary_author_id)
        .where(where)
        .order_by(Work.title)
        .limit(limit)
    )
    return [Issue(work_id=r[0], title=r[1], author=r[2]) for r in session.execute(q).all()]


def _count(session: Session, where) -> int:
    return session.scalar(
        select(func.count()).select_from(select(Work.id).where(where).subquery())
    ) or 0


def no_isbn(session: Session) -> IssueGroup:
    """Cannot be scanned. The single most consequential gap."""
    where = ~Work.id.in_(
        select(Edition.work_id).where(Edition.isbn13.is_not(None))
    )
    return IssueGroup(
        key="no-isbn",
        title="No ISBN",
        why="These cannot be recognised by the scanner at all.",
        fix="Open the book and add an ISBN — most of these are flood losses "
            "destroyed before anyone catalogued them.",
        total=_count(session, where),
        items=_rows(session, where),
    )


def no_cover(session: Session, limit: int = LIMIT) -> IssueGroup:
    """Genuinely has no picture — checked against the art we hold.

    Deliberately NOT "has no cover_id". Most covers were fetched by ISBN before
    ids were captured, so counting ids reported ~2,000 books as art-less while
    their jackets were sitting on disk. Ask what we actually have.
    """
    from stacks.config import get_settings
    from stacks.covers import stored_keys

    held = stored_keys(get_settings())

    rows = session.execute(
        select(
            Work.id, Work.title, Author.name,
            func.array_agg(Edition.isbn13).label("isbns"),
            func.array_agg(Edition.cover_id).label("cover_ids"),
        )
        .outerjoin(Author, Author.id == Work.primary_author_id)
        .join(Edition, Edition.work_id == Work.id)
        .where(Edition.isbn13.is_not(None))
        .group_by(Work.id, Work.title, Author.name)
    ).all()

    missing = []
    for wid, title, author, isbns, cover_ids in rows:
        keys = {i for i in (isbns or []) if i}
        keys |= {f"id{c}" for c in (cover_ids or []) if c}
        if not (keys & held):
            missing.append(Issue(work_id=wid, title=title, author=author))

    return IssueGroup(
        key="no-cover",
        title="No cover art",
        why="Has an ISBN, but no picture has been found for any of its printings.",
        fix="Some genuinely have none at Open Library. For the rest, pick a "
            "different printing whose art does exist.",
        total=len(missing),
        items=missing[:limit],
    )


def junk_titles(session: Session) -> IssueGroup:
    """Series headings the loss-document parser mistook for books.

    The tests are deliberately narrow. The first version matched ``%(have%``
    and *any* title over 120 characters, and 18 of the 26 things it flagged
    were real books: "A Defense of Honor (Haven Manor)" matched on "(Haven",
    and long subtitles are completely normal — "Success With Baby Chicks: A
    Complete Guide to Hatchery Selection, Mail-Order Chicks..." is a genuine
    title, not debris. A list that is mostly false positives does not get
    worked, which defeats the point of having one.

    So: ``have:``/``had:`` introduces a list of contents and is decisive on its
    own, while mere length is only suspicious when the title *also* failed to
    resolve to any edition. Every real long title here matched at least one.
    """
    has_edition = select(Edition.id).where(Edition.work_id == Work.id).exists()
    where = or_(
        Work.title.op("~*")(r"\y(have|had)\s*:"),
        Work.title.ilike("%need titles%"),
        Work.title.ilike("%suggested titles%"),
        and_(func.length(Work.title) > 120, ~has_edition),
        # A title with no letter in it is not a title. The loss document has a
        # line reading "999999999999", which became a work.
        ~Work.title.op("~")(r"[A-Za-z]"),
    )
    return IssueGroup(
        key="junk-titles",
        title="Probably not a book",
        why="These look like headings from the loss document — a series name "
            "carrying a list of the titles under it — rather than one title. "
            "Each one names several real books that are worth recovering "
            "separately.",
        fix="Split out the titles it names, or delete the entry.",
        total=_count(session, where),
        items=_rows(session, where),
    )


def unidentified(session: Session) -> IssueGroup:
    where = Work.title.ilike("Unidentified book %")
    return IssueGroup(
        key="unidentified",
        title="Scanned but unidentified",
        why="Added from a barcode that Open Library could not name.",
        fix="Type the title in by hand — the ISBN is already attached.",
        total=_count(session, where),
        items=_rows(session, where),
    )


def no_author(session: Session) -> IssueGroup:
    where = Work.primary_author_id.is_(None)
    return IssueGroup(
        key="no-author",
        title="No author",
        why="Author-level want rules can never fire on these.",
        fix="Add the author, or let an added ISBN fill it in automatically.",
        total=_count(session, where),
        items=_rows(session, where),
    )


def orphans(session: Session) -> IssueGroup:
    """Works nobody holds and nobody lost — usually an enrichment artefact."""
    where = ~Work.id.in_(select(Copy.work_id))
    return IssueGroup(
        key="orphans",
        title="No copies recorded",
        why="Nothing owned, nothing lost, nothing wanted. Usually left behind "
            "by a merge or an import.",
        fix="Delete, unless it is a book you mean to look for.",
        total=_count(session, where),
        items=_rows(session, where),
    )


def suspicious_duplicates(session: Session, limit: int = LIMIT) -> IssueGroup:
    """Works that share both a normalised title and an author.

    Two records for one book split its holdings, so a scan can report "not
    owned" for something sitting on the shelf under the other record.

    Title alone is not enough, and matching on it flagged five pairs of which
    none were duplicates: Seymour Simon's *Spiders* and Gail Gibbons' *Spiders*
    are different books, as are Rien Poortvliet's *Noah's Ark* and Barbara
    Hazen's. Generic children's titles repeat constantly in a library this
    size. Merging on a title match would have collapsed ten real books into
    five and *undercounted* the shelf — the exact failure this check exists to
    prevent, arrived at from the other direction.

    Grouping by author as well as title means a NULL author still groups with
    another NULL (Postgres treats them as equal in GROUP BY), which is right:
    two untitled-author records of the same name really are indistinguishable.
    """
    dupes = session.execute(
        select(
            Work.sort_title,
            Work.primary_author_id,
            func.count(Work.id).label("n"),
        )
        .group_by(Work.sort_title, Work.primary_author_id)
        .having(func.count(Work.id) > 1)
        .order_by(func.count(Work.id).desc())
        .limit(limit)
    ).all()

    items: list[Issue] = []
    for sort_title, author_id, n in dupes:
        for wid, title, author in session.execute(
            select(Work.id, Work.title, Author.name)
            .outerjoin(Author, Author.id == Work.primary_author_id)
            .where(
                Work.sort_title == sort_title,
                Work.primary_author_id.is_(None)
                if author_id is None
                else Work.primary_author_id == author_id,
            )
        ).all():
            items.append(Issue(work_id=wid, title=title, author=author,
                               detail=f"{n} records share this title and author"))

    return IssueGroup(
        key="duplicates",
        title="Possible duplicates",
        why="Several records share a title and an author. Holdings split "
            "across them, so a scan can say 'not owned' for a book that is on "
            "the shelf. A shared title alone is not listed — generic children's "
            "titles repeat, and those are different books.",
        fix="Merge by hand: move the ISBN onto one record and delete the other.",
        total=len(dupes),
        items=items[:limit],
    )


def all_groups(session: Session) -> list[IssueGroup]:
    groups = [
        no_isbn(session),
        unidentified(session),
        junk_titles(session),
        suspicious_duplicates(session),
        no_author(session),
        no_cover(session),
        orphans(session),
    ]
    return [g for g in groups if g.total]
