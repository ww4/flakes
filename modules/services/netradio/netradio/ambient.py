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
import shutil
import sys
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("netradio.ambient")

AUDIO = (".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav")

# (archive.org item, file, licence) — CC0 or CC-BY(-SA); the licence is
# written beside the audio so its provenance stays with it.
RAIN = [
    ("GOLD_TAPE_46_Thunderstorm_Rain", "G46-03-Long Thunder Storm.flac", "CC0 1.0 (USC Cinema / Sunset Editorial collection)"),
    ("GOLD_TAPE_46_Thunderstorm_Rain", "G46-12-Thunderclap Fox.flac", "CC0 1.0 (USC Cinema / Sunset Editorial collection)"),
    ("GOLD_TAPE_46_Thunderstorm_Rain", "G46-04-Distant Storm.flac", "CC0 1.0 (USC Cinema / Sunset Editorial collection)"),
    ("GOLD_TAPE_46_Thunderstorm_Rain", "G46-01-Light Rain and Natural Thunder.flac", "CC0 1.0 (USC Cinema / Sunset Editorial collection)"),
    ("GOLD_TAPE_46_Thunderstorm_Rain", "G46-09-Steady Rain and Thunder.flac", "CC0 1.0 (USC Cinema / Sunset Editorial collection)"),
    ("StormFrom30To45", "Storm from 30 to 45.flac", "CC BY-SA 3.0 — freetousesounds.com"),
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
    for item, fname, licence in SETS.get(name, []):
        fetch(item, fname, d / fname)
    (d / "LICENCES.txt").write_text(
        "Ambient beds fetched by `netradio ambient`. Freely licensed recordings only.\n\n" +
        "".join(f"{fname}\n    {licence}\n    https://archive.org/details/{item}\n\n" for item, fname, licence in SETS.get(name, [])))
    files = playable(d)
    playlist.parent.mkdir(parents=True, exist_ok=True)
    tmp = playlist.with_suffix(".m3u.tmp")
    tmp.write_text("#EXTM3U\n" + "".join(f + "\n" for f in files))
    tmp.replace(playlist)
    log.info("%s: %d file(s) → %s", name, len(files), playlist)
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
