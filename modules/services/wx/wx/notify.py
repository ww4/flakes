"""Delivery, and the quiet-hours contract.

The standing rule (Chris, 2026-08-19, reaffirmed 2026-09-07): nothing may wake
him except a physical danger to the household. Everything else is HELD and
delivered as ONE consolidated summary after 07:00 — not a ping per occurrence.

So there are exactly two ways out of this module:

  send()   -> straight to ntfy, now. Only `critical` may take this path at night.
  queue()  -> a row in `pending`, flushed by `wx morning` at 07:05.

⚠️ ntfy is reached at http://localhost:8090 via gromit-notify, NOT through the
nginx vhost. That is deliberate and load-bearing: the alert path must not depend
on the thing most likely to be broken. A notification about nginx being down
cannot be delivered through nginx.
"""
from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, time, timezone

from .db import queue_pending, record_push

try:  # pragma: no cover - platform detail
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    EASTERN = None

QUIET_START = time(22, 0)
QUIET_END = time(7, 0)


def local_now(when: datetime | None = None) -> datetime:
    when = when or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(EASTERN) if EASTERN else when.astimezone()


def in_quiet_hours(when: datetime | None = None) -> bool:
    t = local_now(when).time()
    return t >= QUIET_START or t < QUIET_END


def send(title: str, body: str, *, priority: str = "default", tags: str = "",
         click: str = "", notifier: str = "gromit-notify", timeout: int = 30) -> None:
    subprocess.run([notifier, title, body, priority, tags, click],
                   check=True, timeout=timeout, capture_output=True, text=True)


def deliver(con: sqlite3.Connection, *, layer: int, key: str, title: str,
            body: str, alert_class: str, when: datetime | None = None,
            notifier: str = "gromit-notify", tags: str = "") -> str:
    """Send now, or hold until morning. Returns 'sent' | 'held'.

    `critical` is the only class that may pierce quiet hours, and it is the
    only class that maps to ntfy priority `urgent` — which on his phone is what
    gets through Android's scheduled Do Not Disturb.
    """
    quiet = in_quiet_hours(when)
    if alert_class == "critical":
        priority = "urgent"
    elif quiet:
        queue_pending(con, layer=layer, key=key, title=title, body=body)
        return "held"
    else:
        priority = "default"

    send(title, body, priority=priority,
         tags=tags or ("rotating_light" if alert_class == "critical" else "cloud"),
         notifier=notifier)
    record_push(con, layer=layer, key=key, priority=priority, title=title, body=body)
    return "sent"


def flush_pending(con: sqlite3.Connection, *, notifier: str = "gromit-notify") -> int:
    """Deliver everything held overnight as ONE notification. Returns the count.

    One message, not N. The rule is not "delay the pings"; it is "do not ping
    him repeatedly", and a queue that replays fifteen alerts at 07:05 has
    honoured the letter of quiet hours and broken the point of them.
    """
    rows = con.execute("SELECT * FROM pending ORDER BY queued_at").fetchall()
    if not rows:
        return 0

    lines = []
    for r in rows:
        stamp = local_now(datetime.fromisoformat(r["queued_at"])).strftime("%-I:%M %p")
        lines.append(f"{stamp} — {r['title']}: {r['body']}")
    body = "\n".join(lines)
    title = (f"Overnight weather ({len(rows)} held)" if len(rows) > 1
             else f"Overnight: {rows[0]['title']}")

    send(title, body, priority="default", tags="cloud", notifier=notifier)
    record_push(con, layer=0, key="morning", priority="default",
                title=title, body=body)
    con.execute("DELETE FROM pending")
    con.commit()
    return len(rows)
