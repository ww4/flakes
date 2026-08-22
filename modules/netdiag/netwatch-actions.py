"""netwatch-actions — the endpoint behind the ntfy notification buttons.

A NEW DEVICE notification used to end with a shell command, which is useless on
a phone: Chris could read it but not run it. This turns the notification into a
control surface with two buttons.

  Accept      — add the device to the baseline. One tap, done.
  Investigate — fingerprint it (ports, vendor, hostname, HTTP identity) and post
                the findings back as a notification. This is the "diagnose it"
                step Chris asked for, available at the moment he sees the alert
                rather than whenever he next gets to a terminal.

SECURITY. Each button carries a single-use NONCE minted when the alert is
raised, bound to one MAC and one verb, expiring in 7 days. There is no bearer
token and no general "accept any device" verb, deliberately:

  - a static token would be readable by anything that can see the ntfy topic,
    and it would grant accept-anything forever.
  - an unauthenticated endpoint would let an intruder ACCEPT ITSELF into the
    baseline and silence the very alert that spotted it. That is the whole
    guard dog defeated by its own convenience feature.

So a leaked nonce authorises exactly the one action the notification already
offered, once. Binding is to the MAC, not the IP, since the IP can change.

Listens on loopback only; nginx proxies it under the ntfy vhost, which inherits
the LAN/Tailscale source gate from nginx-access.nix.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

STATE_DIR = os.environ.get("NETWATCH_STATE", "/var/lib/netwatch")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
NTFY = os.environ.get("NETWATCH_NTFY", "http://127.0.0.1:8090/gromit-alerts")
PORT = int(os.environ.get("NETWATCH_ACTION_PORT", "8799"))
PREFIX = "/netwatch-action/"


def load() -> dict:
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def notify(title: str, body: str) -> None:
    import urllib.request
    req = urllib.request.Request(
        NTFY, data=body.encode(),
        headers={"Title": title[:200], "Priority": "default", "Tags": "dog"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except OSError as exc:
        print(f"netwatch-actions: ntfy post failed: {exc!r}", file=sys.stderr)


def do_accept(state: dict, mac: str) -> str:
    rec = state.get("devices", {}).get(mac)
    if rec is None:
        return f"{mac} is no longer in the baseline — nothing to accept."
    rec["accepted"] = True
    if not rec.get("label"):
        rec["label"] = rec.get("hostname") or rec.get("vendor") or "accepted from phone"
    return f"Accepted {mac} ({rec.get('label')}) at {rec.get('ip')}."


def do_investigate(state: dict, mac: str) -> str:
    rec = state.get("devices", {}).get(mac) or {}
    ip = rec.get("ip")
    if not ip:
        return f"No current address known for {mac}; cannot fingerprint it."
    try:
        out = subprocess.run(["netdiag", "identify", ip], capture_output=True,
                             text=True, timeout=300).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        # The return value becomes the HTTP response body — a toast the phone
        # shows for two seconds, if at all. A failed investigation must also
        # arrive as a real notification, or the button fails silently (which
        # is exactly how the missing-netdiag bug stayed invisible).
        msg = f"Fingerprint of {ip} failed: {exc!r}"
        notify("netwatch: investigate FAILED", msg)
        return msg
    path = os.path.join(STATE_DIR, f"investigate-{mac.replace(':', '')}.txt")
    with open(path, "w") as fh:
        fh.write(out)
    # Post the useful part back so it is readable on the phone without SSH.
    keep = [ln for ln in out.splitlines()
            if ln.strip() and not ln.startswith("===")][:14]
    notify(f"netwatch: investigated {rec.get('hostname') or mac}",
           "\n".join(keep) + f"\n\nFull output: {path}")
    return f"Investigated {ip}; findings sent as a notification."


class Handler(BaseHTTPRequestHandler):
    server_version = "netwatch-actions"

    def _reply(self, code: int, text: str) -> None:
        body = (text + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self) -> None:
        path = self.path.split("?", 1)[0]
        if not path.startswith(PREFIX):
            return self._reply(404, "not found")
        parts = [p for p in path[len(PREFIX):].split("/") if p]
        if len(parts) != 2:
            return self._reply(400, "expected /<nonce>/<verb>")
        nonce, verb = parts

        state = load()
        actions = state.get("actions", {})
        entry = actions.get(nonce)
        if entry is None:
            # Already used, or never existed. Same answer either way — do not
            # leak which, and do not let a button be replayed.
            return self._reply(410, "This button has already been used, or expired.")
        try:
            expired = datetime.fromisoformat(entry["exp"]) < datetime.now(timezone.utc)
        except (KeyError, ValueError):
            expired = True
        if expired:
            actions.pop(nonce, None)
            save(state)
            return self._reply(410, "This button has expired.")
        if entry.get("verb") != verb:
            # Refuse WITHOUT burning it. Deleting on a mismatch would let any
            # stray probe of /<nonce>/<wrong-verb> destroy the real button
            # before Chris could tap it — a denial-of-action, and one that
            # would look like the feature simply not working.
            return self._reply(400, "That button does not match this action.")

        mac = entry["mac"]
        if verb == "accept":
            msg = do_accept(state, mac)
        elif verb == "investigate":
            msg = do_investigate(state, mac)
        else:
            return self._reply(400, "unknown action")

        # Single use: burn the nonce, and burn its sibling for the same MAC so a
        # handled device cannot be actioned twice from a stale notification.
        for k in [k for k, v in actions.items() if v.get("mac") == mac]:
            actions.pop(k, None)
        save(state)
        print(f"netwatch-actions: {verb} {mac} -> {msg}")
        self._reply(200, msg)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, fmt: str, *args) -> None:
        print("netwatch-actions: " + (fmt % args), file=sys.stderr)


def main() -> int:
    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"netwatch-actions: listening on 127.0.0.1:{PORT}{PREFIX}")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
