#!/usr/bin/env python3
"""media-gate — check that what the download client finished is what it claims.

Written 2026-09-27 after The Ark S03E07/E08: two torrents grabbed from a public
indexer three days before the episodes aired, whose entire payload was a single
Windows .exe. Sonarr refused to import them and then quietly wedged — but
nothing on the box ever said "the thing you are seeding is not a TV episode",
and qBittorrent uploaded 12.7 GB of it to strangers over three weeks.

Two tiers, separated because they cost very different amounts:

  1. EXTENSION CHECK — free. The file list comes from qBittorrent's API, so this
     touches no disk and runs over every torrent on every pass.

  2. CONTENT CHECK — cheap, but not free. ffprobe actually parses the container,
     so a renamed executable, a zero-padded decoy or a truncated rip fails even
     when the extension is honest. This is the half that catches a payload
     nobody has written a signature for yet: it is an allowlist ("does this
     parse as media?"), not a blocklist, so novelty does not help the attacker.
     Verdicts are cached by infohash — probing 450 torrents every half hour
     would spin up every drive in the pool to learn nothing new.

This tool REPORTS. It does not move, delete or pause anything. Pulling files out
from under a seeding torrent is how a library fills up with `missingFiles`, and
what to do about a bad grab is a decision with a person attached.

Exit status: 0 if it ran, 1 on a usage/connection error. Findings are not an
error — they go to the state file, stdout, and (for new ones) ntfy.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

STATE_VERSION = 1

# Anything here is never part of a legitimate media grab. Hits are reported
# regardless of category — an executable in a "manual" torrent is still an
# executable we are seeding.
#
# Split by whether CONTENT can corroborate the extension. A .exe either has a
# PE header or it does not; a .bat is just text, so nothing about its bytes
# distinguishes a malicious one from a harmless one.
BINARY_EXEC_EXT = {
    "exe", "scr", "com", "cpl", "msi", "pif", "dll",
    "jar", "apk", "app", "run", "elf",
}
SCRIPT_EXEC_EXT = {
    "bat", "cmd", "hta", "lnk", "vbs", "vbe", "js", "jse",
    "wsf", "wsh", "ps1", "psm1", "reg", "inf",
}
DANGEROUS_EXT = BINARY_EXEC_EXT | SCRIPT_EXEC_EXT

# Leading bytes of the things a BINARY_EXEC_EXT file would have to be to
# deserve the name.
EXEC_MAGIC = (
    b"MZ",                # DOS/PE  — .exe .scr .com .cpl .dll .pif
    b"\x7fELF",           # ELF     — Linux
    b"PK\x03\x04",        # ZIP     — .jar .apk are zip containers
    b"\xd0\xcf\x11\xe0",  # OLE     — .msi
    b"#!",                # shebang — .run and friends
    b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",   # Mach-O
    b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",                          # Mach-O fat / Java class
)


def looks_executable(path: Path) -> tuple[bool, str]:
    """Do this file's first bytes back up its extension?

    The tool exists because an extension is a claim, not a fact. Trusting a
    DANGEROUS_EXT to mean "dangerous" is the same mistake in the other
    direction, and it is not hypothetical: the first two things this ever
    flagged in anger were both `RARBG_DO_NOT_MIRROR.exe` — 99 bytes of ASCII
    reading "This is not an .exe file", shipped inside old RARBG releases to
    discourage mirroring. Two alerts, two non-events, on the channel reserved
    for real ones.

    Checking the bytes is better than special-casing that filename, which
    would be a blocklist entry an attacker can simply not match.
    """
    try:
        with path.open("rb") as f:
            head = f.read(8)
    except OSError as e:
        # Unreadable is not exonerating — keep treating it as the extension says.
        return True, f"unreadable ({e.__class__.__name__})"
    if not head:
        return False, "empty file"
    if any(head.startswith(m) for m in EXEC_MAGIC):
        return True, ""
    printable = sum(32 <= b < 127 or b in (9, 10, 13) for b in head)
    kind = "plain text" if printable == len(head) else "unrecognised data"
    return False, f"{kind}, no executable header"

VIDEO_EXT = {
    "mkv", "mp4", "m4v", "avi", "mpg", "mpeg", "wmv", "mov", "ts", "m2ts",
    "flv", "webm", "divx", "vob", "ogv", "rmvb", "asf", "mts",
}
AUDIO_EXT = {
    "mp3", "flac", "m4a", "m4b", "ogg", "oga", "opus", "wav", "aac", "wma",
    "ape", "alac", "aiff", "aif", "dsf", "wv", "mka",
}
BOOK_EXT = {"epub", "mobi", "azw", "azw3", "pdf", "cbz", "cbr", "djvu", "fb2"}

# Allowed alongside media without being media themselves.
AUX_EXT = {
    "srt", "sub", "idx", "ass", "ssa", "vtt", "smi", "sup",
    "nfo", "txt", "md", "sfv", "par2", "cue", "log", "m3u", "m3u8",
    "jpg", "jpeg", "png", "gif", "bmp", "webp", "tbn", "url", "diz",
    # Scene and ripper metadata. Every one of these turned up on the first
    # real pass over the pool; none of them is interesting.
    "srr", "md5", "sha1", "accurip", "ffp", "toc", "torrent", "lrc",
}

# Optical-disc structure. A full Blu-ray or DVD rip legitimately ships Java
# archives (BDMV/JAR/*.jar) and a pile of binary index files — flagging those
# as "an executable we are seeding" is noise, and whitelisting `.jar`
# everywhere to silence it would be the wrong trade. Scope the exemption to the
# disc layout instead, so a loose .jar in a TV torrent still flags.
DISC_EXT = {"bdmv", "clpi", "mpls", "bdjo", "xml", "crt", "jar",
            "ifo", "bup", "dat", "otf", "ttf"}
DISC_MARKERS = ("/bdmv/", "/video_ts/", "/audio_ts/", "/certificate/")


def in_disc_structure(name: str) -> bool:
    low = "/" + name.lower()
    return any(m in low for m in DISC_MARKERS)
# unpackerr's job; present on purpose, so noted rather than flagged.
ARCHIVE_EXT = {"rar", "zip", "7z", "tar", "gz", "bz2", "xz", "iso"}

MEDIA_EXT = VIDEO_EXT | AUDIO_EXT | BOOK_EXT

# A video shorter than this is a sample or a stub, not the episode. Applied to
# video only: a legitimate music track is routinely under a minute.
MIN_VIDEO_SECONDS = 120.0

# Scene convention: the throwaway preview clip. A RAR'd season pack often has
# nothing loose on disk EXCEPT these, so probing one and calling the release
# invalid gets the answer exactly backwards.
SAMPLE_MARKERS = ("sample", "-s.mkv", ".s.mkv", "/proof/")


def is_sample(name: str) -> bool:
    return any(m in name.lower() for m in SAMPLE_MARKERS)


def ext_of(name: str) -> str:
    """Lowercase extension without the dot. Handles rar volume parts (.r00)."""
    base = name.rsplit("/", 1)[-1]
    if "." not in base:
        return ""
    e = base.rsplit(".", 1)[1].lower()
    if len(e) == 3 and e[0] == "r" and e[1:].isdigit():
        return "rar"
    if len(e) == 3 and e[0] == "z" and e[1:].isdigit():
        return "zip"
    return e


def ascii_header(s: str) -> str:
    """ntfy sends titles as HTTP headers, which are latin-1 at best.

    A single em dash in a title has previously killed an entire notification
    run rather than just the one alert, so headers are flattened to ASCII here
    and nowhere else is allowed to build them.
    """
    s = unicodedata.normalize("NFKD", s)
    return s.encode("ascii", "ignore").decode("ascii") or "media-gate"


class QB:
    def __init__(self, base: str, timeout: int = 30) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout

    def get(self, path: str, **params: Any) -> Any:
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))


def probe(path: Path, timeout: int = 90) -> tuple[bool, str]:
    """True if this parses as real media. Second element is the reason if not."""
    if not path.exists():
        return False, "file missing on disk"
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", "--", str(path)],
            capture_output=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"ffprobe timed out after {timeout}s"
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace").strip().splitlines()
        msg = err[-1] if err else "ffprobe failed"
        # ffprobe prefixes its complaint with the full path, which is already
        # in the finding. Keep the complaint.
        if msg.startswith(str(path) + ":"):
            msg = msg[len(str(path)) + 1:].strip()
        return False, msg[:200]
    try:
        info = json.loads(p.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return False, "ffprobe emitted unparseable JSON"

    streams = info.get("streams") or []
    kinds = {s.get("codec_type") for s in streams}
    if not (kinds & {"video", "audio"}):
        return False, "no video or audio stream"

    # A cover-art JPEG inside an audio file registers as a video stream, so
    # "has a video stream" alone does not make this a video file.
    real_video = [
        s for s in streams
        if s.get("codec_type") == "video"
        and s.get("disposition", {}).get("attached_pic", 0) != 1
    ]
    if real_video and ext_of(path.name) in VIDEO_EXT:
        try:
            dur = float(info.get("format", {}).get("duration", 0.0))
        except (TypeError, ValueError):
            dur = 0.0
        if 0.0 < dur < MIN_VIDEO_SECONDS:
            return False, f"video is only {dur:.0f}s (sample or stub)"
    return True, ""


def translate(container_path: str, path_map: list[tuple[str, str]]) -> Path:
    for src, dst in path_map:
        if container_path == src or container_path.startswith(src.rstrip("/") + "/"):
            return Path(dst) / container_path[len(src.rstrip("/")):].lstrip("/")
    return Path(container_path)


def load_state(p: Path) -> dict[str, Any]:
    try:
        s = json.loads(p.read_text())
        if s.get("version") == STATE_VERSION:
            return s
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": STATE_VERSION, "probed": {}, "reported": {}}


def notify(url: str, topic: str, title: str, body: str, priority: str) -> None:
    req = urllib.request.Request(
        f"{url.rstrip('/')}/{topic}",
        data=body.encode("utf-8"),
        headers={
            "Title": ascii_header(title),
            "Priority": priority,
            "Tags": "warning",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=15).close()
    except (urllib.error.URLError, OSError) as e:
        print(f"warning: ntfy post failed: {e}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--qb-url", default=os.environ.get("MEDIA_GATE_QB_URL", "http://127.0.0.1:8085"))
    ap.add_argument("--state", type=Path,
                    default=Path(os.environ.get("MEDIA_GATE_STATE", "/var/lib/media-gate/state.json")))
    # Lists arrive as repeated argv, never as a single environment variable: a
    # space-separated list in systemd Environment= silently truncates to its
    # first element and the unit still exits 0.
    ap.add_argument("--path-map", action="append", default=[], metavar="CONTAINER=HOST",
                    help="rewrite a download-client path onto the host filesystem")
    ap.add_argument("--managed-category", action="append", default=[], metavar="NAME",
                    help="category whose contents must actually be media (repeatable)")
    ap.add_argument("--ntfy-url", default=os.environ.get("MEDIA_GATE_NTFY_URL", ""))
    ap.add_argument("--ntfy-topic", default=os.environ.get("MEDIA_GATE_NTFY_TOPIC", ""))
    ap.add_argument("--max-probes", type=int, default=60,
                    help="cap ffprobe calls per run so a first pass cannot stall the timer")
    ap.add_argument("--all", action="store_true", help="re-probe even cached torrents")
    ap.add_argument("--dry-run", action="store_true", help="report but do not notify or persist")
    args = ap.parse_args()

    path_map: list[tuple[str, str]] = []
    for m in args.path_map:
        if "=" not in m:
            print(f"error: --path-map wants CONTAINER=HOST, got {m!r}", file=sys.stderr)
            return 1
        src, dst = m.split("=", 1)
        path_map.append((src, dst))

    managed = set(args.managed_category)
    qb = QB(args.qb_url)
    try:
        torrents = qb.get("/api/v2/torrents/info")
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        print(f"error: qBittorrent API unreachable at {args.qb_url}: {e}", file=sys.stderr)
        return 1

    state = load_state(args.state)
    probed: dict[str, Any] = state["probed"]
    reported: dict[str, Any] = state["reported"]
    live = {t["hash"] for t in torrents}

    findings: list[dict[str, Any]] = []
    probes_done = 0

    for t in torrents:
        h = t["hash"]
        name = t.get("name", "")
        cat = t.get("category", "")
        if t.get("progress", 0) < 1:
            continue
        try:
            files = qb.get("/api/v2/torrents/files", hash=h)
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            continue
        names = [f.get("name", "") for f in files]
        exts = [ext_of(n) for n in names]

        def add(kind: str, detail: str, severity: str) -> None:
            findings.append({"hash": h, "name": name, "category": cat,
                             "kind": kind, "detail": detail, "severity": severity})

        # Tier 1 — extensions. Free for every torrent on every pass; only a
        # flagged file costs a read, and then only its first 8 bytes.
        hits = [n for n, e in zip(names, exts)
                if e in DANGEROUS_EXT and not in_disc_structure(n)]
        real: list[tuple[str, str]] = []
        fake: list[tuple[str, str]] = []
        for n in hits:
            e = ext_of(n)
            if e in SCRIPT_EXEC_EXT:
                # A .bat is text by definition — content cannot exonerate it.
                real.append((n, "script"))
                continue
            ok, why = looks_executable(
                translate(f"{t.get('save_path', '').rstrip('/')}/{n}", path_map))
            (real if ok else fake).append((n, why))

        if real:
            exts_seen = sorted({"." + ext_of(n) for n, _ in real})
            add("executable",
                f"contains {', '.join(exts_seen)}: {real[0][0].rsplit('/', 1)[-1]}",
                "high")
        if fake:
            # Named like an executable, isn't one. Worth recording — we are
            # still handing strangers a file that lies about what it is — but
            # not worth the channel reserved for things that are real.
            add("mislabeled-exec",
                f"{fake[0][0].rsplit('/', 1)[-1]} claims "
                f".{ext_of(fake[0][0])} but is {fake[0][1]}"
                + (f" (+{len(fake) - 1} more)" if len(fake) > 1 else ""),
                "low")

        if cat not in managed:
            continue

        has_archive = any(e in ARCHIVE_EXT for e in exts)
        # Samples are excluded up front: a RAR'd season pack frequently has no
        # loose media on disk except the preview clip, and judging the release
        # by that file inverts the answer.
        media = [(n, e) for n, e in zip(names, exts)
                 if e in MEDIA_EXT and not is_sample(n)]

        if not media:
            # Nothing to judge. Packed releases are unpackerr's job and a disc
            # rip keeps its video inside the disc layout, so neither is a fault.
            if not has_archive and not any(in_disc_structure(n) for n in names):
                add("no-media", f"{len(names)} file(s), none of them media", "high")
            continue

        odd = sorted({e for n, e in zip(names, exts)
                      if e and not in_disc_structure(n)
                      and e not in MEDIA_EXT | AUX_EXT | ARCHIVE_EXT | DANGEROUS_EXT | DISC_EXT})
        if odd:
            add("unknown-ext", f"unrecognised extension(s): {', '.join('.' + o for o in odd)}", "low")

        # Tier 2 — content. Cached by infohash; a torrent is probed once.
        if h in probed and not args.all:
            cached = probed[h]
            if not cached.get("ok"):
                add("invalid-media", cached.get("reason", "failed ffprobe"), "high")
            continue
        if probes_done >= args.max_probes:
            continue

        targets = [translate(f"{t.get('save_path', '').rstrip('/')}/{n}", path_map)
                   for n, _ in media]
        # Largest file is the feature; probing every track of a 20-file album
        # costs 20 drive reads to answer the same question.
        targets.sort(key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)
        if not targets:
            continue
        ok, reason = probe(targets[0])
        probes_done += 1
        probed[h] = {"ok": ok, "reason": reason, "at": int(time.time()),
                     "file": targets[0].name}
        if not ok:
            add("invalid-media", f"{targets[0].name}: {reason}", "high")

    # Only NEW findings are worth waking anyone for. A stuck torrent that was
    # reported yesterday and is still stuck today is the same fact, not a new
    # one — re-announcing it on a timer is how an alert channel gets muted.
    fresh = [f for f in findings if f"{f['hash']}:{f['kind']}" not in reported]
    high = [f for f in fresh if f["severity"] == "high"]

    for f in findings:
        reported.setdefault(f"{f['hash']}:{f['kind']}", int(time.time()))
    for key in list(reported):
        if key.split(":", 1)[0] not in live:
            del reported[key]
    for h in list(probed):
        if h not in live:
            del probed[h]

    report = {
        "scanned": len(torrents),
        "probes_this_run": probes_done,
        "findings": findings,
        "new": fresh,
    }
    print(json.dumps(report, indent=2))

    if high and args.ntfy_url and args.ntfy_topic and not args.dry_run:
        lines = [f"{f['kind']}: {f['name'][:70]}\n   {f['detail'][:120]}" for f in high[:6]]
        if len(high) > 6:
            lines.append(f"...and {len(high) - 6} more")
        notify(args.ntfy_url, args.ntfy_topic,
               f"media-gate: {len(high)} suspect download(s)",
               "\n".join(lines), priority="default")

    if not args.dry_run:
        args.state.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.state.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(args.state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
