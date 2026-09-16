"""Build one .m3u per station from the library's genre tags.

The library is Artist/Album/track with no genre folders, so the only thing
that says what a track *is* is its tag. Each station in the catalogue names
the genre words it collects; a track lands on every station whose words
match its tag (a "Folk Rock" tag goes to both Folk and Rock), and the `all`
station gets everything except the globally excluded kinds.

Tag reads are cached by (size, mtime) so the nightly rescan of ~18k files is
a stat() per file, not a read. Playlists are written atomically — Liquidsoap
watches them with inotify and reloads on change, so it must never see a
half-written one.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("netradio.playlists")

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".wma", ".aif", ".aiff"}

# Kinds of audio that live in the library but are not music you put on.
# Matched the same way station words are (whole word within the tag).
EXCLUDE_WORDS = ("instructional", "audiobook", "spoken", "podcast", "lesson")


@dataclass
class Station:
    mount: str
    name: str
    # None = everything not excluded (the `all` station)
    words: list[str] | None = None
    # None = any era; else the profiler's era buckets this station plays.
    # A track not yet profiled for era stays on a GENRE station that also
    # filters by era (the station keeps its music before the first pass) but
    # off a station DEFINED by era (words None): that one fills as the
    # profile does, rather than being "Everything" for a day.
    eras: list[str] | None = None
    paths: list[str] = field(default_factory=list)


def load_stations(path: Path) -> list[Station]:
    """The catalogue is authored in Nix and handed over as JSON — one source
    of truth for the scanner, the Liquidsoap script and the receiver menu."""
    raw = json.loads(path.read_text())
    stations = []
    for s in raw:
        words = s.get("genres")
        stations.append(Station(
            mount=s["mount"],
            name=s["name"],
            words=[w.lower() for w in words] if words is not None else None,
            eras=s.get("era"),
        ))
    return stations


def split_genre(tag: str) -> list[str]:
    """A genre field can hold several genres — 'Folk/Rock', 'Blues; Soul' —
    and the same genre several ways ('Old-Time', 'Oldtime', 'Old Time').
    Lower-case, split on the separators, and drop the punctuation so the
    station words can be plain."""
    parts = re.split(r"[/;,|]+", tag.lower())
    out = []
    for p in parts:
        p = re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", p)).strip()
        if p:
            out.append(p)
    return out


def word_in(word: str, genres: list[str]) -> bool:
    pat = re.compile(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])")
    return any(pat.search(g) for g in genres)


def read_genre(path: Path) -> str:
    """The raw genre tag, '' when there is none or the file cannot be read."""
    import mutagen  # imported here so `--help` and the tests don't need it

    try:
        f = mutagen.File(path, easy=True)
    except Exception as e:  # mutagen raises a zoo of format-specific errors
        log.debug("unreadable tags: %s (%s)", path, e)
        return ""
    if f is None or not f.tags:
        return ""
    genre = f.tags.get("genre") or []
    return " / ".join(str(g) for g in genre if g)


class TagCache:
    """{path: [size, mtime_ns, genre]} — rewritten whole at the end of a run."""

    def __init__(self, path: Path):
        self.path = path
        self.hits = 0
        self.misses = 0
        try:
            self.data: dict[str, list] = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}
        self.seen: dict[str, list] = {}

    def genre(self, path: Path, st: os.stat_result) -> str:
        key = str(path)
        cached = self.data.get(key)
        if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
            self.hits += 1
            genre = cached[2]
        else:
            self.misses += 1
            genre = read_genre(path)
        self.seen[key] = [st.st_size, st.st_mtime_ns, genre]
        return genre

    def save(self) -> None:
        # Only files seen this run survive, so deletions don't accumulate.
        write_atomic(self.path, json.dumps(self.seen, separators=(",", ":")))


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def scan(roots: list[Path], stations: list[Station], cache: TagCache,
         talk: set[str] | None = None, eras: dict[str, str] | None = None) -> dict:
    """Fill each station's paths. Returns the counts worth logging. `talk` is
    the set of paths the profiler called talk (netradio profile); they are
    kept off every station. `eras` is {path: era} for the era-filtered ones."""
    counts = {"files": 0, "untagged": 0, "excluded": 0, "talk": 0, "unreadable_dirs": 0}
    talk = talk or set()
    eras = eras or {}
    for root in roots:
        if not root.is_dir():
            log.warning("library root missing or unreadable: %s", root)
            counts["unreadable_dirs"] += 1
            continue
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: _walk_error(e, counts)):
            dirnames.sort()
            for name in sorted(filenames):
                p = Path(dirpath) / name
                if p.suffix.lower() not in AUDIO_EXTENSIONS:
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                counts["files"] += 1
                genres = split_genre(cache.genre(p, st))
                if not genres:
                    counts["untagged"] += 1
                if any(word_in(w, genres) for w in EXCLUDE_WORDS):
                    counts["excluded"] += 1
                    continue
                if str(p) in talk:
                    counts["talk"] += 1
                    continue
                era = eras.get(str(p), "")
                for s in stations:
                    if s.eras and (era not in s.eras if era else s.words is None):
                        continue
                    if s.words is None or any(word_in(w, genres) for w in s.words):
                        s.paths.append(str(p))
    return counts


def _walk_error(err: OSError, counts: dict) -> None:
    # A 0700 directory owned by another user (Jellyfin's metadata folders are
    # like this) is skipped, counted, and named once in the log.
    counts["unreadable_dirs"] += 1
    log.info("skipping unreadable directory: %s", err.filename)


def write_playlists(stations: list[Station], out_dir: Path) -> None:
    for s in stations:
        body = "#EXTM3U\n" + "".join(p + "\n" for p in s.paths)
        write_atomic(out_dir / f"{s.mount}.m3u", body)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stations", required=True, type=Path, help="stations.json from the module")
    ap.add_argument("--root", action="append", required=True, type=Path, help="library root (repeatable)")
    ap.add_argument("--out", required=True, type=Path, help="playlist directory")
    ap.add_argument("--cache", required=True, type=Path, help="tag cache file")
    ap.add_argument("--profile", type=Path, help="profile.json from `netradio profile`")
    ap.add_argument("--overrides", type=Path, help="profile-overrides.json")
    ap.add_argument("--summary", type=Path, help="write {mount: {tracks: N}} here (the radio page reads it)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stdout)

    stations = load_stations(args.stations)
    cache = TagCache(args.cache)
    talk: set[str] = set()
    eras: dict[str, str] = {}
    if args.profile:
        from netradio.profile import Profile
        verdicts = Profile.load_verdicts(args.profile, args.overrides)
        talk = {p for p, v in verdicts.items() if v.talk}
        eras = {p: v.era for p, v in verdicts.items() if v.era}
    counts = scan(args.root, stations, cache, talk, eras)
    cache.save()
    write_playlists(stations, args.out)

    for s in stations:
        log.info("%-12s %6d tracks  (%s)", s.mount, len(s.paths), s.name)
    if args.summary:
        write_atomic(args.summary, json.dumps({s.mount: {"tracks": len(s.paths)} for s in stations}))
    log.info("%d audio files, %d untagged, %d excluded, %d talk (profiled), %d unreadable dirs; tag cache %d hits / %d reads",
             counts["files"], counts["untagged"], counts["excluded"], counts["talk"], counts["unreadable_dirs"],
             cache.hits, cache.misses)
    empty = [s.mount for s in stations if not s.paths]
    if empty:
        log.warning("empty stations (their mount will refuse to start): %s", ", ".join(empty))
    if counts["files"] == 0:
        log.error("no audio files found under %s — is the library mounted?", args.root)
        return 1
    return 0
