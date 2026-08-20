"""Command line: `newsdesk <subcommand>`.

The systemd units call these in sequence; everything is also runnable by hand,
which is how the interest profile gets calibrated. `newsdesk rank --dry-run`
prints what today's shortlist would be without touching item state, and that is
the intended tool for the first week.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import collect as collect_mod
from . import corpus as corpus_mod
from . import edition as edition_mod
from . import feedback, gradeserver
from . import sources as sources_mod
from .db import connect, load_profile, seed_sources, state_dir, write_atomic


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

    sub.add_parser("stats", help="counts").set_defaults(func=cmd_stats)
    sub.add_parser("stale", help="sources that are failing or gone quiet").set_defaults(func=cmd_stale)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
