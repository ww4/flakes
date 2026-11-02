"""homelab-heartbeat — sign and send one heartbeat to the external watcher.

Runs on gromit and on wallace, once a minute, from a systemd timer. Reports
"I am alive" and "here is whether I can see my peer", to the correlator on the
lock3 VPS.

⚠️ Everything happens in THIS process on purpose: read the key, probe the peer,
build the payload, sign it, POST it. The alternative — shelling out to openssl
and curl — would put the HMAC key in `openssl -macopt hexkey:...` and the
signature in `curl -H ...`, and both land in /proc/<pid>/cmdline, readable by
any local user. That is the same class of mistake as Uptime Kuma carrying its
push token in a URL path, which is the reason this service exists instead of
Kuma. So: no subprocess for anything that touches the secret.

Config via environment:
    WATCH_URL        e.g. https://watch.rosemaryacres.com/hb
    WATCH_HOST       this host's name as the watcher knows it (gromit|wallace)
    WATCH_KEY_FILE   path to the sops-decrypted key file
    WATCH_KEY_NAME   which entry in that file is ours (defaults to WATCH_HOST)
    WATCH_PEER       peer's name, used to label the observation (wallace|gromit)
    WATCH_PEER_ADDR  peer address to probe
    WATCH_PEER_PORT  peer TCP port to probe (default 22)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid

PEER_TIMEOUT_S = 4.0
POST_TIMEOUT_S = 15.0


def read_key(path: str, name: str) -> bytes:
    """Pull one hex key out of a `name: hex` or `name=hex` file.

    Accepts both shapes because the same values live in two places that format
    them differently: sops yaml here, and `name=hex` lines on lock3. One parser
    for both means the file can be copied either direction without edits.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()

    # sops-nix extracts a single named entry, so the deployed file is the BARE
    # hex value with no name. Handle that first.
    lone = raw.strip()
    if lone and all(c in "0123456789abcdefABCDEF" for c in lone):
        return bytes.fromhex(lone)

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in (":", "="):
            if sep in line:
                k, _, v = line.partition(sep)
                if k.strip() == name:
                    return bytes.fromhex(v.strip())
                break
    raise KeyError(f"no key named {name!r} in {path}")


def peer_is_reachable(addr: str, port: int) -> bool | None:
    """TCP connect to the peer. None means "not configured", not "down".

    Deliberately TCP rather than ICMP: ping needs CAP_NET_RAW, and a heartbeat
    sender should not hold a capability it can avoid. A refused connection still
    proves the host answered, so `False` here means genuinely unreachable rather
    than merely "that port is shut".
    """
    if not addr:
        return None
    try:
        with socket.create_connection((addr, port), timeout=PEER_TIMEOUT_S):
            return True
    except (socket.timeout, OSError) as exc:
        # A refusal came FROM the peer, so the peer is up.
        if isinstance(exc, ConnectionRefusedError):
            return True
        return False


def main() -> int:
    url = os.environ.get("WATCH_URL", "")
    me = os.environ.get("WATCH_HOST", "")
    key_file = os.environ.get("WATCH_KEY_FILE", "")
    key_name = os.environ.get("WATCH_KEY_NAME") or me
    peer = os.environ.get("WATCH_PEER", "")
    peer_addr = os.environ.get("WATCH_PEER_ADDR", "")
    try:
        peer_port = int(os.environ.get("WATCH_PEER_PORT", "22"))
    except ValueError:
        peer_port = 22

    if not url or not me or not key_file:
        print("homelab-heartbeat: WATCH_URL, WATCH_HOST and WATCH_KEY_FILE are required",
              file=sys.stderr)
        return 2
    try:
        key = read_key(key_file, key_name)
    except (OSError, KeyError, ValueError) as exc:
        # Fail LOUD and non-zero. A heartbeat that silently stops is
        # indistinguishable from the host being dead — which is exactly the
        # reading the watcher will take — so this must show up as a failed unit
        # here as well, or the cause is invisible on both ends.
        print(f"homelab-heartbeat: cannot read key: {exc}", file=sys.stderr)
        return 1
    if not key:
        print("homelab-heartbeat: key is empty", file=sys.stderr)
        return 1

    payload: dict[str, object] = {"ts": time.time(), "nonce": uuid.uuid4().hex}
    if peer:
        payload[f"sees_{peer}"] = peer_is_reachable(peer_addr, peer_port)

    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = base64.b64encode(hmac.new(key, body, hashlib.sha256).digest()).decode()

    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Watch-Host": me,
            "X-Watch-Signature": sig,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=POST_TIMEOUT_S) as resp:
            print(f"homelab-heartbeat: {me} -> {resp.status} "
                  f"(peer {peer or 'n/a'}: {payload.get(f'sees_{peer}', 'n/a')})")
            return 0
    except urllib.error.HTTPError as exc:
        # 401/409/400 are OUR bug, not an outage: a wrong key, a replayed nonce,
        # a skewed clock. Distinguished from a transport failure because the
        # remedies are completely different.
        print(f"homelab-heartbeat: rejected {exc.code} {exc.reason} — "
              f"check the key, the clock, and the nonce", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"homelab-heartbeat: could not reach the watcher: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
