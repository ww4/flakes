"""HTTP API and static host for the scanner.

Deliberately small. The interesting logic lives in :mod:`stacks.match`, and the
sale-day path is designed to run entirely on the phone from a cached payload —
this server exists to hand that payload over, to answer the online case, and to
record what was scanned so a bad verdict can be traced afterwards.

Scans and lookups return the *same* shape, a :class:`BookCard`. One rich record
per book, so the interface never has to make someone click through to find out
what they are holding.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from stacks import catalog, covers
from stacks.badges import DERIVED as DERIVED_BADGES
from stacks.badges import compute as compute_badges
from stacks.browse import all_shelves as browse_shelves
from stacks.browse import shelf_by_key
from stacks.cleanup import all_groups as cleanup_groups
from stacks.config import get_settings
from stacks.coverchoice import choose as choose_cover
from stacks.db import get_engine, session_scope
from stacks.enrich.openlibrary import OpenLibraryClient, _author_key
from stacks.match import (
    BUYS,
    STATUS_LABEL,
    MatchResult,
    Verdict,
    _decide,
    _holding_for_work,
    evaluate_scan,
    resolve_work_by_isbn,
    search_works,
    status_for,
    wants_for_work,
)
from stacks.models import (
    Author,
    ClientEvent,
    Copy,
    CopyStatus,
    Edition,
    MatchTier,
    Provenance,
    ScanEvent,
    Series,
    Tag,
    Work,
    WorkTag,
)
from stacks.normalize import normalize_author, normalize_title, to_isbn13, year_from

WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(
    title="stacks",
    description="Physical book catalog — sale-day scanner",
    version="0.1.0",
)

# The catalog payload is ~7.5 MB of JSON that compresses to well under 2 MB —
# the entire premise of handing the phone everything rather than a summary.
# Without this it ships uncompressed, which is four times the transfer over
# whatever connection someone has before leaving the house.
app.add_middleware(GZipMiddleware, minimum_size=1024)


def get_session() -> Session:
    with session_scope() as s:
        yield s


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------


class EditionOut(BaseModel):
    isbn13: str | None = None
    publisher: str | None = None
    year: int | None = None
    binding: str | None = None
    pages: int | None = None
    #: This is the printing that was scanned.
    is_scanned: bool = False
    #: We own a copy of this printing.
    is_owned: bool = False


class CopyOut(BaseModel):
    status: str
    provenance: str
    collections: list[str] = []
    notes: str | None = None
    #: The printing this copy actually is, when the catalog recorded one.
    isbn13: str | None = None
    #: True when this copy is the same printing as the barcode just scanned.
    matches_scan: bool = False


#: Provenance rendered for a human. "libib_import" tells someone nothing about
#: where a book came from; "Libib" at least names the source.
PROVENANCE_LABEL = {
    "libib_import": "Libib",
    "flood_doc": "flood record",
    "re_acquired": "re-acquired",
    "new_purchase": "new purchase",
    "gift": "gift",
    "manual": "manual",
}


class BookCard(BaseModel):
    """Everything known about one book, in one payload."""

    # identity
    work_id: int | None = None
    title: str | None = None
    subtitle: str | None = None
    author: str | None = None
    series: str | None = None
    series_position: float | None = None
    description: str | None = None
    cover: str | None = None
    publisher: str | None = None
    year: int | None = None

    # what we hold — the status tag states a fact, not an alarm
    status: str = "NOT OWNED"
    verdict: str = "NOT_IN_CATALOG"
    should_buy: bool = False
    #: What to do about it, in words. Distinct from `status`.
    recommendation: str = ""
    detail: list[str] = []
    wants: list[str] = []
    present: int = 0
    unverified: int = 0
    lost_flood: int = 0
    loaned: int = 0

    # depth
    editions_known: int = 0
    editions: list[EditionOut] = []
    copies: list[CopyOut] = []
    ol_keys: list[str] = []

    #: Every badge for this book, strongest first. `status` is badges[0].
    badges: list[str] = []
    #: Labels a person assigned, as distinct from derived state.
    tags: list[str] = []
    #: Why this cover was chosen, so an odd one is explicable.
    cover_reason: str | None = None
    #: The ISBN that produced this card, when it came from a scan.
    scanned_isbn: str | None = None
    #: Set when the copy on the shelf is a *different* printing from the one in
    #: hand. Worth saying out loud: it is the difference between a duplicate and
    #: an upgrade, and it is the only edition fact most people care about.
    edition_note: str | None = None

    # provenance of this record
    tier: str | None = None
    confidence: float = 1.0
    source: str | None = None


class SearchHit(BaseModel):
    work_id: int
    title: str
    author: str | None = None
    series: str | None = None
    year: int | None = None
    cover: str | None = None
    #: For the grid view, which renders tiles the same way browse does.
    cover_id: int | None = None
    isbn13: str | None = None
    status: str = "NOT OWNED"
    verdict: str = "NOT_IN_CATALOG"
    recommendation: str = ""
    present: int = 0
    unverified: int = 0
    lost_flood: int = 0


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------


def _editions_and_cover(s: Session, work_id: int) -> tuple[list[Edition], str | None]:
    editions = s.scalars(
        select(Edition)
        .where(Edition.work_id == work_id)
        .order_by(Edition.publish_year.desc().nullslast())
    ).all()
    cover = next(
        (OpenLibraryClient.cover_url(e.isbn13) for e in editions if e.isbn13), None
    )
    return list(editions), cover


def _editions_out(
    editions: list[Edition], scanned_isbn: str | None, owned_isbns: set[str | None]
) -> list[EditionOut]:
    """The edition list, most relevant first.

    After enrichment a popular work can carry hundreds of printings, which is
    noise. The two that matter are the one in your hand and the one on your
    shelf, so those sort to the top and the rest are a tail.
    """
    out = [
        EditionOut(
            isbn13=e.isbn13, publisher=e.publisher, year=e.publish_year,
            binding=e.binding, pages=e.page_count,
            is_scanned=bool(scanned_isbn and e.isbn13 == scanned_isbn),
            is_owned=bool(e.isbn13 and e.isbn13 in owned_isbns),
        )
        for e in editions
    ]
    out.sort(key=lambda e: (not e.is_scanned, not e.is_owned, -(e.year or 0)))
    return out[:20]


def _result_for_work(s: Session, work: Work) -> MatchResult:
    """Evaluate a work directly, bypassing code/title resolution."""
    holding = _holding_for_work(s, work.id)
    verdict, headline, detail = _decide(work, holding)
    wants = wants_for_work(s, work)
    if wants and verdict is Verdict.NOT_IN_CATALOG:
        verdict, headline = Verdict.BUY_WANTED, "On your want list"
    return MatchResult(
        verdict=verdict, tier=None, work=work, holding=holding,
        desired=work.desired_copies, headline=headline, detail=detail, wants=wants,
    )


def _card_for_work(
    s: Session, work: Work, result: MatchResult, scanned_isbn: str | None = None
) -> BookCard:
    editions, cover = _editions_and_cover(s, work.id)
    h = result.holding

    # Copies carry the printing they actually are, where the catalog knows it.
    copy_rows = s.execute(
        select(Copy, Edition.isbn13)
        .outerjoin(Edition, Edition.id == Copy.edition_id)
        .where(Copy.work_id == work.id)
        # Ordered by id so the editor can address rows by position — see
        # /api/work/{id}/copy-ids.
        .order_by(Copy.id)
    ).all()

    # The byline should describe the book in your hand, not the newest printing
    # Open Library happens to know about.
    scanned_edition = (
        next((e for e in editions if e.isbn13 == scanned_isbn), None)
        if scanned_isbn else None
    )
    newest = next((e for e in editions if e.publish_year), editions[0] if editions else None)
    shown = scanned_edition or newest

    owned_isbns_all = {isbn for _c, isbn in copy_rows if isbn}
    pick = choose_cover(
        editions,
        chosen_edition_id=work.cover_edition_id,
        scanned_isbn=scanned_isbn,
        owned_isbns=owned_isbns_all,
    )
    cover, cover_reason = pick.url, pick.reason

    copies_out = [
        CopyOut(
            status=c.status.value,
            provenance=PROVENANCE_LABEL.get(c.provenance.value, c.provenance.value),
            collections=list(c.source_collections or []),
            notes=c.notes,
            isbn13=isbn,
            matches_scan=bool(scanned_isbn and isbn and isbn == scanned_isbn),
        )
        for c, isbn in copy_rows
    ]

    assigned = [
        name for (name,) in s.execute(
            select(Tag.name).join(WorkTag, WorkTag.tag_id == Tag.id)
            .where(WorkTag.work_id == work.id).order_by(Tag.name)
        ).all()
    ]
    badges = compute_badges(
        present=h.present, unverified=h.unverified, lost=h.lost_flood,
        reacq=h.re_acquired, wanted=bool(result.wants), assigned=assigned,
    )

    edition_note = None
    if scanned_isbn and copies_out:
        known = [c for c in copies_out if c.isbn13]
        if known and not any(c.matches_scan for c in known):
            other = known[0]
            match = next((e for e in editions if e.isbn13 == other.isbn13), None)
            desc = " / ".join(
                str(x) for x in (
                    match.publisher if match else None,
                    match.publish_year if match else None,
                ) if x
            )
            edition_note = (
                f"Your copy is a different printing — {other.isbn13}"
                + (f" ({desc})" if desc else "")
            )
        elif not known:
            edition_note = "Which printing you own was never recorded."
    return BookCard(
        work_id=work.id,
        title=work.title,
        subtitle=work.subtitle,
        author=work.primary_author.name if work.primary_author else None,
        series=work.series.name if work.series else None,
        series_position=work.series_position,
        description=work.description,
        cover=cover,
        cover_reason=cover_reason,
        publisher=shown.publisher if shown else None,
        year=shown.publish_year if shown else None,
        status=badges.primary,
        badges=badges.all,
        tags=assigned,
        verdict=result.verdict.value,
        should_buy=result.verdict in BUYS,
        recommendation=result.headline,
        detail=result.detail,
        wants=result.wants,
        present=h.present,
        unverified=h.unverified,
        lost_flood=h.lost_flood,
        loaned=h.loaned,
        editions_known=len(editions),
        # The full list runs to hundreds after enrichment; the newest dozen are
        # what someone holding a book actually compares against.
        editions=_editions_out(editions, scanned_isbn, {c.isbn13 for c in copies_out}),
        copies=copies_out,
        ol_keys=list(work.ol_work_keys or []),
        tier=result.tier.value if result.tier else None,
        confidence=result.confidence,
        scanned_isbn=scanned_isbn,
        edition_note=edition_note,
    )


async def _lookup_external(isbn13: str) -> dict | None:
    """Ask Open Library what an unrecognised book is.

    Two reasons this matters: it lets author- and publisher-level want rules
    fire on a book we have never owned, and it means an unknown scan can still
    show a cover and a title instead of a shrug.
    """
    settings = get_settings()
    try:
        async with OpenLibraryClient(settings) as ol:
            data = await ol._get(f"/isbn/{isbn13}.json")
            if not data:
                return None

            author_keys = [k for k in map(_author_key, data.get("authors") or []) if k]
            description = None

            # Edition records frequently omit authors and always omit the
            # description — both live on the work. Always follow the link: an
            # author-level want rule can only fire on an unowned book if we know
            # who wrote it, and a cover with no blurb is half an answer.
            for wref in data.get("works") or []:
                wkey = (
                    (wref.get("key") or "").rsplit("/", 1)[-1]
                    if isinstance(wref, dict) else None
                )
                if not wkey:
                    continue
                wdata = await ol._get(f"/works/{wkey}.json")
                if wdata:
                    author_keys = author_keys or [
                        k for k in map(_author_key, wdata.get("authors") or []) if k
                    ]
                    desc = wdata.get("description")
                    description = desc.get("value") if isinstance(desc, dict) else desc
                break

            names = []
            for key in author_keys:
                name = await ol.author_name(key)
                if name:
                    names.append(name)

            return {
                "title": data.get("title"),
                "author": names[0] if names else None,
                "publisher": (data.get("publishers") or [None])[0],
                "year": year_from(data.get("publish_date")),
                "description": description,
                "cover": OpenLibraryClient.cover_url(isbn13),
            }
    except Exception:  # noqa: BLE001 — an unavailable lookup must not break a scan
        return None


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------


@app.get("/healthz")
def healthz() -> dict:
    try:
        with get_engine().connect() as conn:
            conn.exec_driver_sql("select 1")
    except Exception as exc:  # noqa: BLE001 — health must report, not raise
        raise HTTPException(status_code=503, detail=f"database unreachable: {exc}") from exc
    return {"ok": True}


@app.get("/api/scan/{code}", response_model=BookCard)
async def scan(
    code: str,
    request: Request,
    title: str | None = None,
    source: str | None = None,
    shown: str | None = None,
    s: Session = Depends(get_session),
) -> BookCard:
    """Evaluate one scanned barcode, returning everything known about the book.

    ``title`` reaches the fuzzy path for a book with no barcode — most of what
    the flood destroyed predates ISBNs entirely.
    """
    result = evaluate_scan(s, code, title_hint=title)

    external = None
    if result.work is None and (isbn := to_isbn13(code, repair=False)):
        external = await _lookup_external(isbn)
        if external:
            result = evaluate_scan(s, code, title_hint=title, external=external)

    s.add(
        ScanEvent(
            scanned_code=code[:64],
            matched_work_id=result.work.id if result.work else None,
            match_tier=result.tier if isinstance(result.tier, MatchTier) else None,
            verdict=result.verdict.value,
            context={"headline": result.headline, "wants": result.wants},
            source=(source or "unknown")[:16],
            client_verdict=(shown or None),
            user_agent=(request.headers.get("user-agent") or "")[:300] or None,
        )
    )

    if result.work is not None:
        return _card_for_work(s, result.work, result, scanned_isbn=to_isbn13(code))

    card = BookCard(
        status=STATUS_LABEL.get(result.verdict, "NOT OWNED"),
        verdict=result.verdict.value,
        should_buy=result.verdict in BUYS,
        recommendation=result.headline,
        detail=result.detail,
        wants=result.wants,
    )
    if external:
        card.title = external.get("title")
        card.author = external.get("author")
        card.publisher = external.get("publisher")
        card.year = external.get("year")
        card.description = external.get("description")
        card.cover = external.get("cover")
        card.source = "openlibrary"
    return card


@app.get("/api/work/{work_id}", response_model=BookCard)
def work_detail(work_id: int, s: Session = Depends(get_session)) -> BookCard:
    work = s.get(Work, work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="no such work")
    return _card_for_work(s, work, _result_for_work(s, work))


@app.get("/api/search", response_model=list[SearchHit])
def search(q: str, limit: int = 25, s: Session = Depends(get_session)) -> list[SearchHit]:
    """Text search over the catalog.

    Returns every candidate, not a single best guess: "magic school bus" matches
    23 works in this library, and collapsing that to one makes browsing
    impossible. Each hit carries its status so the list is readable without
    opening anything.
    """
    out: list[SearchHit] = []
    for work, _score in search_works(s, q, limit=limit):
        result = _result_for_work(s, work)
        verdict, headline = result.verdict, result.headline
        holding = result.holding
        editions, _ = _editions_and_cover(s, work.id)
        newest = next((e for e in editions if e.publish_year), None)
        owned = {
            i for (i,) in s.execute(
                select(Edition.isbn13).join(Copy, Copy.edition_id == Edition.id)
                .where(Copy.work_id == work.id, Edition.isbn13.is_not(None))
            ).all()
        }
        pick = choose_cover(
            editions, chosen_edition_id=work.cover_edition_id, owned_isbns=owned
        )
        out.append(
            SearchHit(
                work_id=work.id,
                title=work.title,
                author=work.primary_author.name if work.primary_author else None,
                series=work.series.name if work.series else None,
                year=newest.publish_year if newest else None,
                cover=pick.url,
                cover_id=pick.edition.cover_id if pick.edition else None,
                isbn13=pick.edition.isbn13 if pick.edition else None,
                status=status_for(verdict, holding),
                verdict=verdict.value,
                recommendation=headline,
                present=holding.present,
                unverified=holding.unverified,
                lost_flood=holding.lost_flood,
            )
        )
    return out


class ShelfItemOut(BaseModel):
    work_id: int
    title: str
    author: str | None = None
    isbn13: str | None = None
    status: str
    year: int | None = None
    cover_id: int | None = None
    badges: list[str] = []


class ShelfOut(BaseModel):
    key: str
    title: str
    subtitle: str | None = None
    total: int = 0
    items: list[ShelfItemOut] = []


@app.get("/api/browse", response_model=list[ShelfOut])
def browse(s: Session = Depends(get_session)) -> list[ShelfOut]:
    """Rows for the browse page.

    Three thousand books is unusable as a list. These rows are the questions
    this library actually raises — what the flood took, what has been replaced,
    what is still being collected.
    """
    return [
        ShelfOut(
            key=sh.key, title=sh.title, subtitle=sh.subtitle, total=sh.total,
            items=[ShelfItemOut(**vars(i)) for i in sh.items],
        )
        for sh in browse_shelves(s)
    ]


@app.get("/covers/id/{cover_id}")
async def cover_by_id(cover_id: int, size: str = "M"):
    """Serve a cover by Open Library's internal id.

    The preferred route: requests by cover id are unlimited, so this needs no
    pacing and no apology. Ids are captured at ingest by enrichment.
    """
    if size not in {"S", "M", "L"}:
        raise HTTPException(status_code=400, detail="size must be S, M or L")
    path = await covers.fetch_by_id(get_settings(), cover_id, size)
    if path is None:
        raise HTTPException(status_code=404, detail="no cover")
    return FileResponse(
        path, media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/covers/{isbn13}")
async def cover(isbn13: str, size: str = "M"):
    """Serve a cover from the local cache, fetching it once if needed.

    Proxied rather than linked directly because Open Library rate-limits cover
    lookups by identifier to 100 per IP per five minutes — one scroll of a
    browse wall would exhaust that and start showing broken images.
    """
    if size not in {"S", "M", "L"}:
        raise HTTPException(status_code=400, detail="size must be S, M or L")
    clean = to_isbn13(isbn13)
    if not clean:
        raise HTTPException(status_code=400, detail="not an ISBN")

    path = await covers.fetch(get_settings(), clean, size)
    if path is None:
        raise HTTPException(status_code=404, detail="no cover")
    return FileResponse(
        path,
        media_type="image/jpeg",
        # Immutable: a given ISBN's cover does not change, and the client
        # should never ask us twice.
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/api/covers/stats")
def cover_stats() -> dict:
    return covers.stats(get_settings())


class WorkPatch(BaseModel):
    title: str | None = None
    author: str | None = None
    series: str | None = None
    series_position: float | None = None
    description: str | None = None
    desired_copies: int | None = None
    #: Pin the cover to a specific printing. clear_cover_choice returns to the
    #: automatic preference.
    cover_edition_id: int | None = None
    clear_cover_choice: bool = False


@app.patch("/api/work/{work_id}", response_model=BookCard)
def patch_work(work_id: int, body: WorkPatch, s: Session = Depends(get_session)) -> BookCard:
    """Edit a work.

    Much of this catalog came from hand-written documents and a crowd-edited
    database, so correcting a title or pinning the right cover is not a luxury —
    it is how the data gets good.
    """
    work = s.get(Work, work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="no such work")

    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="title cannot be empty")
        work.title = title
        work.sort_title = normalize_title(title)
    if body.description is not None:
        work.description = body.description.strip() or None
    if body.desired_copies is not None:
        if body.desired_copies < 0:
            raise HTTPException(status_code=400, detail="desired_copies cannot be negative")
        work.desired_copies = body.desired_copies
    if body.series_position is not None:
        work.series_position = body.series_position

    if body.author is not None:
        name = body.author.strip()
        if not name:
            work.primary_author_id = None
        else:
            sort_name = normalize_author(name)
            author = s.scalar(select(Author).where(Author.sort_name == sort_name))
            if author is None:
                author = Author(name=name, sort_name=sort_name)
                s.add(author)
                s.flush()
            work.primary_author_id = author.id

    if body.series is not None:
        label = body.series.strip()
        if not label:
            work.series_id = None
        else:
            series = s.scalar(select(Series).where(Series.name.ilike(label)))
            if series is None:
                series = Series(name=label)
                s.add(series)
                s.flush()
            work.series_id = series.id

    if body.clear_cover_choice:
        work.cover_edition_id = None
    elif body.cover_edition_id is not None:
        ed = s.get(Edition, body.cover_edition_id)
        if ed is None or ed.work_id != work.id:
            raise HTTPException(status_code=400, detail="that edition is not this book")
        work.cover_edition_id = ed.id

    s.flush()
    return _card_for_work(s, work, _result_for_work(s, work))


class CoverOption(BaseModel):
    edition_id: int
    isbn13: str | None = None
    publisher: str | None = None
    year: int | None = None
    url: str
    is_owned: bool = False
    is_chosen: bool = False


@app.get("/api/work/{work_id}/covers", response_model=list[CoverOption])
def work_covers(work_id: int, s: Session = Depends(get_session)) -> list[CoverOption]:
    """Every cover we could show for this book, to pick from by eye.

    Only editions with a known cover id are offered: an ISBN-only guess may
    resolve to nothing, and a picker full of blanks is worse than a short list.
    """
    work = s.get(Work, work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="no such work")

    owned = {
        isbn for (isbn,) in s.execute(
            select(Edition.isbn13)
            .join(Copy, Copy.edition_id == Edition.id)
            .where(Copy.work_id == work_id, Edition.isbn13.is_not(None))
        ).all()
    }
    editions = s.scalars(
        select(Edition)
        .where(Edition.work_id == work_id, Edition.cover_id.is_not(None))
        .order_by(Edition.publish_year.desc().nullslast())
        .limit(40)
    ).all()
    return [
        CoverOption(
            edition_id=e.id, isbn13=e.isbn13, publisher=e.publisher, year=e.publish_year,
            url=f"covers/id/{e.cover_id}?size=M",
            is_owned=e.isbn13 in owned,
            is_chosen=work.cover_edition_id == e.id,
        )
        for e in editions
    ]


@app.get("/api/work/{work_id}/copy-ids", response_model=list[int])
def work_copy_ids(work_id: int, s: Session = Depends(get_session)) -> list[int]:
    """Copy ids in the same order the card lists them.

    The card deliberately does not carry ids — it is shipped to a phone in bulk
    and every field costs. Editing is rare enough to afford one extra request.
    """
    return [
        cid for (cid,) in s.execute(
            select(Copy.id).where(Copy.work_id == work_id).order_by(Copy.id)
        ).all()
    ]


@app.get("/api/work/{work_id}/edition-ids", response_model=dict[str, int])
def work_edition_ids(work_id: int, s: Session = Depends(get_session)) -> dict[str, int]:
    """ISBN -> edition id, so the editor can act on a printing it only knows by
    its number."""
    return {
        isbn: eid for eid, isbn in s.execute(
            select(Edition.id, Edition.isbn13)
            .where(Edition.work_id == work_id, Edition.isbn13.is_not(None))
        ).all()
    }


class IsbnIn(BaseModel):
    isbn13: str


@app.post("/api/work/{work_id}/isbn", response_model=BookCard)
async def add_isbn(work_id: int, body: IsbnIn, s: Session = Depends(get_session)) -> BookCard:
    """Attach an ISBN to a book.

    The most consequential edit available, because an ISBN is what makes a book
    scannable. Roughly 300 of the flood losses were destroyed before anyone
    catalogued them and have no identifier at all — until one is added here they
    can never be recognised at a sale, which is the whole point of the system.

    Adding one also pulls the printing's details and cover from Open Library, so
    a bare title becomes a real record.
    """
    work = s.get(Work, work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="no such work")

    isbn = to_isbn13(body.isbn13, repair=False)
    if not isbn:
        raise HTTPException(
            status_code=400,
            detail="that is not a valid ISBN — check for a typo (the check digit failed)",
        )

    existing = s.scalar(select(Edition).where(Edition.isbn13 == isbn))
    if existing is not None and existing.work_id != work_id:
        other = s.get(Work, existing.work_id)
        raise HTTPException(
            status_code=409,
            detail=(
                f"that ISBN already belongs to \u201c{other.title if other else '?'}\u201d. "
                "If these are the same book, merge them instead of duplicating the ISBN."
            ),
        )

    if existing is None:
        meta = await _lookup_external(isbn) or {}
        cover_id = None
        try:
            settings = get_settings()
            async with OpenLibraryClient(settings) as ol:
                data = await ol._get(f"/isbn/{isbn}.json")
            from stacks.enrich.openlibrary import _first_cover

            cover_id = _first_cover((data or {}).get("covers"))
        except Exception:  # noqa: BLE001 — metadata is a bonus, the ISBN is the point
            pass

        s.add(Edition(
            work_id=work_id, isbn13=isbn,
            publisher=meta.get("publisher"), publish_year=meta.get("year"),
            cover_id=cover_id,
        ))
        s.flush()

        # A work with no description yet gains one for free.
        if not work.description and meta.get("description"):
            work.description = meta["description"]

    # Point copies with no printing recorded at this one — usually the reason
    # someone is adding it: "this bare record is that book on the shelf".
    edition = s.scalar(select(Edition).where(Edition.isbn13 == isbn))
    s.query(Copy).filter(Copy.work_id == work_id, Copy.edition_id.is_(None)).update(
        {Copy.edition_id: edition.id}, synchronize_session=False
    )
    s.flush()
    return _card_for_work(s, work, _result_for_work(s, work), scanned_isbn=isbn)


@app.delete("/api/edition/{edition_id}", response_model=BookCard)
def delete_edition(edition_id: int, s: Session = Depends(get_session)) -> BookCard:
    """Remove a printing — for a mistyped ISBN.

    Copies pointing at it are detached rather than deleted: the book is still
    on the shelf, we just no longer claim to know which printing it is.
    """
    ed = s.get(Edition, edition_id)
    if ed is None:
        raise HTTPException(status_code=404, detail="no such edition")
    work = s.get(Work, ed.work_id)
    if work is not None and work.cover_edition_id == ed.id:
        work.cover_edition_id = None
    s.query(Copy).filter(Copy.edition_id == ed.id).update(
        {Copy.edition_id: None}, synchronize_session=False
    )
    s.delete(ed)
    s.flush()
    return _card_for_work(s, work, _result_for_work(s, work))


class CopyPatch(BaseModel):
    status: str | None = None
    collections: list[str] | None = None
    notes: str | None = None
    condition: str | None = None


@app.patch("/api/copy/{copy_id}", response_model=BookCard)
def patch_copy(copy_id: int, body: CopyPatch, s: Session = Depends(get_session)) -> BookCard:
    """Edit one physical copy — its state, where it came from, a note."""
    copy = s.get(Copy, copy_id)
    if copy is None:
        raise HTTPException(status_code=404, detail="no such copy")

    if body.status is not None:
        try:
            new = CopyStatus(body.status)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"status must be one of {[c.value for c in CopyStatus]}",
            ) from None
        copy.status = new
        # Marking something present is an act of verification; record when.
        if new is CopyStatus.present:
            copy.last_verified_at = datetime.now(UTC)

    if body.collections is not None:
        copy.source_collections = sorted({c.strip() for c in body.collections if c.strip()})
    if body.notes is not None:
        copy.notes = body.notes.strip() or None
    if body.condition is not None:
        copy.condition = body.condition.strip() or None

    work = s.get(Work, copy.work_id)
    s.flush()
    return _card_for_work(s, work, _result_for_work(s, work))


@app.delete("/api/copy/{copy_id}", response_model=BookCard)
def delete_copy(copy_id: int, s: Session = Depends(get_session)) -> BookCard:
    """Remove a copy record.

    For a mistake — a duplicate row, a bad import. A book that left the house
    should be marked discarded instead, because that is history worth keeping.
    """
    copy = s.get(Copy, copy_id)
    if copy is None:
        raise HTTPException(status_code=404, detail="no such copy")
    work = s.get(Work, copy.work_id)
    s.delete(copy)
    s.flush()
    return _card_for_work(s, work, _result_for_work(s, work))


@app.delete("/api/work/{work_id}")
def delete_work(work_id: int, confirm_title: str, s: Session = Depends(get_session)) -> dict:
    """Delete a work and everything under it.

    Requires the title echoed back in ``confirm_title``. This removes the loss
    record too, which for a destroyed book is the only evidence it ever
    existed — worth one deliberate step.
    """
    work = s.get(Work, work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="no such work")
    if confirm_title.strip() != (work.title or "").strip():
        raise HTTPException(
            status_code=400, detail="confirm_title must match the work's title exactly"
        )

    copies = s.scalars(select(Copy).where(Copy.work_id == work_id)).all()
    editions = s.scalars(select(Edition).where(Edition.work_id == work_id)).all()
    n_c, n_e = len(copies), len(editions)
    work.cover_edition_id = None
    s.flush()
    for c in copies:
        s.delete(c)
    for e in editions:
        s.delete(e)
    s.delete(work)
    s.flush()
    return {"deleted": True, "copies": n_c, "editions": n_e}


class TagOut(BaseModel):
    id: int
    name: str
    color: str | None = None
    works: int = 0


@app.get("/api/tags", response_model=list[TagOut])
def list_tags(s: Session = Depends(get_session)) -> list[TagOut]:
    rows = s.execute(
        select(Tag.id, Tag.name, Tag.color, func.count(WorkTag.work_id))
        .outerjoin(WorkTag, WorkTag.tag_id == Tag.id)
        .group_by(Tag.id, Tag.name, Tag.color)
        .order_by(Tag.name)
    ).all()
    return [TagOut(id=r[0], name=r[1], color=r[2], works=r[3]) for r in rows]


class TagIn(BaseModel):
    name: str
    color: str | None = None


@app.post("/api/work/{work_id}/tags", response_model=BookCard)
def add_tag(work_id: int, body: TagIn, s: Session = Depends(get_session)) -> BookCard:
    """Put a label on a book.

    Only labels a person decides. Derived state (HAVE, LOST, REPLACED,
    UNCONFIRMED) is computed from copy status and cannot be assigned — storing
    it would create a second place for the truth to go stale.
    """
    work = s.get(Work, work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="no such work")

    name = body.name.strip().upper()
    if not name:
        raise HTTPException(status_code=400, detail="a tag needs a name")
    if name in DERIVED_BADGES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{name} is computed from what you own, not assigned. "
                "Change a copy's state instead."
            ),
        )

    tag = s.scalar(select(Tag).where(Tag.name == name))
    if tag is None:
        tag = Tag(name=name, color=body.color)
        s.add(tag)
        s.flush()
    if not s.get(WorkTag, (work_id, tag.id)):
        s.add(WorkTag(work_id=work_id, tag_id=tag.id))
    s.flush()
    return _card_for_work(s, work, _result_for_work(s, work))


@app.delete("/api/work/{work_id}/tags/{name}", response_model=BookCard)
def remove_tag(work_id: int, name: str, s: Session = Depends(get_session)) -> BookCard:
    work = s.get(Work, work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="no such work")
    tag = s.scalar(select(Tag).where(Tag.name == name.strip().upper()))
    if tag is not None:
        link = s.get(WorkTag, (work_id, tag.id))
        if link is not None:
            s.delete(link)
            s.flush()
    return _card_for_work(s, work, _result_for_work(s, work))


class IssueOut(BaseModel):
    work_id: int
    title: str
    author: str | None = None
    detail: str | None = None


class IssueGroupOut(BaseModel):
    key: str
    title: str
    why: str
    fix: str
    total: int
    items: list[IssueOut] = []


@app.get("/api/cleanup", response_model=list[IssueGroupOut])
def cleanup_list(s: Session = Depends(get_session)) -> list[IssueGroupOut]:
    """The catalog's own to-do list.

    Every check here corresponds to a defect found by hand during the build.
    Debris nobody can see never gets fixed.
    """
    return [
        IssueGroupOut(
            key=g.key, title=g.title, why=g.why, fix=g.fix, total=g.total,
            items=[IssueOut(**vars(i)) for i in g.items],
        )
        for g in cleanup_groups(s)
    ]


@app.get("/api/shelf/{key:path}", response_model=ShelfOut)
def shelf_detail(
    key: str, limit: int = 500, s: Session = Depends(get_session)
) -> ShelfOut:
    """One shelf in full, for its own page."""
    sh = shelf_by_key(s, key, limit=limit)
    if sh is None:
        raise HTTPException(status_code=404, detail="no such shelf")
    return ShelfOut(
        key=sh.key, title=sh.title, subtitle=sh.subtitle, total=sh.total,
        items=[ShelfItemOut(**vars(i)) for i in sh.items],
    )


class ClientEventIn(BaseModel):
    kind: str
    detail: dict | None = None
    page: str | None = None


@app.post("/api/client-event")
def client_event(
    body: ClientEventIn, request: Request, s: Session = Depends(get_session)
) -> dict:
    """Record something that happened on the device.

    Camera permission refused, no BarcodeDetector, a decode that never
    resolved. These are the likeliest phone problems and are otherwise
    completely invisible — there is no console to read on a phone in a church
    basement, and "it just doesn't work" is not a debuggable report.
    """
    s.add(ClientEvent(
        kind=body.kind[:48],
        detail=body.detail,
        page=(body.page or "")[:64] or None,
        user_agent=(request.headers.get("user-agent") or "")[:300] or None,
    ))
    return {"ok": True}


class LogLine(BaseModel):
    at: str
    kind: str
    code: str | None = None
    title: str | None = None
    verdict: str | None = None
    client_verdict: str | None = None
    source: str | None = None
    detail: str | None = None
    device: str | None = None
    disagreed: bool = False


@app.get("/api/logs", response_model=list[LogLine])
def logs(limit: int = 120, s: Session = Depends(get_session)) -> list[LogLine]:
    """Recent scans and device events, newest first.

    Built for reading on the phone that produced them.
    """
    out: list[LogLine] = []

    for ev, title in s.execute(
        select(ScanEvent, Work.title)
        .outerjoin(Work, Work.id == ScanEvent.matched_work_id)
        .order_by(ScanEvent.id.desc())
        .limit(limit)
    ).all():
        ctx = ev.context or {}
        # A device that showed something other than what the server computed is
        # the single most useful line in this log: it means the cached catalog
        # answered, and it answered differently.
        disagreed = bool(
            ev.client_verdict and ev.verdict and ev.client_verdict != ev.verdict
        )
        out.append(LogLine(
            at=ev.scanned_at.isoformat(timespec="seconds"),
            kind="scan",
            code=ev.scanned_code,
            title=title,
            verdict=ev.verdict,
            client_verdict=ev.client_verdict,
            source=ev.source,
            detail=ctx.get("headline"),
            device=_short_device(ev.user_agent),
            disagreed=disagreed,
        ))

    for ev in s.scalars(
        select(ClientEvent).order_by(ClientEvent.id.desc()).limit(limit)
    ).all():
        d = ev.detail or {}
        out.append(LogLine(
            at=ev.at.isoformat(timespec="seconds"),
            kind=ev.kind,
            detail=d.get("message") or (json.dumps(d) if d else None),
            device=_short_device(ev.user_agent),
        ))

    out.sort(key=lambda x: x.at, reverse=True)
    return out[:limit]


def _short_device(ua: str | None) -> str | None:
    """Enough of a user agent to tell the phone from the laptop."""
    if not ua:
        return None
    for needle, label in (
        ("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
        ("Macintosh", "Mac"), ("Windows", "Windows"), ("Linux", "Linux"),
    ):
        if needle in ua:
            browser = ("Chrome" if "Chrome" in ua and "Edg" not in ua
                       else "Firefox" if "Firefox" in ua
                       else "Safari" if "Safari" in ua else "?")
            return f"{label}/{browser}"
    return ua[:24]


class ConfirmIn(BaseModel):
    #: confirm — this book is in my hand. IDEMPOTENT: if a copy is already
    #:           confirmed present, it says so rather than inventing a second.
    #: add     — I genuinely own another copy of this.
    #: unhave  — I looked and it is not there; demote to missing.
    action: str = "confirm"
    note: str | None = None


class ConfirmOut(BaseModel):
    outcome: str
    message: str
    card: BookCard


@app.post("/api/confirm/{code}", response_model=ConfirmOut)
async def confirm(
    code: str, body: ConfirmIn | None = None, s: Session = Depends(get_session)
) -> ConfirmOut:
    """Record that this book is physically in hand.

    One action serving two jobs that turn out to be the same job:

    * **The sweep.** A book catalogued before the flood is ``unverified`` until
      someone lays eyes on it. Scanning it here promotes it to ``present``,
      which is the only thing that ever turns "probably yours" into "yours".
    * **Building the catalog.** A book we have never seen becomes a work, an
      edition and a present copy — so a stack of sale purchases can be entered
      by scanning them on the way home.
    """
    body = body or ConfirmIn()
    isbn13 = to_isbn13(code)
    if not isbn13:
        raise HTTPException(status_code=400, detail="not a readable ISBN")

    work, _tier = resolve_work_by_isbn(s, isbn13)
    edition = s.scalar(select(Edition).where(Edition.isbn13 == isbn13))
    meta: dict = {}
    created_work = False

    if work is None:
        # Never seen. Ask Open Library what it is; if even that comes back
        # empty we still record the book, because the barcode in your hand is
        # evidence enough that it exists.
        meta = await _lookup_external(isbn13) or {}
        title = (meta.get("title") or "").strip() or f"Unidentified book {isbn13}"
        author = None
        if meta.get("author"):
            sort_name = normalize_author(meta["author"])
            author = s.scalar(select(Author).where(Author.sort_name == sort_name))
            if author is None:
                author = Author(name=meta["author"], sort_name=sort_name)
                s.add(author)
                s.flush()

        work = Work(
            title=title,
            sort_title=normalize_title(title),
            description=meta.get("description"),
            primary_author_id=author.id if author else None,
            ol_work_keys=[],
        )
        s.add(work)
        s.flush()
        created_work = True

    if edition is None:
        edition = Edition(
            work_id=work.id, isbn13=isbn13,
            publisher=meta.get("publisher"), publish_year=meta.get("year"),
        )
        s.add(edition)
        s.flush()

    now = datetime.now(UTC)

    if body.action == "unhave":
        # "I looked and it is not there." Demote rather than delete: a book that
        # cannot be found is a fact worth keeping, and it is how a sweep records
        # a gap between what the catalog claims and what the shelf holds.
        target = s.scalar(
            select(Copy)
            .where(Copy.work_id == work.id, Copy.status == CopyStatus.present)
            .order_by(Copy.id.desc())
            .limit(1)
        ) or s.scalar(
            select(Copy)
            .where(Copy.work_id == work.id, Copy.status == CopyStatus.unverified)
            .order_by(Copy.id)
            .limit(1)
        )
        if target is None:
            return ConfirmOut(
                outcome="nothing_to_unhave",
                message="No copy recorded to mark missing",
                card=_card_for_work(s, work, _result_for_work(s, work), scanned_isbn=isbn13),
            )
        target.status = CopyStatus.missing
        target.last_verified_at = now
        if body.note:
            target.notes = f"{target.notes or ''} || {body.note}".strip(" |")[:2000]
        s.flush()
        return ConfirmOut(
            outcome="marked_missing",
            message="Marked as not on the shelf",
            card=_card_for_work(s, work, _result_for_work(s, work), scanned_isbn=isbn13),
        )

    if body.action == "confirm":
        # Idempotent. Pressing "I have this" twice previously fell through to
        # the create branch and silently recorded a second copy — the same
        # behaviour as "Another copy", which is exactly why the two buttons
        # looked identical.
        already = s.scalar(
            select(Copy)
            .where(Copy.work_id == work.id, Copy.status == CopyStatus.present)
            .order_by(Copy.id)
            .limit(1)
        )
        if already is not None:
            already.last_verified_at = now
            s.flush()
            return ConfirmOut(
                outcome="already_confirmed",
                message="Already confirmed on the shelf",
                card=_card_for_work(s, work, _result_for_work(s, work), scanned_isbn=isbn13),
            )

    promoted = s.scalar(
        select(Copy)
        .where(Copy.work_id == work.id, Copy.status == CopyStatus.unverified)
        .order_by(Copy.id)
        .limit(1)
    ) if body.action == "confirm" else None

    if promoted is not None:
        was = promoted.provenance.value
        promoted.status = CopyStatus.present
        promoted.last_verified_at = now
        if promoted.edition_id is None:
            promoted.edition_id = edition.id
        if body.note:
            promoted.notes = f"{promoted.notes or ''} || {body.note}".strip(" |")[:2000]
        outcome = "verified"
        message = ("Replaced copy confirmed on the shelf" if was == "re_acquired"
                   else "Confirmed — seen since the flood")
    else:
        s.add(Copy(
            work_id=work.id, edition_id=edition.id,
            status=CopyStatus.present,
            provenance=Provenance.new_purchase if created_work else Provenance.manual,
            last_verified_at=now, notes=body.note,
        ))
        outcome = "added" if created_work else "copy_added"
        message = "Added to the library" if created_work else "Another copy recorded"

    s.flush()
    return ConfirmOut(
        outcome=outcome, message=message,
        card=_card_for_work(s, work, _result_for_work(s, work), scanned_isbn=isbn13),
    )


@app.get("/api/catalog")
def catalog_payload(s: Session = Depends(get_session)) -> JSONResponse:
    """The entire catalog — every work, edition, copy and want rule.

    1.2 MB gzipped for three thousand books, which is why the phone gets all of
    it rather than a summary. See :mod:`stacks.catalog`.
    """
    payload = catalog.build(s)
    return JSONResponse(
        payload,
        headers={
            "Cache-Control": "no-cache",
            "X-Stacks-Works": str(len(payload["works"])),
            "X-Stacks-Isbns": str(len(payload["isbns"])),
        },
    )


if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
