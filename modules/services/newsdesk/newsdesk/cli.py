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
from . import edition as edition_mod
from . import feedback
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
    con = _con(args)
    n_web = feedback.ingest_web(con)
    n_space = feedback.ingest_space(con, Path(args.space)) if args.space else 0
    print(f"newsdesk: ingested {n_web} web grade(s), {n_space} space grade(s)")
    return 0


def cmd_tune(args) -> int:
    con = _con(args)
    feedback.ingest_web(con)
    if args.space:
        feedback.ingest_space(con, Path(args.space))
    report = feedback.tune(con, load_profile())
    if args.space:
        feedback.write_tuning_page(report, Path(args.space))
    print(report)
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

    p = sub.add_parser("grade", help="ingest grades from both surfaces")
    p.add_argument("--space")
    p.set_defaults(func=cmd_grade)

    p = sub.add_parser("tune", help="apply source tuning, propose term changes")
    p.add_argument("--space")
    p.set_defaults(func=cmd_tune)

    sub.add_parser("stats", help="counts").set_defaults(func=cmd_stats)
    sub.add_parser("stale", help="sources that are failing or gone quiet").set_defaults(func=cmd_stale)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
