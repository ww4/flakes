"""Collect: poll every enabled source and record what is new.

Two rules shape this file.

FIRST: one bad source must never take down the run. Failures are per-source,
recorded in the source row, and the process still exits 0. A whole-run failure
is reserved for something structural (no database, no profile).

SECOND — and this is the one that matters — a source that has gone quiet and a
source that has gone DEAD must not look the same. Four of the candidate feeds
for this project were dead while still serving a healthy-looking item count;
the archive updaters next door once answered a real question with "not found"
because they had silently stopped two months earlier. So every source carries
`fail_streak` and `median_gap_h`, and `stale_sources()` reports both kinds of
silence to the edition itself, where they are visible, rather than to a log
nobody reads.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import feeds
from .db import now
from .score import score_text

MAX_WORKERS = 8
# A source is called stale when it has been silent for this many times its own
# median publishing gap. Generous on purpose: a weekly newsletter that skips a
# week is not a problem, and a false stale warning every edition is how the
# warning stops being read.
STALE_GAP_MULTIPLE = 3.0
# ...but never complain before this, so a genuinely rare source (Lyn Alden
# publishes roughly monthly) is not perpetually "stale".
STALE_MIN_DAYS = 21
# Consecutive failed polls before the edition says so.
STALE_FAIL_STREAK = 3
# Items nobody shortlisted eventually stop being news.
EXPIRE_DAYS = 21
# A dormant source counts as awake only if it published something THIS recently.
# Merely seeing its items for the first time does not count: on a fresh
# database every item is new, including a decade of back-catalogue, which would
# announce all ten dormant sources as resurrected on day one.
AWAKEN_MAX_AGE_DAYS = 30
# Past this much silence a source stops being "stale" and becomes dormant: the
# warning has been made, repeating it every morning forever adds nothing, and
# the interesting event is now its return. Matches the 120-day rule the
# catalogue itself was built with.
DORMANT_AFTER_DAYS = 120
# Release feeds are bursty by nature — ten tags in a week, then nothing for two
# months, and neither is a fault. Their silence carries no information, so only
# the failing-poll check applies to them.
NO_SILENCE_CHECK_LANES = {"release-radar"}
# A source that has failed this many consecutive polls (~6 days at four polls a
# day) is not having a bad afternoon — it is blocking us, gone, or moved. It
# decays into dormancy for the same reason a silent one does: the warning has
# been made. Polling continues, so if it ever comes back it is announced.
FAIL_DORMANT_STREAK = 25


def _poll(row: sqlite3.Row) -> dict:
    """Network half — runs in a worker thread, touches no database."""
    out: dict = {"name": row["name"], "row": row}
    try:
        res = feeds.fetch_feed(
            row["url"],
            etag=row["etag"],
            last_modified=row["last_modified"],
            insecure=bool(row["insecure_tls"]),
        )
        out["result"] = res
    except feeds.NotModified:
        out["not_modified"] = True
    except Exception as e:  # noqa: BLE001 - any failure is just this source's
        out["error"] = f"{type(e).__name__}: {e}"[:300]
    return out


def collect(con: sqlite3.Connection, profile: dict, *, only: str | None = None) -> dict:
    # 'corpus' sources are local directories walked by `newsdesk ingest`, not
    # URLs to poll. Fetching one would fail every run and eventually mark a
    # perfectly healthy archive as dead.
    q = "SELECT * FROM sources WHERE enabled = 1 AND kind = 'feed'"
    args: tuple = ()
    if only:
        q += " AND name = ?"
        args = (only,)
    sources = con.execute(q, args).fetchall()
    if not sources:
        # Fail closed: an empty roster means the seed never ran, and silently
        # "succeeding" with zero sources is the whole class of bug this
        # project is trying not to have.
        raise SystemExit("newsdesk: no enabled sources — was the catalogue seeded?")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        polled = list(ex.map(_poll, sources))

    stats = {"sources": len(sources), "ok": 0, "not_modified": 0, "failed": 0,
             "new_items": 0}

    for p in polled:
        name = p["name"]
        row = p["row"]

        if p.get("not_modified"):
            stats["not_modified"] += 1
            con.execute(
                "UPDATE sources SET last_success=?, fail_streak=0, last_error=NULL"
                " WHERE name=?", (now(), name))
            continue

        if "error" in p:
            stats["failed"] += 1
            con.execute(
                "UPDATE sources SET fail_streak = fail_streak + 1, last_error=?"
                " WHERE name=?", (p["error"], name))
            continue

        res = p["result"]
        stats["ok"] += 1
        added = 0
        recent_cutoff = datetime.now(timezone.utc) - timedelta(days=AWAKEN_MAX_AGE_DAYS)
        added_recent = 0
        for e in res.entries:
            if not e.title or not e.url:
                continue
            text = e.body or e.summary
            score, signals = score_text(e.title, text, profile)
            try:
                cur = con.execute(
                    "INSERT OR IGNORE INTO items"
                    " (source, lane, guid, url, title, summary, body, published,"
                    "  first_seen, words, score, signals)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (name, row["lane"], e.guid, e.url, e.title, e.summary,
                     e.body,
                     # Normalised to UTC on the way in. Feeds publish every
                     # offset under the sun, and these are compared as STRINGS
                     # by the recency filter — mixed offsets would silently
                     # mis-order items by up to a day.
                     e.published.astimezone(timezone.utc).isoformat()
                     if e.published else None,
                     now(), len(text.split()), score, json.dumps(signals)),
                )
                added += cur.rowcount
                if cur.rowcount and e.published and e.published > recent_cutoff:
                    added_recent += 1
            except sqlite3.Error:
                continue
        stats["new_items"] += added

        # A dormant source that publishes has just done the one thing it was
        # kept in the catalogue for. Clear the flag so it is announced exactly
        # once and then treated like any other source. Note this tests
        # added_recent, not added — see AWAKEN_MAX_AGE_DAYS.
        if added_recent and row["dormant"]:
            con.execute("UPDATE sources SET dormant=0, awakened_at=? WHERE name=?",
                        (now(), name))

        dates = sorted([e.published for e in res.entries if e.published])
        newest = dates[-1].isoformat() if dates else None
        gaps = [(b - a).total_seconds() / 3600.0
                for a, b in zip(dates, dates[1:]) if (b - a).total_seconds() > 0]
        median_gap = round(statistics.median(gaps), 2) if len(gaps) >= 3 else None

        # last_item_at only ever moves forward: a feed that drops its history
        # (or reorders it) must not make itself look newly stale.
        last_item = max([d for d in (newest, row["last_item_at"]) if d],
                        default=None)
        con.execute(
            "UPDATE sources SET etag=?, last_modified=?, last_success=?,"
            " fail_streak=0, last_error=NULL, last_item_at=?,"
            " median_gap_h=COALESCE(?, median_gap_h)"
            " WHERE name=?",
            (res.etag, res.last_modified, now(), last_item, median_gap, name))

    # Sources that have been silent long enough decay into dormancy, which is
    # what stops the stale list growing without bound.
    dormant_cutoff = (datetime.now(timezone.utc)
                      - timedelta(days=DORMANT_AFTER_DAYS)).isoformat()
    stats["newly_dormant"] = con.execute(
        "UPDATE sources SET dormant=1 WHERE enabled=1 AND dormant=0"
        " AND ((last_item_at IS NOT NULL AND last_item_at < ?)"
        "      OR fail_streak >= ?)",
        (dormant_cutoff, FAIL_DORMANT_STREAK)).rowcount

    cutoff = (datetime.now(timezone.utc) - timedelta(days=EXPIRE_DAYS)).isoformat()
    expired = con.execute(
        "UPDATE items SET state='expired' WHERE state='new' AND first_seen < ?",
        (cutoff,)).rowcount
    stats["expired"] = expired
    con.commit()
    return stats


def _human_gap(hours: float) -> str:
    if hours < 1:
        return "few minutes"
    if hours < 36:
        return f"{hours:.0f}h"
    return f"{hours / 24:.1f} days"


def stale_sources(con: sqlite3.Connection) -> list[dict]:
    """Sources that are failing, or silent well past their own rhythm.

    Returned to the edition renderer, not to a log. The whole point is that
    Chris sees it.
    """
    out = []
    for row in con.execute(
            "SELECT * FROM sources WHERE enabled = 1 AND dormant = 0"):
        if row["fail_streak"] >= STALE_FAIL_STREAK:
            out.append({
                "name": row["name"], "lane": row["lane"], "kind": "failing",
                "detail": f"{row['fail_streak']} consecutive failed polls"
                          f" — {row['last_error'] or 'no error recorded'}",
            })
            continue
        if row["lane"] in NO_SILENCE_CHECK_LANES:
            continue
        last = feeds.parse_date(row["last_item_at"])
        if last is None:
            continue
        silent_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
        if silent_h < STALE_MIN_DAYS * 24:
            continue
        gap = row["median_gap_h"]
        if gap and silent_h > gap * STALE_GAP_MULTIPLE:
            out.append({
                "name": row["name"], "lane": row["lane"], "kind": "silent",
                # Sources that publish in bursts have a median gap well under
                # a day, which rendered as "usually every 0.0" in the edition.
                "detail": f"nothing for {silent_h / 24:.0f} days"
                          f" (usually every {_human_gap(gap)})",
            })
    return sorted(out, key=lambda s: s["name"])


def awakened_sources(con: sqlite3.Connection, days: int = 7) -> list[dict]:
    """Sources that have published for the first time in months.

    This is the payoff for keeping ten dormant feeds in the catalogue. Rick's
    blog restarting, or Living Energy Farm posting again, is exactly the kind
    of thing he would otherwise never find out about.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return [{"name": r["name"], "lane": r["lane"], "detail": "published again"}
            for r in con.execute(
                "SELECT name, lane FROM sources WHERE awakened_at IS NOT NULL"
                " AND awakened_at >= ? ORDER BY name", (cutoff,))]
