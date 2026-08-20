"""Grading and tuning.

Two rules here are load-bearing and should survive any rewrite of this file:

1. **Grading is optional.** Nothing nags, nothing counts ungraded items at
   him, nothing re-notifies. An edition nobody grades is a normal edition.

2. **Silence is not a negative signal.** Only an explicit thumbs-down counts
   against anything. It is tempting to treat "published but never graded" as
   mild disapproval — it is far more likely to mean he was busy, and a busy
   month would quietly poison the profile.

What follows from those: the tuner can only act on evidence it actually has,
so it moves slowly, it can never switch a source off, and it changes SOURCE
weights automatically but only ever PROPOSES term-weight changes. Terms are how
the whole thing decides what he cares about; those should not drift silently.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .db import now, state_dir, write_atomic

# Bounds. Deliberately tight — a tuner that can swing hard on four clicks is a
# tuner that will overfit one bad week.
CAP_MIN, CAP_MAX = 1, 5
WEIGHT_MIN, WEIGHT_MAX = 0.4, 2.0
DOWN_TO_DEMOTE = 4
UP_TO_PROMOTE = 3
TERM_DOCS_TO_PROPOSE = 3

SPACE_TAG_UP = re.compile(r"#good\b", re.IGNORECASE)
SPACE_TAG_DOWN = re.compile(r"#meh\b", re.IGNORECASE)
SPACE_ID = re.compile(r"nd:(\d+)")


# Web grades no longer arrive through this module at all: newsdesk-grade
# writes them straight to the `grades` table as they are clicked. See
# gradeserver.py for why that is a service and not an nginx access_log.


def ingest_space(con: sqlite3.Connection, space_dir: Path) -> int:
    """Read #good / #meh tags off the SilverBullet edition pages."""
    if not space_dir.is_dir():
        return 0
    n = 0
    for page in sorted(space_dir.glob("*.md")):
        for line in page.read_text(errors="replace").splitlines():
            m = SPACE_ID.search(line)
            if not m:
                continue
            if SPACE_TAG_UP.search(line):
                value = 1
            elif SPACE_TAG_DOWN.search(line):
                value = -1
            else:
                continue
            if con.execute("SELECT 1 FROM items WHERE id=?", (int(m.group(1)),)).fetchone() is None:
                continue
            con.execute(
                "INSERT INTO grades (item_id, via, value, at) VALUES (?,'space',?,?)"
                " ON CONFLICT(item_id, via) DO UPDATE SET value=excluded.value, at=excluded.at",
                (int(m.group(1)), value, now()))
            n += 1
    con.commit()
    return n


def _grade_totals(con: sqlite3.Connection) -> dict[int, int]:
    """One net grade per item, so grading in both surfaces is not double-counted."""
    out: dict[int, int] = {}
    for row in con.execute(
            "SELECT item_id, AVG(value) AS v FROM grades GROUP BY item_id"):
        out[row["item_id"]] = 1 if row["v"] > 0 else (-1 if row["v"] < 0 else 0)
    return out


def tune(con: sqlite3.Connection, profile: dict) -> str:
    """Apply bounded source adjustments; propose term changes. Returns a report."""
    totals = _grade_totals(con)
    if not totals:
        return ("No grades recorded yet, so nothing was tuned. That is a fine "
                "state to be in — grading is optional.")

    graded_items = con.execute(
        "SELECT id, source, title, body, summary FROM items WHERE id IN"
        f" ({','.join('?' * len(totals))})", tuple(totals)).fetchall()

    # --- source weights: applied, within bounds ---------------------------
    per_source: dict[str, list[int]] = {}
    for r in graded_items:
        per_source.setdefault(r["source"], []).append(totals[r["id"]])

    applied: list[str] = []
    for source, votes in sorted(per_source.items()):
        up = sum(1 for v in votes if v > 0)
        down = sum(1 for v in votes if v < 0)
        row = con.execute("SELECT cap, weight FROM sources WHERE name=?",
                          (source,)).fetchone()
        if row is None:
            continue
        cap, weight = row["cap"], row["weight"]
        new_cap, new_weight = cap, weight
        if down >= DOWN_TO_DEMOTE and up == 0:
            new_cap = max(CAP_MIN, cap - 1)
            new_weight = max(WEIGHT_MIN, round(weight * 0.85, 3))
        elif up >= UP_TO_PROMOTE and down == 0:
            new_cap = min(CAP_MAX, cap + 1)
            new_weight = min(WEIGHT_MAX, round(weight * 1.15, 3))
        if (new_cap, new_weight) != (cap, weight):
            con.execute("UPDATE sources SET cap=?, weight=? WHERE name=?",
                        (new_cap, new_weight, source))
            applied.append(f"**{source}**: cap {cap}→{new_cap}, "
                           f"weight {weight}→{new_weight} (+{up}/−{down})")

    # --- term weights: proposed only --------------------------------------
    up_docs: dict[str, int] = {}
    down_docs: dict[str, int] = {}
    for r in graded_items:
        blob = f"{r['title']} {r['body'] or r['summary'] or ''}".lower()
        bucket = up_docs if totals[r["id"]] > 0 else down_docs
        for term in profile.get("interests", {}):
            if term.lower() in blob:
                bucket[term] = bucket.get(term, 0) + 1

    proposals: list[str] = []
    for term, weight in sorted(profile.get("interests", {}).items()):
        u, d = up_docs.get(term, 0), down_docs.get(term, 0)
        if d >= TERM_DOCS_TO_PROPOSE and u == 0:
            proposals.append(f'- `"{term}": {weight}` → `{max(1, weight - 2)}`'
                             f" — appeared in {d} rejected items, 0 liked")
        elif u >= TERM_DOCS_TO_PROPOSE and d == 0:
            proposals.append(f'- `"{term}": {weight}` → `{min(9, weight + 1)}`'
                             f" — appeared in {u} liked items, 0 rejected")

    for line in applied:
        con.execute("INSERT INTO tuning_log (at, kind, detail) VALUES (?,?,?)",
                    (now(), "applied", line))
    for line in proposals:
        con.execute("INSERT INTO tuning_log (at, kind, detail) VALUES (?,?,?)",
                    (now(), "proposed", line))
    con.commit()

    n_up = sum(1 for v in totals.values() if v > 0)
    n_down = sum(1 for v in totals.values() if v < 0)
    report = [f"# Newsdesk tuning — {now()[:10]}", "",
              f"{len(totals)} graded item(s): {n_up} relevant, {n_down} not interesting.",
              ""]
    if applied:
        report += ["## Applied (source weights, bounded)", ""] + [f"- {a}" for a in applied] + [""]
    else:
        report += ["No source adjustment met the evidence threshold.", ""]
    if proposals:
        report += ["## Proposed (term weights — NOT applied)", "",
                   "Edit `/var/lib/newsdesk/interests.json` to accept any of these."
                   " Ignoring them is a valid answer.", ""] + proposals + [""]
    return "\n".join(report)


def write_tuning_page(report: str, space_dir: Path) -> None:
    if space_dir.parent.exists():
        try:
            write_atomic(space_dir / "Tuning.md", report + "\n")
        except OSError:
            pass


def stats(con: sqlite3.Connection) -> dict:
    row = con.execute(
        "SELECT (SELECT COUNT(*) FROM sources WHERE enabled=1) AS sources,"
        " (SELECT COUNT(*) FROM items) AS items,"
        " (SELECT COUNT(*) FROM items WHERE state='new') AS pending,"
        " (SELECT COUNT(*) FROM items WHERE state='published') AS published,"
        " (SELECT COUNT(*) FROM grades) AS grades,"
        " (SELECT COUNT(*) FROM editions) AS editions").fetchone()
    return json.loads(json.dumps(dict(row)))
