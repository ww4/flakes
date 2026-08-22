"""Command line: `newsdesk <subcommand>`.

The systemd units call these in sequence; everything is also runnable by hand,
which is how the interest profile gets calibrated. `newsdesk rank --dry-run`
prints what today's shortlist would be without touching item state, and that is
the intended tool for the first week.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import collect as collect_mod
from . import corpus as corpus_mod
from . import edition as edition_mod
from . import feedback, gradeserver
from . import site as site_mod
from . import sources as sources_mod
from .db import connect, load_profile, now, seed_sources, state_dir, write_atomic


def _con(args):
    return connect(Path(args.db) if args.db else None)


def cmd_seed(args) -> int:
    con = _con(args)
    added, updated = seed_sources(con, Path(args.catalogue))
    load_profile(Path(args.profile) if args.profile else None)
    print(f"newsdesk: seeded {added} new source(s), refreshed {updated}")
    return 0


def cmd_collect(args) -> int:
    con = _con(args)
    profile = load_profile()
    stats = collect_mod.collect(con, profile, only=args.source)
    print(json.dumps(stats))
    print(f"newsdesk: {stats['ok']} ok, {stats['not_modified']} unchanged, "
          f"{stats['failed']} failed, {stats['new_items']} new item(s)",
          file=sys.stderr)
    return 0


def cmd_ingest(args) -> int:
    """Walk the local corpus sources (directories of markdown, not feeds)."""
    con = _con(args)
    stats = corpus_mod.ingest(con, load_profile(), only=args.source)
    print(json.dumps(stats))
    if stats["missing"]:
        print(f"newsdesk: {stats['missing']} corpus director(ies) MISSING",
              file=sys.stderr)
    print(f"newsdesk: {stats['added']} new post(s) from {stats['corpora']} corpus/corpora "
          f"({stats['skipped']} stubs skipped)", file=sys.stderr)
    return 0


def cmd_rank(args) -> int:
    con = _con(args)
    if args.dry_run:
        # Calibration mode: score and select, print, change nothing. The whole
        # point of the first week is to look at this output and fix the
        # profile before a reader is ever involved.
        result = edition_mod.rank(con, args.kind, fetch_articles=False,
                                  dry_run=True)
        for c in result["candidates"]:
            print(f"{c['score']:>8.1f}  {c['lane']:<14} {c['source'][:28]:<28} "
                  f"{c['title'][:70]}")
            if args.verbose:
                print(f"          {' '.join(c['signals'])}")
        print(f"\n{result['shortlisted']} of {result['considered']} considered",
              file=sys.stderr)
        return 0

    result = edition_mod.rank(con, args.kind)
    out = state_dir() / "candidates.json"
    write_atomic(out, json.dumps(result, indent=1))
    print(result["shortlisted"])
    print(f"newsdesk: {result['shortlisted']} shortlisted from "
          f"{result['considered']} candidate(s) -> {out}", file=sys.stderr)
    return 0


def cmd_publish(args) -> int:
    con = _con(args)
    judged = ""
    if args.input and args.input != "-":
        p = Path(args.input)
        if p.exists():
            judged = p.read_text(errors="replace")
    else:
        judged = sys.stdin.read()
    res = edition_mod.publish(
        con, args.kind, judged,
        space_dir=Path(args.space) if args.space else None,
        cmark=args.cmark)
    print(json.dumps(res))
    print(f"newsdesk: published {res['published']} of {res['shortlisted']} "
          f"shortlisted as {res['edition']}"
          + (" (READER FAILED — raw ranking published)" if res["fell_back"] else ""),
          file=sys.stderr)
    return 0


def cmd_grade(args) -> int:
    """Ingest the SPACE grading surface only.

    Web grades arrive live via newsdesk-grade and are already in the table.
    """
    con = _con(args)
    n_space = feedback.ingest_space(con, Path(args.space)) if args.space else 0
    print(f"newsdesk: ingested {n_space} space grade(s)")
    return 0


def cmd_serve(args) -> int:
    return gradeserver.serve(host=args.host, port=args.port,
                             db_path=Path(args.db) if args.db else None)


def cmd_tune(args) -> int:
    con = _con(args)
    if args.space:
        feedback.ingest_space(con, Path(args.space))
    report = feedback.tune(con, load_profile())
    if args.space:
        feedback.write_tuning_page(report, Path(args.space))
    print(report)
    return 0


def cmd_sources(args) -> int:
    """Apply the control block on the Sources page, then rewrite the page."""
    con = _con(args)
    page = Path(args.page)
    res = sources_mod.sync(con, page)
    print(f"newsdesk: sources page synced — {res['applied']} instruction(s) "
          f"applied, {res['returned']} returned")
    return 0


def cmd_render(args) -> int:
    """Build the site with Zola. A failed build keeps the last good one."""
    ok, out = site_mod.build(Path(args.site_src), Path(args.out), zola=args.zola)
    print(out)
    if not ok:
        print("newsdesk: zola build FAILED — the previously published site is "
              "untouched", file=sys.stderr)
        return 1
    print(f"newsdesk: site built -> {args.out}", file=sys.stderr)
    return 0


def cmd_migrate_site(args) -> int:
    """One-time: turn everything already published into markdown pages.

    Editions were rendered straight to HTML before the move to Zola, and saved
    articles were standalone HTML files. Both are recoverable: the edition text
    lives on its SilverBullet page, and an archived article's text is the body
    of its own <article> element.
    """
    con = _con(args)
    n_ed = n_read = 0

    for row in con.execute("SELECT id, kind, tldr, n_published FROM editions"
                           " ORDER BY id"):
        md = Path(args.space or "/var/lib/silverbullet/Areas/Newsdesk") / f"{row['id']}.md"
        if not md.exists():
            print(f"  no space page for {row['id']} — skipped", file=sys.stderr)
            continue
        body = md.read_text(errors="replace")
        body = re.sub(r"\A#[^\n]*\n+", "", body)          # drop the H1; the template has it
        tldr = row["tldr"] or ""
        if not tldr:
            m = re.search(r"^TLDR:\s*(.+)$", body, re.M | re.I)
            tldr = m.group(1).strip() if m else ""
        kind_title = edition_mod.KINDS.get(row["kind"], {}).get("title", "Edition")
        site_mod.write_edition(row["id"], kind_title, row["id"].rsplit("-", 1)[0],
                               tldr, body, n_published=row["n_published"] or 0)
        con.execute("UPDATE editions SET tldr=? WHERE id=?", (tldr, row["id"]))
        n_ed += 1

    web = Path(args.out)
    for row in con.execute("SELECT id, title, source, url, published, archived_path"
                           " FROM items WHERE archived_path IS NOT NULL"):
        old = web / row["archived_path"]
        if not str(row["archived_path"]).endswith(".html") or not old.exists():
            continue
        html = old.read_text(errors="replace")
        m = re.search(r"<article>(.*?)</article>", html, re.S)
        if not m:
            print(f"  could not recover text for item {row['id']}", file=sys.stderr)
            continue
        text = (m.group(1).replace("&lt;", "<").replace("&gt;", ">")
                .replace("&amp;", "&"))
        rel = site_mod.write_read(row["id"], row["title"], row["source"], row["url"],
                                  row["published"], now(), text)
        con.execute("UPDATE items SET archived_path=? WHERE id=?", (rel, row["id"]))
        n_read += 1

    con.commit()
    print(f"newsdesk: converted {n_ed} edition(s) and {n_read} archived article(s) "
          f"to markdown")
    return 0


def cmd_stats(args) -> int:
    print(json.dumps(feedback.stats(_con(args)), indent=1))
    return 0


def cmd_stale(args) -> int:
    stale = collect_mod.stale_sources(_con(args))
    for s in stale:
        print(f"{s['kind']:<8} {s['name']:<38} {s['detail']}")
    print(f"{len(stale)} stale source(s)", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="newsdesk")
    ap.add_argument("--db", help="override the database path (tests, dry runs)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("seed", help="load the packaged catalogue and profile")
    p.add_argument("--catalogue", required=True)
    p.add_argument("--profile")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("collect", help="poll every enabled source")
    p.add_argument("--source", help="just this one, for debugging")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("ingest", help="ingest local corpus sources")
    p.add_argument("--source")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("rank", help="build the shortlist for an edition")
    p.add_argument("--kind", choices=sorted(edition_mod.KINDS), default="brief")
    p.add_argument("--dry-run", action="store_true",
                   help="print the shortlist and change nothing")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_rank)

    p = sub.add_parser("publish", help="turn the judged markdown into an edition")
    p.add_argument("--kind", choices=sorted(edition_mod.KINDS), default="brief")
    p.add_argument("--input", default="-")
    p.add_argument("--space")
    p.add_argument("--cmark")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("grade", help="ingest grades from the SilverBullet pages")
    p.add_argument("--space")
    p.set_defaults(func=cmd_grade)

    p = sub.add_parser("serve", help="run the grading endpoint (loopback only)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8123)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("tune", help="apply source tuning, propose term changes")
    p.add_argument("--space")
    p.set_defaults(func=cmd_tune)

    p = sub.add_parser("sources",
                       help="apply the Sources page control block and rewrite it")
    p.add_argument("--page", required=True,
                   help="path to Areas/Newsdesk/Sources.md in the space")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("render", help="build the site with zola")
    p.add_argument("--site-src", required=True, help="templates/config from the store")
    p.add_argument("--out", required=True)
    p.add_argument("--zola", default="zola")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("migrate-site",
                       help="one-time: convert already-published HTML to markdown")
    p.add_argument("--space", help="SilverBullet Areas/Newsdesk directory")
    p.add_argument("--out", required=True, help="the existing web directory")
    p.set_defaults(func=cmd_migrate_site)

    sub.add_parser("stats", help="counts").set_defaults(func=cmd_stats)
    sub.add_parser("stale", help="sources that are failing or gone quiet").set_defaults(func=cmd_stale)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
