"""Schema and connection handling for the newsdesk store.

One SQLite file holds everything: the source roster (with its live freshness
stats), every item ever seen, the grades, and the edition history. Keeping the
roster IN the database rather than reading sources.json every run is deliberate
— the tuner adjusts source weights and caps, and a packaged JSON that gets
overwritten on every deploy would silently undo his feedback.

The packaged sources.json is therefore a SEED: it inserts sources that are new
and refreshes the immutable facts (url, lane, tier) of ones that already exist,
but never touches the tuned columns.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    name          TEXT PRIMARY KEY,
    lane          TEXT NOT NULL,
    url           TEXT NOT NULL,
    tier          TEXT NOT NULL,
    insecure_tls  INTEGER NOT NULL DEFAULT 0,
    note          TEXT NOT NULL DEFAULT '',
    -- Known to have been silent for months when the catalogue was built. Kept
    -- on purpose: polling is free and the point is to notice if it WAKES.
    -- Excluded from stale warnings; cleared (once, loudly) on the next item.
    dormant       INTEGER NOT NULL DEFAULT 0,
    awakened_at   TEXT,
    -- tuned columns: written by the tuner and by hand, never by the seeder
    cap           INTEGER NOT NULL DEFAULT 1,
    weight        REAL    NOT NULL DEFAULT 1.0,
    enabled       INTEGER NOT NULL DEFAULT 1,
    -- fetch bookkeeping
    etag          TEXT,
    last_modified TEXT,
    last_success  TEXT,
    last_error    TEXT,
    fail_streak   INTEGER NOT NULL DEFAULT 0,
    last_item_at  TEXT,
    median_gap_h  REAL
);

CREATE TABLE IF NOT EXISTS items (
    id         INTEGER PRIMARY KEY,
    source     TEXT NOT NULL REFERENCES sources(name) ON DELETE CASCADE,
    lane       TEXT NOT NULL,
    guid       TEXT NOT NULL,
    url        TEXT NOT NULL,
    title      TEXT NOT NULL,
    summary    TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    published  TEXT,
    first_seen TEXT NOT NULL,
    words      INTEGER NOT NULL DEFAULT 0,
    score      REAL,
    signals    TEXT NOT NULL DEFAULT '[]',
    -- new -> shortlisted -> published | passed_over ; expired = aged out unseen
    state      TEXT NOT NULL DEFAULT 'new',
    edition    TEXT,
    UNIQUE (source, guid)
);
CREATE INDEX IF NOT EXISTS items_state  ON items (state, score DESC);
CREATE INDEX IF NOT EXISTS items_source ON items (source, first_seen);

CREATE TABLE IF NOT EXISTS grades (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    via     TEXT    NOT NULL,          -- 'web' | 'space'
    value   INTEGER NOT NULL,          -- +1 relevant, -1 not interesting
    at      TEXT    NOT NULL,
    PRIMARY KEY (item_id, via)
);

CREATE TABLE IF NOT EXISTS editions (
    id       TEXT PRIMARY KEY,         -- e.g. 2026-08-20-brief
    kind     TEXT NOT NULL,
    created  TEXT NOT NULL,
    n_short  INTEGER NOT NULL DEFAULT 0,
    n_published INTEGER NOT NULL DEFAULT 0,
    judged   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tuning_log (
    id      INTEGER PRIMARY KEY,
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL,             -- 'applied' | 'proposed'
    detail  TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state_dir() -> Path:
    return Path(os.environ.get("NEWSDESK_STATE", "/var/lib/newsdesk"))


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or (state_dir() / "news.db")
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    con.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    con.commit()
    return con


def seed_sources(con: sqlite3.Connection, catalogue: Path) -> tuple[int, int]:
    """Insert new sources; refresh immutable facts on existing ones.

    Never overwrites cap/weight/enabled — those belong to the tuner and to
    whoever edits the database by hand. A deploy must not undo a week of
    feedback.
    """
    data = json.loads(Path(catalogue).read_text())
    added = updated = 0
    for s in data["sources"]:
        cur = con.execute("SELECT name FROM sources WHERE name = ?", (s["name"],))
        if cur.fetchone() is None:
            con.execute(
                "INSERT INTO sources (name, lane, url, tier, insecure_tls, note,"
                " dormant, cap) VALUES (?,?,?,?,?,?,?,?)",
                (s["name"], s["lane"], s["url"], s["tier"],
                 int(bool(s.get("insecure_tls"))), s.get("note", ""),
                 int(bool(s.get("dormant"))), int(s["cap"])),
            )
            added += 1
        else:
            # dormant is deliberately NOT refreshed here: once a source has
            # woken up, a later deploy carrying the old catalogue must not put
            # it back to sleep.
            con.execute(
                "UPDATE sources SET lane=?, url=?, tier=?, insecure_tls=?, note=?"
                " WHERE name=?",
                (s["lane"], s["url"], s["tier"], int(bool(s.get("insecure_tls"))),
                 s.get("note", ""), s["name"]),
            )
            updated += 1
    con.commit()
    return added, updated


def load_profile(default_profile: Path | None = None) -> dict:
    """State-dir profile wins; the packaged default only seeds it.

    Same contract as podcast-triage's interest profile, deliberately: scoring
    you cannot tune without a rebuild is scoring you stop trusting.
    """
    live = state_dir() / "interests.json"
    if live.exists():
        return json.loads(live.read_text())
    src = default_profile or os.environ.get("NEWSDESK_DEFAULT_PROFILE")
    if src and Path(src).exists():
        text = Path(src).read_text()
        live.parent.mkdir(parents=True, exist_ok=True)
        tmp = live.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.replace(live)
        return json.loads(text)
    raise SystemExit("newsdesk: no interest profile found")


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
