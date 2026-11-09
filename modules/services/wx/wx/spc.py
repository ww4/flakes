"""SPC convective outlooks — the official product Ryan Hall gets compared against.

This module is what makes Layer 3 more than a keyword alarm. The question it
answers is narrow and specific:

    "For the days Ryan is talking about, has SPC already drawn a risk area
     over this house?"

If YES, Layer 1 will tell Chris in its own time and Ryan repeating it is noise.
If NO, then Ryan is AHEAD of the official product — and that is the only thing
in this whole design that no weather app already does for him.

Verified live 2026-09-06: day1/2/3 categorical outlooks are served as GeoJSON
with a LABEL2 of 'Marginal Risk', 'Slight Risk', 'General Thunderstorms Risk'
and so on. Day 4-8 is a separate probabilistic product.
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request

from .db import now
from .geo import point_in_geometry

DAY_URLS = {
    1: "https://www.spc.noaa.gov/products/outlook/day1otlk_cat.nolyr.geojson",
    2: "https://www.spc.noaa.gov/products/outlook/day2otlk_cat.nolyr.geojson",
    3: "https://www.spc.noaa.gov/products/outlook/day3otlk_cat.nolyr.geojson",
}

# Ordered weakest to strongest. 'General Thunderstorms' is NOT a severe risk —
# treating it as one would make SPC look like it had already drawn every summer
# afternoon, and Layer 3 would go permanently silent. That failure is silent
# and total, so it gets its own constant rather than a clever comparison.
RISK_ORDER = [
    "general thunderstorms risk",
    "marginal risk",
    "slight risk",
    "enhanced risk",
    "moderate risk",
    "high risk",
]
SEVERE_FLOOR = RISK_ORDER.index("marginal risk")


def _rank(label: str | None) -> int:
    if not label:
        return -1
    key = label.strip().lower()
    if not key.endswith(" risk"):
        key += " risk"
    return RISK_ORDER.index(key) if key in RISK_ORDER else -1


def strongest_at_point(payload: dict, lat: float, lon: float) -> str | None:
    """The highest categorical risk whose polygon contains the point."""
    best_label, best_rank = None, -1
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        label = props.get("LABEL2") or props.get("LABEL") or props.get("label")
        if not point_in_geometry(feature.get("geometry"), lon, lat):
            continue
        r = _rank(label)
        if r > best_rank:
            best_label, best_rank = label, r
    return best_label


def fetch_day(day: int, *, user_agent: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(DAY_URLS[day], headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def refresh(con: sqlite3.Connection, lat: float, lon: float, *,
            user_agent: str) -> dict[int, str | None]:
    """Update the cached day1-3 picture. A failed day must not fail the others.

    ⚠️ A fetch failure writes NOTHING rather than writing 'no risk'. "We could
    not look" and "we looked and there is nothing" are different facts, and
    collapsing them here would make Layer 3 push on every SPC outage.
    """
    result: dict[int, str | None] = {}
    for day in sorted(DAY_URLS):
        try:
            payload = fetch_day(day, user_agent=user_agent)
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            continue
        label = strongest_at_point(payload, lat, lon)
        severe = _rank(label) >= SEVERE_FLOOR
        con.execute(
            "INSERT INTO spc_state (day, fetched_at, label, in_risk) VALUES (?,?,?,?)"
            " ON CONFLICT(day) DO UPDATE SET"
            " fetched_at = excluded.fetched_at, label = excluded.label,"
            " in_risk = excluded.in_risk",
            (day, now(), label, 1 if severe else 0))
        result[day] = label
    con.commit()
    return result


def drawn_days(con: sqlite3.Connection) -> set[int]:
    """Days 1-3 where SPC currently has a severe risk over the point."""
    rows = con.execute("SELECT day FROM spc_state WHERE in_risk = 1").fetchall()
    return {r["day"] for r in rows}


def known_days(con: sqlite3.Connection) -> set[int]:
    """Days we have actually looked at. Absence of a row is not 'no risk'."""
    return {r["day"] for r in con.execute("SELECT day FROM spc_state").fetchall()}
