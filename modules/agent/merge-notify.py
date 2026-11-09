"""merge-notify — turn Forgejo `pull_request` webhooks into one log line each.

THE PROBLEM. An agent pane only learns a PR merged by going to look, and it
mostly does not: on 2026-11-09 three flakes PRs (#489, #490, #491) were merged
while a session still believed #489 was open, and the usual outcome is work
piled onto a branch that then needs rebasing. Chris asked for the opposite
direction — the forge tells us.

THE SHAPE. One org-level Forgejo webhook (every ww4 repo, now and future) posts
to this loopback-only receiver, which appends one line per merge to a log. A
pane streams that log with `merge-watch`, so the event arrives in the
conversation rather than being discovered later. The log is also the history:
a session starting cold reads the tail and knows what it missed.

⚠️ EVERY LINE IS A WAKE-UP, NOT A FACT. PR titles and branch names are free
text that ends up inside a model's context, so nothing crosses this boundary
unsanitised: each field is either a validated token (repo, number, sha, login,
branch) or a charset-filtered, newline-stripped, length-capped title. Act on a
line by querying the forge or git — never by trusting what the line says.
"""
import hashlib
import hmac
import json
import os
import re
import signal
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional

LOG_PATH = os.environ.get("MERGE_LOG", "/var/lib/merge-notify/merges.log")
SECRET_PATH = os.environ.get(
    "MERGE_SECRET_FILE", "/var/lib/merge-notify/hook-secret.txt")
PORT = int(os.environ.get("MERGE_PORT", "8794"))
MAX_BODY = 1 << 20

# Validated shapes. A field that does not match is dropped, not coerced.
RE_REPO = re.compile(r"^[A-Za-z0-9._-]{1,64}/[A-Za-z0-9._-]{1,100}$")
RE_REF = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
RE_LOGIN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
RE_SHA = re.compile(r"^[0-9a-f]{7,64}$")
# The title is prose, so it gets a charset filter instead of a shape. No
# double quote and no backslash: the line wraps the title in quotes, and a
# title that can close them is a title that can forge a field.
RE_TITLE_UNSAFE = re.compile(r"[^A-Za-z0-9 ._:;/#@+()&'!?,=*-]")
TITLE_MAX = 120


def secret() -> bytes:
    with open(SECRET_PATH, "rb") as fh:
        return fh.read().strip()


def signed(body: bytes, header: Optional[str]) -> bool:
    """Forgejo signs the raw body with the hook secret (hex sha256 HMAC)."""
    if not header:
        return False
    want = hmac.new(secret(), body, hashlib.sha256).hexdigest()
    got = header.strip()
    if got.startswith("sha256="):
        got = got[len("sha256="):]
    return hmac.compare_digest(want, got)


def token(value: Any, pattern: re.Pattern) -> str:
    text = value if isinstance(value, str) else ""
    return text if pattern.match(text) else "?"


def title(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = RE_TITLE_UNSAFE.sub("", text)
    return text[:TITLE_MAX].strip()


def line_for(payload: dict) -> Optional[str]:
    """One line, or None for an event we do not report."""
    if payload.get("action") != "closed":
        return None
    pr = payload.get("pull_request")
    if not isinstance(pr, dict):
        return None
    number = pr.get("number")
    if not isinstance(number, int):
        return None

    repo = payload.get("repository")
    repo_name = token(
        (repo or {}).get("full_name") if isinstance(repo, dict) else "",
        RE_REPO)
    merged = pr.get("merged") is True
    verb = "MERGED" if merged else "CLOSED-UNMERGED"

    base = (pr.get("base") or {}) if isinstance(pr.get("base"), dict) else {}
    head = (pr.get("head") or {}) if isinstance(pr.get("head"), dict) else {}
    who = pr.get("merged_by") if merged else payload.get("sender")
    who_login = token(
        (who or {}).get("login") if isinstance(who, dict) else "", RE_LOGIN)
    sha = token(pr.get("merge_commit_sha"), RE_SHA)[:12]

    stamp = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    return (
        f'{stamp} {verb} {repo_name}#{number} '
        f'{token(head.get("ref"), RE_REF)}->{token(base.get("ref"), RE_REF)} '
        f'by={who_login} sha={sha} "{title(pr.get("title"))}"'
    )


def append(line: str) -> None:
    # Line-at-a-time, flushed and fsynced: a `tail -F` consumer must see the
    # event now, and a crash must not lose it.
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


class Handler(BaseHTTPRequestHandler):
    server_version = "merge-notify/1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s\n" % (fmt % args))
        sys.stderr.flush()

    def _reply(self, code: int, text: str) -> None:
        body = (text + "\n").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 (http.server's interface)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._reply(400, "bad length")
            return
        if length <= 0 or length > MAX_BODY:
            self._reply(413, "bad body size")
            return
        body = self.rfile.read(length)

        # ⚠️ Loopback-only is not authentication: any local user could POST a
        # forged merge. The HMAC is what makes a line trustworthy as an EVENT
        # (it still is not trustworthy as a fact — see the module docstring).
        sig = (self.headers.get("X-Gitea-Signature")
               or self.headers.get("X-Forgejo-Signature")
               or self.headers.get("X-Hub-Signature-256"))
        try:
            ok = signed(body, sig)
        except OSError as exc:
            self.log_message("secret unreadable: %s", exc)
            self._reply(500, "no secret")
            return
        if not ok:
            self.log_message("rejected: bad or missing signature")
            self._reply(401, "bad signature")
            return

        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            self._reply(400, "bad json")
            return
        if not isinstance(payload, dict):
            self._reply(400, "bad json")
            return

        line = line_for(payload)
        if line is None:
            self._reply(200, "ignored")
            return
        append(line)
        self.log_message("%s", line)
        self._reply(200, "ok")

    def do_GET(self) -> None:  # noqa: N802
        # A liveness probe that reveals nothing.
        self._reply(200, "merge-notify")


def main() -> int:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    httpd = HTTPServer(("127.0.0.1", PORT), Handler)
    sys.stderr.write("merge-notify listening on 127.0.0.1:%d -> %s\n"
                     % (PORT, LOG_PATH))
    sys.stderr.flush()
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
