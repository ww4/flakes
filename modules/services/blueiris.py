"""blueiris — CLI for Blue Iris NVRs over the JSON API.

Built 2026-09-08 for Craigmyle Tractor, whose NVR reaches gromit over Tailscale
(tag:cust-craigmyle). Designed to be multi-site from the start: each customer is
one env file in ~/.config/blueiris/<site>.env holding BI_URL/BI_USER/BI_PASSWORD,
and every command takes --site.

WHY THIS EXISTS. On 2026-09-08 the network said all 25 cameras were serving RTSP
while Chris was seeing cameras down, and answering "which does the NVR think are
offline, and why" required a two-hour drive. It is one API call.

⚠️ THE API DOES NOT EXPOSE CAMERA IP ADDRESSES. `camconfig` returns behaviour
only — alerts, motion, record, schedule — with no address or source-URL field.
So a Blue Iris name cannot be mapped to an IP from here. Two ways round it:
  - `snapshot` / `snapshot-all` pull a frame THROUGH the NVR, which identifies
    what a camera is looking at without needing to reach the camera at all.
    That is how you map a name to a physical location.
  - mapping a name to an ADDRESS needs something on the customer LAN (marcus on
    site, or 4via6 subnet routing), because gromit can currently reach the NVR
    and nothing behind it.

⚠️ AUTH IS LAN-DEPENDENT. From the customer LAN Blue Iris reports
"auth-exempt": true and lets clients in unauthenticated. Over Tailscale the
source is 100.x, which it does not treat as LAN, so it reports false and demands
the MD5 challenge. Never assume a working LAN test means the remote path works.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

CONF_DIR = os.environ.get("BLUEIRIS_CONF", os.path.expanduser("~/.config/blueiris"))


def load_site(site: str) -> dict:
    path = os.path.join(CONF_DIR, f"{site}.env")
    if not os.path.exists(path):
        avail = sorted(f[:-4] for f in os.listdir(CONF_DIR)
                       if f.endswith(".env")) if os.path.isdir(CONF_DIR) else []
        sys.exit(f"blueiris: no config for site '{site}' at {path}\n"
                 f"          known sites: {', '.join(avail) or '<none>'}")
    cfg = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    for k in ("BI_URL", "BI_USER", "BI_PASSWORD"):
        if not cfg.get(k):
            sys.exit(f"blueiris: {k} missing or empty in {path}")
    return cfg


class BI:
    def __init__(self, cfg: dict):
        self.url = cfg["BI_URL"].rstrip("/") + "/json"
        self.user, self.pw = cfg["BI_USER"], cfg["BI_PASSWORD"]
        self.session = None

    def _post(self, payload: dict, timeout: int = 30) -> dict:
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError) as exc:
            sys.exit(f"blueiris: {self.url} unreachable: {exc}")

    def login(self) -> None:
        first = self._post({"cmd": "login"})
        sid = first.get("session")
        if not sid:
            sys.exit("blueiris: server returned no session token")
        # response = md5("user:session:password"). Single pass, no realm —
        # this is NOT HTTP digest, despite looking like it.
        digest = hashlib.md5(f"{self.user}:{sid}:{self.pw}".encode()).hexdigest()
        second = self._post({"cmd": "login", "session": sid, "response": digest})
        if second.get("result") != "success":
            reason = (second.get("data") or {}).get("reason", "rejected")
            sys.exit(f"blueiris: login failed ({reason}). Check "
                     f"{CONF_DIR}/<site>.env")
        self.session = sid

    def cmd(self, name: str, timeout: int = 30, **kw) -> dict:
        return self._post({"cmd": name, "session": self.session, **kw}, timeout)

    def cameras(self) -> list[dict]:
        data = self.cmd("camlist", timeout=45).get("data") or []
        # Drop group/cycle pseudo-entries: they have no device behind them and
        # report isOnline=None, which would otherwise pollute every count.
        return [c for c in data
                if not c.get("group") and c.get("optionValue")
                and not str(c.get("optionValue", "")).startswith("@")]


def fmt_cams(cams: list[dict], only_bad: bool) -> int:
    rows = [c for c in cams if (not only_bad or c.get("isOnline") is not True)]
    if not rows:
        print("  all cameras online")
        return 0
    print(f"  {'CAMERA':<24} {'SHORT':<8} {'ONLINE':<7} {'FPS':<7} ERROR")
    for c in sorted(rows, key=lambda x: (x.get("optionDisplay") or "")):
        print(f"  {str(c.get('optionDisplay') or '?')[:24]:<24} "
              f"{str(c.get('optionValue') or '')[:8]:<8} "
              f"{str(c.get('isOnline')):<7} {str(c.get('FPS', '')):<7} "
              f"{(c.get('error') or '').strip()[:44]}")
    return len(rows)


def main() -> int:
    p = argparse.ArgumentParser(prog="blueiris", description=__doc__.split("\n")[0])
    p.add_argument("--site", default="craigmyle", help="config in ~/.config/blueiris/<site>.env")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cams", help="all cameras with health")
    sub.add_parser("offline", help="only cameras the NVR considers down")
    sub.add_parser("status", help="NVR system status")
    lg = sub.add_parser("log", help="NVR system log")
    lg.add_argument("-n", type=int, default=25)
    sn = sub.add_parser("snapshot", help="pull one frame THROUGH the NVR")
    sn.add_argument("camera")
    sn.add_argument("-o", "--out", default="")
    sa = sub.add_parser("snapshot-all", help="a frame from every camera")
    sa.add_argument("-d", "--dir", default="./bi-snapshots")
    args = p.parse_args()

    bi = BI(load_site(args.site))
    bi.login()

    if args.cmd in ("cams", "offline"):
        cams = bi.cameras()
        n = fmt_cams(cams, args.cmd == "offline")
        online = sum(1 for c in cams if c.get("isOnline") is True)
        print(f"\n  {online}/{len(cams)} online" +
              (f"  ({n} needing attention)" if args.cmd == "offline" and n else ""))
        return 0

    if args.cmd == "status":
        d = bi.cmd("status").get("data") or {}
        for k in sorted(d):
            if not isinstance(d[k], (dict, list)):
                print(f"  {k:<18} {d[k]}")
        return 0

    if args.cmd == "log":
        for e in (bi.cmd("log").get("data") or [])[-args.n:]:
            print(f"  {e.get('date', '')}  {str(e.get('level', '')):<3} {e.get('msg', '')[:110]}")
        return 0

    # Snapshots go through the NVR's own image endpoint, authenticated by the
    # session cookie — so they work even when the cameras themselves are
    # unreachable from here, which is the normal case without subnet routing.
    base = bi.url[: -len("/json")]
    if args.cmd == "snapshot":
        out = args.out or f"./{args.camera}.jpg"
        _fetch_image(base, bi.session, args.camera, out)
        return 0

    if args.cmd == "snapshot-all":
        os.makedirs(args.dir, exist_ok=True)
        ok = 0
        for c in bi.cameras():
            short = c["optionValue"]
            name = (c.get("optionDisplay") or short).replace("/", "-")
            if _fetch_image(base, bi.session, short,
                            os.path.join(args.dir, f"{name}.jpg")):
                ok += 1
        print(f"\n  {ok} snapshot(s) in {args.dir}")
        print("  Open them: the picture identifies WHICH physical camera each")
        print("  name is, which no network scan can tell you.")
        return 0
    return 0


def _fetch_image(base: str, session: str, short: str, out: str) -> bool:
    url = f"{base}/image/{short}?session={session}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            body = r.read()
    except (urllib.error.URLError, OSError) as exc:
        print(f"  {short:<10} FAILED: {exc}")
        return False
    if not body.startswith(b"\xff\xd8"):
        print(f"  {short:<10} not a JPEG ({len(body)} bytes) — camera offline?")
        return False
    with open(out, "wb") as fh:
        fh.write(body)
    print(f"  {short:<10} -> {out} ({len(body) // 1024} KB)")
    return True


if __name__ == "__main__":
    sys.exit(main())
