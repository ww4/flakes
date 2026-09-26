"""`netradio speaker` — gromit's own sound card as a playback endpoint.

The library stations already reach the phone and the living-room receiver;
this makes the green jack on the back of the box a third place to send
them. It keeps one ffplay child alive on a station's Icecast mount, and
answers a small JSON API on loopback so the remote can drive it:

    GET  /state                  {playing, mount, volume, muted, error}
    POST /play    {"mount": "rain"}
    POST /stop
    POST /volume  {"level": 40} | {"step": 5} | {"step": -5}
    POST /mute    {"on": true}

Volume is the ALSA mixer (amixer), so it is the real output level, not a
software attenuation — nothing else on the box is competing for the card.
A crashed player is restarted; a station that stops streaming leaves the
service trying, which is what you want for an all-night rain loop.

The default mount plays at boot, so power-cycling the box brings the rain
back with no phone involved.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

log = logging.getLogger("netradio.speaker")

MOUNT_RE = re.compile(r"^[a-z0-9-]+$")


class Mixer:
    """The card's playback volume through amixer. `control` is the mixer
    control ('Master' on most, 'PCM' on some codecs); the first one that
    answers is used, so a different card needs no configuration."""

    CANDIDATES = ("Master", "PCM", "Speaker", "Headphone", "Digital")

    def __init__(self, card: str, amixer: str = "amixer", control: str = ""):
        self.card = card
        self.amixer = amixer
        self.control = control or self._find()

    def _run(self, *args: str) -> str:
        """amixer missing or the card gone: no volume control, but the music
        keeps playing — never take the player down over the mixer."""
        try:
            return subprocess.run([self.amixer, "-c", self.card, *args],
                                  capture_output=True, text=True, timeout=10).stdout
        except Exception as e:
            log.warning("amixer %s failed: %s", " ".join(args), e)
            return ""

    def _find(self) -> str:
        out = self._run("scontrols")
        names = re.findall(r"Simple mixer control '([^']+)'", out)
        for want in self.CANDIDATES:
            if want in names:
                return want
        return names[0] if names else ""

    def state(self) -> tuple[int | None, bool]:
        """(volume 0-100, muted)."""
        if not self.control:
            return None, False
        out = self._run("sget", self.control)
        pct = re.search(r"\[(\d{1,3})%\]", out)
        return (int(pct.group(1)) if pct else None), ("[off]" in out)

    def set(self, level: int) -> None:
        """Level only. Deliberately NOT `unmute` as well: the receiver does
        not unmute when you change its volume, and a box that boots muted
        must stay muted until someone asks for sound (Chris, 2026-09-25)."""
        if self.control:
            self._run("sset", self.control, f"{max(0, min(100, int(level)))}%")

    def step(self, delta: int) -> None:
        cur, _ = self.state()
        self.set((cur if cur is not None else 50) + delta)

    def mute(self, on: bool) -> None:
        if self.control:
            self._run("sset", self.control, "mute" if on else "unmute")


class Player:
    """One ffplay on one mount, kept alive. Switching mounts kills and
    restarts it — a stream is not seekable, so there is nothing to keep."""

    def __init__(self, base: str, ffplay: str, device: str = "", wake: str = ""):
        self.base = base.rstrip("/")
        self.ffplay = ffplay
        self.device = device
        self.wake_url = wake.rstrip("/")
        self.mount = ""
        self.proc: subprocess.Popen | None = None
        self.error = ""
        self.lock = threading.RLock()

    def url(self, mount: str) -> str:
        return f"{self.base}/{mount}.mp3"

    def wake(self, mount: str) -> None:
        """Start the station's encoder before connecting to it.

        The encoders are on-demand: nginx fires `auth_request /_wake` when a
        listener asks for /radio/<mount>.mp3, and only then does Liquidsoap
        start that output. Connecting straight to Icecast skips all of that,
        so the mount does not exist and Icecast answers 404 — which is what
        this service did on every attempt until 2026-09-25. Calling the wake
        service directly is the same door, without nginx's https redirect."""
        if not self.wake_url:
            return
        req = urllib.request.Request(self.wake_url + "/wake",
                                     headers={"X-Original-URI": f"/radio/{mount}.mp3"})
        try:
            urllib.request.urlopen(req, timeout=30).read()
        except Exception as e:
            log.warning("wake for %s failed (%s) — trying the mount anyway", mount, e)

    def play(self, mount: str) -> None:
        with self.lock:
            self.stop()
            self.wake(mount)
            cmd = [self.ffplay, "-nodisp", "-autoexit", "-loglevel", "warning", "-infbuf", self.url(mount)]
            # inherit the unit's environment (PATH comes from Environment= in
            # the service) and only say which audio device to open — hardcoding
            # PATH here broke the player anywhere that path does not exist
            # AUDIODEV must name the hardware. ALSA's `default` is redirected
            # to PipeWire by 99-pipewire-default.conf, and PipeWire here is a
            # per-user service belonging to the desktop session — a system
            # service reaching for it gets "Host is down" (2026-09-25).
            env = {**os.environ, "SDL_AUDIODRIVER": "alsa", "AUDIODEV": self.device or "plughw:0,0"}
            log.info("playing %s: %s", mount, shlex.join(cmd))
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
            self.mount, self.error = mount, ""

    def stop(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc, self.mount = None, ""

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def watch(self, stop: threading.Event, every: float = 5.0) -> None:
        """Restart a player that died while a mount is selected — a station
        that goes quiet for a moment (a deploy restarting Liquidsoap) must
        not end the night's rain."""
        while not stop.wait(every):
            with self.lock:
                if self.mount and not self.alive():
                    err = ""
                    if self.proc and self.proc.stderr:
                        try:
                            err = (self.proc.stderr.read() or b"").decode(errors="replace").strip()[-200:]
                        except Exception:
                            pass
                    self.error = err
                    log.warning("player for %s died (%s), restarting", self.mount, err or "no output")
                    mount = self.mount
                    self.proc = None
                    time.sleep(2)
                    self.play(mount)


def make_handler(player: Player, mixer: Mixer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _reply(self, code: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _json(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n))
            except ValueError:
                return {}

        def _state(self) -> dict:
            vol, muted = mixer.state()
            return {"playing": player.alive(), "mount": player.mount, "volume": vol, "muted": muted,
                    "control": mixer.control, "error": player.error}

        def do_GET(self):
            if urlparse(self.path).path in ("/state", "/"):
                return self._reply(200, self._state())
            self._reply(404, {"error": "not found"})

        def do_POST(self):
            path = urlparse(self.path).path
            body = self._json()
            try:
                if path == "/play":
                    mount = str(body.get("mount") or "")
                    if not MOUNT_RE.match(mount):
                        return self._reply(400, {"error": "mount must be a station mount"})
                    player.play(mount)
                elif path == "/stop":
                    player.stop()
                elif path == "/volume":
                    if "level" in body:
                        mixer.set(int(body["level"]))
                    elif "step" in body:
                        mixer.step(int(body["step"]))
                    else:
                        return self._reply(400, {"error": "level or step required"})
                elif path == "/mute":
                    mixer.mute(bool(body.get("on")))
                else:
                    return self._reply(404, {"error": "not found"})
            except Exception as e:
                log.exception("%s failed", path)
                return self._reply(500, {"error": str(e)})
            time.sleep(0.3)
            self._reply(200, self._state())

    return Handler


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--icecast", default="http://127.0.0.1:8020", help="where the mounts are served")
    ap.add_argument("--listen", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8013)
    ap.add_argument("--card", default="0", help="ALSA card index or name for the mixer")
    ap.add_argument("--control", default="", help="mixer control (default: the first of Master/PCM/Speaker…)")
    ap.add_argument("--device", default="plughw:0,0",
                    help="ALSA device to open; NOT `default`, which PipeWire claims")
    ap.add_argument("--wake", default="", help="the wake service, e.g. http://127.0.0.1:8011 (starts the encoder)")
    ap.add_argument("--default-mount", default="", help="play this at startup (the rain, usually)")
    ap.add_argument("--start-volume", type=int, help="set the mixer here at startup")
    ap.add_argument("--start-muted", action="store_true",
                    help="come up silent — the stream runs, the jack is quiet until unmuted")
    ap.add_argument("--ffplay", default=shutil.which("ffplay") or "ffplay")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stdout)

    mixer = Mixer(args.card, control=args.control)
    log.info("mixer control: %s (card %s)", mixer.control or "none found", args.card)
    if args.start_volume is not None:
        mixer.set(args.start_volume)
    if args.start_muted:
        mixer.mute(True)          # after the level, so the level is ready when it is unmuted
        log.info("starting muted")
    player = Player(args.icecast, args.ffplay, args.device, args.wake)
    stop = threading.Event()
    threading.Thread(target=player.watch, args=(stop,), daemon=True).start()

    if args.default_mount:
        player.play(args.default_mount)

    srv = ThreadingHTTPServer((args.listen, args.port), make_handler(player, mixer))
    signal.signal(signal.SIGTERM, lambda *_: (stop.set(), player.stop(), srv.shutdown()))
    log.info("speaker on %s:%d, playing %s", args.listen, args.port, args.default_mount or "nothing")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    stop.set()
    player.stop()
    return 0
