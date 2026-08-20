"""Good reads: the long-form section, its rotation, and the archive.

This section has a different physics from the rest of the brief and needs its
own rules.

WHY IT IS NOT JUST A LANE
-------------------------
News perishes; essays do not. The daily brief drops anything older than ten
days, and when that filter was measured against the corpus it was hiding **358
long pieces, 914,000 words** — most of Dan Luu's archive, Ken Shirriff's
teardowns, Front Porch Republic, Practical Engineering — none of which is any
worse for being three years old. So this pool has NO recency filter at all, and
that is the single decision that makes the section possible.

Long pieces also come from every lane (linux 40% of the backlog, macro 20%,
bitcoin 16%, down to network at 2%), so "longform" is an axis, not a topic.

ROTATION — Chris's requirement, in his words
--------------------------------------------
    "if a longread doesn't get picked that's not necessarily a negative
    signal. It can be added back to the mix at a later date, but at a lower
    score... I want to feel like if I miss a good article, it will come back
    around. On the flip side, once I read the article, mark it off so it
    doesn't rotate back in."

So:
  * showing an item is not consuming it — it records `shown_count` and comes
    back after a cooldown, weighted down but never to zero (MIN_MULTIPLIER is
    a floor, so a great essay ignored five times still outranks a mediocre one
    never shown);
  * a CLICK retires it. Clicking is the read signal, tracked by routing the
    title through /news/r before redirecting to the source;
  * a thumbs-DOWN retires it too — that is an explicit no, unlike silence.

This is also why unchosen candidates must NOT be marked `passed_over` the way
news items are. Doing that would burn a 914,000-word backlog inside a month.

VARIETY
-------
The backlog is 30% Dan Luu and 14% Liberty Street Economics. Picking "the best
long piece" three times a day, with no other rule, produces a systems blog with
his name on it. Hence one item per source and one per lane per edition.

THE ARCHIVE
-----------
Picks get their full text stored as a page on our own site, because link rot is
real and, as Chris put it, text is cheap and there is no reason to throw it out
once it has been curated. Only picks — never the whole intake.

⚠️ The archive is a PERSONAL copy behind the same Tailscale source gate as
every other vhost on this host. It is not published, not indexed, and must not
be. Every archived page carries the original author, publication, date and a
link back.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

import random

from . import feeds
from .db import now, state_dir, write_atomic

# What counts as long. Deliberately below the 1,500 used for measurement: a
# tight 1,100-word essay is a good read, and the reader judges quality anyway.
MIN_WORDS = 1000

# How many go on the reader's desk to choose 2-3 from.
SHORTLIST = 12

# Rotation. Fixed cooldown so an ignored piece reliably comes back around,
# with the weight decaying each time it is passed over.
COOLDOWN_DAYS = 14
DECAY = 0.8
MIN_MULTIPLIER = 0.3

# Archive: fetch the real article, not the feed's truncated body. The scoring
# path caps bodies at 20k chars, which is why 155 items in the corpus sit
# exactly at that ceiling and their true length is unknowable.
ARCHIVE_MAX_CHARS = 400_000


def eligible(con: sqlite3.Connection, *, min_words: int = MIN_WORDS,
             seed: str | None = None) -> list[sqlite3.Row]:
    """Everything that could be a good read today, in the order to consider it.

    ⚠️ NOT ordered by `score`. The keyword profile rewards nixos/bitcoin/
    mikrotik density — it was built to rank NEWS about the things he runs. For
    "any topic, good writing" it is worse than useless: measured against the
    real backlog it buried Dan Luu, Palladium and Craig Mod under 1,100-word
    technical posts, because an essay called "Culture matters" scores nothing
    on a profile full of `compact block` and `routeros`.

    So the shortlist is a DIVERSE SAMPLE, not a ranking, and every judgement of
    quality is left to the reader, which is the only thing here that can
    actually read. Order is `rotation weight × a per-day shuffle`:

      * a never-shown piece outranks a much-shown one about three to one —
        enough to work through a backlog, not so much that a skipped piece
        waits a year to come back;
      * the shuffle is seeded by the date, so it is stable within a run and
        different tomorrow.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=COOLDOWN_DAYS)).isoformat()
    rows = con.execute(
        "SELECT i.*, s.tier, s.note AS source_note, s.evergreen FROM items i"
        " JOIN sources s ON s.name = i.source"
        " WHERE i.words >= ?"
        "   AND i.clicked_at IS NULL"          # read -> retired, never returns
        "   AND i.state != 'published'"        # already ran as news
        "   AND s.enabled = 1"
        "   AND s.longform = 1"               # newsletters/aggregators opt out
        "   AND i.lane != 'release-radar'"    # a release note is never an essay
        "   AND (i.last_shown_at IS NULL OR i.last_shown_at < ?)"
        "   AND NOT EXISTS (SELECT 1 FROM grades g"
        "                   WHERE g.item_id = i.id AND g.value < 0)",
        (min_words, cutoff)).fetchall()
    rng = random.Random(seed or datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    return sorted(rows, key=lambda r: -(weight(r) * rng.random()))


def weight(row: sqlite3.Row) -> float:
    return max(MIN_MULTIPLIER, DECAY ** (row["shown_count"] or 0))


def shortlist(con: sqlite3.Connection, *, limit: int = SHORTLIST,
              min_words: int = MIN_WORDS, seed: str | None = None) -> list[dict]:
    """Pick candidates, one per source and one per lane, to hand the reader."""
    seen_sources: set[str] = set()
    seen_lanes: set[str] = set()
    out: list[dict] = []
    spare: list[sqlite3.Row] = []

    rows = eligible(con, min_words=min_words, seed=seed)

    # Evergreen archives get ONE reserved slot between them, never more.
    # Rick's blog alone is 479 eligible pieces against a pool of ~500; left to
    # compete it would appear every single morning, which is not what "sprinkle
    # some in occasionally" means. Reserving the slot also guarantees the
    # reader always HAS the option when the day's news happens to resonate.
    evergreens = [r for r in rows if r["evergreen"]]
    if evergreens:
        pick = evergreens[0]
        seen_sources.add(pick["source"])
        seen_lanes.add(pick["lane"])
        out.append(_as_candidate(pick))

    for row in (r for r in rows if not r["evergreen"]):
        if len(out) >= limit:
            break
        if row["source"] in seen_sources:
            continue
        if row["lane"] in seen_lanes:
            spare.append(row)
            continue
        seen_sources.add(row["source"])
        seen_lanes.add(row["lane"])
        out.append(_as_candidate(row))

    # Once every lane is represented, relax the lane rule but keep one per
    # source, so a thin day still fills the desk.
    for row in spare:
        if len(out) >= limit:
            break
        if row["source"] in seen_sources:
            continue
        seen_sources.add(row["source"])
        out.append(_as_candidate(row))
    return out


def _as_candidate(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "evergreen": bool(row["evergreen"]),
        "lane": row["lane"],
        "source": row["source"],
        "source_note": row["source_note"] or "",
        "title": row["title"],
        "url": row["url"],
        "published": row["published"],
        "words": row["words"],
        "shown_before": row["shown_count"] or 0,
        "text": (row["body"] or row["summary"] or "")[:24000],
    }


def mark_shown(con: sqlite3.Connection, item_ids: list[int]) -> None:
    """Record that these were offered. NOT the same as consuming them."""
    con.executemany(
        "UPDATE items SET shown_count = shown_count + 1, last_shown_at = ?"
        " WHERE id = ?", [(now(), i) for i in item_ids])
    con.commit()


def record_click(con: sqlite3.Connection, item_id: int) -> bool:
    """A click is the read signal. Retires the item from rotation for good."""
    cur = con.execute(
        "UPDATE items SET clicked_at = COALESCE(clicked_at, ?) WHERE id = ?",
        (now(), item_id))
    con.commit()
    return cur.rowcount > 0


# --------------------------------------------------------------------------
# the archive
# --------------------------------------------------------------------------

def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return (s or "untitled")[:70]


def archive(con: sqlite3.Connection, item_id: int, *, web_dir=None) -> str | None:
    """Store our own copy of a pick. Returns the site-relative path, or None.

    Best effort by design: a failure here must never cost the edition. The
    stored feed body is the fallback when the live fetch fails, and if there is
    no usable text at all we skip rather than write an empty shell.
    """
    row = con.execute(
        "SELECT i.*, s.insecure_tls FROM items i JOIN sources s ON s.name=i.source"
        " WHERE i.id = ?", (item_id,)).fetchone()
    if row is None:
        return None
    if row["archived_path"]:
        return row["archived_path"]

    text = ""
    try:
        text = feeds.fetch_article_text(
            row["url"], insecure=bool(row["insecure_tls"]),
            limit=ARCHIVE_MAX_CHARS)
    except Exception:  # noqa: BLE001 — fall back to what the feed gave us
        text = ""
    if len(text.split()) < len((row["body"] or "").split()):
        text = row["body"] or row["summary"] or ""
    if len(text.split()) < 100:
        return None

    rel = f"archive/{row['id']}-{_slug(row['title'])}.html"
    path = (web_dir or (state_dir() / "web")) / rel
    write_atomic(path, _archive_page(row, text))
    path.chmod(0o644)
    con.execute("UPDATE items SET archived_path = ? WHERE id = ?", (rel, row["id"]))
    con.commit()
    return rel


ARCHIVE_STYLE = (
    "body{max-width:42rem;margin:2rem auto;padding:0 1.2rem;"
    "font:17px/1.7 Georgia,'Iowan Old Style',serif;color:#e6e6e6;background:#181818}"
    "header{border-bottom:1px solid #444;padding-bottom:1rem;margin-bottom:2rem;"
    "font-family:system-ui,sans-serif}"
    "h1{font-size:1.6rem;line-height:1.25;margin:0 0 .5rem}"
    "a{color:#6cb6ff}.meta{color:#999;font-size:.85rem;font-family:system-ui,sans-serif}"
    "article{white-space:pre-wrap}"
    "footer{margin-top:3rem;border-top:1px solid #444;padding-top:1rem;"
    "color:#888;font-size:.8rem;font-family:system-ui,sans-serif}"
)


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _archive_page(row: sqlite3.Row, text: str) -> str:
    when = (row["published"] or "")[:10]
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        f"<title>{_esc(row['title'])}</title><style>{ARCHIVE_STYLE}</style></head><body>"
        f"<header><h1>{_esc(row['title'])}</h1>"
        f"<div class=\"meta\">{_esc(row['source'])}"
        + (f" · {when}" if when else "")
        + f" · <a href=\"{_esc(row['url'])}\">original</a></div></header>"
        f"<article>{_esc(text)}</article>"
        "<footer>Personal reading archive — a copy kept against link rot, on a "
        "host reachable only over Tailscale. All rights remain with the "
        f"original author and publication. Retrieved {now()[:10]} from "
        f"<a href=\"{_esc(row['url'])}\">{_esc(row['url'])}</a>.</footer>"
        "</body></html>"
    )
