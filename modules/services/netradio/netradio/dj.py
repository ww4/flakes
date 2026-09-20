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
import os
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

import datetime as dt

from netradio import schedule as sched
from netradio.config import Config
from netradio.profile import Profile
from netradio.feeds import era_ok
from netradio.rules import Verdict
from netradio.wake import Liquidsoap

log = logging.getLogger("netradio.dj")

LOOKAHEAD = 2           # queued items to keep waiting behind the playing one
NO_REPEAT = 300         # tracks remembered to avoid replaying too soon
POLL = 5.0              # seconds between queue checks
KEEP_BREAKS = 4         # rendered break files kept per station
INBOX_RETRY_S = 60      # a skip/request press is retried this long if Liquidsoap is down, then dropped
EXCURSION = 0.1         # share of base-programme picks from the station's fringe (<mount>-fringe.m3u);
                        # a station's `excursion` setting overrides it


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


def artist_of_path(path: str) -> str:
    """The library folder's artist (/mnt/…/Music/<Artist>/…), normalised the
    way dislikes.json keys artists."""
    parts = path.split("/")
    return parts[4].strip().lower() if len(parts) > 5 else ""


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


# --- the programme: segments and spotlights of one curated station ------------

def breaks_spec(value, default: tuple[int, int] = (3, 4)) -> tuple[int, int]:
    """A station's `breaks_every`: an int N (every N songs), "a-b" (a random
    count in that range), 0 (no breaks). Anything else: the default."""
    if value is None or value == "":
        return default
    try:
        if isinstance(value, str) and "-" in value:
            a, b = (int(x) for x in value.split("-", 1))
            return (min(a, b), max(a, b)) if a > 0 and b > 0 else (0, 0)
        n = int(value)
        return (n, n) if n >= 0 else default
    except (TypeError, ValueError):
        return default


class Programme:
    """What a curated station should be drawing from right now, and what
    the DJ should say about the day. Reads the runtime config (schedule,
    feeds, artists) and the pools the scanner wrote; resolves `auto` slots
    once a day and remembers the pick in picks.json."""

    SPOTLIGHT_WEIGHT = 0.7   # share of tracks by the spotlit artist; the rest similar artists

    def __init__(self, station: dict, cfg: Config | None, pools: Path | None, lastfm_key: str = ""):
        self.station = station
        self.cfg = cfg
        self.pools = pools
        self.lastfm_key = lastfm_key
        self.clock = dt.datetime.now          # injectable for tests
        self._stations_mtime = -1.0

    def refresh_station(self) -> dict | None:
        """The station's current settings from stations.json (edited on the
        admin page); None when unchanged since the last look."""
        if not self.cfg:
            return None
        try:
            mtime = (self.cfg.root / "stations.json").stat().st_mtime
        except OSError:
            return None
        if mtime == self._stations_mtime:
            return None
        self._stations_mtime = mtime
        for s in self.cfg.stations():
            if s.get("mount") == self.station.get("mount"):
                self.station = s
                return s
        return None

    def active(self, now: dt.datetime | None = None) -> dict | None:
        if not self.cfg or self.station.get("kind") == "specialty":
            return None
        now = now or self.clock()
        slot = sched.active_slot(self.station["mount"], self.cfg.schedule(), now)
        if slot is None:
            return None
        picks = self.cfg.picks()
        resolved, changed = sched.resolve(slot, now.date(), picks, station=self.station,
                                          artists=self.cfg.artists(), feeds=self.cfg.feeds())
        if changed:
            self.cfg.save_picks(picks)
            log.info("%s: picked %s for %s", self.station["mount"], resolved.get(resolved["kind"]), slot.get("id"))
        return resolved if resolved.get("kind") in ("feed", "artist") else None

    def upcoming_text(self, rng: random.Random, now: dt.datetime | None = None) -> str:
        if not self.cfg or self.station.get("kind") == "specialty":
            return ""
        now = now or self.clock()
        up = sched.upcoming(self.station["mount"], self.cfg.schedule(), now)
        resolved = []
        picks = self.cfg.picks()
        changed_any = False
        for a, b, s in up:
            r, changed = sched.resolve(s, now.date(), picks, station=self.station,
                                       artists=self.cfg.artists(), feeds=self.cfg.feeds())
            changed_any |= changed
            if r.get("kind") in ("feed", "artist"):
                resolved.append((a, b, r))
        if changed_any:
            self.cfg.save_picks(picks)
        return sched.promo(self.station.get("name", ""), resolved, rng)

    def intro_text(self, slot: dict, rng: random.Random) -> str:
        return sched.intro(self.station.get("name", ""), slot, rng)

    # -- pools
    def _m3u(self, path: Path) -> list[str]:
        try:
            with path.open() as fh:
                return [l.rstrip("\n") for l in fh if l.strip() and not l.startswith("#")]
        except OSError:
            return []

    def pool(self, slot: dict, rng: random.Random) -> list[str]:
        """The tracks a slot draws from. An artist spotlight is mostly the
        artist, with similar artists we own filling the rest."""
        if not self.pools:
            return []
        if slot.get("kind") == "feed":
            return self._m3u(self.pools / "feeds" / f"{slot['feed']}.m3u")
        if slot.get("kind") == "artist":
            artists = self.cfg.artists() if self.cfg else {}
            main = artists.get(slot["artist"], {})
            own = self._m3u(self.pools / "artists" / f"{main.get('slug', '')}.m3u") if main else []
            if rng.random() < self.SPOTLIGHT_WEIGHT or not self.lastfm_key:
                return own
            sims = self.similar(slot["artist"], artists)
            pool = [t for a in sims for t in self._m3u(self.pools / "artists" / f"{artists[a]['slug']}.m3u")]
            return pool or own
        return []

    def similar(self, artist: str, artists: dict) -> list[str]:
        """Last.fm similar artists that exist in the library (cached in
        similar.json; a failed lookup is remembered as empty for the day)."""
        if not self.cfg:
            return []
        cache = self.cfg._read("similar.json", {})
        entry = cache.get(artist)
        today = dt.date.today().isoformat()
        if entry and entry.get("date") == today or (entry and entry.get("names")):
            names = entry["names"]
        else:
            names = lastfm_similar(artist, self.lastfm_key)
            cache[artist] = {"date": today, "names": names}
            try:
                self.cfg._write("similar.json", cache)
            except OSError:
                pass
        from netradio.feeds import norm_artist
        known = {norm_artist(a): a for a in artists}
        out = []
        for n in names:
            k = norm_artist(n)
            for kn, real in known.items():
                if k and (k == kn or k in kn) and real != artist and real not in out:
                    out.append(real)
        return out


def lastfm_similar(artist: str, key: str, limit: int = 30) -> list[str]:
    if not key:
        return []
    import urllib.parse
    import urllib.request
    url = ("https://ws.audioscrobbler.com/2.0/?method=artist.getSimilar&autocorrect=1&format=json"
           f"&limit={limit}&api_key={key}&artist={urllib.parse.quote(artist)}")
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.load(r)
        return [a["name"] for a in data.get("similarartists", {}).get("artist", [])]
    except Exception as e:
        log.warning("Last.fm similar lookup for %r failed: %s", artist, e)
        return []


# --- one station -------------------------------------------------------------

_VERDICTS: dict[tuple[str, float], dict[str, Verdict]] = {}
_VERDICTS_LOCK = threading.Lock()


def shared_verdicts(path: Path, overrides: Path | None, mtime: float) -> dict[str, Verdict]:
    """One verdict table per profile file, shared by every station's DJ.
    Each of 25 stations parsing the 24 MB profile into its own 20k
    Verdicts put the DJ at 2.2 GB RSS (2026-09-19); the table is read-only
    once built, so one copy serves all."""
    key = (str(path), mtime)
    with _VERDICTS_LOCK:
        table = _VERDICTS.get(key)
        if table is None:
            table = Profile.load_verdicts(path, overrides)
            _VERDICTS.clear()          # an older profile's table is not wanted by anyone now
            _VERDICTS[key] = table
        return table


class StationDJ:
    def __init__(self, mount: str, name: str, playlist: Path, out_dir: Path,
                 ls: Liquidsoap, tts: Kokoro, rng: random.Random | None = None,
                 lookahead: int = LOOKAHEAD, breaks_every: tuple[int, int] = (3, 4),
                 voice_gain: str = "1.8", profile: Path | None = None, overrides: Path | None = None,
                 now_dir: Path | None = None, programme: Programme | None = None, inbox: Path | None = None):
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
        self.fringe: list[str] = []         # matched by a later tag only: played a little (EXCURSION)
        self.playlist_mtime = -1.0
        self.fringe_mtime = -1.0
        self.excursion = EXCURSION
        self.recent: deque[str] = deque(maxlen=NO_REPEAT)
        self.since_break: list[Track] = []
        self.until_break = self.rng.randint(*breaks_every)
        self.breaks_made = 0
        self.profile_path = profile
        self.overrides_path = overrides
        self.profile_mtime = -1.0
        self.verdicts: dict[str, Verdict] = {}
        self.planned: Track | None = None   # chosen one ahead, so a track's
                                            # exit can suit what follows it
        self.now_dir = now_dir              # where the radio page reads "next" from
        # skip / request files from the admin API: ONE inbox for all stations,
        # beside the per-station break dirs (out_dir is <dj>/<mount>; the
        # admin writes <dj>/inbox — 2026-09-18 the first skip went unseen)
        self.inbox = inbox if inbox is not None else out_dir.parent / "inbox"
        self.dislikes: dict = {"tracks": {}, "artists": {}}
        self.dislikes_mtime = 0.0
        self.pushed: list[dict] = []        # what was queued, in order, for that
        self.last_break_text = ""
        self.programme = programme          # segments/spotlights (curated stations)
        self.segment: dict | None = None    # the slot the queue is currently drawing from
        self.promo_next = False             # say the day's schedule at the next break

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
        self.load_fringe()
        log.info("%s: playlist loaded, %d tracks, %d fringe", self.mount, len(self.tracks), len(self.fringe))

    def load_fringe(self) -> None:
        fringe = self.playlist.with_name(self.playlist.stem + "-fringe.m3u")
        try:
            mtime = fringe.stat().st_mtime
        except OSError:
            self.fringe = []
            return
        if mtime == self.fringe_mtime:
            return
        with fringe.open() as fh:
            self.fringe = [l.rstrip("\n") for l in fh if l.strip() and not l.startswith("#")]
        self.fringe_mtime = mtime

    def load_profile(self) -> None:
        if not self.profile_path:
            return
        try:
            mtime = self.profile_path.stat().st_mtime
        except OSError:
            return
        if mtime != self.profile_mtime:
            self.verdicts = shared_verdicts(self.profile_path, self.overrides_path, mtime)
            self.profile_mtime = mtime
            log.info("%s: profile loaded, %d talk tracks kept out", self.mount,
                     sum(1 for v in self.verdicts.values() if v.talk))

    def verdict(self, track: Track) -> Verdict:
        return self.verdicts.get(track.path, Verdict(False, False, False, ""))

    def verdict_of(self, path: str) -> Verdict:
        return self.verdicts.get(path, Verdict(False, False, False, ""))

    def pending(self) -> int:
        """How many requests wait in the Liquidsoap queue (RIDs, whitespace-separated)."""
        reply = self.ls.command(f"q_{self.mount}.queue")
        return len(reply.split()) if reply.strip() else 0

    def choose(self) -> Track | None:
        self.load_playlist()
        self.load_profile()
        candidates = self.tracks
        if self.segment is not None and self.programme is not None:
            seg = self.programme.pool(self.segment, self.rng)
            # a segment draws from a feed's or an artist's whole pool; the
            # station's own era rule still applies (no shellac sides in a
            # Classic Country spotlight, 2026-09-17)
            era_rule = (self.programme.station.get("base") or {}).get("era")
            if seg and era_rule:
                seg = [t for t in seg if era_ok(era_rule, self.verdict_of(t).era)]
            if seg:
                candidates = seg
        elif self.fringe and self.rng.random() < self.excursion:
            # the base programme's excursion: a track of a neighbouring
            # genre now and then, never the bulk (Chris, 2026-09-19)
            candidates = self.fringe
        if not candidates:
            return None
        self.load_dislikes()
        never = self.dislikes.get("tracks") or {}
        playable = [t for t in candidates if t not in never and not self.verdicts.get(t, Verdict(False, False, False, "")).talk]
        if not playable:
            return None
        pool = [t for t in playable if t not in self.recent] or playable
        less = self.dislikes.get("artists") or {}
        for _ in range(8):   # an artist marked "less" gets a quarter of the plays it would have had
            path = self.rng.choice(pool)
            if not less or artist_of_path(path) not in less or self.rng.random() < 0.25:
                break
        self.recent.append(path)
        return read_tags(path)

    def load_dislikes(self) -> None:
        cfg = self.programme.cfg if self.programme is not None else None
        if cfg is None:
            return
        try:
            mtime = (cfg.root / "dislikes.json").stat().st_mtime
        except OSError:
            return
        if mtime != self.dislikes_mtime:
            self.dislikes = cfg.dislikes()
            self.dislikes_mtime = mtime
            log.info("%s: dislikes loaded: %d tracks never, %d artists less", self.mount,
                     len(self.dislikes.get("tracks") or {}), len(self.dislikes.get("artists") or {}))

    # -- listener feedback: the admin API drops a JSON file per action into
    # <out>/inbox/<mount>-<n>.json; the fill loop acts on them in order.
    def handle_inbox(self) -> None:
        try:
            files = sorted(self.inbox.glob(f"{self.mount}-*.json"))
        except OSError:
            return
        for f in files:
            try:
                req = json.loads(f.read_text())
                age = time.time() - f.stat().st_mtime
            except (OSError, ValueError):
                f.unlink(missing_ok=True)
                continue
            try:
                if req.get("action") == "skip":
                    reply = self.ls.command(f"src_{self.mount}.skip")
                    if reply.startswith("ERROR"):    # an unknown command is a quiet failure otherwise
                        log.error("%s: skip refused by Liquidsoap: %s", self.mount, reply)
                    else:
                        log.info("%s: skipped on request", self.mount)
                elif req.get("action") == "request" and req.get("path"):
                    self.play_request(req["path"], req.get("who", ""))
            except OSError as e:
                # Liquidsoap's socket is gone (a deploy restarting it): a fresh
                # press waits for the next pass; an old one would surprise.
                if age < INBOX_RETRY_S:
                    log.warning("%s: inbox %s waits, Liquidsoap unreachable (%s)", self.mount, f.name, e)
                    break
                log.warning("%s: inbox %s dropped after %.0fs, Liquidsoap unreachable (%s)", self.mount, f.name, age, e)
            except Exception:
                log.exception("%s: inbox %s failed", self.mount, f.name)
            f.unlink(missing_ok=True)

    def play_request(self, path: str, who: str = "") -> None:
        """Queue a listener's request to play NEXT: the tracks already waiting
        are dropped from Liquidsoap's queue (they were the shuffle's choice,
        nothing is lost) and re-chosen after; a short "by request" line is
        rendered in front of it."""
        track = read_tags(path)
        for rid in self.ls.command(f"q_{self.mount}.queue").split():
            self.ls.command(f"q_{self.mount}.ignore {rid}")
        self.pushed = []
        intro = self.render_break(f"{'By request' if not who else 'By request from ' + who}: {track.artist}, {track.title}.")
        if intro:
            self.push(intro, "request intro", {"kind": "break", "artist": self.name, "title": "By request"})
        self.push(self.track_uri(track, None), f"request {track.artist} - {track.title}",
                  {"kind": "track", "artist": track.artist, "title": track.title, "request": True})
        self.since_break = []
        log.info("%s: request queued next: %s - %s", self.mount, track.artist, track.title)

    def check_settings(self) -> None:
        """Pick up a changed break frequency without a restart."""
        if self.programme is None:
            return
        st = self.programme.refresh_station()
        if st is not None and isinstance(st.get("excursion"), (int, float)) and st["excursion"] != self.excursion:
            self.excursion = max(0.0, min(1.0, float(st["excursion"])))
            log.info("%s: excursion %.0f%% now", self.mount, self.excursion * 100)
        if st is not None and "breaks_every" in st:
            spec = breaks_spec(st.get("breaks_every"), self.breaks_every)
            if spec != self.breaks_every:
                log.info("%s: breaks every %s songs now", self.mount, "never" if spec == (0, 0) else f"{spec[0]}-{spec[1]}")
                self.breaks_every = spec
                self.until_break = self.rng.randint(*spec) if spec != (0, 0) else 10 ** 9

    def check_segment(self) -> None:
        """Notice a segment starting or ending: switch pools, drop the track
        planned under the old one, and queue an intro before the next track."""
        if self.programme is None:
            return
        try:
            now = self.programme.active()
        except Exception:
            log.exception("%s: schedule lookup failed", self.mount)
            return
        key = (now or {}).get("id"), (now or {}).get("kind"), (now or {}).get("feed") or (now or {}).get("artist")
        old = (self.segment or {}).get("id"), (self.segment or {}).get("kind"), (self.segment or {}).get("feed") or (self.segment or {}).get("artist")
        if key == old:
            return
        self.segment = now
        self.planned = None
        if now is not None:
            text = self.programme.intro_text(now, self.rng)
            uri = self.render_break(text)
            if uri:
                self.push(uri, "segment intro", {"kind": "break", "artist": self.name, "title": now.get("name", "Segment")})
            log.info("%s: segment %s starts (%s)", self.mount, now.get("name"), now.get("kind"))
        else:
            log.info("%s: segment over, back to the base", self.mount)
        self.since_break = []
        self.until_break = self.rng.randint(*self.breaks_every) if self.breaks_every != (0, 0) else 10 ** 9

    def next_track(self) -> tuple[Track | None, Track | None]:
        """The track to queue now and the one planned after it."""
        track = self.planned or self.choose()
        self.planned = self.choose() if track else None
        return track, self.planned

    def track_uri(self, track: Track, following: Track | None) -> str:
        """A plain path, unless the profile has something to say: a gain
        that brings the track to the target loudness; a track that ends in
        chatter, or on a hard stop (the bluegrass ending), is not
        crossfaded over — it lands, then the next starts; one that starts
        with chatter is not faded into under the previous song's tail."""
        v = self.verdict(track)
        meta: dict[str, str] = {}
        if v.gain_db is not None and v.gain_db != 0.0:
            meta["liq_amplify"] = f"{v.gain_db:+.1f} dB"   # to the station's target loudness
        if v.tail_talk or v.hard_stop:
            meta.update({"liq_cross_duration": "0.5", "liq_fade_out": "0"})
        if v.head_talk:
            meta["liq_fade_in"] = "0"
        if following is not None and self.verdict(following).head_talk:
            meta.setdefault("liq_cross_duration", "0.5")
        return annotate(meta, track.path) if meta else track.path

    # -- outputs
    def push(self, uri: str, what: str, entry: dict | None = None) -> None:
        reply = self.ls.command(f"q_{self.mount}.push {uri}")
        log.info("%s: queued %s (%s)", self.mount, what, reply.strip() or "ok")
        if entry:
            self.pushed.append(entry)
            self.pushed = self.pushed[-20:]

    def write_next(self, pending: int) -> None:
        """<now_dir>/<mount>-next.json: what is still waiting in the queue (the
        last `pending` things pushed) and the DJ's last break, for the page."""
        if not self.now_dir:
            return
        data = {"next": self.pushed[-pending:] if pending > 0 else [],
                "planned": ({"artist": self.planned.artist, "title": self.planned.title} if self.planned else None),
                "last_break": self.last_break_text,
                "segment": ({"name": self.segment.get("name"), "kind": self.segment.get("kind")} if self.segment else None)}
        try:
            self.now_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.now_dir / f".{self.mount}-next.json.tmp"
            tmp.write_text(json.dumps(data))
            tmp.replace(self.now_dir / f"{self.mount}-next.json")
        except OSError as e:
            log.warning("%s: could not write next.json: %s", self.mount, e)

    def make_break(self, nxt: Track) -> str | None:
        text = compose(self.since_break, nxt, self.name, self.rng)
        # Every other break carries the day's schedule, radio-style.
        if self.programme is not None:
            self.promo_next = not self.promo_next
            if self.promo_next:
                try:
                    promo = self.programme.upcoming_text(self.rng)
                except Exception:
                    log.exception("%s: promo failed", self.mount)
                    promo = ""
                if promo:
                    text = f"{text} {promo}"
        return self.render_break(text)

    def render_break(self, text: str) -> str | None:
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
        self.last_break_text = text
        return annotate({
            "title": "Station break", "artist": self.name, "dj": "true",
            "liq_amplify": self.voice_gain,   # speech renders ~8 dB under the music
            "liq_fade_in": "0", "liq_fade_out": "0", "liq_cross_duration": "0.5",
        }, str(path))

    def fill(self) -> None:
        """Top the queue up to `lookahead`. Each step queues one track, with a
        break in front of it when the count says so."""
        self.check_settings()
        self.check_segment()
        self.handle_inbox()
        n = self.pending()
        while n < self.lookahead:
            track, following = self.next_track()
            if track is None:
                log.warning("%s: playlist empty, nothing to queue", self.mount)
                return
            if self.until_break <= 0 and self.breaks_every != (0, 0):
                uri = self.make_break(track)
                if uri:
                    self.push(uri, "break", {"kind": "break", "artist": self.name, "title": "Station break"})
                    n += 1
                self.since_break = []
                self.until_break = self.rng.randint(*self.breaks_every)
            self.push(self.track_uri(track, following), f"{track.artist} - {track.title}",
                      {"kind": "track", "artist": track.artist, "title": track.title})
            self.since_break.append(track)
            self.until_break -= 1
            n += 1
        self.write_next(n)

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.fill()
            except Exception:
                log.exception("%s: fill failed", self.mount)
            stop.wait(POLL)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True, type=Path, help="the runtime config dir (stations, schedule, feeds)")
    ap.add_argument("--playlists", required=True, type=Path)
    ap.add_argument("--pools", required=True, type=Path, help="feeds/ and artists/ pools from the scanner")
    ap.add_argument("--socket", required=True, type=Path, help="liquidsoap server socket")
    ap.add_argument("--out", required=True, type=Path, help="where rendered breaks go")
    ap.add_argument("--kokoro-url", action="append", required=True, help="tried in order")
    ap.add_argument("--voice", default="af_heart")
    ap.add_argument("--breaks-every", default="3-4", help="tracks between breaks, min-max")
    ap.add_argument("--profile", type=Path, help="profile.json from `netradio profile`")
    ap.add_argument("--overrides", type=Path, help="profile-overrides.json")
    ap.add_argument("--now-dir", type=Path, help="where <mount>-next.json goes, for the radio page")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stdout)
    lo, hi = (int(x) for x in args.breaks_every.split("-"))
    cfg = Config(args.config)
    stations = cfg.stations()
    lastfm_key = os.environ.get("LASTFM_API_KEY", "")
    ls = Liquidsoap(args.socket)
    tts = Kokoro(args.kokoro_url, args.voice)
    stop = threading.Event()
    threads = []
    for s in stations:
        prog = Programme(s, cfg, args.pools, lastfm_key)
        dj = StationDJ(s["mount"], s["name"], args.playlists / f"{s['mount']}.m3u",
                       args.out / s["mount"], ls, tts, breaks_every=breaks_spec(s.get("breaks_every"), (lo, hi)),
                       profile=args.profile, overrides=args.overrides, now_dir=args.now_dir, programme=prog,
                       inbox=args.out / "inbox")
        t = threading.Thread(target=dj.run, args=(stop,), name=s["mount"], daemon=True)
        t.start()
        threads.append(t)
    log.info("DJ on %d stations, a break every %d-%d tracks, voice %s via %s%s",
             len(stations), lo, hi, args.voice, ", ".join(args.kokoro_url),
             "; spotlights with Last.fm similar artists" if lastfm_key else "")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop.set()
    return 0
