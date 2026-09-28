"""Stars: the ratings module and the library file agreeing about a track.

Chris, 2026-09-27: "Yes I want the Stars to show in jellyfin. Off by default is
a good idea. It should go both directions too, if you find stars in jellyfin,
that should influence the ratings module."

So this runs both ways over the library's own tags:

  OUT  a track with an opinion gets a star rating written into the file, which
       is what makes it visible in Jellyfin and in any other player that reads
       ratings.
  IN   a star already in a file becomes an opinion. That covers a rating set by
       any tagger, anything that was there before netradio existed, and —
       once a rating reaches the file — a star set elsewhere.

Formats, because every one of them chose differently:

  ID3 (mp3)      POPM frame, 0-255, with the buckets Windows and everything
                 after it settled on: 1*=1, 2*=64, 3*=128, 4*=196, 5*=255.
                 Also TXXX:RATING as a plain number, which some readers prefer.
  Vorbis (flac,  RATING as 0-100 (20 per star) and FMPS_RATING as 0.0-1.0,
  ogg, opus)     the two conventions in circulation.
  MP4 (m4a)      the ----:com.apple.iTunes:RATING freeform atom.

=== Why this cannot feed itself ===

The dangerous shape here is a loop: we write a star, read it back, treat it as
the listener's opinion, and slowly drift. So every track remembers
`stars_seen` — the last value this code either wrote or read — and only a
DIFFERENCE from that counts as a new human opinion. Our own writes are
therefore invisible to the import, and a star someone actually changed is not.

A star arriving from outside wins over the accumulated score, because it is the
more deliberate gesture: pressing skip four times is a mood, setting a rating is
a judgement. The score is moved to match (see `score_for_stars`), preserving the
thumb/skip counts underneath so nothing is lost.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from netradio import ratings
from netradio.config import Config

log = logging.getLogger("netradio.stars")

# 1-5 stars -> POPM byte. The de-facto buckets; a reader that uses ranges
# rather than exact values lands on the same star either way.
POPM = {1: 1, 2: 64, 3: 128, 4: 196, 5: 255}
POPM_EMAIL = "netradio"


def popm_to_stars(n: int) -> int:
    """POPM byte -> stars, by the ranges readers actually use."""
    if n <= 0:
        return 0
    if n < 32:
        return 1
    if n < 96:
        return 2
    if n < 160:
        return 3
    if n < 226:
        return 4
    return 5


def score_for_stars(stars: int) -> int:
    """A star set by hand, as a score. The inverse of ratings.stars(), which
    maps score -> stars; three stars is "no opinion", the middle of the dial.

        stars   1   2   3   4   5
        score  -3  -1   0  +1  +2
    """
    return {1: -3, 2: -1, 3: 0, 4: 1, 5: 2}.get(int(stars), 0)


# --- the files ---------------------------------------------------------------

def read_stars(path: str) -> int | None:
    """The star rating in the file, or None if it carries none."""
    try:
        import mutagen
        from mutagen.id3 import ID3
    except ImportError:                                  # pragma: no cover
        log.warning("mutagen is not available; cannot read ratings")
        return None
    try:
        f = mutagen.File(path)
    except Exception as e:
        log.debug("unreadable: %s (%s)", path, e)
        return None
    if f is None or f.tags is None:
        return None
    tags = f.tags
    # ID3: POPM first, then a TXXX fallback
    if isinstance(tags, ID3) or hasattr(tags, "getall"):
        try:
            popm = tags.getall("POPM")
            if popm:
                return popm_to_stars(int(getattr(popm[0], "rating", 0)))
        except Exception:
            pass
        try:
            for frame in tags.getall("TXXX"):
                if (frame.desc or "").upper() == "RATING" and frame.text:
                    return _scale_to_stars(str(frame.text[0]))
        except Exception:
            pass
    # Vorbis comments and MP4 both behave like a mapping
    for key in ("FMPS_RATING", "fmps_rating", "RATING", "rating",
                "----:com.apple.iTunes:RATING"):
        try:
            val = tags.get(key)
        except Exception:
            val = None
        if val:
            raw = val[0]
            if isinstance(raw, bytes):
                raw = raw.decode(errors="replace")
            got = _scale_to_stars(str(raw))
            if got:
                return got
    return None


def _scale_to_stars(raw: str) -> int:
    """A rating written as 0-1, 0-5, 0-100 or 0-255 — all of which occur — read
    onto 1-5. The scale is inferred from the magnitude, which is the only thing
    available when the tag does not say."""
    raw = (raw or "").strip()
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0
    if v <= 0:
        return 0
    # "1" is ambiguous — one star, or FMPS_RATING 1.0 meaning five. The decimal
    # point is the only thing that distinguishes them, so it is what decides:
    # a fractional value is on the 0.0-1.0 scale, an integer is not.
    fractional = "." in raw
    if fractional and v <= 1.0:       # FMPS_RATING, 0.0-1.0
        return max(1, min(5, round(v * 5)))
    if v <= 5:                        # plain stars
        return int(round(v))
    if v <= 100:                      # RATING, 0-100
        return max(1, min(5, round(v / 20)))
    return popm_to_stars(int(v))      # POPM-ish byte


def write_stars(path: str, stars: int) -> bool:
    """Write the rating into the file. True if the file was changed."""
    try:
        import mutagen
        from mutagen.id3 import ID3, POPM as POPMFrame, TXXX
    except ImportError:                                  # pragma: no cover
        log.warning("mutagen is not available; cannot write ratings")
        return False
    stars = max(1, min(5, int(stars)))
    try:
        f = mutagen.File(path)
        if f is None:
            return False
        if f.tags is None:
            f.add_tags()
        tags = f.tags
        if isinstance(tags, ID3):
            tags.delall("POPM")
            tags.add(POPMFrame(email=POPM_EMAIL, rating=POPM[stars], count=0))
            tags.delall("TXXX:RATING")
            tags.add(TXXX(encoding=3, desc="RATING", text=[str(stars)]))
        elif hasattr(tags, "__setitem__"):
            if path.lower().endswith((".m4a", ".mp4", ".m4b")):
                tags["----:com.apple.iTunes:RATING"] = [str(stars).encode()]
            else:
                tags["RATING"] = [str(stars * 20)]
                tags["FMPS_RATING"] = [f"{stars / 5:.1f}"]
        else:
            return False
        f.save()
        return True
    except Exception as e:
        log.warning("could not write a rating to %s: %s", path, e)
        return False


# --- the two directions ------------------------------------------------------

def sync(cfg: Config, *, write: bool = True, read: bool = True,
         dry_run: bool = False, limit: int = 0) -> dict:
    """Reconcile ratings.json with the library's own tags.

    IN first, then OUT: a star someone set is the more deliberate signal, so it
    is allowed to move the score before the score is written back out.
    """
    store = cfg.ratings()
    tracks = dict(store.get("tracks") or {})
    counts = {"imported": 0, "exported": 0, "unchanged": 0, "failed": 0, "considered": 0}

    for path, entry in list(tracks.items()):
        counts["considered"] += 1
        if limit and counts["considered"] > limit:
            break
        if not Path(path).exists():
            continue
        seen = entry.get("stars_seen")

        if read:
            found = read_stars(path)
            if found and found != seen:
                # somebody set this by hand; move the score to match and keep
                # the thumb/skip history underneath it
                want = score_for_stars(found)
                have = ratings.score(entry)
                entry["thumbs"] = int(entry.get("thumbs", 0)) + max(0, want - have)
                entry["skips"] = int(entry.get("skips", 0)) + max(0, have - want)
                entry["stars_seen"] = found
                entry["stars_from"] = "file"
                counts["imported"] += 1
                log.info("%s: %d stars found in the file -> score %d", Path(path).name, found, want)
                continue

        if write:
            want = ratings.stars(store, path)
            if want is None or want == seen:
                counts["unchanged"] += 1
                continue
            if dry_run:
                log.info("would write %d stars to %s", want, path)
                counts["exported"] += 1
                continue
            if write_stars(path, want):
                entry["stars_seen"] = want
                entry["stars_from"] = "netradio"
                counts["exported"] += 1
            else:
                counts["failed"] += 1

    if not dry_run:
        store["tracks"] = tracks
        cfg.save_ratings(store)
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True, type=Path, help="the runtime config dir")
    ap.add_argument("--no-write", action="store_true", help="import only; never touch a file")
    ap.add_argument("--no-read", action="store_true", help="export only")
    ap.add_argument("--dry-run", action="store_true", help="say what would be written, write nothing")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many tracks (for a first careful pass)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stdout)
    counts = sync(Config(args.config), write=not args.no_write, read=not args.no_read,
                  dry_run=args.dry_run, limit=args.limit)
    log.info("stars: %s", ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0
