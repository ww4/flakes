"""What the listener thinks of a track, and what that does to how often it plays.

Chris, 2026-09-27: "Merge the next and less button. If I want less of something
then you should automatically skip it, and if I do skip something you should
probably play less of it. It's the same input. In its place put a thumbs up.
This shouldn't interrupt the flow but you can collect thumbs and put them
towards repeat plays."

So there is one negative gesture and one positive one:

  SKIP   skips the track now AND counts against it. Pressed once it is a
         shrug; pressed on the same track repeatedly it stops coming back.
         Nothing has to be declared "never" for that to happen.
  THUMB  counts for it, and does nothing else — no interruption, which is the
         whole point of a thumb.

A score is simply `thumbs - skips`, and the score becomes a weight. For the
TRACK's own score:

    score   -4     -3     -2    -1     0    +1   +2   +3
    weight  out   1/8    1/4   1/2     1     2    4    8

The artist's score multiplies that at half strength, and the product is capped
both ways — so a much-thumbed song by a much-thumbed artist comes round oftener
but cannot take a station over, which unbounded multiplication would let it do
(a thrice-thumbed track measured 22x before the cap).

Two properties worth stating, because they are why this shape and not a
threshold: it is gradual (one skip halves a track's chances rather than
banishing it, so a skip because you were not in the mood is not a life
sentence), and it is escalating (four skips and it is gone without a verdict
ever being pronounced).

The artist carries a score too, aggregated from their tracks, applied at
HALF strength — dampened deliberately, so disliking one song does not quietly
mute a whole catalogue.

`dislikes.json` is untouched and still means what it meant: an explicit,
permanent no. This file is the soft, accumulating opinion beside it.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Iterable

log = logging.getLogger("netradio.ratings")

EMPTY: dict = {"tracks": {}, "artists": {}}

# score -> weight is 2**score, clamped. OUT is the score at which a track stops
# being offered at all; +3 is as much as a thumb can buy, so a favourite cannot
# crowd a station down to a handful of songs.
OUT = -4
MAX_UP = 3
# The artist's own score counts half as much as the track's, and cannot on its
# own remove anything — only a track's score can do that.
ARTIST_DAMPING = 0.5
ARTIST_FLOOR = -3
# The most any combination of thumbs may buy. Without it the track and artist
# terms multiply without limit and one favourite crowds out everything else.
MAX_WEIGHT = 8.0


def now() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat(timespec="minutes")


def norm_artist(name: str) -> str:
    """The same key dislikes.json uses for artists, so the two agree."""
    return (name or "").strip().lower()


def _entry(store: dict, kind: str, key: str) -> dict:
    return store.setdefault(kind, {}).setdefault(key, {"skips": 0, "thumbs": 0})


def record(store: dict, *, kind: str, path: str, artist: str = "", title: str = "") -> dict:
    """Count one gesture. `kind` is "skip" or "thumb". Returns the store."""
    if kind not in ("skip", "thumb"):
        raise ValueError(f"unknown feedback: {kind!r}")
    field = "skips" if kind == "skip" else "thumbs"
    if path:
        t = _entry(store, "tracks", path)
        t[field] = int(t.get(field, 0)) + 1
        t["artist"], t["title"], t["when"] = artist or t.get("artist", ""), title or t.get("title", ""), now()
    if artist:
        a = _entry(store, "artists", norm_artist(artist))
        a[field] = int(a.get(field, 0)) + 1
        a["when"] = now()
    return store


def score(entry: dict | None) -> int:
    if not entry:
        return 0
    return int(entry.get("thumbs", 0)) - int(entry.get("skips", 0))


def track_score(store: dict, path: str) -> int:
    return score((store.get("tracks") or {}).get(path))


def artist_score(store: dict, artist: str) -> int:
    return score((store.get("artists") or {}).get(norm_artist(artist)))


def weight(store: dict, path: str, artist: str = "") -> float:
    """How often this track should play relative to an unrated one. 0.0 means
    do not offer it."""
    s = track_score(store, path)
    if s <= OUT:
        return 0.0
    s = min(s, MAX_UP)
    w = 2.0 ** s
    if artist:
        a = max(artist_score(store, artist), ARTIST_FLOOR)
        w *= 2.0 ** (min(a, MAX_UP) * ARTIST_DAMPING)
    return max(min(w, MAX_WEIGHT), 0.0)


def weights(store: dict, paths: Iterable[str], artist_of) -> list[float]:
    """Weights for a pool, in order. `artist_of` maps a path to an artist name —
    the caller has that (the DJ reads it from the path's folder)."""
    return [weight(store, p, artist_of(p)) for p in paths]


def pick(store: dict, paths: list[str], artist_of, rng) -> str | None:
    """One path, chosen with the weights above.

    Falls back to an unweighted choice when every candidate weighs zero, which
    happens when a small station is entirely skipped-out: better to play
    something the listener once skipped than to go silent (and the DJ's own
    `recent` list still keeps it from repeating).
    """
    if not paths:
        return None
    ws = weights(store, paths, artist_of)
    if not any(w > 0 for w in ws):
        log.info("every candidate is skipped out (%d of them); playing anyway", len(paths))
        return rng.choice(paths)
    total = math.fsum(ws)
    r = rng.random() * total
    upto = 0.0
    for p, w in zip(paths, ws):
        upto += w
        if upto >= r:
            return p
    return paths[-1]


def summary(store: dict) -> str:
    tracks = store.get("tracks") or {}
    artists = store.get("artists") or {}
    up = sum(1 for e in tracks.values() if score(e) > 0)
    down = sum(1 for e in tracks.values() if score(e) < 0)
    out = sum(1 for e in tracks.values() if score(e) <= OUT)
    return (f"{len(tracks)} tracks rated ({up} up, {down} down, {out} out), "
            f"{len(artists)} artists")


def stars(store: dict, path: str) -> int | None:
    """The score as a 1-5 star rating, for writing back into the library, or
    None when the track has no opinion either way.

        score  <=-3  -2  -1   0   +1  +2  >=+3
        stars     1   2   2   -    4   5     5

    Nothing writes this yet — it is here because the mapping is the part worth
    agreeing on before anything touches the files (Chris asked whether thumbs
    could become stars; the answer is yes, and this is the translation).
    """
    s = track_score(store, path)
    if s == 0:
        return None
    if s <= -3:
        return 1
    if s < 0:
        return 2
    if s == 1:
        return 4
    return 5
