"""A small JSON service in front of one Roku.

ECP is already HTTP and already unauthenticated, so the obvious question is why
anything sits in front of it at all. Four reasons, and they are the whole job:

  * A PAGE CANNOT REACH IT. The remote is served from a domain; the Roku is an
    address on the LAN with no CORS headers and no TLS. One origin, proxied, is
    the only shape a browser will accept.
  * ECP speaks XML and answers 200 to nonsense. `/keypress/Hmoe` succeeds and
    does nothing. Keys are checked here (see ecp.KEYS) so a typo is a 400 with
    a reason rather than a button that silently does not work.
  * POLICY. ECP has no notion of what a household wants to allow. PowerOff is
    one request away from a dark television while somebody is watching it, so
    it is gated on an option that defaults to off.
  * The address is DHCP's. Discovery, and re-discovery after a move, live here
    so nothing upstream has to care.

Bound to localhost. Anything that can reach this can already reach the Roku
directly, so this is not a security boundary and does not pretend to be one —
it is a translation layer with one opinion about power.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from roku import discover
from roku.ecp import ACTIONS, KEYS, POWER_KEYS, Roku, RokuError

log = logging.getLogger("roku.api")

# How long a discovered address is trusted before it is looked up again. DHCP
# leases outlive this comfortably; the point is to recover from a move without
# anybody restarting anything.
REDISCOVER_S = 300.0


class Device:
    """The Roku this service speaks for, and how it is found.

    A configured host is used as given and never searched for — somebody who
    pinned an address meant it. Without one, SSDP is asked, and the answer is
    kept until it stops working.
    """

    def __init__(self, host: str = "", port: int = 8060, timeout: float = 5.0):
        self.fixed = host
        self.port = port
        self.timeout = timeout
        self.lock = threading.Lock()
        self._host = host
        self._found_at = 0.0

    def _search(self) -> str:
        hits = discover.find()
        if not hits:
            return ""
        if len(hits) > 1:
            log.warning("%d Rokus answered; using %s. Set services.roku.host to choose.",
                        len(hits), hits[0]["host"])
        self.port = hits[0]["port"]
        return hits[0]["host"]

    def roku(self) -> Roku:
        with self.lock:
            if self.fixed:
                return Roku(self.fixed, self.port, self.timeout)
            if not self._host or time.monotonic() - self._found_at > REDISCOVER_S:
                found = self._search()
                if found:
                    self._host, self._found_at = found, time.monotonic()
            if not self._host:
                raise RokuError("no Roku found on this network, and none configured")
            return Roku(self._host, self.port, self.timeout)

    def forget(self) -> None:
        """Called when a request fails: the next one searches again rather than
        retrying an address that has moved."""
        if not self.fixed:
            with self.lock:
                self._host, self._found_at = "", 0.0


def make_handler(device: Device, allow_power: bool):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):      # the journal gets our own lines
            log.debug(fmt, *args)

        # -- replies -------------------------------------------------------
        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _fail(self, code: int, why: str):
            self._json({"error": why}, code)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        # -- routing -------------------------------------------------------
        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def _route(self, method: str):
            u = urlparse(self.path)
            parts = [unquote(p) for p in u.path.strip("/").split("/") if p]
            q = parse_qs(u.query)
            try:
                # Discovery answers even when nothing is reachable — it is how
                # you find out there is nothing to reach.
                if method == "GET" and parts == ["discover"]:
                    return self._json({"found": discover.find()})

                if method == "GET" and parts == ["status"]:
                    return self._json(device.roku().status())
                if method == "GET" and parts == ["apps"]:
                    return self._json({"apps": device.roku().apps()})
                if method == "GET" and len(parts) == 3 and parts[0] == "apps" and parts[2] == "icon":
                    data, ctype = device.roku().icon(parts[1])
                    return self._send(200, data, ctype)

                if method == "POST" and len(parts) == 2 and parts[0] == "key":
                    key = parts[1]
                    # A bad key is the CALLER's mistake and nothing is sent, so
                    # it is a 400. Reporting it as 502 blamed the device for a
                    # request it never saw (2026-10-03).
                    if key not in KEYS:
                        return self._fail(400, f"not an ECP key: {key!r}")
                    action = (q.get("action") or ["keypress"])[0]
                    if action not in ACTIONS:
                        return self._fail(400, f"not an ECP action: {action!r}")
                    if key in POWER_KEYS and not allow_power:
                        return self._fail(403, "power keys are disabled; set services.roku.allowPower")
                    device.roku().press(key, action)
                    log.info("%s %s", action, key)
                    return self._json({"ok": True, "key": key, "action": action})

                if method == "POST" and parts == ["type"]:
                    text = str(self._body().get("text", ""))
                    if not text:
                        return self._fail(400, "nothing to type")
                    sent = device.roku().literal(text)
                    log.info("typed %d character(s)", sent)
                    return self._json({"ok": True, "sent": sent})

                if method == "POST" and len(parts) == 2 and parts[0] == "launch":
                    device.roku().launch(parts[1], {k: v[0] for k, v in q.items()})
                    log.info("launched %s", parts[1])
                    return self._json({"ok": True, "app": parts[1]})

                if method == "POST" and parts == ["search"]:
                    body = self._body()
                    device.roku().search(**{k: v for k, v in body.items() if isinstance(v, (str, int))})
                    log.info("searched for %r", body.get("keyword"))
                    return self._json({"ok": True})

                return self._fail(404, "no such endpoint")
            except RokuError as e:
                # The box is asleep, moved or gone. Say so plainly and make the
                # next call look for it again.
                device.forget()
                log.warning("%s %s: %s", method, u.path, e)
                return self._fail(502, str(e))
            except Exception as e:                       # never take the service down
                log.exception("%s %s", method, u.path)
                return self._fail(500, str(e))

    return Handler


def serve(device: Device, listen: str, port: int, allow_power: bool) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((listen, port), make_handler(device, allow_power))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
