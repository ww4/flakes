"""The grading endpoint, as a tiny loopback HTTP service.

WHY THIS EXISTS AS A SERVICE AND NOT AS AN nginx access_log
-----------------------------------------------------------
The first design had nginx answer `/news/g` with `return 204` and record the
click by writing a custom-format access_log into /var/lib/newsdesk/grades. It
was appealing because it added no moving parts. It also took the entire box
offline on 2026-08-20, and would have done so twice more:

  1. the custom `log_format` came from appendHttpConfig, which renders AFTER
     the server blocks that reference it -> `unknown log format`;
  2. the tmpfiles rule creating that directory named a group that does not
     exist, so the directory was never created -> nginx `[emerg] open() ...
     (2: No such file or directory)`;
  3. and even with both fixed, nginx runs `ProtectSystem=strict` with an empty
     `ReadWritePaths`, so /var/lib/newsdesk is read-only inside its mount
     namespace -> `(30: Read-only file system)`.

Every one of those is fatal at CONFIG-PARSE time, which means nginx refuses to
start at all and every vhost on the host goes down — Forgejo, Jellyfin,
Vaultwarden, ntfy, the lot. That is a preposterous blast radius for a
thumbs-up button.

So the button now has its own process. nginx only proxies to 127.0.0.1, which
it can always do, and the worst case for a bug in here is that one link 502s.
The failure is confined to the feature that owns it.

Deliberately minimal: no framework, no auth, no state beyond the database, and
it binds the loopback interface only. The vhost already sits behind the
tailnet source gate.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .db import connect, now

VALUES = {"up": 1, "down": -1}


class GradeHandler(BaseHTTPRequestHandler):
    server_version = "newsdesk-grade/1.0"
    db_path = None

    def log_message(self, fmt, *args):  # noqa: A002 - stdlib signature
        # journald already timestamps; keep one terse line per request.
        sys.stderr.write("newsdesk-grade: " + (fmt % args) + "\n")

    def _respond(self, code: int) -> None:
        self.send_response(code)
        self.send_header("Content-Length", "0")
        # A grade is a state change; nothing about it should be cached or
        # re-issued from a history navigation.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        url = urlparse(self.path)
        if url.path.rstrip("/") not in ("/g", "/news/g"):
            self._respond(404)
            return
        q = parse_qs(url.query)
        raw_id = (q.get("i") or [""])[0]
        value = VALUES.get((q.get("v") or [""])[0].lower())
        if not raw_id.isdigit() or value is None:
            self._respond(400)
            return
        try:
            con = connect(self.db_path)
        except sqlite3.Error:
            self._respond(503)
            return
        try:
            if con.execute("SELECT 1 FROM items WHERE id=?",
                           (int(raw_id),)).fetchone() is None:
                self._respond(404)
                return
            con.execute(
                "INSERT INTO grades (item_id, via, value, at) VALUES (?,'web',?,?)"
                " ON CONFLICT(item_id, via) DO UPDATE SET"
                " value=excluded.value, at=excluded.at",
                (int(raw_id), value, now()))
            con.commit()
        except sqlite3.Error as e:  # noqa: BLE001
            sys.stderr.write(f"newsdesk-grade: {type(e).__name__}: {e}\n")
            self._respond(503)
            return
        finally:
            con.close()
        self._respond(204)


def serve(host: str = "127.0.0.1", port: int = 8123, db_path=None) -> int:
    GradeHandler.db_path = db_path
    # Touch the schema once at startup so the first click is not the thing
    # that discovers the database is unusable.
    connect(db_path).close()
    httpd = ThreadingHTTPServer((host, port), GradeHandler)
    httpd.daemon_threads = True
    sys.stderr.write(f"newsdesk-grade: listening on {host}:{port}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(serve(port=int(os.environ.get("NEWSDESK_GRADE_PORT", "8123"))))
