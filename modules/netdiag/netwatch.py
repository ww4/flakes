"""netwatch — the guard dog. Periodic netdiag runs with a persistent baseline.

Answers "is anything new or suspicious on the network" by diffing each scan
against an accepted baseline, rather than by re-describing the network every
time. Alerts are state-change only, matching the house polite-polling ethos.

THE DESIGN CONSTRAINT THAT MATTERS MOST: a guard dog that silently fails looks
exactly like a quiet network. Every path that could return "nothing found" must
be able to distinguish "nothing there" from "the check broke" —

  - a scan returning ZERO hosts is an ERROR, never an all-clear
  - a scan returning implausibly few hosts (< half the running median) raises a
    DEGRADED alert rather than reporting every missing device as gone
  - every successful run stamps state.json; an independent watchdog timer
    (netwatch.nix) alerts if that stamp goes stale, so the death of this script
    is itself an alert

Subcommands: scan (15 min) · drift (hourly) · report (daily) · audit (weekly)
             accept <mac> [label] · status
"""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

STATE_DIR = os.environ.get("NETWATCH_STATE", "/var/lib/netwatch")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
NTFY = os.environ.get("NETWATCH_NTFY", "http://127.0.0.1:8090/gromit-alerts")
# Base for the ntfy action buttons. Hosted under the ntfy vhost, which already
# has DNS + a cert and inherits the LAN/Tailscale source gate.
ACTION_BASE = os.environ.get(
    "NETWATCH_ACTION_BASE",
    "https://ntfy.rosemaryacres.com/netwatch-action")

# A scan that finds fewer than this fraction of the running median host count is
# treated as broken rather than believed. Picked low enough that a genuinely
# quiet night does not trip it, high enough to catch a half-failed sweep.
SANITY_FRACTION = 0.5


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def quiet_hours() -> bool:
    """22:00-07:00 local. House rule: no overnight pages."""
    return datetime.now().hour >= 22 or datetime.now().hour < 7


def notify(title: str, body: str, priority: str = "default",
           tags: str = "eyes", actions: str = "") -> None:
    """Post to ntfy, never louder than quiet hours allow.

    ⚠️ THERE IS DELIBERATELY NO ESCAPE HATCH. This function used to take a
    `critical` flag that skipped the quiet-hours downgrade, and netwatch used it
    for spoofing signatures and self-check failures. Chris's rule, 2026-08-19:

        "I don't care about man in the middle or arp spoofing at 3:00 a.m. when
        I'm asleep... I don't want to be awoken unless there's a physical threat
        to my family such as fire, a flood or an electrical problem. That goes
        for all classes of network traffic."

    Every event netwatch can raise is a network event, so NOTHING here may ever
    pierce quiet hours. The parameter is gone rather than merely defaulted to
    False, so a future edit cannot reintroduce a 3 a.m. page by passing a flag.
    The attacker is not going anywhere by morning.
    """
    if quiet_hours():
        priority = "low"
    safe = re.sub(r"[^\x20-\x7e]", "", title)[:200]
    headers = {"Title": safe, "Priority": priority, "Tags": tags}
    if actions:
        headers["Actions"] = actions
    req = urllib.request.Request(NTFY, data=body.encode(), headers=headers)
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except OSError as exc:
        print(f"netwatch: ntfy post failed: {exc!r}", file=sys.stderr)


def flush_held(state: dict) -> None:
    """Deliver everything held overnight, once, after quiet hours end.

    Chris's instruction for anything fishy on the network is "diagnose it,
    possibly isolate it, make a note and flag me in the morning". This is the
    flag-in-the-morning half: findings raised between 22:00 and 07:00 are held
    and arrive as ONE consolidated summary, rather than as a trickle of silent
    drawer notifications he has to reconstruct a timeline from.
    """
    held = state.get("held") or []
    if not held or quiet_hours():
        return
    # A handful: replay each one intact, because they carry their ACTION BUTTONS
    # and a summary would strip them — leaving him back at "here is a thing you
    # cannot act on", which is the problem this was built to solve. In the
    # morning several actionable notifications beat one unactionable digest.
    if len(held) <= 5:
        for h in held:
            notify(h["title"], f"[held from {h['at'][11:16]}Z overnight]\n" + h["body"],
                   h.get("priority", "default"), h.get("tags", "eyes"),
                   h.get("actions", ""))
    else:
        # A flood is itself the finding; summarise rather than spam.
        lines = [f"- {h['at'][11:16]}Z  {h['title']}" for h in held]
        notify(f"netwatch: {len(held)} finding(s) overnight",
               "Held through quiet hours, as agreed:\n" + "\n".join(lines) +
               "\n\nDetail: netwatch status  ·  /var/lib/netwatch/",
               "default", "dog")
    state["held"] = []


def alert_once(state: dict, key: str, title: str, body: str,
               priority: str = "default", tags: str = "eyes",
               rearm_hours: int = 12, actions: str = "") -> None:
    """Notify only when this condition is NEWLY true, or has gone stale.

    The house rule is notify on state CHANGE, and netwatch broke it badly: one
    unchanging fault ("scan found no hosts") produced 34 identical
    notifications between 23:00 and 07:15 on 2026-08-19, every 15 minutes, all
    night. The condition never changed — only the clock did.

    So an alert fires once, then stays quiet while the condition persists. It
    re-arms after `rearm_hours` so a problem that is still there tomorrow says
    so once more rather than being forgotten entirely. `alert_clear` resets it
    the moment the condition resolves, so a recurrence pages promptly instead
    of being swallowed by the re-arm window.
    """
    fired = state.setdefault("alerted", {})
    prev = fired.get(key)
    if prev:
        try:
            age_h = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(prev)).total_seconds() / 3600.0
        except ValueError:
            age_h = rearm_hours + 1
        if age_h < rearm_hours:
            print(f"netwatch: [{key}] still true, alert suppressed "
                  f"({age_h:.1f}h since last)")
            return
    fired[key] = now()
    if quiet_hours():
        # Hold it. flush_held() delivers one consolidated summary after 07:00.
        state.setdefault("held", []).append(
            {"at": now(), "title": title, "body": body,
             "priority": priority, "tags": tags, "actions": actions})
        print(f"netwatch: [{key}] held until morning — {title}")
        return
    notify(title, body, priority, tags, actions)


def alert_clear(state: dict, key: str) -> None:
    """Condition resolved — the next occurrence should alert immediately."""
    state.setdefault("alerted", {}).pop(key, None)


def load_state() -> dict:
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"seeded": None, "devices": {}, "gateway": {},
                "counts": [], "last_scan": None, "last_report": None}


def save_state(state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


# Why the most recent subprocess failed, for whoever asks next. A module-level
# dict rather than a changed return type: run() has many callers that only want
# stdout, and threading a status through all of them would be a large change for
# a small need. Cleared at the top of every run().
LAST_RUN: dict[str, object] = {}


def run(cmd: list[str], timeout: int = 120) -> str:
    """Run a command, return its stdout. Failure details land in LAST_RUN.

    ⚠️ THIS USED TO DISCARD `returncode` AND `stderr` ENTIRELY (`check=False`
    then `return res.stdout`), which made two very different outcomes produce
    the identical empty string:

        - the command FAILED and wrote its reason to stderr
        - the command SUCCEEDED and legitimately found nothing

    So `arp_census` returning [] could mean "the network is empty" or
    "netdiag-priv exited 1", and nothing downstream could tell. The scan-empty
    alert correctly refuses to call either one all-clear — but it could not say
    which, and captured no evidence. None of the 41 failures across 1,182 runs
    (2026-08-19 to 2026-08-31) is explainable after the fact as a result. The
    2026-08-31 08:00 failure took 3.2 s, the same as a healthy run, and logged
    nothing whatsoever.

    Control flow is unchanged: the return value is still stdout, and an
    exception still yields "". This only stops throwing the evidence away.
    """
    LAST_RUN.clear()
    LAST_RUN["cmd"] = " ".join(cmd)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout, check=False)
        LAST_RUN["rc"] = res.returncode
        LAST_RUN["stderr"] = (res.stderr or "").strip()[:500]
        LAST_RUN["stdout_bytes"] = len(res.stdout or "")
        if res.returncode != 0:
            # Loud in the journal even when this caller shrugs at empty output.
            # A non-zero exit is a fact worth recording regardless of whether
            # the immediate caller happens to care.
            print(f"netwatch: {cmd[0]} exited {res.returncode}: "
                  f"{LAST_RUN['stderr'] or '(no stderr)'}", file=sys.stderr)
        return res.stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        LAST_RUN["rc"] = None
        LAST_RUN["stderr"] = repr(exc)[:500]
        LAST_RUN["stdout_bytes"] = 0
        print(f"netwatch: {cmd[0]} failed: {exc!r}", file=sys.stderr)
        return ""


def last_run_detail() -> str:
    """One-line description of the most recent run(), for alert bodies."""
    if not LAST_RUN:
        return "no subprocess recorded"
    rc = LAST_RUN.get("rc")
    rc_s = "exec failed" if rc is None else f"exit {rc}"
    return (f"{LAST_RUN.get('cmd')} -> {rc_s}, "
            f"{LAST_RUN.get('stdout_bytes')} bytes stdout\n"
            f"stderr: {LAST_RUN.get('stderr') or '(none)'}")


def default_iface() -> str:
    out = run(["ip", "-j", "route", "show", "default"])
    try:
        return json.loads(out)[0]["dev"]
    except (json.JSONDecodeError, IndexError, KeyError):
        return ""


def gateway_ip() -> str:
    out = run(["ip", "-j", "route", "show", "default"])
    try:
        return json.loads(out)[0]["gateway"]
    except (json.JSONDecodeError, IndexError, KeyError):
        return ""


ARPSCAN_DETAIL = "no arp-scan recorded"


def arp_census(iface: str) -> list[tuple[str, str, str]]:
    """Distinct [(ip, mac, vendor)] from netdiag-priv. Root under systemd.

    ⚠️ DEDUPLICATION IS LOAD-BEARING, not tidiness. arp-scan runs with
    --retry=3 and prints one line per REPLY, so a host that answers more than
    one probe appears more than once. That is normal and says nothing about
    the network. Treating those rows as separate hosts caused two real faults
    on 2026-08-19:

      - the same MAC appeared 2-3x under one IP, which the duplicate-IP check
        read as an address conflict and reported as possible ARP SPOOFING.
      - the host COUNT was inflated by the extra rows, and that count feeds the
        running median behind the degraded-scan floor, so the floor was being
        calibrated against a number that bounced with retry luck.

    A genuine conflict is one IP with two DIFFERENT MACs. Row count never was
    the signal.
    """
    out = run(["netdiag-priv", "arpscan", iface], timeout=180)
    # Pin the outcome NOW. LAST_RUN is overwritten by the next run() anywhere in
    # the process, so reading it later would silently describe a different
    # command — the same class of mistake as attaching a count to the wrong
    # window. The scan-empty alert reads ARPSCAN_DETAIL, never LAST_RUN.
    global ARPSCAN_DETAIL
    ARPSCAN_DETAIL = last_run_detail()
    seen: dict[tuple[str, str], str] = {}
    for line in out.splitlines():
        parts = line.split("\t") if "\t" in line else line.split(None, 2)
        if len(parts) >= 2 and re.match(r"^\d+\.\d+\.\d+\.\d+$", parts[0]):
            vendor = parts[2].strip() if len(parts) > 2 else "unknown"
            key = (parts[0], parts[1].lower())
            # Keep the most informative vendor string across duplicate replies.
            if key not in seen or seen[key] in ("", "unknown"):
                seen[key] = vendor
    return [(ip, mac, vendor) for (ip, mac), vendor in seen.items()]


def is_randomised(mac: str) -> bool:
    """U/L bit set = locally administered = a randomising phone or laptop."""
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except ValueError:
        return False


def hostname_for(ip: str) -> str:
    """Reverse-DNS name, or "".

    The gateway's DHCP server registers client names (wallace.lan, Pixel-7.lan,
    Marys-Air.lan), which makes this the single most useful identifier
    available — far better than a MAC, and free. Deliberately stdlib-only via
    the system resolver, so the unit gains no new dependency.
    """
    try:
        name = socket.gethostbyaddr(ip)[0]
    except (OSError, socket.herror, socket.gaierror):
        return ""
    # Strip the search domain: "wallace.lan" -> "wallace". Keep it recognisable.
    for suffix in (".lan", ".local", ".home", ".localdomain"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return "" if name in ("_gateway", ip) else name


def cmd_scan() -> int:
    """Every 15 min: presence diff against the accepted baseline."""
    state = load_state()
    flush_held(state)   # deliver anything held overnight, once, after 07:00
    iface = default_iface()
    # NOTE on all three self-check failures below: they are NOT `critical`.
    # A broken watchdog is a fix-this-today problem, not a wake-up-now one, and
    # the unit already exits non-zero — which trips the existing
    # SystemdUnitFailed alerting through the normal channel. That escalation
    # path worked correctly on 2026-08-19 (fired 07:02, resolved 07:22) while
    # netwatch's own priority-4 pages were pure noise on top of it.
    if not iface:
        alert_once(state, "no-route", "netwatch: no default route",
                   "Cannot determine an interface to scan. netwatch is blind.",
                   "high", "warning")
        save_state(state)
        return 1

    rows = arp_census(iface)

    # --- the two failure modes that must never read as "all clear" ---
    if not rows:
        # ⚠️ SAY WHICH. "arp-scan failed" and "arp-scan found nothing" are
        # different faults with different fixes, and until 2026-08-31 this alert
        # could not tell them apart because run() discarded the exit code. 41
        # such failures in 1,182 runs went unexplained. ARPSCAN_DETAIL carries
        # the exit code, stdout size and stderr from the scan itself.
        print(f"netwatch: arp-scan on {iface} yielded no rows — {ARPSCAN_DETAIL}",
              file=sys.stderr)
        alert_once(state, "scan-empty", "netwatch: scan found no hosts",
                   f"arp-scan on {iface} produced zero hosts. This is a broken "
                   f"check, not a quiet network — netwatch is not watching.\n"
                   f"{ARPSCAN_DETAIL}\n"
                   f"A non-zero exit means the SCAN broke; exit 0 with no rows "
                   f"means it ran and genuinely saw nothing (check the link).\n"
                   f"Check: netdiag-priv arpscan {iface}",
                   "high", "warning")
        save_state(state)
        return 1
    alert_clear(state, "scan-empty")

    counts = (state.get("counts") or [])[-19:] + [len(rows)]
    state["counts"] = counts
    if len(counts) >= 5:
        median = sorted(counts[:-1])[len(counts[:-1]) // 2]
        # ⚠️ The half-median floor is a PROXY for "the sweep broke", and on this
        # segment the proxy does not discriminate. gromit sees 4-5 hosts and
        # 2-3 of them are phones on randomised MACs, so everyone leaving for
        # work halves the count and trips a floor of median*0.5 with nothing
        # wrong. That is exactly what happened on 2026-08-31: counts ran 4,3,4
        # through 07:45 and then 2 from 08:00, failing the unit every 15 min on
        # a Monday-morning departure.
        #
        # The direct test is whether the SWEEP worked, and the gateway answers
        # it: wired, always on, always replies to ARP. If it is in `rows` the
        # sweep demonstrably ran, and a low count is real (devices left) rather
        # than a fault. Requiring BOTH conditions keeps the broken-sweep
        # detection — a sweep that truly breaks loses the gateway too — while
        # dropping the empty-house false positive. Defence 1 (zero rows =
        # ERROR) is untouched and still catches total failure.
        gw = gateway_ip()
        gw_answered = bool(gw) and any(ip == gw for ip, _m, _v in rows)
        if len(rows) < median * SANITY_FRACTION and not gw_answered:
            alert_once(state, "scan-degraded", "netwatch: scan DEGRADED",
                       f"Found {len(rows)} hosts; the running median is "
                       f"{median}, and the gateway {gw or '(unknown)'} did not "
                       f"answer. Treating this as a failed sweep rather than "
                       f"reporting {median - len(rows)} devices as newly "
                       f"missing.",
                       "high", "warning")
            save_state(state)
            return 1
    alert_clear(state, "scan-degraded")
    alert_clear(state, "no-route")

    devices = state.setdefault("devices", {})
    seeding = state.get("seeded") is None
    new, rebound, dup, rotated = [], [], [], []

    # Hostname -> the accepted MAC currently holding it. This is what makes MAC
    # ROTATION survivable: phones randomise their MAC per network and re-roll it
    # periodically, so a device accepted yesterday reappears under a brand-new
    # MAC and would otherwise alert as a stranger every time. The DHCP-registered
    # name is stable across that, so it — not the MAC — is the durable identity.
    known_by_host: dict[str, str] = {}
    for m, r in devices.items():
        h = r.get("hostname") or ""
        if h and r.get("accepted"):
            known_by_host.setdefault(h, m)

    by_ip: dict[str, set[str]] = {}
    for ip, mac, vendor in rows:
        # A SET, not a list. The conflict signal is "how many DIFFERENT MACs
        # claim this address", and a list counts repeated sightings of the same
        # one — which is what produced the false spoofing alerts.
        by_ip.setdefault(ip, set()).add(mac)
        rec = devices.get(mac)
        if rec is None:
            host = hostname_for(ip)
            prior = known_by_host.get(host) if host else None
            if prior and prior in devices:
                # Same name, new MAC: a re-randomisation, not a new device.
                # Carry the label and acceptance across so curation survives.
                old = devices.pop(prior)
                old.update({"ip": ip, "last_seen": now(), "hostname": host})
                if vendor and vendor != "unknown":
                    old["vendor"] = vendor
                devices[mac] = old
                known_by_host[host] = mac
                rotated.append((host, prior, mac))
                continue
            devices[mac] = {
                "first_seen": now(), "last_seen": now(), "ip": ip,
                "vendor": vendor, "label": "", "ports": [],
                "hostname": host,
                # Seeding auto-accepts everything present on the first run, so
                # day one is not an alert storm nobody reads.
                "accepted": seeding,
            }
            if host:
                known_by_host.setdefault(host, mac)
            if not seeding:
                new.append((ip, mac, vendor, host))
        else:
            if rec.get("ip") != ip:
                rebound.append((mac, rec.get("ip"), ip))
                rec["ip"] = ip
            rec["last_seen"] = now()
            if vendor and vendor != "unknown":
                rec["vendor"] = vendor
            # Backfill names for devices recorded before this existed, and pick
            # up a rename (a device renamed on the router should show its name).
            if not rec.get("hostname"):
                rec["hostname"] = hostname_for(ip)

    for ip, macs in by_ip.items():
        if len(macs) > 1:
            dup.append((ip, sorted(macs)))

    # Gateway MAC change = someone is answering for the router: ARP spoofing /
    # MITM, or the router was replaced. Serious, and still NOT worth waking
    # anyone for — it is held until morning like every other network finding.
    gw_ip = gateway_ip()
    gw_mac = next((m for i, m, _ in rows if i == gw_ip), None)
    known_gw = state.setdefault("gateway", {})
    if gw_mac and known_gw.get("mac") and known_gw["mac"] != gw_mac:
        # Keyed by the NEW mac so a genuine second change still pages; a single
        # unchanged situation does not re-page every 15 minutes.
        alert_once(state, f"gw-mac:{gw_mac}", "netwatch: GATEWAY MAC CHANGED",
                   f"{gw_ip} was {known_gw['mac']}, now {gw_mac}.\n"
                   f"This is the signature of ARP spoofing / a MITM, or the "
                   f"router was genuinely replaced. Verify before trusting "
                   f"the LAN.",
                   "high", "rotating_light")
    if gw_mac:
        known_gw.update({"ip": gw_ip, "mac": gw_mac})

    if seeding:
        state["seeded"] = now()
        notify("netwatch: baseline seeded",
               f"{len(rows)} devices recorded and auto-accepted on {iface}.\n"
               f"From now on any unknown MAC raises an alert.\n"
               f"Review: netwatch status",
               "default", "dog")

    for host, old_mac, new_mac in rotated:
        # Informational, and quiet. This is a phone doing exactly what modern
        # phones do; alerting at it weekly would train Chris to ignore netwatch.
        print(f"netwatch: {host} re-randomised its MAC "
              f"({old_mac} -> {new_mac}) — same device, label preserved")

    for ip, mac, vendor, host in new:
        who = host or vendor or "unknown device"
        if is_randomised(mac):
            hint = ("\nMAC is locally administered — a phone/laptop using "
                    "per-network randomisation, not a spoof.")
        else:
            hint = ""
        # Two single-use nonces, one per button, bound to this MAC. The buttons
        # replace the shell command this notification used to end with — which
        # was unusable on a phone, and so amounted to no call to action at all.
        acts = state.setdefault("actions", {})
        exp = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(
            timespec="seconds")
        n_acc, n_inv = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        acts[n_acc] = {"mac": mac, "verb": "accept", "exp": exp}
        acts[n_inv] = {"mac": mac, "verb": "investigate", "exp": exp}
        buttons = (
            f"http, Accept, {ACTION_BASE}/{n_acc}/accept, method=POST, clear=true; "
            f"http, Investigate, {ACTION_BASE}/{n_inv}/investigate, method=POST"
        )
        # Through alert_once, NOT notify: an unknown device appearing at 03:00
        # is exactly the "something fishy" case Chris wants held and flagged in
        # the morning, not trickled into the drawer overnight one at a time.
        alert_once(state, f"new:{mac}", f"netwatch: NEW DEVICE {who} ({ip})",
                   f"{mac}\n{vendor}{hint}\n\n"
                   f"Accept adds it to the baseline. Investigate fingerprints "
                   f"it and sends the findings back.",
                   "high", "eyes", actions=buttons)
    for mac, old_ip, new_ip in rebound:
        alert_once(state, f"rebound:{mac}:{new_ip}",
                   "netwatch: device changed address",
                   f"{mac}\n{old_ip} -> {new_ip}\n"
                   f"Normal after a DHCP lease change; suspicious if this "
                   f"device is supposed to hold a reservation.",
                   "default", "arrows_counterclockwise")
    # Keyed by the IP AND the exact set of claimants, so the same standing
    # conflict pages once rather than every 15 minutes, while a different pair
    # of devices fighting still gets its own alert.
    seen_dups = set()
    for ip, macs in dup:
        key = "dup:" + ip + ":" + ",".join(macs)
        seen_dups.add(key)
        alert_once(state, key, "netwatch: DUPLICATE IP",
                   f"{ip} is claimed by {len(macs)} MACs:\n" + "\n".join(macs) +
                   "\nEither an address conflict or ARP spoofing.",
                   "high", "warning")
    for key in [k for k in state.get("alerted", {}) if k.startswith("dup:")]:
        if key not in seen_dups:
            alert_clear(state, key)

    state["last_scan"] = now()
    save_state(state)
    unaccepted = sum(1 for d in devices.values() if not d.get("accepted"))
    print(f"netwatch: {len(rows)} hosts, {len(new)} new, "
          f"{unaccepted} awaiting acceptance")
    return 0


def cmd_drift() -> int:
    """Hourly: has a known device started listening on something new?"""
    state = load_state()
    devices = state.get("devices", {})
    if not devices:
        print("netwatch: no baseline yet; run netwatch scan first")
        return 0
    ports = "22,23,80,111,139,443,445,554,3389,5000,5900,8080,8291,9100,37777"
    changed = []
    for mac, rec in devices.items():
        ip = rec.get("ip")
        if not ip or not rec.get("accepted"):
            continue
        out = run(["nmap", "-Pn", "-T4", "--open", "-p", ports, ip, "-oG", "-"],
                  timeout=120)
        found = sorted({int(m) for m in re.findall(r"(\d+)/open", out)})
        before = rec.get("ports") or []
        if before and found != before:
            opened = [p for p in found if p not in before]
            if opened:
                changed.append((ip, mac, opened, rec.get("label") or rec.get("vendor")))
        rec["ports"] = found
    for ip, mac, opened, label in changed:
        notify(f"netwatch: new service on {ip}",
               f"{label}\n{mac}\nNewly listening: "
               f"{', '.join(str(p) for p in opened)}\n\n"
               f"Expected after an update or a new app; worth a look if not.",
               "default", "electric_plug")
    save_state(state)
    print(f"netwatch: drift check done, {len(changed)} host(s) changed")
    return 0


def section(title: str, body: str) -> str:
    return f"\n## {title}\n\n{body.strip()}\n"


def cmd_report() -> int:
    """Daily: the health report. Runs the checks a diff cannot cover."""
    state = load_state()
    iface = default_iface()
    parts = []

    devices = state.get("devices", {})
    unaccepted = {m: d for m, d in devices.items() if not d.get("accepted")}
    parts.append(section(
        "Inventory",
        f"{len(devices)} devices known, {len(unaccepted)} awaiting acceptance."
        + ("".join(f"\n  - {d.get('ip')}  {m}  {d.get('vendor')}"
                   for m, d in unaccepted.items()) if unaccepted else "")))

    rogue = run(["netdiag", "rogue", "20"], timeout=300)
    parts.append(section("Rogue DHCP / IPv6 RA", rogue[-1500:] or "(no output)"))

    storm = run(["netdiag", "storm", "10"], timeout=300)
    parts.append(section("Broadcast / loop", storm[-1200:] or "(no output)"))

    exposure = run(["netdiag", "exposure"], timeout=180)
    parts.append(section("Firewall exposure", exposure[-1500:] or "(no output)"))

    listen = run(["netdiag", "listen", "20"], timeout=300)
    parts.append(section("Subnets seen on this segment", listen[-1200:] or "(no output)"))

    report = f"# netwatch report {now()}\n\ninterface: {iface}\n" + "".join(parts)
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, f"report-{datetime.now():%Y-%m-%d}.md")
    with open(path, "w") as fh:
        fh.write(report)

    # Only page on things that need a human. The report itself is on disk.
    flags = []
    if "LOOP DETECTED" in storm:
        flags.append("L2 LOOP")
    if "DOUBLE NAT" in exposure or "CGNAT" in exposure:
        flags.append("NAT position changed")
    if unaccepted:
        flags.append(f"{len(unaccepted)} unaccepted device(s)")
    if flags:
        # An L2 loop used to be sent critical here. It is a network event, so
        # it is not — the daily report now runs after quiet hours anyway.
        notify("netwatch: daily report needs attention",
               " · ".join(flags) + f"\n\nFull report: {path}",
               "default", "dog")

    state["last_report"] = now()
    save_state(state)
    print(f"netwatch: report written to {path}")
    return 0


def cmd_audit() -> int:
    """Weekly: the slower sweeps."""
    state = load_state()
    out = [f"# netwatch weekly audit {now()}\n"]
    out.append(section("Camera census", run(["netdiag", "cameras"], timeout=600)[-2000:]))
    out.append(section("Ubiquiti / unadopted APs", run(["netdiag", "unifi"], timeout=600)[-1500:]))
    gw = gateway_ip()
    if gw:
        out.append(section(f"Gateway exposure ({gw})",
                           run(["netdiag", "audit", gw], timeout=900)[-2000:]))
    path = os.path.join(STATE_DIR, f"audit-{datetime.now():%Y-%m-%d}.md")
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(path, "w") as fh:
        fh.write("".join(out))
    state["last_audit"] = now()
    save_state(state)
    print(f"netwatch: audit written to {path}")
    return 0


def cmd_accept(argv: list[str]) -> int:
    if not argv:
        print("usage: netwatch accept <mac> [label]", file=sys.stderr)
        return 2
    mac = argv[0].lower()
    label = " ".join(argv[1:])
    state = load_state()
    rec = state.get("devices", {}).get(mac)
    if rec is None:
        print(f"netwatch: {mac} is not in the baseline", file=sys.stderr)
        return 1
    rec["accepted"] = True
    if label:
        rec["label"] = label
    save_state(state)
    print(f"netwatch: accepted {mac} ({label or rec.get('vendor')})")
    return 0


def cmd_status() -> int:
    state = load_state()
    devices = state.get("devices", {})
    print(f"seeded    : {state.get('seeded') or 'NOT YET'}")
    print(f"last scan : {state.get('last_scan')}")
    print(f"last report: {state.get('last_report')}")
    print(f"devices   : {len(devices)}")

    def ipkey(kv):
        try:
            return [int(o) for o in kv[1].get("ip", "0.0.0.0").split(".")]
        except ValueError:
            return [0, 0, 0, 0]

    print("\nKNOWN DEVICES:")
    print(f"  {'IP':<16} {'NAME':<24} {'MAC':<18} VENDOR")
    for mac, d in sorted(devices.items(), key=ipkey):
        # label (human-set) beats hostname (DHCP-registered) beats nothing.
        name = d.get("label") or d.get("hostname") or ""
        tag = " [rand]" if is_randomised(mac) else ""
        mark = "" if d.get("accepted") else "  <-- UNACCEPTED"
        print(f"  {d.get('ip', '?'):<16} {name[:24]:<24} {mac}{tag:<7} "
              f"{(d.get('vendor') or '?')[:26]}{mark}")

    pending = [(m, d) for m, d in devices.items() if not d.get("accepted")]
    if pending:
        print(f"\n{len(pending)} awaiting acceptance — netwatch accept <mac> <label>")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    sub, rest = argv[0], argv[1:]
    handlers = {
        "scan": lambda: cmd_scan(), "drift": lambda: cmd_drift(),
        "report": lambda: cmd_report(), "audit": lambda: cmd_audit(),
        "status": lambda: cmd_status(), "accept": lambda: cmd_accept(rest),
    }
    fn = handlers.get(sub)
    if fn is None:
        print(f"netwatch: unknown subcommand '{sub}'", file=sys.stderr)
        return 2
    return fn()


if __name__ == "__main__":
    sys.exit(main())
