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
# been dead for a year — and a muted camera that comes back and STAYS back
# auto-unmutes, so a stale mute cannot hide the next outage.
#
# "Stays back" is load-bearing: see clear_settled_mutes(). Clearing on the first
# recovery looks equivalent and is not, because a FLAPPING camera recovers every
# few minutes and would clear its own mute immediately.
#
# THREE fault shapes, three responses — see the flapping section below:
#   down        -> alert once, alert again when it returns
#   flapping    -> alert ONCE as flapping; individual up/down suppressed
#   unreachable -> an ERROR; never rendered as an all-clear

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


# ------------------------------------------------------------------ flapping
#
# A camera that drops and comes back every few minutes is a DIFFERENT fault from
# a camera that is down, and the difference matters twice over.
#
# It is invisible to a 10-minute poll. Found 2026-09-27 while diagnosing the
# Craigmyle alarms: the Rita's group dropped ~60 times in a day and the watch
# alerted THREE times, because only the drops straddling a poll are ever seen.
# Sampling a fast signal slowly does not just under-report it — it reports a
# random subset, which is what "alarms going off and on" actually was.
#
# So flapping is measured from the NVR'S OWN counters instead of from our
# samples. Blue Iris coalesces repeated log lines into one row and keeps a
# repeat count, so `sum(count)` over recent "Signal: restored" rows is the true
# drop count, at the NVR's resolution rather than ours.
#
# And it is reported ONCE, as flapping — not as an alternating stream of
# down/recovered pairs. A flapping camera that pages twice an hour trains you to
# ignore the channel, which is the failure mode this whole tool exists to avoid.

FLAP_WINDOW_H = 24      # look back this far
FLAP_ENTER = 6          # drops in the window to call it flapping
FLAP_EXIT = 2           # drops in the window to call it stable again
_RESTORED = "signal: restored"


def _logtime(e: dict) -> str:
    d = e.get("date")
    if isinstance(d, (int, float)):
        from datetime import datetime
        return datetime.fromtimestamp(d).strftime("%m-%d %H:%M:%S")
    return str(d or "")


def log_rows(bi: "BI") -> list:
    """NVR log, NEWEST FIRST.

    The API already returns newest-first, which the original `log -n` got
    backwards: it sliced `[-n:]` and so printed the OLDEST n entries every time
    (fixed 2026-09-27, after it hid the very events being investigated). Sorted
    explicitly here rather than trusting the order, so the contract is ours.
    """
    rows = bi.cmd("log").get("data") or []
    return sorted(rows, key=lambda e: e.get("date") or 0, reverse=True)


def _repeats(e: dict) -> int:
    """The coalesced repeat count on a log row, defaulting to a single event."""
    c = str(e.get("count") or 1)
    return int(c) if c.isdigit() and int(c) > 0 else 1


def restored_totals(rows: list) -> dict:
    """Total 'Signal: restored' events per camera currently visible in the log.

    Counting RESTORES, not "network retry", is deliberate and is the
    discriminator between the two fault shapes. A restore only happens on a
    completed drop->recovery cycle, which is exactly what flapping is. A camera
    that is simply DOWN retries forever and never restores, so it can never be
    misfiled as flapping — Cam26 at Craigmyle sits on 57,000+ retries and must
    keep reading as one dead camera, not an emergency.
    """
    out: dict = {}
    for e in rows:
        if _RESTORED not in str(e.get("msg", "")).lower():
            continue
        obj = str(e.get("obj") or "").strip()
        if obj:
            out[obj] = out.get(obj, 0) + _repeats(e)
    return out


def record_drops(st: dict, rows: list, window_h: int = FLAP_WINDOW_H) -> dict:
    """Roll the drop ledger forward from this poll. Returns drops per camera.

    Counted as DELTAS held in our own state rather than by summing a window of
    the NVR's log, because that log is not a stable substrate: Blue Iris
    rewrites a row in place (bumping `count` and moving `date` forward) and
    ages rows out unpredictably. Observed 2026-09-27 — a row timestamped 12:15
    with count=65 had vanished entirely twenty minutes later while rows from
    09-18 survived. Any "sum the last 24h of rows" reading is therefore
    unstable, and two runs minutes apart genuinely disagreed.

    A delta ledger is immune to all of that: if a counter rises we log the
    increase; if it falls (rows aged out) we log nothing and re-baseline.
    """
    import time
    now = time.time()
    prev = st.setdefault("flapseen", {})
    ledger = st.setdefault("flapledger", {})
    totals = restored_totals(rows)
    seeding = not prev

    for obj, total in totals.items():
        before = prev.get(obj)
        prev[obj] = total
        if seeding or before is None:
            continue                      # first sight: baseline, never a burst
        delta = total - before
        if delta > 0:
            ledger.setdefault(obj, []).append([now, delta])

    cutoff = now - window_h * 3600
    out: dict = {}
    for obj, events in list(ledger.items()):
        keep = [ev for ev in events if ev[0] >= cutoff]
        if keep:
            ledger[obj] = keep
            out[obj] = sum(int(n) for _, n in keep)
        else:
            ledger.pop(obj, None)
    return out


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


def flap_transitions(st: dict, cams: list, drops: dict) -> set:
    """Update flap state, alert on the EDGES, return who is flapping now.

    Hysteresis on purpose: enter at FLAP_ENTER drops, leave only at FLAP_EXIT.
    A single threshold would have a camera sitting near it toggle
    flapping/stable and produce exactly the alarm storm this replaces.
    """
    names = {c["optionValue"]: (c.get("optionDisplay") or c["optionValue"])
             for c in cams}
    flaps = st.setdefault("flapping", {})
    now_flapping = set()

    for short, name in names.items():
        n = drops.get(short, 0)
        was = short in flaps
        if n >= FLAP_ENTER and not was:
            flaps[short] = {"since": _now(), "drops": n}
            emit(st, f"blueiris: CAMERA FLAPPING — {name}",
                 f"{name} ({short}) has dropped and recovered {n} times in the "
                 f"last {FLAP_WINDOW_H}h, per the NVR's own log.\n\n"
                 f"This is a link/power fault, not a dead camera — it keeps "
                 f"coming back. Individual up/down alerts for it are suppressed "
                 f"while this lasts; you get one more when it settles.\n\n"
                 f"If several cameras flap together they share an upstream — "
                 f"look at that switch or run, not the cameras.\n\n"
                 f"To silence it while it waits on a fix:  "
                 f"blueiris mute {short} --days 7 \"flapping, awaiting repair\"",
                 "high", "warning")
        elif was and n <= FLAP_EXIT:
            since = flaps.pop(short, {}).get("since", "?")
            emit(st, f"blueiris: camera settled — {name}",
                 f"{name} ({short}) has stopped flapping "
                 f"({n} drops in the last {FLAP_WINDOW_H}h).\n"
                 f"It had been flapping since {since}.",
                 "default", "white_check_mark")
        elif was:
            flaps[short]["drops"] = n          # still flapping; refresh count
            now_flapping.add(short)
        if short in flaps:
            now_flapping.add(short)
    return now_flapping


# A mute is cleared automatically only once the camera has been SOLIDLY back:
# continuously online this long, and not flapping.
MUTE_CLEAR_STABLE_H = 24


def clear_settled_mutes(st: dict, site: str, flapping: set) -> None:
    """Auto-clear mutes, but only for cameras that genuinely recovered.

    The original rule was "a recovered camera auto-unmutes", so a stale mute
    could not hide the next outage. Correct for a dead camera that gets
    replaced — and useless for a FLAPPING one, which "recovers" every few
    minutes and would clear its own mute within one poll of it being set
    (found 2026-09-27: muting the flapping Rita's group would have silenced
    nothing). So recovery now has to STICK before it counts.
    """
    from datetime import datetime, timedelta, timezone
    for short in list((st.get("muted") or {}).keys()):
        cam = (st.get("cameras") or {}).get(short) or {}
        if not cam.get("online") or short in flapping:
            continue
        since = cam.get("online_since")
        if not since:
            continue
        try:
            up_since = datetime.fromisoformat(since)
        except ValueError:
            continue
        if datetime.now(timezone.utc) - up_since < timedelta(
                hours=MUTE_CLEAR_STABLE_H):
            continue
        st["muted"].pop(short, None)
        name = cam.get("name", short)
        emit(st, f"blueiris: mute cleared — {name}",
             f"{name} ({short}) at {site} has been stable for "
             f"{MUTE_CLEAR_STABLE_H}h, so its mute was cleared automatically. "
             f"It will alert normally again.",
             "default", "white_check_mark")


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

    # Drop counts from the NVR's own log. A failure here must not sink the
    # whole run: state changes are still worth reporting without flap data.
    try:
        drops = record_drops(st, log_rows(bi))
    except Exception as exc:                        # noqa: BLE001
        print(f"  warning: flap data unavailable: {exc!r}", file=sys.stderr)
        drops = {}

    for c in cams:
        short = c["optionValue"]
        name = c.get("optionDisplay") or short
        online = c.get("isOnline") is True
        err = (c.get("error") or "").strip()
        prev = known.get(short)
        # How long has it been continuously up? Needed for mute clearing, which
        # must not trigger on a flapping camera's momentary recovery.
        if online and prev and prev.get("online") and prev.get("online_since"):
            online_since = prev["online_since"]
        else:
            online_since = _now() if online else None
        known[short] = {"name": name, "online": online, "last": _now(),
                        "error": err, "online_since": online_since,
                        "drops24h": drops.get(short, 0)}
        if seeding or prev is None:
            continue
        if prev.get("online") and not online:
            went_down.append((name, short, err))
        elif not prev.get("online") and online:
            came_back.append((name, short))

    flapping = flap_transitions(st, cams, drops)

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
        if short in flapping:
            # Already reported as flapping. Reporting each individual drop on
            # top of that is the alarm storm, not extra information.
            print(f"  {name} went down — already reported FLAPPING, not alerting")
            continue
        emit(st, f"blueiris: CAMERA DOWN — {name}",
             f"{name} ({short}) at {site} stopped responding.\n"
             f"error: {err or '<none reported>'}\n\n"
             f"If it cannot be fixed soon:  blueiris mute {short} --days 30 "
             f"\"awaiting replacement\"",
             "high", "camera")

    for name, short in came_back:
        if short in flapping:
            print(f"  {name} back up — already reported FLAPPING, not alerting")
            continue
        if mute_active(st, short)[0]:
            print(f"  {name} back up but still MUTED — see mute-clear below")
            continue
        emit(st, f"blueiris: camera recovered — {name}",
             f"{name} ({short}) at {site} is online again.",
             "default", "white_check_mark")

    clear_settled_mutes(st, site, flapping)

    save_state(site, st)
    online = sum(1 for c in cams if c.get("isOnline") is True)
    muted_n = len(st.get("muted") or {})
    print(f"  {online}/{len(cams)} online, {len(went_down)} newly down, "
          f"{len(came_back)} recovered, {len(flapping)} flapping, "
          f"{muted_n} muted")
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
    fl = sub.add_parser("flaps", help="drop counts per camera from the NVR log")
    fl.add_argument("--hours", type=int, default=FLAP_WINDOW_H)
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

    if args.cmd == "flaps":
        # READ-ONLY: shows the ledger the watch maintains, and never advances
        # it. An ad-hoc `flaps` must not consume deltas the watch needs, or
        # running it would blind the next poll.
        rows = log_rows(bi)
        st = load_state(args.site)
        ledger = st.get("flapledger") or {}
        import time as _t
        cutoff = _t.time() - args.hours * 3600
        drops = {o: sum(int(n) for ts, n in ev if ts >= cutoff)
                 for o, ev in ledger.items()}
        drops = {o: n for o, n in drops.items() if n}
        lifetime = restored_totals(rows)
        names = {c["optionValue"]: (c.get("optionDisplay") or c["optionValue"])
                 for c in bi.cameras()}
        if not ledger:
            print("  no flap ledger yet — it is built by `blueiris watch`, "
                  "so counts appear after a few polls.")
            print("  NVR lifetime restore counts (context only, not a rate):")
            for o, n in sorted(lifetime.items(), key=lambda kv: -kv[1])[:12]:
                print(f"    {o:<10} {names.get(o, '?')[:24]:<24} {n}")
            return 0
        if args.json:
            print(json.dumps({"window_hours": args.hours,
                              "drops": {k: {"name": names.get(k, k), "drops": v}
                                        for k, v in drops.items()}}, indent=1))
            return 0
        if not drops:
            print(f"  no signal drops logged in the last {args.hours}h")
            return 0
        print(f"  drops per camera, last {args.hours}h "
              f"(>= {FLAP_ENTER} counts as flapping)")
        print(f"  {'CAMERA':<10} {'NAME':<24} DROPS")
        for short, n in sorted(drops.items(), key=lambda kv: -kv[1]):
            mark = "  <<< FLAPPING" if n >= FLAP_ENTER else ""
            print(f"  {short:<10} {names.get(short, '?')[:24]:<24} {n}{mark}")
        # Cameras dropping together share an upstream — the single most useful
        # thing to notice, and easy to miss in a sorted list.
        #
        # Clustered on the RATIO between neighbouring rates, not on fixed
        # buckets: bucketing by n//10 split the Craigmyle Rita's group (73, 66,
        # 65, 64, 59 — plainly one fault) across three buckets, which is the
        # opposite of the point.
        flap = sorted(((n, names.get(s, s)) for s, n in drops.items()
                       if n >= FLAP_ENTER), reverse=True)
        clusters: list = []
        for n, nm in flap:
            if clusters and n >= clusters[-1][-1][0] * 0.75:
                clusters[-1].append((n, nm))
            else:
                clusters.append([(n, nm)])
        for cl in clusters:
            if len(cl) > 1:
                lo, hi = cl[-1][0], cl[0][0]
                print(f"\n  ⚠ {len(cl)} cameras dropping at a similar rate "
                      f"({lo}-{hi} in {args.hours}h) — almost certainly ONE "
                      f"shared upstream, not {len(cl)} faults:\n"
                      f"      {', '.join(sorted(nm for _, nm in cl))}")
        return 0

    if args.cmd == "status":
        d = bi.cmd("status").get("data") or {}
        for k in sorted(d):
            if not isinstance(d[k], (dict, list)):
                print(f"  {k:<18} {d[k]}")
        return 0

    if args.cmd == "log":
        rows = log_rows(bi)[:args.n]
        if args.json:
            print(json.dumps(rows, indent=1))
            return 0
        print(f"  {'WHEN':<16} {'LVL':<4} {'CAMERA':<10} {'xN':<5} MESSAGE")
        for e in reversed(rows):           # oldest first: reads like a story
            print(f"  {_logtime(e):<16} {str(e.get('level', '')):<4} "
                  f"{str(e.get('obj') or '-')[:10]:<10} "
                  f"{str(e.get('count') or ''):<5} {str(e.get('msg', ''))[:70]}")
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
