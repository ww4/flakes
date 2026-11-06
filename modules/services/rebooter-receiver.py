"""Receive push notifications from the ConnectSense Rebooter Pro at Craigmyle.

v1 IS AN OBSERVATION INSTRUMENT, NOT A FEATURE.

The device was subscribed on site 2026-11-04 (POST /notify, registered by IP at
https://104.207.80.108:443/notify). What it actually SENDS is unknown: the
vendor's reference repo carries only the client half, the receiving half is not
published, and the device exposes no way to read a subscription back -- GET
/notify returns 405. Guessing a schema and parsing against it would turn a
wrong guess into silently discarded evidence, so this records every request
verbatim and interprets nothing.

Once one real notification has landed, the shape is known and the useful thing
can be built on top of it: a LONG-WINDOW TREND of reboots per week. That is the
instrument this whole chain exists for -- an auto-rebooter hides decay, because
a daily reboot is far under any flapping threshold and a 65-second outage falls
between two ten-minute camera polls.

Deliberately NOT in v1: an ntfy notification. The message text would have to
invent a description of a payload nobody has seen, and a new topic Chris is not
subscribed to is a silent no-op. Add it when there is something true to say.
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# A notification is small. The cap exists so a malformed or hostile
# Content-Length cannot make this process allocate without bound; the record
# says when it bit rather than dropping the request silently.
MAX_BODY = 65536

EVENTS_FILE = "events.ndjson"


def build_record(now, method, path, peer, headers, raw, truncated=False):
    """Everything known about one request, as a single JSON-serialisable dict.

    Pure, so the test can exercise it without a socket. Body handling is
    belt-and-braces on purpose: `raw` is ALWAYS present even when `json`
    parses, because the first time we read one of these we want the bytes as
    sent, not this function's opinion of them.
    """
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
        "epoch": round(now, 3),
        "method": method,
        "path": path,
        "peer": peer,
        # X-Real-IP is set by gromit's nginx; without it `peer` is just 127.0.0.1.
        "client": headers.get("X-Real-IP") or peer,
        "headers": dict(headers),
        "body_bytes": len(raw),
    }
    if truncated:
        record["truncated"] = True
        record["truncated_at"] = MAX_BODY

    # Decode for the record, never for control flow. errors="replace" means a
    # non-UTF-8 byte produces a readable record instead of an exception that
    # would lose the whole event.
    record["raw"] = raw.decode("utf-8", errors="replace")
    try:
        record["json"] = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        record["parse_error"] = "%s: %s" % (type(exc).__name__, exc)
    return record


def append_event(state_dir, record):
    """One NDJSON line per event, flushed immediately.

    Appending (not read-modify-write) means a crash mid-run cannot corrupt
    what already landed, and two writers cannot lose each other's events.
    """
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, EVENTS_FILE)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
    return path


class Handler(BaseHTTPRequestHandler):
    server_version = "rebooter-receiver/1"
    state_dir = "/var/lib/rebooter-receiver"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.client_address[0], fmt % args))

    def _reply(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # A health path so the deploy can prove the unit is actually serving,
        # rather than inferring it from the unit being "active".
        if self.path.rstrip("/") in ("/health", ""):
            self._reply(200, {"ok": True, "service": "rebooter-receiver"})
        else:
            self._reply(404, {"ok": False})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        truncated = length > MAX_BODY
        raw = self.rfile.read(min(length, MAX_BODY)) if length > 0 else b""

        record = build_record(
            time.time(), "POST", self.path,
            self.client_address[0], self.headers, raw, truncated,
        )
        try:
            append_event(self.state_dir, record)
        except Exception as exc:
            # Answer 200 anyway. A device that cannot deliver may stop trying,
            # and losing the SUBSCRIPTION costs a drive to Craigmyle, while
            # losing one event costs nothing. The failure is loud in the
            # journal instead.
            sys.stderr.write("FAILED TO RECORD EVENT: %r\n" % (exc,))
        else:
            sys.stderr.write("recorded %d-byte event on %s\n" % (len(raw), self.path))
        self._reply(200, {"ok": True})


def main():
    port = int(os.environ.get("REBOOTER_RECEIVER_PORT", "8795"))
    Handler.state_dir = os.environ.get(
        "REBOOTER_RECEIVER_STATE", "/var/lib/rebooter-receiver")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    sys.stderr.write("rebooter-receiver listening on 127.0.0.1:%d\n" % port)
    server.serve_forever()


if __name__ == "__main__":
    main()
