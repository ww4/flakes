"""`netradio speaker` — gromit's own sound card as a playback endpoint.

The library stations already reach the phone and the living-room receiver;
this makes the green jack on the back of the box a third place to send
them. It keeps one ffmpeg child alive on a station's Icecast mount, decoding
straight to the ALSA device, and answers a small JSON API on loopback so the
remote can drive it:

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

# ALSA's `default` is redirected to PipeWire by 99-pipewire-default.conf, and
# PipeWire on this box is a per-user service belonging to the desktop session.
# A system service reaching for it gets EHOSTDOWN — "Host is down".
DEFAULT_DEVICE = "plughw:0,0"


class Mixer:
    """The card's playback volume through amixer. `control` is the mixer
    control ('Master' on most, 'PCM' on some codecs); the first one that
    answers is used, so a different card needs no configuration."""

    CANDIDATES = ("Master", "PCM", "Speaker", "Headphone", "Digital")

    # Class-level defaults so an instance built without __init__ still behaves.
    # Tests and diagnostics do that, and every time a new instance attribute
    # appeared they broke on AttributeError instead of on the thing under test.
    cap = 100
    target = 50
    _muted = False
    # Asymmetric on purpose. Coming back is a fade IN and wants to be gentle;
    # going away, and any ordinary volume change, wants to feel immediate.
    # Chris set 300 ms in by ear (2026-09-27).
    fade_in_ms = 300
    fade_out_ms = 120

    def __init__(self, card: str, amixer: str = "amixer", control: str = "", cap: int = 100,
                 fade_in_ms: int | None = None, fade_out_ms: int | None = None):
        self.card = card
        self.amixer = amixer
        # A ceiling nothing can exceed: not the UI, not a stray request, not a
        # diagnostic. Chris found the live level at 100% on 2026-09-26 with no
        # way to tell what had put it there, and on speakers that is the kind of
        # accident worth making impossible rather than unlikely.
        self.cap = max(0, min(100, int(cap)))
        if fade_in_ms is not None:
            self.fade_in_ms = max(0, int(fade_in_ms))
        if fade_out_ms is not None:
            self.fade_out_ms = max(0, int(fade_out_ms))
        self.control = control or self._find()
        self.lock = threading.RLock()
        self._muted = False
        # The codec's own mute switch is never touched after this (see mute()),
        # so clear it ONCE at startup — a `[off]` left in alsa-state from before
        # would otherwise make the service silent forever while it happily
        # ramped a volume nobody could hear. This is the one click per start.
        if self.control:
            self._run("sset", self.control, "unmute")
        # Where an unmute returns to. A muted control reads 0%, so the level
        # has to be remembered rather than read back.
        cur, _ = self.state()
        self.target = cur if cur else 50

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
        """(volume 0-100, muted). Muted is OUR state, because the mute is a
        level of zero rather than the codec's switch; `[off]` still counts, so
        someone muting the card by hand is not reported as unmuted."""
        if not self.control:
            return None, False
        out = self._run("sget", self.control)
        pct = re.search(r"\[(\d{1,3})%\]", out)
        return (int(pct.group(1)) if pct else None), (self._muted or "[off]" in out)

    def set(self, level: int) -> None:
        """Level only. Deliberately NOT `unmute` as well: the receiver does
        not unmute when you change its volume, and a box that boots muted
        must stay muted until someone asks for sound (Chris, 2026-09-25)."""
        if self.control:
            self._run("sset", self.control, f"{max(0, min(self.cap, int(level)))}%")

    def choose(self, level: int) -> None:
        """A user-driven level change: remember it, and slide rather than jump.
        While muted it is only remembered — changing the volume must not bring
        the sound back."""
        level = max(0, min(self.cap, int(level)))
        with self.lock:
            cur, muted = self.state()
            log.info("volume %s -> %d%s", cur, level, " (muted, remembered only)" if muted else "")
            self.target = level
            if muted:
                return
            self._ramp(cur if cur is not None else level, level, self.fade_out_ms)

    def step(self, delta: int) -> None:
        with self.lock:
            self.choose(self.target + delta)

    # One write every ~12 ms: fine enough that the steps are inaudible, coarse
    # enough that a 300 ms fade is 25 subprocess calls and not 300.
    STEP_MS = 12

    def _ramp(self, start: int, end: int, ms: int) -> None:
        """Walk the level from start to end over `ms`, so the change is a slope
        rather than a step. Each amixer write is one codec register write;
        spacing them is the whole trick.

        The pacing is against the CLOCK, not a fixed sleep per step: every
        write forks amixer, which costs a few milliseconds of its own, so
        sleeping a flat interval made the fade reliably longer than asked for.
        """
        if start == end or ms <= 0:
            if start != end:
                self.set(end)
            return
        steps = max(2, round(ms / self.STEP_MS))
        began = time.monotonic()
        for i in range(1, steps + 1):
            self.set(round(start + (end - start) * i / steps))
            due = began + (ms / 1000.0) * i / steps
            remaining = due - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)

    def mute(self, on: bool) -> None:
        """Mute by taking the level to zero. The codec's mute switch is never
        written.

        The story, because it rules out the obvious alternatives. Chris asked
        for zero-crossing detection; a fade was tried first, on the theory that
        the click was the envelope step from full level to nothing. The fade is
        audible and pleasant — and the click was completely unchanged
        (2026-09-26). That measurement is the answer: a transient that does not
        care what the volume is, is not in the signal. It is the pin widget's
        mute bit stepping the analog stage's DC operating point, which is also
        why zero-crossing detection could never have helped — there is no
        crossing to catch.

        So: no switch. `Master` bottoms out at -64 dB (dbmin -6400 on this
        codec), which is inaudible, and the DAC node reports `mute=0` — it has
        no mute of its own to use instead. Ramping to the bottom is both silent
        and click-free, and `_muted` carries the state the switch used to.
        """
        if not self.control:
            return
        with self.lock:
            cur, _ = self.state()
            cur = cur if cur is not None else 50
            log.info("%s (level %s, returning to %d)", "mute" if on else "unmute", cur, self.target)
            if on:
                if not self._muted:
                    self.target = cur          # remember where to come back to
                self._ramp(cur, 0, self.fade_out_ms)
                self._muted = True
            else:
                self._muted = False
                self._ramp(0, self.target, self.fade_in_ms)


class Player:
    """One ffmpeg on one mount, kept alive. Switching mounts kills and
    restarts it — a stream is not seekable, so there is nothing to keep.

    ffmpeg, not ffplay, for one reason: the output device has to be an
    *argument*. ffplay is an SDL program and takes its device from the
    AUDIODEV environment variable — but the ffplay in nixpkgs links
    sdl2-compat, which reimplements the SDL2 API on top of SDL3, and SDL3
    dropped AUDIODEV (the string does not appear in the library at all). So
    the variable was accepted, ignored, and ffplay opened ALSA's `default`
    anyway: straight into PipeWire, "Host is down", forever. An ignored
    environment variable fails silently; `-f alsa plughw:0,0` cannot."""

    def __init__(self, base: str, ffmpeg: str, device: str = "", wake: str = ""):
        self.base = base.rstrip("/")
        self.ffmpeg = ffmpeg
        self.device = device or DEFAULT_DEVICE
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

    def command(self, mount: str) -> list[str]:
        """The argv, separately so a test can read the device back out of it."""
        return [self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "warning",
                # Icecast drops a listener now and then; reconnect rather than
                # wait for watch() to notice five seconds later.
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-i", self.url(mount),
                "-f", "alsa", self.device]

    def play(self, mount: str) -> None:
        with self.lock:
            self.stop()
            self.wake(mount)
            cmd = self.command(mount)
            # inherit the unit's environment (PATH comes from Environment= in
            # the service) — hardcoding PATH here broke the player anywhere
            # that path does not exist
            log.info("playing %s: %s", mount, shlex.join(cmd))
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                         env={**os.environ})
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
            # `volume` is the CHOSEN level, which is where an unmute returns
            # to. Reporting the live register instead made the slider snap to
            # 0 the moment you muted, and a fade in progress made it jitter.
            return {"playing": player.alive(), "mount": player.mount,
                    "volume": mixer.target if mixer.control else vol,
                    "actual": vol, "muted": muted,
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
                        mixer.choose(int(body["level"]))
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
    ap.add_argument("--device", default=DEFAULT_DEVICE,
                    help="ALSA device to open; NOT `default`, which PipeWire claims")
    ap.add_argument("--wake", default="", help="the wake service, e.g. http://127.0.0.1:8011 (starts the encoder)")
    ap.add_argument("--default-mount", default="", help="play this at startup (the rain, usually)")
    ap.add_argument("--start-volume", type=int, help="set the mixer here at startup")
    ap.add_argument("--max-volume", type=int, default=100,
                    help="a ceiling no request can exceed — protects the speakers from a stray 100%%")
    ap.add_argument("--fade-in-ms", type=int, default=300, help="how long an unmute takes to come back")
    ap.add_argument("--fade-out-ms", type=int, default=120, help="how long a mute, or a volume change, takes")
    ap.add_argument("--start-muted", action="store_true",
                    help="come up silent — the stream runs, the jack is quiet until unmuted")
    ap.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stdout)

    mixer = Mixer(args.card, control=args.control, cap=args.max_volume,
                  fade_in_ms=args.fade_in_ms, fade_out_ms=args.fade_out_ms)
    log.info("mixer control: %s (card %s), ceiling %d%%, fade %d/%d ms in/out",
             mixer.control or "none found", args.card, mixer.cap, mixer.fade_in_ms, mixer.fade_out_ms)
    if args.start_volume is not None:
        mixer.set(args.start_volume)
    if args.start_muted:
        mixer.mute(True)          # after the level, so the level is ready when it is unmuted
        log.info("starting muted")
    player = Player(args.icecast, args.ffmpeg, args.device, args.wake)
    stop = threading.Event()
    threading.Thread(target=player.watch, args=(stop,), daemon=True).start()

    if args.default_mount:
        player.play(args.default_mount)

    srv = ThreadingHTTPServer((args.listen, args.port), make_handler(player, mixer))
    # Serve on a THREAD and wait here. The obvious shape — serve_forever() in
    # the main thread with a SIGTERM handler that calls srv.shutdown() —
    # deadlocks: Python runs signal handlers on the main thread, so shutdown()
    # blocks waiting for a serve loop that cannot run until the handler
    # returns. systemd then waited the full 90 s TimeoutStop and SIGKILLed,
    # which is 90 s of silence on every single deploy (2026-09-26).
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    log.info("speaker on %s:%d, playing %s", args.listen, args.port, args.default_mount or "nothing")
    try:
        # A POLLING wait, not a bare stop.wait(). An untimed wait depends on the
        # signal interrupting a C-level lock acquire, which held on the box and
        # did NOT in the build sandbox — the process outlived SIGTERM by more
        # than 15 s there. Waking twice a second makes the handler's effect
        # observable no matter how the platform treats the interrupt.
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    stop.set()
    player.stop()
    srv.shutdown()          # safe from here: the loop is on another thread
    srv.server_close()
    return 0
