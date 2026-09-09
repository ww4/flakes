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
import re
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


# ---------------------------------------------------------------- alerting
#
# Camera-down alerting, reusing the discipline netwatch arrived at the hard way:
#   - state CHANGE only. A camera that has been down for a week is not news
#     every 10 minutes; it was news once.
#   - NOTHING here may pierce quiet hours. A dead camera is not a fire. Chris's
#     rule (2026-08-19) covers "all classes of network traffic", and this is one
#     of them — findings raised 22:00-07:00 are HELD and delivered after 07:00.
#   - an API failure is an ERROR, never an all-clear. "0 cameras returned" and
#     "all cameras fine" must never render the same.
#
# MUTE exists because cameras cannot always be replaced immediately, and a
# permanently-red indicator is how an alert channel dies. Mutes EXPIRE by
# default (30 days) — a mute with no end date is how you forget a camera has
# been dead for a year — and a muted camera that COMES BACK auto-unmutes, so a
# stale mute cannot hide the next outage.

STATE_DIR = os.environ.get("BLUEIRIS_STATE", "/var/lib/blueiris")
NTFY = os.environ.get("BLUEIRIS_NTFY", "http://127.0.0.1:8090/gromit-alerts")


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _quiet_hours() -> bool:
    from datetime import datetime
    h = datetime.now().hour
    return h >= 22 or h < 7


def _state_path(site: str) -> str:
    return os.path.join(STATE_DIR, f"{site}.json")


def load_state(site: str) -> dict:
    try:
        with open(_state_path(site)) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"seeded": None, "cameras": {}, "muted": {}, "held": []}


def save_state(site: str, st: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = _state_path(site) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=2, sort_keys=True)
    os.replace(tmp, _state_path(site))


def _ascii_header(value: str) -> str:
    """HTTP headers are latin-1. Titles are prose, so they are not.

    Found the hard way 2026-09-08: the em dash in "CAMERA DOWN - Front Entrance"
    raised UnicodeEncodeError inside http.client, which (a) lost the alert and
    (b) killed the whole watch run before it could save state. Transliterate the
    typography we actually use rather than deleting it, so the title reads
    "CAMERA DOWN - Front Entrance" and not "CAMERA DOWN  Front Entrance".
    """
    for src, dst in (("—", "-"), ("–", "-"), ("→", "->"),
                     ("’", "'"), ("“", '"'), ("”", '"'),
                     ("…", "..."), ("⚠", "!")):
        value = value.replace(src, dst)
    return re.sub(r"[^\x20-\x7e]", "", value)[:200]


def _post_ntfy(title: str, body: str, priority: str, tags: str) -> None:
    if _quiet_hours():
        priority = "low"
    req = urllib.request.Request(
        NTFY, data=body.encode(),
        headers={"Title": _ascii_header(title), "Priority": priority,
                 "Tags": tags})
    # Deliberately broad. Failing to SEND an alert must never abort the run that
    # DETECTS the outage: losing one notification is a nuisance, but losing the
    # state write means the mute list never persists and the next poll
    # re-detects everything from scratch. Log it and carry on.
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:  # noqa: E722 - see comment above
        print(f"  ntfy post failed: {exc!r}", file=sys.stderr)


def emit(st: dict, title: str, body: str, priority="default", tags="camera"):
    """Send now, or hold until morning. Never wakes anyone."""
    if _quiet_hours():
        st.setdefault("held", []).append(
            {"at": _now(), "title": title, "body": body,
             "priority": priority, "tags": tags})
        print(f"  held until morning: {title}")
        return
    _post_ntfy(title, body, priority, tags)
    print(f"  alerted: {title}")


def flush_held(st: dict) -> None:
    held = st.get("held") or []
    if not held or _quiet_hours():
        return
    if len(held) <= 5:
        for h in held:
            _post_ntfy(h["title"], f"[held from {h['at'][11:16]}Z overnight]\n" + h["body"],
                       h.get("priority", "default"), h.get("tags", "camera"))
    else:
        lines = [f"- {h['at'][11:16]}Z  {h['title']}" for h in held]
        _post_ntfy(f"blueiris: {len(held)} overnight finding(s)",
                   "Held through quiet hours:\n" + "\n".join(lines),
                   "default", "camera")
    st["held"] = []


def mute_active(st: dict, short: str) -> tuple[bool, str]:
    m = (st.get("muted") or {}).get(short)
    if not m:
        return False, ""
    until = m.get("until")
    if until:
        from datetime import datetime, timezone
        try:
            if datetime.fromisoformat(until) < datetime.now(timezone.utc):
                st["muted"].pop(short, None)      # expired -> alerting resumes
                return False, "mute expired"
        except ValueError:
            pass
    return True, m.get("reason", "")


def cmd_watch(bi: "BI", site: str) -> int:
    st = load_state(site)
    flush_held(st)
    cams = bi.cameras()

    # An empty list is a BROKEN CHECK, not a quiet estate.
    if not cams:
        emit(st, "blueiris: camlist returned NOTHING",
             f"The NVR answered but listed zero cameras ({site}). This is a "
             f"broken check, not an all-clear — nothing is being watched.",
             "high", "warning")
        save_state(site, st)
        return 1

    known = st.setdefault("cameras", {})
    seeding = st.get("seeded") is None
    went_down, came_back = [], []

    for c in cams:
        short = c["optionValue"]
        name = c.get("optionDisplay") or short
        online = c.get("isOnline") is True
        err = (c.get("error") or "").strip()
        prev = known.get(short)
        known[short] = {"name": name, "online": online, "last": _now(),
                        "error": err}
        if seeding or prev is None:
            continue
        if prev.get("online") and not online:
            went_down.append((name, short, err))
        elif not prev.get("online") and online:
            came_back.append((name, short))

    if seeding:
        st["seeded"] = _now()
        off = [c for c in cams if c.get("isOnline") is not True]
        emit(st, f"blueiris: watching {site}",
             f"{len(cams)} cameras recorded, {len(off)} already offline "
             f"(not alerted — baseline). From now on any change is reported.\n"
             + "\n".join(
                 f"  {c.get('optionDisplay')}: {(c.get('error') or '').strip()}"
                 for c in off),
             "default", "camera")

    for name, short, err in went_down:
        muted, reason = mute_active(st, short)
        if muted:
            print(f"  {name} went down but is MUTED ({reason}) — not alerting")
            continue
        emit(st, f"blueiris: CAMERA DOWN — {name}",
             f"{name} ({short}) at {site} stopped responding.\n"
             f"error: {err or '<none reported>'}\n\n"
             f"If it cannot be fixed soon:  blueiris mute {short} --days 30 "
             f"\"awaiting replacement\"",
             "high", "camera")

    for name, short in came_back:
        # A recovered camera auto-unmutes: a stale mute must not hide the NEXT
        # outage on a camera that has since been repaired.
        if (st.get("muted") or {}).pop(short, None):
            print(f"  {name} recovered — mute cleared automatically")
        emit(st, f"blueiris: camera recovered — {name}",
             f"{name} ({short}) at {site} is online again.",
             "default", "white_check_mark")

    save_state(site, st)
    online = sum(1 for c in cams if c.get("isOnline") is True)
    muted_n = len(st.get("muted") or {})
    print(f"  {online}/{len(cams)} online, {len(went_down)} newly down, "
          f"{len(came_back)} recovered, {muted_n} muted")
    return 0


def cmd_mute(site: str, short: str, days: int, forever: bool, reason: str) -> int:
    st = load_state(site)
    until = None
    if not forever:
        from datetime import datetime, timedelta, timezone
        until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(
            timespec="seconds")
    st.setdefault("muted", {})[short] = {"until": until, "reason": reason,
                                         "set": _now()}
    save_state(site, st)
    when = "indefinitely (no expiry — you will not be reminded)" if forever \
        else f"for {days} days (until {until[:10]})"
    print(f"  muted {short} {when}" + (f" — {reason}" if reason else ""))
    return 0


def cmd_unmute(site: str, short: str) -> int:
    st = load_state(site)
    if (st.get("muted") or {}).pop(short, None) is None:
        print(f"  {short} was not muted")
        return 1
    save_state(site, st)
    print(f"  unmuted {short} — it will alert again on the next change")
    return 0


def cmd_muted(site: str, as_json: bool = False) -> int:
    st = load_state(site)
    m = st.get("muted") or {}
    if as_json:
        print(json.dumps({"site": site, "muted": m}, indent=1, sort_keys=True))
        return 0
    if not m:
        print("  nothing muted")
        return 0
    print(f"  {'CAMERA':<10} {'UNTIL':<12} REASON")
    for short, d in sorted(m.items()):
        u = (d.get("until") or "")[:10] or "FOREVER"
        print(f"  {short:<10} {u:<12} {d.get('reason', '')}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="blueiris", description=__doc__.split("\n")[0])
    p.add_argument("--site", default="craigmyle", help="config in ~/.config/blueiris/<site>.env")
    # Machine-readable output for `cams`, `offline` and `muted`. Exists so the
    # MCP layer consumes STRUCTURED data instead of scraping these columns —
    # a report format is not an API, and column-scraping breaks the first time
    # a camera name gets long enough to truncate.
    p.add_argument("--json", action="store_true",
                   help="emit JSON (cams/offline/muted) for machine consumers")
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
    sub.add_parser("watch", help="alert on camera state CHANGES (for the timer)")
    mu = sub.add_parser("mute", help="stop alerting on a camera that cannot be fixed yet")
    mu.add_argument("camera")
    mu.add_argument("--days", type=int, default=30)
    mu.add_argument("--forever", action="store_true")
    mu.add_argument("reason", nargs="?", default="")
    um = sub.add_parser("unmute", help="resume alerting on a camera")
    um.add_argument("camera")
    sub.add_parser("muted", help="list muted cameras and when they expire")
    args = p.parse_args()

    # These are local state only — no point authenticating to the NVR, and
    # muting must keep working when the NVR is the thing that is unreachable.
    if args.cmd == "mute":
        return cmd_mute(args.site, args.camera, args.days, args.forever, args.reason)
    if args.cmd == "unmute":
        return cmd_unmute(args.site, args.camera)
    if args.cmd == "muted":
        return cmd_muted(args.site, args.json)

    bi = BI(load_site(args.site))
    bi.login()

    if args.cmd == "watch":
        return cmd_watch(bi, args.site)

    if args.cmd in ("cams", "offline"):
        cams = bi.cameras()
        if args.json:
            rows = [c for c in cams
                    if args.cmd != "offline" or c.get("isOnline") is not True]
            print(json.dumps({
                "site": args.site,
                "total": len(cams),
                "online": sum(1 for c in cams if c.get("isOnline") is True),
                "cameras": [{"name": c.get("optionDisplay"),
                             "short": c.get("optionValue"),
                             "online": c.get("isOnline"),
                             "fps": c.get("FPS"),
                             "error": (c.get("error") or "").strip()}
                            for c in sorted(
                                rows, key=lambda x: (x.get("optionDisplay") or ""))],
            }, indent=1))
            return 0
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
