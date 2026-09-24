"""`netradio ambient` — fetch the ambient beds (rain) a fixed station loops.

A fixed station's playlist is not built from the library: it points at a
handful of long recordings kept in <state>/ambient/<name>/. This fetches
them from the Internet Archive — freely licensed recordings only, which
also happen to loop better than the short clips a streaming site serves
(longer takes, no re-download when someone's CDN moves) — and writes
playlists/<mount>.m3u.

    netradio ambient --dir /var/lib/netradio/ambient --playlist /var/lib/netradio/playlists/rain.m3u

Idempotent: a file already on disk with the right size is left alone, so
this can run at boot and after every deploy. Add your own recordings by
dropping them in the directory; anything readable there is played.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("netradio.ambient")

AUDIO = (".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav")

# (archive.org item, file, licence) — the rain beds.
#
# Chosen by MEASUREMENT, not by title (2026-09-23). The first set here was a
# studio sound-effects tape: every cut opens with an announcer slating it
# ("number four…"), which is exactly what Chris heard, and the cuts are dark
# and thunder-heavy. These were picked by fingerprinting rainymood.com — the
# bed he actually likes — into octave bands and ranking Creative Commons
# recordings by how close their spectral SHAPE is to it. Its signature is a
# flat 63 Hz–8 kHz response (close, enveloping rain); the ones below sit
# 2.4–4.5 dB RMS from that curve, where the studio tape sat at 14.8.
#
# All are non-commercial licences, which private home playback satisfies;
# attribution is written to LICENCES.txt beside the audio.
RAIN = [
    ("aporee_49306_56200", "soundmap202005212.mp3", "CC BY-NC-ND 3.0 — radio aporee, Chaozhou Township, Pingtung County"),
    ("aporee_42317_48252", "2017629rainnightwindowshuters0021.mp3", "CC BY-NC-ND 3.0 — radio aporee, Unije: rain and window shutters at night"),
    ("aporee_51774_59139", "2012160067ChuvacarroPiso.mp3", "Public Domain Mark 1.0 — radio aporee, Praia do Pisao: heavy rain at the beach"),
    ("aporee_71764_83802", "180223009.mp3", "CC BY-NC 3.0 — radio aporee, Mbarara City, Uganda: heavy rain"),
    ("aporee_35521_40790", "132RainTent15Mar86Knockree4416.mp3", "CC BY-NC-ND 3.0 — radio aporee, Co. Wicklow: rain on a tent"),
]
SETS = {"rain": RAIN}


def fetch(item: str, name: str, dest: Path, timeout: float = 300.0) -> bool:
    """Download one archive.org file unless it is already here and whole."""
    url = f"https://archive.org/download/{item}/{urllib.parse.quote(name)}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30) as r:
            size = int(r.headers.get("Content-Length") or 0)
    except Exception as e:
        log.warning("%s: cannot reach (%s)", name, e)
        return dest.exists()
    if dest.exists() and (not size or dest.stat().st_size == size):
        log.debug("%s: already here", name)
        return True
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r, tmp.open("wb") as fh:
            shutil.copyfileobj(r, fh)
        tmp.replace(dest)
        log.info("%s: %.1f MB", name, dest.stat().st_size / 1e6)
        return True
    except Exception as e:
        tmp.unlink(missing_ok=True)
        log.warning("%s: download failed (%s)", name, e)
        return dest.exists()


def slate_or_gap(path: Path, ffprobe_window: float = 14.0) -> str:
    """'' if the file is a usable bed, else why it is not.

    A sound-effects tape slates each cut — a second or two of an announcer,
    a pause, then the effect. That is audible on a radio station and it
    reached Chris before anything caught it (2026-09-23). The signature is
    cheap to test for: a short burst of audio followed by a real silence,
    near the head of the file. A long silence anywhere in the window is
    also disqualifying — a rain bed should never go quiet.
    """
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-t", str(ffprobe_window), "-i", str(path),
                            "-af", "silencedetect=noise=-45dB:d=0.4,ametadata=mode=print:file=-", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("%s: could not inspect (%s) — using it anyway", path.name, e)
        return ""
    starts = [float(m) for m in re.findall(r"silence_start=([\d.]+)", r.stderr + r.stdout)]
    ends = [float(m) for m in re.findall(r"silence_end=([\d.]+)", r.stderr + r.stdout)]
    if starts and starts[0] < 4.0 and ends:
        return f"looks slated: audio stops at {starts[0]:.1f}s, resumes at {ends[0]:.1f}s"
    for a, b in zip(starts, ends):
        if b - a > 3.0:
            return f"goes silent for {b - a:.0f}s at {a:.0f}s"
    return ""


def playable(d: Path) -> list[str]:
    """Everything audible in the directory, sorted — Chris's own recordings
    dropped in here play beside the fetched ones."""
    try:
        return sorted(str(p) for p in d.iterdir() if p.suffix.lower() in AUDIO and p.is_file())
    except OSError:
        return []


def build(name: str, dir_: Path, playlist: Path) -> int:
    d = dir_ / name
    d.mkdir(parents=True, exist_ok=True)
    manifest = d / ".fetched.json"
    try:
        was = set(json.loads(manifest.read_text()))
    except (OSError, ValueError):
        was = set()
    wanted = {fname for _, fname, _ in SETS.get(name, [])}
    # a bed this tool fetched that is no longer wanted goes; anything dropped
    # in by hand is never touched (that is how you add your own recordings)
    for stale in was - wanted:
        if (d / stale).exists():
            (d / stale).unlink()
            log.info("%s: dropped (no longer in the set)", stale)
    for item, fname, licence in SETS.get(name, []):
        fetch(item, fname, d / fname)
    manifest.write_text(json.dumps(sorted(wanted)))
    (d / "LICENCES.txt").write_text(
        "Ambient beds fetched by `netradio ambient`. Freely licensed recordings only.\n\n" +
        "".join(f"{fname}\n    {licence}\n    https://archive.org/details/{item}\n\n" for item, fname, licence in SETS.get(name, [])))
    files, rejected = [], []
    for f in playable(d):
        why = slate_or_gap(Path(f))
        (rejected if why else files).append((f, why) if why else f)
        if why:
            log.warning("%s: NOT a usable bed — %s", Path(f).name, why)
    playlist.parent.mkdir(parents=True, exist_ok=True)
    tmp = playlist.with_suffix(".m3u.tmp")
    tmp.write_text("#EXTM3U\n" + "".join(f + "\n" for f in files))
    tmp.replace(playlist)
    log.info("%s: %d file(s) → %s%s", name, len(files), playlist,
             f" ({len(rejected)} rejected)" if rejected else "")
    return len(files)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", required=True, type=Path, help="where the beds live (<state>/ambient)")
    ap.add_argument("--playlist", required=True, type=Path, help="the m3u to write (playlists/<mount>.m3u)")
    ap.add_argument("--set", default="rain", choices=sorted(SETS))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    n = build(args.set, args.dir, args.playlist)
    if not n:
        print("no audio in the ambient directory — nothing to play", file=sys.stderr)
        return 1
    return 0
