"""briefing — render the dashboard's briefing card.

One subcommand, one output file. Every path comes from the environment so the
NixOS module owns the layout and the tests never touch live state.

⚠️ Exit 0 whenever a page was written. Every source is optional by design: a
missing daybook page is an ordinary Sunday, a missing sentinel state file is a
sentinel that has not ticked yet, and a missing edition is a quiet news day.
A non-zero exit here means the file could not be written at all.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from . import projects, render, render_projects, sources


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _write(page: str, name: str) -> Path:
    """Write-and-rename: nginx may serve this path at any moment, and a partial
    write would be served as a truncated page. Rename is atomic in-directory."""
    out_dir = Path(_env("BRIEFING_PUBLIC_DIR", "/var/lib/briefing/public"))
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / name
    tmp = target.with_suffix(".html.tmp")
    tmp.write_text(page, encoding="utf-8")
    tmp.chmod(0o644)
    tmp.replace(target)
    return target


def cmd_card(args) -> int:
    issues = sources.sentinel(
        _env("BRIEFING_SENTINEL_STATE", "/var/lib/sentinel/state.json"),
        _env("BRIEFING_SENTINEL_INCIDENTS", "/var/lib/sentinel/incidents"),
    )
    plan = sources.daybook(_env("BRIEFING_SPACE", "/var/lib/silverbullet"))
    ed = sources.edition(
        _env("BRIEFING_PUBLISH", "/var/lib/newsdesk/last-publish.json"),
        base_url=_env("BRIEFING_NEWS_URL", "https://digest.rosemaryacres.com/news"),
    )

    page = render.render(
        issues, plan, ed,
        notes_url=_env("BRIEFING_NOTES_URL", "https://notes.rosemaryacres.com"),
        sentinel_url=_env("BRIEFING_SENTINEL_URL",
                          "https://digest.rosemaryacres.com/sentinel/"),
        now=datetime.now(),
    )

    target = _write(page, "card.html")

    print("briefing: %d open, %d plan items, edition=%s -> %s"
          % (len(issues), len(plan), "yes" if ed else "no", target))
    return 0


def cmd_projects(args) -> int:
    """Render the Lab tab's project card. Read-only over the board's files."""
    found = projects.load(_env("BRIEFING_PROJECTS",
                               "/home/claude/project-board/projects"))
    page = render_projects.render(
        found,
        board_url=_env("BRIEFING_BOARD_URL",
                       "https://digest.rosemaryacres.com/board/"))
    target = _write(page, "projects.html")
    blocked = sum(1 for p in found if p["status"] == "blocked")
    print("briefing: %d project(s), %d blocked -> %s" % (len(found), blocked, target))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="briefing", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("card", help="render the dashboard briefing card").set_defaults(fn=cmd_card)
    sub.add_parser("projects", help="render the project board card").set_defaults(fn=cmd_projects)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
