"""State: SQLite, one file, created on first use.

The schema exists to answer three questions that no single poll can answer on
its own:

  * have I already told him about this alert?  (nws_alert.notified_class)
  * has Ryan's framing of THIS system changed since his last video?
    (extraction.system_id + escalation)
  * is something waiting to be delivered at 07:00?  (pending)

`pending` is the quiet-hours contract made concrete. The standing rule is that
overnight findings are held and delivered as ONE consolidated summary after
07:00 — not a ping per occurrence. A row here is a thing that WOULD have been
sent had it not been the middle of the night.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS nws_alert (
    id              TEXT PRIMARY KEY,
    event           TEXT NOT NULL,
    severity        TEXT,
    urgency         TEXT,
    certainty       TEXT,
    headline        TEXT,
    area_desc       TEXT,
    onset           TEXT,
    ends            TEXT,
    sent            TEXT,
    first_seen      TEXT NOT NULL,
    -- The class we NOTIFIED at, not the class it is. A reissue at the same
    -- class must not notify again; an escalation must.
    notified_class  TEXT,
    notified_at     TEXT
);

CREATE TABLE IF NOT EXISTS video (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    published    TEXT,
    duration     INTEGER,
    first_seen   TEXT NOT NULL,
    -- pending | have | deferred | failed | skipped
    state        TEXT NOT NULL DEFAULT 'pending',
    transcript   TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_attempt TEXT,
    last_error   TEXT
);

CREATE TABLE IF NOT EXISTS extraction (
    video_id     TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    system_id    TEXT,
    hazards      TEXT NOT NULL DEFAULT '[]',
    regions      TEXT NOT NULL DEFAULT '[]',
    window_start TEXT,
    window_end   TEXT,
    confidence   TEXT,
    escalation   TEXT,
    fence_score  INTEGER NOT NULL DEFAULT 0,
    summary      TEXT,
    -- The one field written for a glance: northern Kentucky only, or the
    -- string "nothing for northern Kentucky". `summary` stays the whole
    -- video, because the switchboard and the newsdesk both want all of it.
    -- ⚠️ Added after the table existed, so it is ALSO in MIGRATIONS below —
    -- CREATE TABLE IF NOT EXISTS does nothing to a database that already has
    -- the table, and this column would silently never appear on gromit.
    local        TEXT,
    quotes       TEXT NOT NULL DEFAULT '[]',
    raw          TEXT
);

CREATE TABLE IF NOT EXISTS spc_state (
    day         INTEGER PRIMARY KEY,
    fetched_at  TEXT NOT NULL,
    valid_date  TEXT,
    label       TEXT,
    in_risk     INTEGER NOT NULL DEFAULT 0
);

-- Every push we actually made. Layer 3's per-system rate limit reads this, and
-- it is the record that answers "why did it not tell me?" after the fact.
CREATE TABLE IF NOT EXISTS push (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at   TEXT NOT NULL,
    layer     INTEGER NOT NULL,
    key       TEXT NOT NULL,
    priority  TEXT NOT NULL,
    title     TEXT NOT NULL,
    body      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS push_key ON push (key, sent_at);

-- Held by quiet hours. Flushed as one summary by `wx morning`.
CREATE TABLE IF NOT EXISTS pending (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    queued_at TEXT NOT NULL,
    layer     INTEGER NOT NULL,
    key       TEXT NOT NULL,
    title     TEXT NOT NULL,
    body      TEXT NOT NULL
);

-- Free-form counters/marks, e.g. the last successful channel poll.
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_dir() -> Path:
    """Where the database lives.

    Defaults to /var/lib/wx, but the tests set WX_STATE to a sandbox path and
    the suite refuses to run without it — the same guard the newsdesk uses,
    for the same reason: a test suite that can write to live state will
    eventually corrupt it.
    """
    return Path(os.environ.get("WX_STATE", "/var/lib/wx"))


# Columns added after a table first shipped. `CREATE TABLE IF NOT EXISTS` is a
# no-op on an existing database, so every column added later has to be applied
# here as well — otherwise it exists on a fresh install and nowhere else, and
# the only symptom is a field that is always empty on the machine that matters.
MIGRATIONS = [
    ("extraction", "local", "TEXT"),
]


def _apply_migrations(con: sqlite3.Connection) -> list[str]:
    """Add any missing column from MIGRATIONS. Returns what it added.

    Idempotent by inspection rather than by catching an error: `PRAGMA
    table_info` is the authority on what the live table actually has, and
    swallowing "duplicate column name" would also swallow a real failure.
    """
    added = []
    for table, column, decl in MIGRATIONS:
        cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        if not cols:
            continue          # table not created yet; SCHEMA will include it
        if column not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            added.append(f"{table}.{column}")
    if added:
        con.commit()
    return added


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else state_dir() / "wx.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _apply_migrations(con)
    con.commit()
    return con


def get_meta(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    return row["v"] if row else default


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT INTO meta (k, v) VALUES (?, ?)"
                " ON CONFLICT(k) DO UPDATE SET v = excluded.v", (key, value))
    con.commit()


def record_push(con: sqlite3.Connection, *, layer: int, key: str,
                priority: str, title: str, body: str) -> None:
    con.execute("INSERT INTO push (sent_at, layer, key, priority, title, body)"
                " VALUES (?,?,?,?,?,?)", (now(), layer, key, priority, title, body))
    con.commit()


def queue_pending(con: sqlite3.Connection, *, layer: int, key: str,
                  title: str, body: str) -> None:
    con.execute("INSERT INTO pending (queued_at, layer, key, title, body)"
                " VALUES (?,?,?,?,?)", (now(), layer, key, title, body))
    con.commit()


def load_location(path: str | Path) -> tuple[float, float]:
    """Read the coordinates out of the sops-decrypted location file.

    ⚠️ PII. These are Chris's home coordinates. They live in
    secrets/wx-location.json (sops, encrypted to gromit's host key and his
    admin key) and are decrypted to a 0400 file at activation. They must never
    be committed in plaintext, logged, or put in a notification body — both
    repos are mirrored, and ww4/flakes is PUBLIC on GitHub.

    Accepts either a JSON object or simple `key: value` lines, because how
    sops-nix materialises a secret depends on the `format`/`key` options and
    getting a 0400 file wrong is a silent, awkward failure at 3 a.m. Parsing
    both costs eight lines and removes the whole question.
    """
    text = Path(path).read_text(encoding="utf-8")
    data: dict = {}
    stripped = text.strip()
    if stripped.startswith("{"):
        data = json.loads(stripped)
    else:
        for line in stripped.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            k, v = line.split(":", 1)
            data[k.strip()] = v.strip().strip("'\"")
    try:
        return float(data["latitude"]), float(data["longitude"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"{path}: expected 'latitude' and 'longitude'; got keys {sorted(data)}"
        ) from exc
