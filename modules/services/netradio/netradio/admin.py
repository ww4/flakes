"""The admin API behind radio.rosemaryacres.com/admin — edits the runtime
config (feeds, stations, schedule) and files requests for the privileged
side (compile a feed, apply: rescan + restart Liquidsoap when mounts change).

Loopback only; nginx proxies /admin/api/ to it. The vhost is the
Tailscale/LAN gate. Everything it writes is a JSON file under the config
dir; the scanner and the DJ read those.

  GET    /api/state                      everything the page shows
  GET    /api/options?station=<mount>    feeds + artists compatible with a station
  POST   /api/feeds                      {title, description} → new feed, compile requested
  PUT    /api/feeds/<id>                 {title?, description?, rule?, family?, listenable?}
  POST   /api/feeds/<id>/compile         (re)compile from the description
  DELETE /api/feeds/<id>
  PUT    /api/stations/<mount>           {name?, family?, base?, breaks_every?, excursion?}
  PUT    /api/schedule                   the whole list of slots
  POST   /api/apply                      rescan now (and restart Liquidsoap if mounts changed)
  GET    /api/pandora                    what the receiver played on Pandora: artists per station, in-library flags
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import logging
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from netradio import feeds as feedrules
from netradio import schedule as sched
from netradio.config import write_atomic, FAMILIES, Config, compatible, new_id

log = logging.getLogger("netradio.admin")
LOCK = threading.Lock()
MOUNT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


class Admin:
    def __init__(self, cfg: Config, dj_dir: Path | None = None, playlists: Path | None = None):
        self.cfg = cfg
        self.dj_dir = dj_dir            # the DJ's state: inbox/ takes skip + request files
        self.playlists = playlists      # library.m3u for search
        self._library: list[str] | None = None
        self._library_mtime = 0.0
        self._inbox_n = itertools.count()

    # -- reads
    def pandora(self) -> dict:
        """What the receiver has played on Pandora (netradio pandora writes
        <config>/pandora.jsonl): artists per station, thumbed first, marked
        by whether the library has them. The list to grow the library from."""
        from netradio.pandora import read_log, summarise
        records = read_log(self.cfg.root / "pandora.jsonl")
        return {"stations": summarise(records, set(self.cfg.artists())), "plays": len(records)}

    def state(self) -> dict:
        feeds = self.cfg.feeds()
        stations = self.cfg.stations()
        listenable = {s.get("feed") for s in stations if s.get("kind") == "specialty"}
        for fid, f in feeds.items():
            f["listenable"] = fid in listenable
        artists = self.cfg.artists()
        pending = sorted(p.name for p in (self.cfg.root / "requests").glob("*.json")) if (self.cfg.root / "requests").exists() else []
        today = dt.date.today()
        picks = self.cfg.picks()
        todays = {}
        for s in stations:
            if s.get("kind") == "specialty":
                continue
            items = []
            for a, b, slot in sched.slots_today(s["mount"], self.cfg.schedule(), today):
                r, _ = sched.resolve(slot, today, picks, station=s, artists=artists, feeds=feeds)
                items.append({"start": a.strftime("%H:%M"), "end": b.strftime("%H:%M"), "name": r.get("name"),
                              "kind": r.get("kind"), "value": r.get("feed") or r.get("artist"), "id": slot.get("id")})
            todays[s["mount"]] = items
        return {"feeds": feeds, "stations": stations, "schedule": self.cfg.schedule(),
                "artists": {a: {"tracks": i["tracks"], "families": i["families"]} for a, i in artists.items()},
                "families": FAMILIES, "pending_requests": pending, "today": todays,
                "now": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}

    def options(self, mount: str) -> dict:
        station = next((s for s in self.cfg.stations() if s["mount"] == mount), None)
        fam = (station or {}).get("family")
        feeds = self.cfg.feeds()
        artists = self.cfg.artists()
        return {
            "feeds": [{"id": fid, "title": f.get("title", fid), "count": f.get("count", 0)}
                      for fid, f in sorted(feeds.items()) if f.get("status") == "ready" and compatible(fam, f.get("family"))],
            "artists": [{"name": a, "tracks": i["tracks"]}
                        for a, i in sorted(artists.items(), key=lambda kv: -kv[1]["tracks"])
                        if i["tracks"] >= sched.MIN_TRACKS_FOR_SPOTLIGHT and compatible(fam, i.get("families"))],
        }

    # -- writes
    def add_feed(self, body: dict) -> dict:
        title = str(body.get("title") or "").strip()
        if not title:
            raise ValueError("a title is required")
        feeds = self.cfg.feeds()
        fid = new_id(title, set(feeds))
        feeds[fid] = {"title": title, "description": str(body.get("description") or "").strip(),
                      "family": body.get("family") or [], "status": "pending", "rule": None, "count": 0,
                      "shellac": bool(body.get("shellac", False)), "note": "waiting for the compile step"}
        self.cfg.save_feeds(feeds)
        self.cfg.request("compile", {"feed": fid})
        if body.get("listenable", True):
            self._set_listenable(fid, True)
        return {"id": fid}

    def update_feed(self, fid: str, body: dict) -> dict:
        feeds = self.cfg.feeds()
        if fid not in feeds:
            raise KeyError(fid)
        f = feeds[fid]
        recompile = False
        if "title" in body:
            f["title"] = str(body["title"]).strip() or f["title"]
        if "description" in body and str(body["description"]).strip() != f.get("description", ""):
            f["description"] = str(body["description"]).strip()
            recompile = "rule" not in body
        if "family" in body:
            fam = body["family"] or []
            if any(x not in FAMILIES for x in fam):
                raise ValueError(f"family must be from {FAMILIES}")
            f["family"] = fam
        if "shellac" in body:
            f["shellac"] = bool(body["shellac"])
        if "rule" in body and body["rule"] is not None:
            errs = feedrules.validate(body["rule"])
            if errs:
                raise ValueError("; ".join(errs))
            f["rule"] = body["rule"]
            f["status"] = "ready"
            f["note"] = "rule edited by hand"
        self.cfg.save_feeds(feeds)
        if "listenable" in body:
            self._set_listenable(fid, bool(body["listenable"]))
        if recompile:
            f["status"] = "pending"
            self.cfg.save_feeds(feeds)
            self.cfg.request("compile", {"feed": fid})
        else:
            self.cfg.request("apply", {"reason": f"feed {fid} edited"})
        return {"ok": True, "recompile": recompile}

    def compile_feed(self, fid: str) -> dict:
        feeds = self.cfg.feeds()
        if fid not in feeds:
            raise KeyError(fid)
        feeds[fid]["status"] = "pending"
        feeds[fid]["note"] = "waiting for the compile step"
        self.cfg.save_feeds(feeds)
        self.cfg.request("compile", {"feed": fid})
        return {"ok": True}

    def delete_feed(self, fid: str) -> dict:
        feeds = self.cfg.feeds()
        feeds.pop(fid, None)
        self.cfg.save_feeds(feeds)
        self._set_listenable(fid, False)
        self.cfg.save_schedule([s for s in self.cfg.schedule() if s.get("feed") != fid])
        self.cfg.request("apply", {"reason": f"feed {fid} removed"})
        return {"ok": True}

    def _set_listenable(self, fid: str, on: bool) -> None:
        stations = self.cfg.stations()
        have = [s for s in stations if s.get("kind") == "specialty" and s.get("feed") == fid]
        if on and not have:
            feed = self.cfg.feeds().get(fid, {})
            mount = fid if MOUNT_RE.match(fid) else new_id(fid, {s["mount"] for s in stations})
            if any(s["mount"] == mount for s in stations):
                mount = new_id(fid, {s["mount"] for s in stations})
            stations.append({"mount": mount, "name": feed.get("title", fid), "kind": "specialty", "feed": fid,
                             "family": feed.get("family") or []})
        elif not on and have:
            stations = [s for s in stations if s not in have]
        else:
            return
        self.cfg.save_stations(stations)

    def update_station(self, mount: str, body: dict) -> dict:
        stations = self.cfg.stations()
        st = next((s for s in stations if s["mount"] == mount), None)
        if st is None:
            raise KeyError(mount)
        if "name" in body:
            st["name"] = str(body["name"]).strip() or st["name"]
        if "family" in body:
            fam = body["family"] or []
            if any(x not in FAMILIES for x in fam):
                raise ValueError(f"family must be from {FAMILIES}")
            st["family"] = fam
        if "breaks_every" in body:
            v = body["breaks_every"]
            if isinstance(v, str) and re.match(r"^\d+-\d+$", v):
                st["breaks_every"] = v
            else:
                try:
                    n = int(v)
                except (TypeError, ValueError):
                    raise ValueError("breaks_every must be a whole number (0 = never) or a range like 3-4")
                if n < 0 or n > 50:
                    raise ValueError("breaks_every must be 0..50")
                st["breaks_every"] = n
        if "excursion" in body and st.get("kind") != "specialty":
            try:
                pct = float(body["excursion"])
            except (TypeError, ValueError):
                raise ValueError("excursion must be a percentage, 0-50")
            if not 0 <= pct <= 50:
                raise ValueError("excursion must be 0..50 (percent of base picks from the fringe)")
            st["excursion"] = round(pct / 100, 3)
        if "base" in body and st.get("kind") != "specialty":
            errs = feedrules.validate(body["base"] or {})
            if errs:
                raise ValueError("; ".join(errs))
            st["base"] = body["base"]
        self.cfg.save_stations(stations)
        self.cfg.request("apply", {"reason": f"station {mount} edited"})
        return {"ok": True}

    def save_schedule(self, slots: list) -> dict:
        if not isinstance(slots, list):
            raise ValueError("schedule must be a list")
        mounts = {s["mount"] for s in self.cfg.stations() if s.get("kind") != "specialty"}
        feeds = self.cfg.feeds()
        seen: set[str] = set()
        clean = []
        for s in slots:
            if not isinstance(s, dict):
                raise ValueError("each slot must be an object")
            if s.get("station") not in mounts:
                raise ValueError(f"unknown curated station {s.get('station')!r}")
            kind = s.get("kind")
            if kind not in ("feed", "artist", "auto"):
                raise ValueError(f"slot kind must be feed, artist or auto, not {kind!r}")
            if kind == "feed" and s.get("feed") not in feeds:
                raise ValueError(f"unknown feed {s.get('feed')!r}")
            if kind == "artist" and not s.get("artist"):
                raise ValueError("an artist slot needs an artist")
            if kind == "auto" and s.get("like") not in ("feed", "artist"):
                raise ValueError("an auto slot needs like: feed or artist")
            if sched.slot_window({**s, "days": "daily"}, dt.date.today()) is None:
                raise ValueError(f"bad start time {s.get('start')!r} (HH:MM)")
            minutes = int(s.get("minutes") or 60)
            if not 15 <= minutes <= 720:
                raise ValueError("minutes must be 15..720")
            sid = s.get("id") or new_id(f"{s['station']}-{s.get('name') or kind}", seen)
            while sid in seen:
                sid = new_id(sid, seen)
            seen.add(sid)
            clean.append({**s, "id": sid, "minutes": minutes})
        self.cfg.save_schedule(clean)
        return {"ok": True, "count": len(clean)}

    def apply(self) -> dict:
        self.cfg.request("apply", {"reason": "requested from the admin page"})
        return {"ok": True}

    # -- listener feedback (2026-09-17): skip, never again, less of, requests
    def _inbox(self, mount: str, payload: dict) -> dict:
        if not self.dj_dir:
            raise ValueError("no DJ directory configured")
        inbox = self.dj_dir / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        name = f"{mount}-{int(dt.datetime.now().timestamp() * 1000)}-{next(self._inbox_n):04d}.json"
        write_atomic(inbox / name, json.dumps(payload))
        return {"ok": True, "queued": name}

    def skip(self, mount: str) -> dict:
        return self._inbox(mount, {"action": "skip"})

    def request(self, mount: str, body: dict) -> dict:
        path = str(body.get("path") or "")
        if path not in set(self.library()):
            raise ValueError("not a library track")
        # deliberately no requester name: nothing in the app sends one, and an
        # unauthenticated free-text field would let anyone on the tailnet put a
        # name in the DJ's mouth (2026-09-25)
        return self._inbox(mount, {"action": "request", "path": path})

    def dislike(self, body: dict) -> dict:
        """{"path": …, "scope": "track"|"artist", "title": …, "artist": …}.
        A track dislike keeps that file off every station from the next scan
        (and the DJ stops choosing it at once); an artist dislike plays that
        artist a quarter as often."""
        d = self.cfg.dislikes()
        when = dt.datetime.now().isoformat(timespec="minutes")
        path, scope = str(body.get("path") or ""), body.get("scope", "track")
        if scope == "artist":
            artist = str(body.get("artist") or "").strip().lower()
            if not artist:
                raise ValueError("artist required")
            d.setdefault("artists", {})[artist] = {"when": when, "title": body.get("title", "")}
        else:
            if not path:
                raise ValueError("path required")
            d.setdefault("tracks", {})[path] = {"when": when, "artist": body.get("artist", ""), "title": body.get("title", "")}
        self.cfg.save_dislikes(d)
        if scope != "artist" and body.get("mount"):
            self._inbox(str(body["mount"]), {"action": "skip"})
        return {"ok": True, "tracks": len(d.get("tracks", {})), "artists": len(d.get("artists", {}))}

    def undislike(self, body: dict) -> dict:
        d = self.cfg.dislikes()
        (d.get("artists", {}) if body.get("scope") == "artist" else d.get("tracks", {})).pop(str(body.get("key") or ""), None)
        self.cfg.save_dislikes(d)
        return {"ok": True}

    def art(self, path: str) -> tuple[bytes, str] | None:
        """Cover art for a library track: the file's embedded picture, else a
        cover/folder image beside it. (bytes, mime) or None."""
        if path not in set(self.library()):
            raise ValueError("not a library track")
        try:
            import mutagen
            f = mutagen.File(path)
        except Exception:
            f = None
        if f is not None:
            tags = getattr(f, "tags", None) or {}
            for key in list(tags.keys()):
                if key.startswith("APIC"):                       # mp3
                    pic = tags[key]
                    return bytes(pic.data), ("image/jpeg" if (pic.mime or "").lower() in ("", "image/jpg") else pic.mime)
            covr = tags.get("covr") if hasattr(tags, "get") else None
            if covr:                                             # m4a
                data = covr[0]
                return bytes(data), "image/png" if bytes(data[:4]) == b"\x89PNG" else "image/jpeg"
            for pic in getattr(f, "pictures", []) or []:         # flac
                return bytes(pic.data), pic.mime or "image/jpeg"
        folder = Path(path).parent
        for name in ("cover.jpg", "Cover.jpg", "folder.jpg", "Folder.jpg", "cover.png", "front.jpg", "Front.jpg", "album.jpg"):
            fp = folder / name
            if fp.exists():
                return fp.read_bytes(), "image/png" if name.endswith(".png") else "image/jpeg"
        return None

    def library(self) -> list[str]:
        if not self.playlists:
            return []
        f = self.playlists / "library.m3u"
        try:
            mtime = f.stat().st_mtime
        except OSError:
            return []
        if self._library is None or mtime != self._library_mtime:
            self._library = [l.strip() for l in f.read_text().splitlines() if l.strip() and not l.startswith("#")]
            self._library_mtime = mtime
        return self._library

    def search(self, q: str, limit: int = 30) -> list[dict]:
        """Library tracks whose artist/album/file name carry every word of q."""
        words = [w for w in q.lower().split() if w]
        if not words:
            return []
        out = []
        for path in self.library():
            rel = path.split("/Music/")[-1] if "/Music/" in path else path
            low = rel.lower()
            if all(w in low for w in words):
                parts = rel.split("/")
                out.append({"path": path, "artist": parts[0] if len(parts) > 2 else "", "album": parts[-2] if len(parts) > 2 else "",
                            "title": re.sub(r"^\d+[\s.-]+", "", parts[-1].rsplit(".", 1)[0])})
                if len(out) >= limit:
                    break
        return out


def thumbnail(data: bytes, size: int) -> tuple[bytes, str]:
    """Square-ish JPEG thumbnail, `size` px on the long edge; the original
    if Pillow can't read it."""
    try:
        from io import BytesIO
        from PIL import Image
        im = Image.open(BytesIO(data))
        im.thumbnail((max(64, min(size, 1024)),) * 2)
        out = BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=82, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return data, "image/jpeg"


def make_handler(admin: Admin):
    class Handler(BaseHTTPRequestHandler):
        def _json(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _reply(self, code: int, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _route(self, method: str):
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            q = parse_qs(u.query)
            try:
                with LOCK:
                    if parts == ["api", "state"] and method == "GET":
                        return self._reply(200, admin.state())
                    if parts == ["api", "options"] and method == "GET":
                        return self._reply(200, admin.options((q.get("station") or [""])[0]))
                    if parts == ["api", "feeds"] and method == "POST":
                        return self._reply(201, admin.add_feed(self._json()))
                    if len(parts) == 3 and parts[:2] == ["api", "feeds"]:
                        if method == "PUT":
                            return self._reply(200, admin.update_feed(parts[2], self._json()))
                        if method == "DELETE":
                            return self._reply(200, admin.delete_feed(parts[2]))
                    if len(parts) == 4 and parts[:2] == ["api", "feeds"] and parts[3] == "compile" and method == "POST":
                        return self._reply(200, admin.compile_feed(parts[2]))
                    if len(parts) == 4 and parts[:2] == ["api", "feeds"] and parts[3] == "answer" and method == "GET":
                        return self._reply(200, {"answer": admin.cfg.answer(parts[2])})
                    if len(parts) == 3 and parts[:2] == ["api", "stations"] and method == "PUT":
                        return self._reply(200, admin.update_station(parts[2], self._json()))
                    if parts == ["api", "schedule"] and method == "PUT":
                        return self._reply(200, admin.save_schedule(self._json()))
                    if len(parts) == 4 and parts[:2] == ["api", "dj"] and parts[3] == "skip" and method == "POST":
                        return self._reply(200, admin.skip(parts[2]))
                    if len(parts) == 4 and parts[:2] == ["api", "dj"] and parts[3] == "request" and method == "POST":
                        return self._reply(200, admin.request(parts[2], self._json()))
                    if parts == ["api", "dislike"] and method == "POST":
                        return self._reply(200, admin.dislike(self._json()))
                    if parts == ["api", "dislike"] and method == "DELETE":
                        return self._reply(200, admin.undislike(self._json()))
                    if parts == ["api", "dislikes"] and method == "GET":
                        return self._reply(200, admin.cfg.dislikes())
                    if parts == ["api", "pandora"] and method == "GET":
                        return self._reply(200, admin.pandora())
                    if parts == ["api", "art"] and method == "GET":
                        qs = parse_qs(u.query)
                        path = qs.get("path", [""])[0]
                        try:
                            got = admin.art(path)
                        except OSError as e:       # unreadable file or cover: no art, not a 500
                            log.warning("art %s: %s", path, e)
                            got = None
                        if not got:
                            return self._reply(404, {"error": "no art"})
                        data, mime = got
                        size = qs.get("size", [""])[0]
                        if size.isdigit():          # a thumbnail for the tiles (a full cover can be 600 KB)
                            data, mime = thumbnail(data, int(size))
                        self.send_response(200)
                        self.send_header("Content-Type", mime)
                        self.send_header("Cache-Control", "public, max-age=86400")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    if parts == ["api", "search"] and method == "GET":
                        q = parse_qs(u.query).get("q", [""])[0]
                        return self._reply(200, admin.search(q))
                    if parts == ["api", "apply"] and method == "POST":
                        return self._reply(200, admin.apply())
                return self._reply(404, {"error": "not found"})
            except KeyError as e:
                return self._reply(404, {"error": f"no such item: {e}"})
            except (ValueError, TypeError) as e:
                return self._reply(400, {"error": str(e)})
            except Exception as e:
                log.exception("%s %s failed", method, self.path)
                return self._reply(500, {"error": f"{type(e).__name__}: {e}"})

        def do_GET(self): self._route("GET")
        def do_POST(self): self._route("POST")
        def do_PUT(self): self._route("PUT")
        def do_DELETE(self): self._route("DELETE")

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    return Handler


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--listen", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8012)
    ap.add_argument("--dj-dir", type=Path, help="the DJ's state dir (inbox/ for skip + request)")
    ap.add_argument("--playlists", type=Path, help="playlists dir (library.m3u for search)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stdout)
    srv = ThreadingHTTPServer((args.listen, args.port), make_handler(Admin(Config(args.config), args.dj_dir, args.playlists)))
    log.info("admin API on %s:%d, config %s", args.listen, args.port, args.config)
    srv.serve_forever()
    return 0
