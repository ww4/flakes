"""Ingesting a local corpus — a directory of markdown, not a feed.

Some things worth reading are not published on the open web at all. Rick's blog
is the case that prompted this: 2,520 posts already archived on this box by
ww4/drycreek-archive, of which 479 run past 800 words, and its live RSS feed
only exposes the most recent handful. Chris asked to have it back in rotation
"on occasion" — the archive is the only way to do that.

The same path serves any corpus he owns and points at: a directory of markdown
with YAML front matter. Nothing here fetches anything.

⚠️ This deliberately ingests only from the LOCAL disk. It is not a scraper. If
a corpus is copyrighted, the copy on this box is his and stays on this box.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .db import now
from .score import score_text

# Scraped archives carry wreckage — server errors, empty stubs, navigation
# fragments. This is a corpus of essays; anything shorter is not one.
MIN_WORDS = 300

# Signatures of a failed scrape rather than a post. Found in the Dry Creek
# archive, which contains pages like "UserLand Frontier Server Error".
JUNK = re.compile(
    r"(server error|there was an error|page not found|"
    r"because it doesn't exist|error was detected by)", re.I)


def parse_post(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta: dict = {}
    body = raw
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end > 0:
            for line in raw[3:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip("'\"")
            body = raw[end + 4:]
    # Drop the duplicated H1 + byline the archive builder writes above the text.
    body = re.sub(r"\A\s*#[^\n]*\n(\*[^\n]*\*\n)?", "", body).strip()
    words = len(body.split())
    if words < MIN_WORDS or JUNK.search(body[:400]):
        return None
    date = (meta.get("date") or "").strip()
    return {
        "guid": path.name,
        "url": meta.get("url") or f"file://{path}",
        "title": meta.get("title") or path.stem.replace("-", " "),
        "body": body,
        "published": f"{date}T00:00:00+00:00" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else None,
        "words": words,
    }


def ingest(con: sqlite3.Connection, profile: dict, *, only: str | None = None) -> dict:
    """Walk every corpus source and insert what is not already there."""
    q = "SELECT * FROM sources WHERE enabled = 1 AND kind = 'corpus'"
    args: tuple = ()
    if only:
        q += " AND name = ?"
        args = (only,)
    sources = con.execute(q, args).fetchall()

    stats = {"corpora": len(sources), "added": 0, "skipped": 0, "missing": 0}
    for src in sources:
        root = Path(src["url"])
        if not root.is_dir():
            # Fail loudly in the return value rather than silently ingesting
            # nothing — an empty result and a missing directory must not look
            # the same.
            stats["missing"] += 1
            con.execute("UPDATE sources SET last_error=?, fail_streak=fail_streak+1"
                        " WHERE name=?",
                        (f"corpus directory not found: {root}", src["name"]))
            continue

        added = 0
        for path in sorted(root.glob("*.md")):
            post = parse_post(path)
            if post is None:
                stats["skipped"] += 1
                continue
            score, signals = score_text(post["title"], post["body"], profile)
            try:
                cur = con.execute(
                    "INSERT OR IGNORE INTO items"
                    " (source, lane, guid, url, title, summary, body, published,"
                    "  first_seen, words, score, signals)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (src["name"], src["lane"], post["guid"], post["url"],
                     post["title"], "", post["body"], post["published"],
                     now(), post["words"], score, json.dumps(signals)))
                added += cur.rowcount
            except sqlite3.Error:
                continue
        stats["added"] += added
        con.execute("UPDATE sources SET last_success=?, fail_streak=0, last_error=NULL"
                    " WHERE name=?", (now(), src["name"]))
    con.commit()
    return stats
