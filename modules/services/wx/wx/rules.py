"""LAYER 3 — the judgement call, written down so it can be argued with.

Chris asked for "ad hoc NTFY alerts on trends I should pay close attention to"
and explicitly left the threshold to me. This module is that threshold. It is a
pure function over an extraction plus the current SPC picture, which is the
only reason it can be tested at all — every interesting decision here is one
that would otherwise only be observable during an actual storm.

PUSH ONLY WHEN ALL FOUR HOLD:

  1. the hazard is in the severe class          (not heat, not rain)
  2. the region is inside the northern KY fence
  3. the window starts within 7 days
  4. AND EITHER he escalated against his own previous video on this system,
     OR SPC has not drawn a risk area over the house for those days

Clause 4 is the entire point. If SPC already has an Enhanced Risk on Tuesday,
Layer 1 will tell him and Ryan saying so is redundant noise. The push fires
when RYAN IS AHEAD OF THE OFFICIAL PRODUCT — the one thing here that no weather
app already does.

⚠️ "We could not fetch SPC" is treated as "SPC has it", not as "SPC has
nothing". The inverse would turn every SPC outage into a push storm, and an
outage is exactly when nobody is around to notice.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

SEVERE_HAZARDS = {
    "tornado", "derecho", "damaging wind", "flash flood", "hail",
    "ice storm", "significant snow",
}

MAX_LEAD_DAYS = 7
# SPC's categorical outlook only reaches day 3. Anything past it is, by
# definition, not yet drawn.
SPC_HORIZON = 3
PER_SYSTEM_COOLDOWN = timedelta(hours=24)


class Decision:
    __slots__ = ("push", "reason")

    def __init__(self, push: bool, reason: str):
        self.push = push
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Decision(push={self.push}, reason={self.reason!r})"


def _parse_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def window_offsets(start, end, today: date) -> list[int]:
    """Day offsets from today covered by the forecast window, clamped sanely."""
    s = _parse_date(start)
    if s is None:
        return []
    e = _parse_date(end) or s
    if e < s:
        s, e = e, s
    first = (s - today).days
    last = (e - today).days
    # A window entirely in the past is a recap, not a forecast.
    if last < 0:
        return []
    return list(range(max(first, 0), min(last, MAX_LEAD_DAYS) + 1))


def spc_is_ahead(offsets: list[int], drawn: set[int], known: set[int]) -> bool:
    """True when SPC has NOT drawn the threat for the days Ryan is talking about.

    Reads as: is there at least one day in his window that SPC either cannot
    reach yet (beyond day 3) or has looked at and left blank?
    """
    for off in offsets:
        if off > SPC_HORIZON:
            return True            # past the outlook horizon: nothing drawn yet
        if off < 1:
            continue               # today; Layer 1 owns today
        if off not in known:
            continue               # we could not look — assume covered
        if off not in drawn:
            return True            # looked, and SPC left it blank
    return False


def last_push_at(con: sqlite3.Connection, key: str) -> datetime | None:
    row = con.execute(
        "SELECT sent_at FROM push WHERE key = ? ORDER BY sent_at DESC LIMIT 1",
        (key,)).fetchone()
    if not row:
        return None
    try:
        return datetime.fromisoformat(row["sent_at"])
    except ValueError:
        return None


def evaluate(fields: dict, *, drawn: set[int], known: set[int],
             today: date, previous_push: datetime | None = None,
             when: datetime | None = None) -> Decision:
    hazards = set(fields.get("hazards") or [])
    severe = hazards & SEVERE_HAZARDS
    if not severe:
        return Decision(False, f"no severe hazard (hazards={sorted(hazards) or 'none'})")

    if (fields.get("fence_score") or 0) < 2:
        return Decision(False, f"outside the fence (regions={fields.get('regions') or []})")

    offsets = window_offsets(fields.get("window_start"), fields.get("window_end"), today)
    if not offsets:
        return Decision(False, "no forecast window, or the window has passed")
    if min(offsets) > MAX_LEAD_DAYS:
        return Decision(False, f"beyond {MAX_LEAD_DAYS} days out")

    escalating = fields.get("escalation") == "up"
    ahead = spc_is_ahead(offsets, drawn, known)
    if not (escalating or ahead):
        return Decision(False, "SPC already has it drawn and he is not escalating")

    # Speculation this far out is his normal register; it only earns an
    # interruption when he is actually ramping up about it.
    if fields.get("confidence") == "speculative" and not escalating:
        return Decision(False, "speculative and not escalating")

    if previous_push is not None:
        now_ = when or datetime.now(timezone.utc)
        if previous_push.tzinfo is None:
            previous_push = previous_push.replace(tzinfo=timezone.utc)
        if now_ - previous_push < PER_SYSTEM_COOLDOWN:
            return Decision(False, "already pushed about this system in the last 24 h")

    why = "he is escalating" if escalating else "ahead of SPC"
    return Decision(True, f"{', '.join(sorted(severe))} within {max(offsets)} days — {why}")


def decide(con: sqlite3.Connection, fields: dict, *, drawn: set[int],
           known: set[int], today: date | None = None) -> Decision:
    """evaluate(), with the cooldown looked up from the push log."""
    key = f"ryan:{fields.get('system_id') or 'unknown'}"
    return evaluate(fields, drawn=drawn, known=known,
                    today=today or datetime.now(timezone.utc).date(),
                    previous_push=last_push_at(con, key))


def load_fields(row: sqlite3.Row) -> dict:
    """An extraction row back into the dict shape evaluate() expects."""
    return {
        "system_id": row["system_id"],
        "hazards": json.loads(row["hazards"] or "[]"),
        "regions": json.loads(row["regions"] or "[]"),
        "window_start": row["window_start"],
        "window_end": row["window_end"],
        "confidence": row["confidence"],
        "escalation": row["escalation"],
        "fence_score": row["fence_score"],
        "summary": row["summary"],
        "quotes": json.loads(row["quotes"] or "[]"),
    }
