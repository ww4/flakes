#!/usr/bin/env python3
"""Render the project board: one JSON file per project -> one HTML page.

The board answers a question the agent cannot answer for itself: on a project
that runs for days, where is it, and is it waiting on Chris? Asking the agent
mid-run gets you what its context happens to hold.

Deliberately dumb. There is no database, no API and no CLI for mutating state:
a project IS a JSON file the agent edits with ordinary file tools, and this
script only ever READS them. That shape is the lesson from Clanker Therapy 2
(Jupiter Extras, 2026-10-02), where both hosts had watched agents abandon a
structured to-do tool -- "bots would use it and set it up and then totally stop
using it" -- and both had settled on one plain file the agent maintains instead.
Chris on the alternative: "they spend 20, 30% of their time managing Beads."

Usage:
    project-board render     write web/index.html (the default)
    project-board check      validate every project file; non-zero if any is bad

Environment (set by the unit; overridden by the tests):
    PROJECT_BOARD_PROJECTS    dir of <slug>.json            [required by unit]
    PROJECT_BOARD_WEB         dir to write index.html into
    PROJECT_BOARD_STALE_HOURS quiet-for-this-long marker    [48]
"""
from __future__ import annotations

import html
import json
import os
import sys
import time

PROJECTS = os.environ.get("PROJECT_BOARD_PROJECTS", "/home/claude/project-board/projects")
WEB = os.environ.get("PROJECT_BOARD_WEB", "/var/lib/project-board/web")
STALE_HOURS = int(os.environ.get("PROJECT_BOARD_STALE_HOURS", "48"))

# Order is the reading order of the page: what needs Chris, then what is moving,
# then what is parked, then what is finished.
STATUSES = ["blocked", "active", "paused", "done"]
STEP_STATES = ["done", "doing", "blocked", "todo"]

STYLE = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{max-width:52rem;margin:0 auto;padding:1.2rem 1rem 3rem;
     font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;
     color:#e6e6e6;background:#181818}
h1{font-size:1.4rem;margin:0 0 .2em}
.sub{color:#8a8a8a;font-size:.85em;margin:0 0 1.6em}
.card{border:1px solid #3a3a3a;border-left:3px solid #555;border-radius:8px;
      padding:.8em 1em;margin:0 0 1.1em;background:#1f1f1f}
.card.blocked{border-left-color:#d08a28}
.card.active{border-left-color:#4a90d9}
.card.paused{border-left-color:#666}
.card.done{border-left-color:#3f9a4f}
.card.broken{border-left-color:#c0392b;background:#2a1d1b}
.card h2{font-size:1.05rem;margin:0 0 .35em;display:flex;flex-wrap:wrap;
         gap:.5em;align-items:baseline}
.pill{font-size:.68rem;font-weight:600;letter-spacing:.04em;text-transform:uppercase;
      padding:.15em .5em;border-radius:4px;background:#333;color:#ccc}
.pill.blocked{background:#4a3310;color:#ffc265}
.pill.active{background:#15314a;color:#8cc4f5}
.pill.done{background:#14361b;color:#86d294}
.quiet{font-size:.72rem;color:#d08a28;border:1px solid #5a4318;border-radius:4px;
       padding:.1em .45em}
.goal{color:#b9b9b9;margin:.1em 0 .7em}
.next{margin:.6em 0 .2em;padding:.5em .7em;background:#252525;border-radius:6px}
.next b{color:#8cc4f5}
.blockedon{margin:.6em 0 .2em;padding:.5em .7em;background:#2b2314;border-radius:6px}
.blockedon b{color:#ffc265}
.bar{height:6px;background:#2e2e2e;border-radius:3px;overflow:hidden;margin:.5em 0 .3em}
.bar span{display:block;height:100%;background:#4a90d9}
.count{font-size:.78em;color:#8a8a8a}
ul.steps{list-style:none;padding:0;margin:.6em 0 0}
ul.steps li{padding:.12em 0 .12em 1.4em;position:relative;color:#cfcfcf}
ul.steps li:before{position:absolute;left:0;font-size:.85em}
li.done:before{content:"[x]";color:#86d294}
li.doing:before{content:"[>]";color:#8cc4f5}
li.blocked:before{content:"[!]";color:#ffc265}
li.todo:before{content:"[ ]";color:#777}
li.done{color:#8a8a8a}
li .note{color:#8a8a8a;font-size:.85em}
.links{margin:.7em 0 0;font-size:.9em}
.links a{color:#6cb6ff;margin-right:.9em}
details{margin:.7em 0 0;font-size:.9em}
summary{cursor:pointer;color:#8a8a8a}
details ol{margin:.5em 0 0;padding-left:1.2em;color:#b9b9b9}
.meta{color:#6f6f6f;font-size:.76em;margin:.8em 0 0}
.empty{border:1px dashed #444;border-radius:8px;padding:1.4em;color:#8a8a8a;text-align:center}
code{background:#2c2c2c;padding:.08em .3em;border-radius:3px;font-size:.9em}
pre{white-space:pre-wrap;background:#2c2c2c;padding:.5em;border-radius:6px;
    font-size:.82em;margin:.4em 0 0;overflow-x:auto}
"""


def esc(v) -> str:
    return html.escape(str(v), quote=True)


def safe_url(u: str) -> str | None:
    """Only http(s). A board is written by an agent; a javascript: href on a
    page Chris opens is not a risk worth carrying for zero benefit."""
    u = str(u).strip()
    return u if u.lower().startswith(("http://", "https://")) else None


def load() -> tuple[list[dict], list[tuple[str, str]]]:
    """Returns (projects, broken). A broken file is RETURNED, never skipped:
    silently dropping it would make a typo look like a finished project."""
    projects: list[dict] = []
    broken: list[tuple[str, str]] = []
    try:
        names = sorted(n for n in os.listdir(PROJECTS) if n.endswith(".json"))
    except OSError as exc:
        return [], [(PROJECTS, "cannot list the projects directory: %s" % exc)]

    for name in names:
        path = os.path.join(PROJECTS, name)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            broken.append((name, str(exc)))
            continue
        if not isinstance(data, dict):
            broken.append((name, "top level is %s, expected an object" % type(data).__name__))
            continue
        if not str(data.get("title", "")).strip():
            broken.append((name, 'no "title"'))
            continue
        status = str(data.get("status", "active")).lower()
        if status not in STATUSES:
            broken.append((name, 'status %r is not one of %s' % (status, ", ".join(STATUSES))))
            continue
        # mtime, not a field. An "updated" the agent has to remember to bump is
        # an "updated" that lies the first time it forgets -- and the whole
        # point of the marker below is to show when nothing has happened.
        try:
            data["_mtime"] = os.path.getmtime(path)
        except OSError:
            data["_mtime"] = 0.0
        data["_status"] = status
        data["_file"] = name
        projects.append(data)
    return projects, broken


def steps_of(p: dict) -> list[dict]:
    out = []
    for s in p.get("steps") or []:
        if isinstance(s, str):
            s = {"name": s, "state": "todo"}
        if not isinstance(s, dict):
            continue
        state = str(s.get("state", "todo")).lower()
        out.append({
            "name": str(s.get("name", "")),
            "state": state if state in STEP_STATES else "todo",
            "note": str(s.get("note", "")),
        })
    return out


def ago(seconds: float) -> str:
    if seconds < 90 * 60:
        return "%dm" % max(1, int(seconds // 60))
    if seconds < 48 * 3600:
        return "%dh" % int(seconds // 3600)
    return "%dd" % int(seconds // 86400)


def render_card(p: dict, now: float) -> str:
    status = p["_status"]
    steps = steps_of(p)
    done = sum(1 for s in steps if s["state"] == "done")
    quiet = now - p["_mtime"]

    bits = ['<article class="card %s">' % status]
    head = ['<h2>', esc(p["title"]), '<span class="pill %s">%s</span>' % (status, status)]
    # The marker exists for exactly one failure: a project that says "active"
    # while nothing has touched it for days. Only "active" can go quiet --
    # paused and done are quiet on purpose, and a blocked project is already
    # flagged with what it needs.
    if status == "active" and quiet > STALE_HOURS * 3600:
        head.append('<span class="quiet">quiet %s</span>' % esc(ago(quiet)))
    head.append('</h2>')
    bits.append("".join(head))

    if str(p.get("goal", "")).strip():
        bits.append('<p class="goal">%s</p>' % esc(p["goal"]))

    if steps:
        pct = int(round(100.0 * done / len(steps)))
        bits.append('<div class="bar"><span style="width:%d%%"></span></div>' % pct)
        bits.append('<p class="count">%d of %d step%s</p>'
                    % (done, len(steps), "" if len(steps) == 1 else "s"))

    if status == "blocked":
        on = str(p.get("blocked_on", "")).strip() or "(not recorded — say what it needs)"
        bits.append('<p class="blockedon"><b>Blocked on:</b> %s</p>' % esc(on))
    elif status != "done" and str(p.get("next", "")).strip():
        bits.append('<p class="next"><b>Next:</b> %s</p>' % esc(p["next"]))

    if steps:
        li = []
        for s in steps:
            note = ' <span class="note">— %s</span>' % esc(s["note"]) if s["note"] else ""
            li.append('<li class="%s">%s%s</li>' % (s["state"], esc(s["name"]), note))
        bits.append('<ul class="steps">%s</ul>' % "".join(li))

    links = []
    for l in p.get("links") or []:
        if not isinstance(l, dict):
            continue
        url = safe_url(l.get("url", ""))
        if url:
            links.append('<a href="%s">%s</a>' % (esc(url), esc(l.get("label") or url)))
    if links:
        bits.append('<p class="links">%s</p>' % "".join(links))

    log = [e for e in (p.get("log") or []) if isinstance(e, dict)]
    if log:
        items = "".join(
            "<li>%s%s</li>" % (
                ("<b>%s</b> — " % esc(e["when"])) if str(e.get("when", "")).strip() else "",
                esc(e.get("what", "")),
            )
            for e in reversed(log)
        )
        bits.append('<details><summary>Log (%d)</summary><ol>%s</ol></details>' % (len(log), items))

    meta = []
    if str(p.get("started", "")).strip():
        meta.append("started %s" % esc(p["started"]))
    meta.append("touched %s ago" % esc(ago(quiet)))
    meta.append("<code>%s</code>" % esc(p["_file"]))
    bits.append('<p class="meta">%s</p>' % " · ".join(meta))

    bits.append("</article>")
    return "".join(bits)


def render() -> str:
    now = time.time()
    projects, broken = load()
    projects.sort(key=lambda p: (STATUSES.index(p["_status"]), -p["_mtime"]))

    counts = {s: sum(1 for p in projects if p["_status"] == s) for s in STATUSES}
    summary = ", ".join("%d %s" % (counts[s], s) for s in STATUSES if counts[s])
    if broken:
        summary = (summary + ", " if summary else "") + "%d UNREADABLE" % len(broken)

    out = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>Project board — Gromit</title>",
        "<style>%s</style></head><body>" % STYLE,
        "<h1>Project board</h1>",
        '<p class="sub">%s · rendered %s</p>' % (
            esc(summary or "nothing tracked"),
            esc(time.strftime("%a %-d %b %Y %-I:%M %p %Z", time.localtime(now))),
        ),
    ]

    for name, err in broken:
        out.append(
            '<article class="card broken"><h2>%s<span class="pill">unreadable</span></h2>'
            "<p>This project file could not be read, so whatever it was tracking is "
            "NOT on this board.</p><pre>%s</pre></article>" % (esc(name), esc(err))
        )

    if not projects and not broken:
        # An explicit empty state. A blank page cannot be told apart from a
        # renderer that failed, and this board's whole job is to be trusted.
        out.append(
            '<div class="empty"><p>No projects are being tracked.</p>'
            "<p>Drop a <code>&lt;slug&gt;.json</code> into "
            "<code>%s</code> and this page will list it.</p></div>" % esc(PROJECTS)
        )

    out += [p for p in (render_card(x, now) for x in projects)]
    out.append("</body></html>")
    return "".join(out)


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "render"

    if cmd == "check":
        _, broken = load()
        for name, err in broken:
            print("project-board: %s: %s" % (name, err), file=sys.stderr)
        if broken:
            print("project-board: %d unreadable project file(s)" % len(broken), file=sys.stderr)
            return 1
        print("project-board: all project files parse")
        return 0

    if cmd != "render":
        print(__doc__, file=sys.stderr)
        return 1

    page = render()
    os.makedirs(WEB, exist_ok=True)
    # Written via a temp file in the same directory and renamed: nginx must
    # never be able to serve a half-written page.
    tmp = os.path.join(WEB, ".index.html.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(page)
    os.replace(tmp, os.path.join(WEB, "index.html"))
    os.chmod(os.path.join(WEB, "index.html"), 0o644)

    projects, broken = load()
    print("project-board: rendered %d project(s), %d unreadable -> %s/index.html"
          % (len(projects), len(broken), WEB))
    # Exit 0 even with a broken file: the page RENDERED, and it names the
    # problem in red at the top. Failing here would take the whole board down
    # over one typo, which is the opposite of what a status surface is for.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
