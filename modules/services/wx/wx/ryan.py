"""LAYER 2, part one — getting Ryan Hall's words off YouTube.

Measured on 2026-09-05/06 rather than assumed:

  * auto-captions come back in ~3.9 s with no API key and no cookies
  * the /videos tab excludes Shorts and returns only long-form (640-860 s,
    1-2 uploads a day), so it is the right listing to poll
  * ⚠️ the caption endpoint returns HTTP 429 after roughly FOUR pulls

That 429 is why this module is written around deferral rather than retry loops.
A transcript we cannot fetch right now is not an error; it is a thing to try
again later, and the backoff has to survive across process runs, which means it
lives in the database and not in a loop.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import now

CHANNEL_ID = "UCJHAT3Uvv-g3I8H3GhHWV7w"
VIDEOS_URL = "https://www.youtube.com/@RyanHallYall/videos"

# Anything shorter is a Short or a channel bumper, not a forecast. The /videos
# tab already excludes Shorts; this is the belt to that pair of braces.
MIN_DURATION = 240

MAX_ATTEMPTS = 6
BASE_BACKOFF = timedelta(minutes=30)

# How many transcripts one run may pull. The whole point of the 429 finding:
# a run that tries to catch up on ten videos gets nothing at all.
PER_RUN_LIMIT = 2


def list_recent(*, ytdlp: str = "yt-dlp", limit: int = 8,
                timeout: int = 120) -> list[dict]:
    """Recent long-form uploads: id, title, duration. One network call."""
    proc = subprocess.run(
        # --no-update: suppress the >90-days version warning. It is advice for
        # a human at a terminal, and here it is 365 characters of stderr ahead
        # of anything that matters (see _salient).
        [ytdlp, "--no-update", "--flat-playlist", "--playlist-end", str(limit),
         "-J", VIDEOS_URL],
        capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp listing failed: {proc.stderr.strip()[:300]}")
    payload = json.loads(proc.stdout)
    out = []
    for entry in payload.get("entries") or []:
        duration = int(entry.get("duration") or 0)
        if duration < MIN_DURATION:
            continue
        out.append({
            "id": entry.get("id"),
            "title": (entry.get("title") or "").strip(),
            "duration": duration,
        })
    return [v for v in out if v["id"]]


def parse_json3(raw: str) -> str:
    """Flatten YouTube's json3 caption format into plain prose.

    Auto-captions arrive as overlapping timed segments; joining the events and
    collapsing whitespace is enough, and it keeps the text small enough that a
    12-minute video is only a few thousand tokens.
    """
    payload = json.loads(raw)
    parts: list[str] = []
    for event in payload.get("events") or []:
        for seg in event.get("segs") or []:
            text = seg.get("utf8") or ""
            if text.strip():
                parts.append(text)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def fetch_transcript(video_id: str, *, ytdlp: str = "yt-dlp",
                     timeout: int = 120) -> str:
    """Auto-captions for one video, as prose. Raises on 429 so the caller defers."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "sub"
        proc = subprocess.run(
            [ytdlp, "--no-update", "--skip-download", "--write-auto-subs",
             "--sub-langs", "en", "--sub-format", "json3", "-o", str(out),
             f"https://www.youtube.com/watch?v={video_id}"],
            capture_output=True, text=True, timeout=timeout, check=False)
        files = sorted(Path(tmp).glob("*.json3"))
        if not files:
            err = (proc.stderr or proc.stdout or "").strip()
            # ⚠️ THE ERROR LINES FIRST, not the head of stderr.
            #
            # `record_failure` decides deferral-vs-failure by looking for
            # "429" in this message, and the message used to be `err[:300]` —
            # the raw head of stderr. On 2026-10-02 yt-dlp passed 90 days old
            # and began printing a 365-character version WARNING ahead of
            # everything else, so the real line
            #
            #   ERROR: Unable to download video subtitles for 'en':
            #          HTTP Error 429: Too Many Requests
            #
            # sat at character 428 of 450 and was cut off. "429" stopped
            # appearing, every throttle was recorded as a hard failure, and
            # four videos burned their six attempts and parked in `failed`
            # where nothing retries them. Measured, not inferred: '429' in
            # err[:300] was False while '429' in err was True.
            #
            # Hoisting the ERROR: lines guarantees the signal survives any
            # truncation, whatever upstream decides to print before it.
            raise RuntimeError(f"no captions for {video_id}: {_salient(err)[:300]}")
        return parse_json3(files[0].read_text(encoding="utf-8"))


def _salient(stderr: str, limit: int = 300) -> str:
    """Reorder yt-dlp's stderr so the diagnosis survives truncation.

    yt-dlp prints warnings before errors and its version warning alone is 365
    characters, which is longer than anything we store. Any line mentioning an
    error or an HTTP status is pulled to the front; the rest follows as
    context for a human reading it later.
    """
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    loud, quiet = [], []
    for line in lines:
        upper = line.upper()
        (loud if upper.startswith("ERROR") or "HTTP ERROR" in upper
         else quiet).append(line)
    return " ".join(loud + quiet)[:limit] if (loud or quiet) else ""


def _due(row: sqlite3.Row, when: datetime) -> bool:
    """Has this deferred video waited out its backoff?"""
    if not row["last_attempt"]:
        return True
    try:
        last = datetime.fromisoformat(row["last_attempt"])
    except ValueError:
        return True
    return when >= last + BASE_BACKOFF * (2 ** max(0, row["attempts"] - 1))


def sync_listing(con: sqlite3.Connection, videos: list[dict]) -> int:
    """Record uploads we have not seen before. Returns how many are new."""
    added = 0
    for v in videos:
        cur = con.execute(
            "INSERT OR IGNORE INTO video (id, title, duration, first_seen, state)"
            " VALUES (?,?,?,?, 'pending')",
            (v["id"], v["title"], v["duration"], now()))
        added += cur.rowcount
    con.commit()
    return added


def pending_videos(con: sqlite3.Connection, *, when: datetime | None = None,
                   limit: int = PER_RUN_LIMIT) -> list[sqlite3.Row]:
    """Videos wanting a transcript, newest first, respecting per-video backoff."""
    when = when or datetime.now(timezone.utc)
    rows = con.execute(
        "SELECT * FROM video WHERE state IN ('pending','deferred')"
        " AND attempts < ? ORDER BY first_seen DESC", (MAX_ATTEMPTS,)).fetchall()
    return [r for r in rows if _due(r, when)][:limit]


def reset_failed(con: sqlite3.Connection) -> list[str]:
    """Put `failed` videos back in the queue. Returns the ids revived.

    Deliberately manual (`wx retry`) rather than automatic. A video reaches
    `failed` after six real attempts, and a module that quietly revived them
    on every run would hammer an endpoint that has already said no six times.

    It exists because a classification bug can strand videos that were never
    hard failures: between 2026-10-02 and 2026-10-19 four uploads were parked
    here by rate limits that should have been deferrals (see _salient). After
    a fix like that, something has to let them back in.
    """
    rows = con.execute("SELECT id FROM video WHERE state = 'failed'").fetchall()
    ids = [r["id"] for r in rows]
    if ids:
        con.execute("UPDATE video SET state = 'pending', attempts = 0,"
                    " last_attempt = NULL, last_error = NULL WHERE state = 'failed'")
        con.commit()
    return ids


def store_transcript(con: sqlite3.Connection, video_id: str, text: str) -> None:
    con.execute("UPDATE video SET transcript = ?, state = 'have', last_error = NULL"
                " WHERE id = ?", (text, video_id))
    con.commit()


def record_failure(con: sqlite3.Connection, video_id: str, error: str) -> str:
    """Defer (retryable) or give up. Returns the new state.

    A 429 is explicitly NOT a failure — it is the expected steady state of
    pulling captions from a datacentre address, and burning the attempt budget
    on it would mean losing videos to a rate limit that clears on its own.
    """
    row = con.execute("SELECT attempts FROM video WHERE id = ?", (video_id,)).fetchone()
    attempts = (row["attempts"] if row else 0) + 1
    throttled = "429" in error or "too many requests" in error.lower()
    state = "deferred" if (throttled or attempts < MAX_ATTEMPTS) else "failed"
    # Throttling costs half an attempt's worth of budget, so a long throttled
    # spell still eventually gives up rather than retrying forever.
    if throttled:
        attempts = min(attempts, MAX_ATTEMPTS - 1)
    con.execute("UPDATE video SET state = ?, attempts = ?, last_attempt = ?,"
                " last_error = ? WHERE id = ?",
                (state, attempts, now(), error[:500], video_id))
    con.commit()
    return state
