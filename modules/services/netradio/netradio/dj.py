"""The DJ: pick the tracks, and every few of them say what just played and
what is next.

Liquidsoap on its own does not know what comes next, so the DJ owns the
sequencing. Per station it keeps a Liquidsoap request queue two items ahead;
because it chose the tracks it knows the previous few and the next one
exactly. Every 3-4 tracks it writes a break — a few sentences from templates
with the wording varied so it doesn't repeat — renders it with Kokoro while
the current song plays, and queues it between tracks. If the DJ is down the
station's plain shuffle playlist takes over (a fallback in the Liquidsoap
script), so the worst case is today's behaviour.

Queues are kept filled even while a station's encoder is off: nothing plays
until someone tunes in, the two waiting tracks then play first, and their
announcement is generated only when it is about to be queued — so it is
never stale.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from netradio.wake import Liquidsoap

log = logging.getLogger("netradio.dj")

LOOKAHEAD = 2           # queued items to keep waiting behind the playing one
NO_REPEAT = 300         # tracks remembered to avoid replaying too soon
POLL = 5.0              # seconds between queue checks
KEEP_BREAKS = 4         # rendered break files kept per station


@dataclass
class Track:
    path: str
    title: str
    artist: str


# --- what the DJ says -------------------------------------------------------

OPENERS_ONE = [
    "That was {t1}.",
    "You just heard {t1}.",
    "{t1}, there.",
    "That one was {t1}.",
]
OPENERS_MANY = [
    "That was {t1}, and before it {rest}.",
    "You just heard {t1}; before that, {rest}.",
    "{t1} to finish that set, after {rest}.",
    "That set: {rest}, then {t1}.",
    "Just now, {t1} — and earlier {rest}.",
]
NEXTS = [
    "Coming up, {n}.",
    "Up next, {n}.",
    "Next, {n}.",
    "Here's {n}.",
    "Now, {n}.",
    "Staying with it: {n}.",
]
STATION = [
    "This is {s}.",
    "You're listening to {s}.",
    "{s}, from the library.",
    "",
    "",
]
FORMS = [
    "{title} by {artist}",
    "{artist} with {title}",
    "{title} from {artist}",
    "{artist}, {title}",
]


def say_track(t: Track, rng: random.Random) -> str:
    return rng.choice(FORMS).format(title=t.title, artist=t.artist)


def join_list(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def compose(previous: list[Track], nxt: Track, station: str, rng: random.Random) -> str:
    """The break text. previous = oldest first; the most recent is what just
    finished playing when this is heard."""
    parts = []
    if previous:
        recent = previous[-1]
        earlier = list(reversed(previous[:-1]))[:3]  # newest first, at most three
        if earlier:
            parts.append(rng.choice(OPENERS_MANY).format(
                t1=say_track(recent, rng),
                rest=join_list([say_track(t, rng) for t in earlier])))
        else:
            parts.append(rng.choice(OPENERS_ONE).format(t1=say_track(recent, rng)))
    tail = [rng.choice(NEXTS).format(n=say_track(nxt, rng))]
    station_line = rng.choice(STATION).format(s=station)
    if station_line:
        tail.insert(rng.randrange(2), station_line)
    parts.extend(tail)
    return " ".join(parts)


# --- the pieces it talks to --------------------------------------------------

class Kokoro:
    """OpenAI-compatible /v1/audio/speech. Tries the URLs in order (wallace
    first, gromit's own container as the fallback — same list the switchboard
    uses)."""

    def __init__(self, urls: list[str], voice: str, timeout: float = 90.0):
        self.urls = urls
        self.voice = voice
        self.timeout = timeout

    def render(self, text: str) -> bytes:
        body = json.dumps({"model": "kokoro", "voice": self.voice, "input": text,
                           "response_format": "wav"}).encode()
        last: Exception | None = None
        for url in self.urls:
            req = urllib.request.Request(f"{url.rstrip('/')}/v1/audio/speech", data=body,
                                         headers={"Content-Type": "application/json"})
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = r.read()
                log.debug("kokoro %s rendered %d bytes in %.1fs", url, len(data), time.monotonic() - t0)
                return data
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                last = e
                log.warning("kokoro %s failed: %s", url, e)
        raise RuntimeError(f"no Kokoro answered: {last}")


def read_tags(path: str) -> Track:
    """Title/artist from the tags; the filename and folder names when a tag
    is missing (Artist/Album/NN Title.ext is how the library is laid out)."""
    title = artist = ""
    try:
        import mutagen
        f = mutagen.File(path, easy=True)
        if f is not None and f.tags:
            title = " ".join(str(x) for x in (f.tags.get("title") or []) if x)
            artist = " ".join(str(x) for x in (f.tags.get("artist") or []) if x)
    except Exception:
        pass
    p = Path(path)
    if not title:
        title = re.sub(r"^\s*\d+[\s._-]*", "", p.stem).strip() or p.stem
    if not artist:
        artist = p.parent.parent.name if p.parent.parent.name not in ("", "/") else "an unknown artist"
    return Track(path, clean(title), clean(artist))


def clean(s: str) -> str:
    """Tag text as speech: drop bracketed asides and stray punctuation Kokoro
    would read out or trip on."""
    s = re.sub(r"[\[\(][^\]\)]*[\]\)]", "", s)
    s = s.replace('"', "").replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip(" -–—,.;:")
    return s or "untitled"


def annotate(meta: dict[str, str], path: str) -> str:
    """Liquidsoap's annotate: URI. Values are double-quoted; quotes and
    backslashes inside are escaped."""
    def q(v: str) -> str:
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return "annotate:" + ",".join(f"{k}={q(v)}" for k, v in meta.items()) + ":" + path


# --- one station -------------------------------------------------------------

class StationDJ:
    def __init__(self, mount: str, name: str, playlist: Path, out_dir: Path,
                 ls: Liquidsoap, tts: Kokoro, rng: random.Random | None = None,
                 lookahead: int = LOOKAHEAD, breaks_every: tuple[int, int] = (3, 4),
                 voice_gain: str = "1.8"):
        self.mount = mount
        self.name = name
        self.playlist = playlist
        self.out_dir = out_dir
        self.ls = ls
        self.tts = tts
        self.rng = rng or random.Random()
        self.lookahead = lookahead
        self.breaks_every = breaks_every
        self.voice_gain = voice_gain
        self.tracks: list[str] = []
        self.playlist_mtime = -1.0
        self.recent: deque[str] = deque(maxlen=NO_REPEAT)
        self.since_break: list[Track] = []
        self.until_break = self.rng.randint(*breaks_every)
        self.breaks_made = 0
        self.queued: list[str] = []  # descriptions, for the log

    # -- inputs
    def load_playlist(self) -> None:
        try:
            mtime = self.playlist.stat().st_mtime
        except OSError:
            self.tracks = []
            return
        if mtime == self.playlist_mtime:
            return
        with self.playlist.open() as fh:
            self.tracks = [l.rstrip("\n") for l in fh if l.strip() and not l.startswith("#")]
        self.playlist_mtime = mtime
        log.info("%s: playlist loaded, %d tracks", self.mount, len(self.tracks))

    def pending(self) -> int:
        """How many requests wait in the Liquidsoap queue (RIDs, whitespace-separated)."""
        reply = self.ls.command(f"q_{self.mount}.queue")
        return len(reply.split()) if reply.strip() else 0

    def choose(self) -> Track | None:
        self.load_playlist()
        if not self.tracks:
            return None
        pool = [t for t in self.tracks if t not in self.recent] or self.tracks
        path = self.rng.choice(pool)
        self.recent.append(path)
        return read_tags(path)

    # -- outputs
    def push(self, uri: str, what: str) -> None:
        reply = self.ls.command(f"q_{self.mount}.push {uri}")
        log.info("%s: queued %s (%s)", self.mount, what, reply.strip() or "ok")

    def make_break(self, nxt: Track) -> str | None:
        text = compose(self.since_break, nxt, self.name, self.rng)
        try:
            wav = self.tts.render(text)
        except Exception as e:
            log.error("%s: break not rendered, skipping it: %s", self.mount, e)
            return None
        self.breaks_made += 1
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"break-{self.breaks_made:06d}.wav"
        path.write_bytes(wav)
        for old in sorted(self.out_dir.glob("break-*.wav"))[:-KEEP_BREAKS]:
            old.unlink(missing_ok=True)
        log.info("%s: break: %s", self.mount, text)
        return annotate({
            "title": "Station break", "artist": self.name, "dj": "true",
            "liq_amplify": self.voice_gain,   # speech renders ~8 dB under the music
            "liq_fade_in": "0", "liq_fade_out": "0", "liq_cross_duration": "0.5",
        }, str(path))

    def fill(self) -> None:
        """Top the queue up to `lookahead`. Each step queues one track, with a
        break in front of it when the count says so."""
        n = self.pending()
        while n < self.lookahead:
            track = self.choose()
            if track is None:
                log.warning("%s: playlist empty, nothing to queue", self.mount)
                return
            if self.until_break <= 0:
                uri = self.make_break(track)
                if uri:
                    self.push(uri, "break")
                self.since_break = []
                self.until_break = self.rng.randint(*self.breaks_every)
            self.push(track.path, f"{track.artist} - {track.title}")
            self.since_break.append(track)
            self.until_break -= 1
            n += 1

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.fill()
            except Exception:
                log.exception("%s: fill failed", self.mount)
            stop.wait(POLL)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stations", required=True, type=Path)
    ap.add_argument("--playlists", required=True, type=Path)
    ap.add_argument("--socket", required=True, type=Path, help="liquidsoap server socket")
    ap.add_argument("--out", required=True, type=Path, help="where rendered breaks go")
    ap.add_argument("--kokoro-url", action="append", required=True, help="tried in order")
    ap.add_argument("--voice", default="af_heart")
    ap.add_argument("--breaks-every", default="3-4", help="tracks between breaks, min-max")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stdout)
    lo, hi = (int(x) for x in args.breaks_every.split("-"))
    stations = json.loads(args.stations.read_text())
    ls = Liquidsoap(args.socket)
    tts = Kokoro(args.kokoro_url, args.voice)
    stop = threading.Event()
    threads = []
    for s in stations:
        dj = StationDJ(s["mount"], s["name"], args.playlists / f"{s['mount']}.m3u",
                       args.out / s["mount"], ls, tts, breaks_every=(lo, hi))
        t = threading.Thread(target=dj.run, args=(stop,), name=s["mount"], daemon=True)
        t.start()
        threads.append(t)
    log.info("DJ on %d stations, a break every %d-%d tracks, voice %s via %s",
             len(stations), lo, hi, args.voice, ", ".join(args.kokoro_url))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop.set()
    return 0
