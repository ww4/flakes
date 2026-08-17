"""Command line interface."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from stacks import workops
from stacks.config import get_settings
from stacks.db import init_schema, run_migrations, session_scope
from stacks.enrich.openlibrary import OpenLibraryClient
from stacks.importers.flood_load import load as flood_load
from stacks.importers.libib import import_libib_exports
from stacks.importers.list_split import load as list_split_load
from stacks.importers.sale_doc import load as sale_load
from stacks.importers.series_volumes import load as series_volumes_load
from stacks.match import Verdict, evaluate_scan
from stacks.models import Author, Copy, CopyStatus, Edition, Household, WantRule, Work
from stacks.normalize import title_variants, to_isbn10, to_isbn13, year_from
from stacks.repair import run as repair_run

app = typer.Typer(help="stacks — physical book catalog", no_args_is_help=True)
console = Console()
log_ = logging.getLogger("stacks.enrich")

_VERDICT_STYLE: dict[Verdict, tuple[str, str]] = {
    Verdict.BUY_WANTED: ("bold magenta", "WANTED"),
    Verdict.BUY_REPLACE: ("bold green", "REPLACE"),
    Verdict.BUY_MORE: ("green", "BUY ANOTHER"),
    Verdict.CAUTION_UNVERIFIED: ("yellow", "CAUTION"),
    Verdict.SKIP_HAVE: ("red", "SKIP"),
    Verdict.NOT_IN_CATALOG: ("dim", "not in your catalog"),
    Verdict.UNKNOWN: ("dim", "unknown"),
}
# Every verdict must have a style, or `stacks scan` raises KeyError on a
# perfectly valid result. Checked at import so it cannot ship broken.
assert set(_VERDICT_STYLE) == set(Verdict), "verdict style map is out of sync"


@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


@app.command()
def initdb(
    bare: bool = typer.Option(
        False, "--bare", help="Build tables straight from the models, skipping "
                              "migration history (throwaway databases only)"
    ),
) -> None:
    """Bring the database up to the latest migration."""
    if bare:
        init_schema()
        console.print(
            "[yellow]schema built from models — no migration history, "
            "cannot be upgraded later[/yellow]"
        )
    else:
        run_migrations()
        console.print("[green]migrated to head[/green]")


@app.command("import-libib")
def import_libib(
    path: Path = typer.Argument(..., exists=True, readable=True,
                                help="A CSV, or a directory of per-collection CSVs"),
    household: str = typer.Option(None, help="Owning household name (created if absent)"),
) -> None:
    """Import Libib exports as UNVERIFIED holdings.

    Nothing here is treated as confirmed. The export says what was owned before
    the flood; only the sweep says what survived. Pass a directory to fold every
    collection export together — a book catalogued in two collections is one
    book, not two copies.
    """
    files = sorted(path.glob("*.csv")) if path.is_dir() else [path]
    if not files:
        console.print(f"[red]no CSV files found in {path}[/red]")
        raise typer.Exit(1)

    with session_scope() as s:
        hh_id = None
        if household:
            hh = s.scalar(select(Household).where(Household.name == household))
            if hh is None:
                hh = Household(name=household)
                s.add(hh)
                s.flush()
            hh_id = hh.id

        stats = import_libib_exports(s, files, owner_household_id=hh_id)

    console.print(f"[green]{stats.summary()}[/green]")
    if stats.merged_across_collections:
        console.print(
            f"[dim]{stats.merged_across_collections} rows folded into an existing "
            f"holding (same book, another collection)[/dim]"
        )
    if stats.skipped_no_title:
        console.print(f"[yellow]skipped {stats.skipped_no_title} rows with no title[/yellow]")

    t = Table(title="collections (provenance, not location)", show_header=True)
    t.add_column("collection")
    t.add_column("rows", justify="right")
    for name, n in sorted(stats.collections.items(), key=lambda kv: -kv[1]):
        t.add_row(name, str(n))
    console.print(t)

    for w in stats.warnings[:8]:
        console.print(f"  [yellow]![/yellow] {w}")
    if len(stats.warnings) > 8:
        console.print(f"  ... and {len(stats.warnings) - 8} more")


@app.command()
def enrich(
    limit: int = typer.Option(50, help="Max works to enrich this run"),
    expand: bool = typer.Option(True, help="Also pull every other edition of each work"),
) -> None:
    """Resolve works against Open Library and expand them to all known editions.

    The expansion is what makes the sale-day check edition-agnostic: after this
    runs, owning any printing means every printing's ISBN is known to us.
    """
    asyncio.run(_enrich(limit, expand))


async def _enrich(limit: int, expand: bool) -> None:
    settings = get_settings()
    resolved = added = merged = 0

    with session_scope() as s:
        pending = s.scalars(
            select(Work)
            .where(func.cardinality(Work.ol_work_keys) == 0)
            .limit(limit)
        ).all()
        targets = [
            (w.id, s.scalar(select(Edition.isbn13).where(Edition.work_id == w.id,
                                                         Edition.isbn13.is_not(None)).limit(1)))
            for w in pending
        ]

    if not targets:
        console.print("[dim]nothing to enrich[/dim]")
        return

    failures = 0
    async with OpenLibraryClient(settings) as ol:
        for work_id, isbn13 in targets:
            if not isbn13:
                continue
            try:
                key = await ol.work_key_for_isbn(isbn13)
                if not key:
                    continue
                ol_work = await ol.fetch_work(key)
                editions = await ol.editions_for_work(key) if expand else []
            except Exception as exc:  # noqa: BLE001
                # One malformed record must not end the run. This is a batch of
                # thousands against a crowd-edited catalog, and a single
                # unexpected shape previously killed it after 75 works.
                failures += 1
                log_.warning("enrich failed for work %s (isbn %s): %s", work_id, isbn13, exc)
                continue

            with session_scope() as s:
                work = s.get(Work, work_id)
                if work is None:
                    continue

                # Another provisional work already resolved to this OL key:
                # they are the same book. Fold this one into it.
                twin = s.scalar(
                    select(Work).where(Work.ol_work_keys.any(key), Work.id != work_id)
                )
                if twin is not None:
                    # workops.merge_work_into repoints EVERYTHING (copies,
                    # editions, scan history, want rules, requests, tags).
                    # The hand-rolled version here repointed only copies +
                    # editions, so merging a previously-scanned duplicate
                    # raised IntegrityError out of session_scope and killed
                    # the whole batch run mid-way.
                    workops.merge_work_into(s, work, twin)
                    merged += 1
                    work = twin
                else:
                    work.ol_work_keys = [*work.ol_work_keys, key]
                    if ol_work:
                        work.description = work.description or ol_work.description
                        work.subtitle = work.subtitle or ol_work.subtitle
                    resolved += 1

                for oe in editions:
                    i13 = to_isbn13(oe.isbn13) or to_isbn13(oe.isbn10)
                    if not i13:
                        continue
                    if s.scalar(select(Edition.id).where(Edition.isbn13 == i13)):
                        continue
                    s.add(
                        Edition(
                            work_id=work.id,
                            isbn13=i13,
                            isbn10=to_isbn10(oe.isbn10),
                            publisher=oe.publisher,
                            publish_date=oe.publish_date,
                            publish_year=year_from(oe.publish_date),
                            binding=oe.binding,
                            page_count=oe.page_count,
                            language=oe.language,
                            ol_edition_key=oe.ol_edition_key,
                            cover_id=oe.cover_id,
                        )
                    )
                    added += 1

    console.print(
        f"[green]resolved {resolved} works, merged {merged} duplicates, "
        f"added {added} editions[/green]"
        + (f"  [yellow]{failures} failed (skipped)[/yellow]" if failures else "")
    )


@app.command("import-flood")
def import_flood(
    path: Path = typer.Argument(..., exists=True, readable=True),
    threshold: float = typer.Option(0.72, help="Trigram score to merge into an existing work"),
) -> None:
    """Load the hand-written flood loss document.

    Folds the two halves together, matches into the existing catalog where it
    can, and records what each blue annotation actually meant.
    """
    with session_scope() as s:
        stats = flood_load(s, path, threshold=threshold)

    console.print(f"[green]{stats.summary()}[/green]")
    if stats.borderline:
        console.print(
            f"\n[yellow]{len(stats.borderline)} borderline merges "
            f"(score near the threshold — worth an eye):[/yellow]"
        )
        for flood_t, existing_t, score in stats.borderline[:15]:
            console.print(f"  {score:.2f}  {flood_t[:42]:<44} -> {existing_t[:42]}")


@app.command("split-lists")
def split_lists(
    path: Path = typer.Option(
        Path("data/list-records.toml"), exists=True, readable=True,
        help="Which document lines name several books, and which",
    ),
) -> None:
    """Split loss-document lines that list several books into one record each.

    "Cam janson had: corn popper, camp mystery, it's a raid, ..." is twenty
    lost books recorded as one unscannable 200-character title. Each fragment
    is resolved against the catalog, so a loss lands on the real book wherever
    the Libib export already knows it.
    """
    with session_scope() as s:
        stats = list_split_load(s, path)

    console.print(f"[green]{stats.summary()}[/green]")
    if stats.matches:
        console.print("\n[cyan]Matched onto books already in the catalog:[/cyan]")
        for frag, title, score in stats.matches:
            console.print(f"  {score:.2f}  {frag[:34]:<36} -> {title[:52]}")
    if stats.unmatched:
        console.print(
            f"\n[yellow]{len(stats.unmatched)} had no match and became new "
            f"records, named for what the document said:[/yellow]"
        )
        for title in stats.unmatched:
            console.print(f"  {title[:78]}")


@app.command("name-volumes")
def name_volumes(
    path: Path = typer.Option(
        Path("data/series-volumes.toml"), exists=True, readable=True,
        help="Volume numbers resolved to the titles they name",
    ),
) -> None:
    """Give the volume-numbered placeholders their real titles.

    "Magic Tree House #17" is a true record and a useless one — a number does
    not scan and does not match. Where the catalog already holds the book, the
    loss moves onto it rather than leaving two records to split the holdings.
    """
    with session_scope() as s:
        stats = series_volumes_load(s, path)

    console.print(f"[green]{stats.summary()}[/green]")
    if stats.merges:
        console.print("\n[cyan]Merged onto books already in the catalog:[/cyan]")
        for placeholder, existing in stats.merges:
            console.print(f"  {placeholder[:30]:<32} -> {existing[:46]}")


@app.command("import-wants")
def import_wants(path: Path = typer.Argument(..., exists=True, readable=True)) -> None:
    """Load the 'books to look for at sale' document as standing want rules.

    These are instructions, not titles: "any books by Seymour Simon", "DK
    books", "Hardy Boys, have 1,2,3,4,6,10...". They are what actually fires
    when a barcode is scanned at a sale.
    """
    with session_scope() as s:
        stats = sale_load(s, path)
        review = s.execute(
            select(WantRule.kind, WantRule.label, WantRule.raw_text)
            .where(WantRule.needs_review.is_(True))
        ).all()

    console.print(f"[green]{stats.summary()}[/green]")
    if review:
        console.print(
            f"\n[yellow]{len(review)} rules the parser could not confidently "
            f"classify — 50 lines is small enough to correct by hand:[/yellow]"
        )
        for kind, label, raw in review:
            console.print(f"  [{kind.value:<9}] {label[:34]:<36} {(raw or '')[:52]}")


@app.command("wants")
def show_wants(kind: str = typer.Option(None, help="Filter by kind")) -> None:
    """List the standing want rules and their have/missing counts."""
    with session_scope() as s:
        stmt = select(WantRule).order_by(WantRule.kind, WantRule.label)
        if kind:
            stmt = stmt.where(WantRule.kind == kind)
        rules = s.scalars(stmt).all()
        rows = [
            (
                r.kind.value,
                r.label,
                sum(1 for e in r.entries if e.have),
                sum(1 for e in r.entries if not e.have),
                "yes" if r.needs_review else "",
            )
            for r in rules
        ]

    t = Table(title=f"want rules ({len(rows)})", show_header=True)
    for col in ("kind", "label", "have", "missing", "review?"):
        t.add_column(col, justify="right" if col in ("have", "missing") else "left")
    for row in rows:
        t.add_row(row[0], row[1][:46], str(row[2]), str(row[3]), row[4])
    console.print(t)


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", help="Bind address"),
    port: int = typer.Option(8099),
    reload: bool = typer.Option(False, help="Auto-reload on code changes"),
) -> None:
    """Run the API and the scanner PWA.

    Note: the browser barcode API requires a secure context. Over plain HTTP
    only localhost qualifies — reaching this by LAN IP silently leaves
    BarcodeDetector undefined, and the scanner falls back to manual entry with
    no obvious reason why. Put it behind TLS for real use.
    """
    import uvicorn

    console.print(f"[green]serving on http://{host}:{port}[/green]")
    uvicorn.run("stacks.api:app", host=host, port=port, reload=reload)


@app.command("backfill-cover-ids")
def backfill_cover_ids(limit: int = typer.Option(4000, help="Max editions this run")) -> None:
    """Fetch Open Library cover ids for editions enriched before we stored them.

    Uses the Books API (``/isbn/{isbn}.json``), which carries the normal API
    pacing rather than the cover service's 100-per-5-minutes ceiling — so this
    runs at roughly 3/second rather than 20/minute.
    """
    asyncio.run(_backfill_cover_ids(limit))


async def _backfill_cover_ids(limit: int) -> None:
    from stacks.enrich.openlibrary import _first_cover

    settings = get_settings()
    with session_scope() as s:
        # Only editions we would actually show: one per work we hold something
        # of. Fetching ids for 55,000 editions nobody will look at is waste.
        targets = [
            (eid, isbn) for eid, isbn in s.execute(
                select(Edition.id, Edition.isbn13)
                .join(Copy, Copy.work_id == Edition.work_id)
                .where(Edition.isbn13.is_not(None), Edition.cover_id.is_(None))
                .group_by(Edition.id, Edition.isbn13)
                .limit(limit)
            ).all()
        ]

    if not targets:
        console.print("[dim]nothing to backfill[/dim]")
        return

    console.print(f"[dim]resolving cover ids for {len(targets)} editions…[/dim]")
    found = 0
    async with OpenLibraryClient(settings) as ol:
        for n, (eid, isbn) in enumerate(targets, 1):
            try:
                data = await ol._get(f"/isbn/{isbn}.json")
            except Exception as exc:  # noqa: BLE001
                log_.warning("cover id lookup failed for %s: %s", isbn, exc)
                continue
            cid = _first_cover((data or {}).get("covers"))
            if cid:
                with session_scope() as s:
                    ed = s.get(Edition, eid)
                    if ed is not None:
                        ed.cover_id = cid
                found += 1
            if n % 100 == 0:
                console.print(f"[dim]{n}/{len(targets)} — {found} ids[/dim]")

    console.print(f"[green]{found} cover ids stored[/green]")


@app.command("warm-covers")
def warm_covers(limit: int = typer.Option(2000, help="Max covers to fetch this run")) -> None:
    """Pre-fetch cover images so browsing is instant.

    Deliberately slow: Open Library allows 100 cover-by-identifier requests per
    IP per five minutes, so this paces at one every ~3 seconds. Leave it running
    — each cover is fetched once and kept forever, and the browse page gets
    faster as it goes.
    """
    asyncio.run(_warm_covers(limit))


async def _warm_covers(limit: int) -> None:
    from stacks import covers as covers_mod

    settings = get_settings()
    with session_scope() as s:
        # One representative edition per work we hold something of — there is no
        # point fetching art for books nobody will browse to. Cover id first:
        # that route is unlimited, so those fetch at full speed.
        targets = [
            (cid, isbn) for cid, isbn in s.execute(
                select(func.min(Edition.cover_id), func.min(Edition.isbn13))
                .join(Copy, Copy.work_id == Edition.work_id)
                .where(Edition.isbn13.is_not(None))
                .group_by(Edition.work_id)
                .limit(limit)
            ).all()
        ]

    by_id = [(c, i) for c, i in targets if c]
    by_isbn = [(c, i) for c, i in targets if not c]
    console.print(
        f"[dim]{len(by_id)} by cover id (unlimited), "
        f"{len(by_isbn)} by ISBN (paced at one per 3.1s)[/dim]"
    )

    done = hit = miss = 0
    for cid, isbn in by_id + by_isbn:
        path = (await covers_mod.fetch_by_id(settings, cid, "M")) if cid \
            else (await covers_mod.fetch(settings, isbn, "M"))
        done += 1
        hit += path is not None
        miss += path is None
        if done % 50 == 0:
            console.print(f"[dim]{done}/{len(targets)} — {hit} stored, {miss} without art[/dim]")

    console.print(f"[green]{hit} covers cached, {miss} had none[/green]")
    console.print(covers_mod.stats(settings))


@app.command("repair-copies")
def repair_copies() -> None:
    """Collapse holdings that describe one book as several.

    A destroyed book named twice in the loss document, or catalogued in Libib
    and then destroyed, should be one record — not three. Idempotent; the
    importers now prevent both cases, this fixes data loaded before they did.
    """
    with session_scope() as s:
        stats = repair_run(s)
    console.print(f"[green]{stats.summary()}[/green]")


@app.command("resolve-titles")
def resolve_titles(
    limit: int = typer.Option(100, help="Max works to resolve this run"),
    threshold: float = typer.Option(0.80, help="Title similarity to accept a match"),
) -> None:
    """Resolve ISBN-less works against Open Library by title search.

    This is the only route available for the flood losses: they were destroyed
    before anyone catalogued them, so there is no barcode — just a hand-written
    title. Reports how many could not be identified, which is the number that
    decides whether the source photographs are worth parsing.
    """
    asyncio.run(_resolve_titles(limit, threshold))


async def _resolve_titles(limit: int, threshold: float) -> None:
    from difflib import SequenceMatcher

    settings = get_settings()

    with session_scope() as s:
        pending = s.execute(
            select(Work.id, Work.title, Author.name)
            .outerjoin(Author, Author.id == Work.primary_author_id)
            .outerjoin(Edition, Edition.work_id == Work.id)
            .where(func.cardinality(Work.ol_work_keys) == 0, Edition.id.is_(None))
            .limit(limit)
        ).all()

    if not pending:
        console.print("[dim]nothing to resolve[/dim]")
        return

    console.print(f"[dim]resolving {len(pending)} ISBN-less works against Open Library...[/dim]")
    resolved = ambiguous = unresolved = 0
    misses: list[str] = []

    async with OpenLibraryClient(settings) as ol:
        for work_id, title, author in pending:
            # Try the title as written, then progressively stripped forms. The
            # flood doc appends "reader" as a category, which defeats an exact
            # search; the book itself is catalogued under the bare title.
            best, score, tried_any = None, 0.0, False
            for variant in title_variants(title):
                docs = await ol.search_work(variant, author)
                if not docs:
                    continue
                tried_any = True
                for d in docs:
                    sc = SequenceMatcher(
                        None, variant.lower(), (d.get("title") or "").lower()
                    ).ratio()
                    if sc > score:
                        best, score = d, sc
                if score >= threshold:
                    break

            if not tried_any:
                unresolved += 1
                misses.append(title)
                continue

            if best is None or score < threshold:
                ambiguous += 1
                misses.append(f"{title}  ~?~  {(best or {}).get('title', '')} ({score:.2f})")
                continue

            key = (best.get("key") or "").rsplit("/", 1)[-1]
            with session_scope() as s:
                w = s.get(Work, work_id)
                if w is not None and key:
                    w.ol_work_keys = [*w.ol_work_keys, key]
                    if not w.subtitle and best.get("first_publish_year"):
                        pass
            resolved += 1

    total = len(pending)
    console.print(
        f"\n[green]resolved {resolved}/{total} ({resolved/total:.0%})[/green]  "
        f"[yellow]ambiguous {ambiguous}[/yellow]  [red]no result {unresolved}[/red]"
    )
    if misses:
        console.print(
            "\n[dim]could not identify (first 25) — "
            "these are the photo candidates:[/dim]"
        )
        for m in misses[:25]:
            console.print(f"  · {m[:100]}")


@app.command()
def scan(code: str, title: str = typer.Option(None, help="Title hint for pre-ISBN books")) -> None:
    """Evaluate one barcode the way the sale-day scanner will."""
    with session_scope() as s:
        r = evaluate_scan(s, code, title_hint=title)

    style, label = _VERDICT_STYLE[r.verdict]
    console.print(f"\n[{style}]{label}[/{style}] — {r.headline}")
    if r.work:
        author = r.work.primary_author.name if r.work.primary_author else "unknown"
        console.print(f"  [bold]{r.work.title}[/bold] — {author}")
    for d in r.detail:
        console.print(f"  · {d}")
    for w in r.wants:
        console.print(f"  [magenta]★ {w}[/magenta]")
    if r.tier:
        console.print(f"  [dim]matched via {r.tier.value} ({r.confidence:.0%})[/dim]")
    console.print()


@app.command()
def stats() -> None:
    """Catalog counts, broken out by verification confidence."""
    with session_scope() as s:
        works = s.scalar(select(func.count(Work.id))) or 0
        editions = s.scalar(select(func.count(Edition.id))) or 0
        authors = s.scalar(select(func.count(Author.id))) or 0
        rows = s.execute(
            select(Copy.status, func.count(Copy.id)).group_by(Copy.status)
        ).all()

    t = Table(title="stacks", show_header=True)
    t.add_column("metric")
    t.add_column("count", justify="right")
    t.add_row("works", str(works))
    t.add_row("editions", str(editions))
    t.add_row("authors", str(authors))
    for status, n in sorted(rows, key=lambda r: -r[1]):
        t.add_row(f"copies: {status.value}", str(n))
    console.print(t)


@app.command("export-offline")
def export_offline(out: Path = typer.Argument(Path("offline-set.json"))) -> None:
    """Write the offline set the phone carries to a book sale.

    Every ISBN of every work we have any record of, mapped to a compact summary.
    This is what makes the sale check work with no signal.
    """
    with session_scope() as s:
        rows = s.execute(
            select(Edition.isbn13, Work.id, Work.title, Work.desired_copies)
            .join(Work, Work.id == Edition.work_id)
            .where(Edition.isbn13.is_not(None))
        ).all()
        holdings = {
            wid: {"present": p, "unverified": u, "lost": lost}
            for wid, p, u, lost in s.execute(
                select(
                    Copy.work_id,
                    func.count(Copy.id).filter(Copy.status == CopyStatus.present),
                    func.count(Copy.id).filter(Copy.status == CopyStatus.unverified),
                    func.count(Copy.id).filter(Copy.status == CopyStatus.lost_flood),
                ).group_by(Copy.work_id)
            ).all()
        }

    isbn_map = {
        isbn: {"w": wid, "t": title, "d": desired, **holdings.get(wid, {})}
        for isbn, wid, title, desired in rows
    }
    out.write_text(json.dumps({"version": 1, "isbns": isbn_map}, separators=(",", ":")))
    size_kb = out.stat().st_size / 1024
    console.print(
        f"[green]{len(isbn_map)} ISBNs -> {out} ({size_kb:.0f} KB)[/green]"
    )


if __name__ == "__main__":
    app()
