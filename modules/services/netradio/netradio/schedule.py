"""Segments and spotlights: when a curated station plays something narrower
than its base, and what the DJ says about it.

A slot (schedule.json):

    {"id": "country-sat-swing", "station": "country", "name": "Western Swing Hour",
     "kind": "feed" | "artist" | "auto",
     "feed": "western-swing" | "artist": "George Jones" | "like": "feed" | "artist",
     "days": "daily" | "weekdays" | "weekends" | ["mon", "sat"],
     "start": "13:00", "minutes": 60}

`auto` slots are filled by the DJ once per day: something compatible with
the station that hasn't been picked for that slot recently (picks.json
remembers), so a spotlight Chris set up once keeps happening with a
different artist each time even when he sets nothing.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import random

from netradio.config import compatible

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MIN_TRACKS_FOR_SPOTLIGHT = 8
RECENT_PICKS = 6


def slot_days(spec) -> set[str]:
    if spec in (None, "", "daily"):
        return set(DAYS)
    if spec == "weekdays":
        return set(DAYS[:5])
    if spec == "weekends":
        return {"sat", "sun"}
    return {d.lower()[:3] for d in spec}


def slot_window(slot: dict, date: dt.date) -> tuple[dt.datetime, dt.datetime] | None:
    if DAYS[date.weekday()] not in slot_days(slot.get("days")):
        return None
    try:
        h, m = (int(x) for x in str(slot.get("start", "")).split(":"))
    except ValueError:
        return None
    start = dt.datetime.combine(date, dt.time(h, m))
    return start, start + dt.timedelta(minutes=int(slot.get("minutes") or 60))


def slots_today(station: str, slots: list[dict], date: dt.date) -> list[tuple[dt.datetime, dt.datetime, dict]]:
    out = []
    for s in slots:
        if s.get("station") != station:
            continue
        w = slot_window(s, date)
        if w:
            out.append((w[0], w[1], s))
    return sorted(out, key=lambda x: x[0])


def active_slot(station: str, slots: list[dict], now: dt.datetime) -> dict | None:
    for start, end, s in slots_today(station, slots, now.date()):
        if start <= now < end:
            return s
    return None


# --- auto picks ---------------------------------------------------------------

def spotlight_candidates(artists: dict, families: list[str] | None) -> list[str]:
    return sorted(a for a, info in artists.items()
                  if info.get("tracks", 0) >= MIN_TRACKS_FOR_SPOTLIGHT and compatible(families, info.get("families")))


def feed_candidates(feeds: dict, families: list[str] | None) -> list[str]:
    return sorted(fid for fid, f in feeds.items()
                  if f.get("status") == "ready" and (f.get("count") or 0) >= MIN_TRACKS_FOR_SPOTLIGHT
                  and compatible(families, f.get("family")))


def resolve(slot: dict, date: dt.date, picks: dict, *, station: dict, artists: dict, feeds: dict) -> tuple[dict, bool]:
    """The concrete slot for `date` and whether picks changed. A non-auto
    slot is itself. An auto slot becomes a feed or artist slot for the day,
    remembered in picks[slot id] so every reader agrees and so it isn't the
    same pick as the last few."""
    if slot.get("kind") != "auto":
        return slot, False
    key = slot.get("id") or slot.get("name") or "auto"
    history = picks.get(key, [])
    today = date.isoformat()
    for h in history:
        if h.get("date") == today:
            return {**slot, "kind": h["kind"], h["kind"]: h["value"],
                    "name": slot.get("name") or default_name(h["kind"], h["value"], feeds)}, False
    like = slot.get("like") or "artist"
    fam = station.get("family")
    pool = spotlight_candidates(artists, fam) if like == "artist" else feed_candidates(feeds, fam)
    recent = [h["value"] for h in history[-RECENT_PICKS:]]
    fresh = [p for p in pool if p not in recent] or pool
    if not fresh:
        return slot, False
    rng = random.Random(hashlib.sha256(f"{key}:{today}".encode()).hexdigest())
    value = rng.choice(fresh)
    history.append({"date": today, "kind": like, "value": value})
    picks[key] = history[-30:]
    return {**slot, "kind": like, like: value, "name": slot.get("name") or default_name(like, value, feeds)}, True


def default_name(kind: str, value: str, feeds: dict) -> str:
    if kind == "artist":
        return f"{value} spotlight"
    return (feeds.get(value) or {}).get("title") or value


# --- what the DJ says ----------------------------------------------------------

def say_time(t: dt.datetime) -> str:
    # Spoken by Kokoro: "8 PM" reads cleanly; "p.m." followed by a period does not.
    h = t.hour % 12 or 12
    ampm = "AM" if t.hour < 12 else "PM"
    return f"{h} {ampm}" if t.minute == 0 else f"{h}:{t.minute:02d} {ampm}"


def part_of_day(t: dt.datetime) -> str:
    if t.hour < 12:
        return "this morning"
    if t.hour < 17:
        return "this afternoon"
    return "tonight"


PROMO_FEED = [
    "Stay tuned at {time} for {name}.",
    "Coming up at {time}: {name}.",
    "{Part}, at {time}, it's {name}.",
    "Don't go far — {name} at {time}.",
]
PROMO_ARTIST = [
    "{Part}'s spotlight is on {artist}, at {time}.",
    "At {time} we're spotlighting {artist}.",
    "Stay with us: an hour of {artist} at {time}.",
    "{artist} gets the spotlight at {time}.",
]
INTRO_FEED = [
    "It's {name}, here on {station}.",
    "This is {name} — the next while on {station}.",
    "Time for {name} on {station}.",
]
INTRO_ARTIST = [
    "This hour we're spotlighting {artist}, on {station}.",
    "It's the {artist} spotlight, here on {station}.",
    "Settle in — an hour with {artist} on {station}.",
]


def promo(station_name: str, upcoming: list[tuple[dt.datetime, dt.datetime, dict]], rng: random.Random,
          at_most: int = 2) -> str:
    """A sentence or two about what's coming later today on this station."""
    parts = []
    for start, _end, s in upcoming[:at_most]:
        ctx = {"time": say_time(start), "Part": part_of_day(start).capitalize(), "part": part_of_day(start),
               "name": s.get("name") or "a special hour", "artist": s.get("artist") or ""}
        if s.get("kind") == "artist" and s.get("artist"):
            parts.append(rng.choice(PROMO_ARTIST).format(**ctx))
        else:
            parts.append(rng.choice(PROMO_FEED).format(**ctx))
    return " ".join(parts)


def intro(station_name: str, slot: dict, rng: random.Random) -> str:
    ctx = {"station": station_name, "name": slot.get("name") or "a special hour", "artist": slot.get("artist") or ""}
    if slot.get("kind") == "artist" and slot.get("artist"):
        return rng.choice(INTRO_ARTIST).format(**ctx)
    return rng.choice(INTRO_FEED).format(**ctx)


def upcoming(station: str, slots: list[dict], now: dt.datetime) -> list[tuple[dt.datetime, dt.datetime, dict]]:
    return [(a, b, s) for a, b, s in slots_today(station, slots, now.date()) if a > now]
