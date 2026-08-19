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
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

STATE_DIR = os.environ.get("NETWATCH_STATE", "/var/lib/netwatch")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
NTFY = os.environ.get("NETWATCH_NTFY", "http://127.0.0.1:8090/gromit-alerts")

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
           tags: str = "eyes", critical: bool = False) -> None:
    """Post to ntfy. Non-critical alerts go silent during quiet hours rather
    than being suppressed — they still land, they just do not buzz."""
    if quiet_hours() and not critical:
        priority = "low"
    safe = re.sub(r"[^\x20-\x7e]", "", title)[:200]
    req = urllib.request.Request(
        NTFY, data=body.encode(),
        headers={"Title": safe, "Priority": priority, "Tags": tags})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except OSError as exc:
        print(f"netwatch: ntfy post failed: {exc!r}", file=sys.stderr)


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


def run(cmd: list[str], timeout: int = 120) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout, check=False)
        return res.stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"netwatch: {cmd[0]} failed: {exc!r}", file=sys.stderr)
        return ""


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


def arp_census(iface: str) -> list[tuple[str, str, str]]:
    """[(ip, mac, vendor)] from netdiag-priv. Runs as root under systemd."""
    out = run(["netdiag-priv", "arpscan", iface], timeout=180)
    rows = []
    for line in out.splitlines():
        parts = line.split("\t") if "\t" in line else line.split(None, 2)
        if len(parts) >= 2 and re.match(r"^\d+\.\d+\.\d+\.\d+$", parts[0]):
            vendor = parts[2].strip() if len(parts) > 2 else "unknown"
            rows.append((parts[0], parts[1].lower(), vendor))
    return rows


def is_randomised(mac: str) -> bool:
    """U/L bit set = locally administered = a randomising phone or laptop."""
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except ValueError:
        return False


def cmd_scan() -> int:
    """Every 15 min: presence diff against the accepted baseline."""
    state = load_state()
    iface = default_iface()
    if not iface:
        notify("netwatch: no default route",
               "Cannot determine an interface to scan. netwatch is blind.",
               "high", "warning", critical=True)
        return 1

    rows = arp_census(iface)

    # --- the two failure modes that must never read as "all clear" ---
    if not rows:
        notify("netwatch: scan returned NOTHING",
               f"arp-scan on {iface} produced zero hosts. This is a broken "
               f"check, not a quiet network — netwatch is not watching.\n"
               f"Check: netdiag-priv arpscan {iface}",
               "high", "warning", critical=True)
        return 1

    counts = (state.get("counts") or [])[-19:] + [len(rows)]
    state["counts"] = counts
    if len(counts) >= 5:
        median = sorted(counts[:-1])[len(counts[:-1]) // 2]
        if len(rows) < median * SANITY_FRACTION:
            notify("netwatch: scan DEGRADED",
                   f"Found {len(rows)} hosts; the running median is {median}. "
                   f"Treating this as a failed sweep rather than reporting "
                   f"{median - len(rows)} devices as newly missing.",
                   "high", "warning", critical=True)
            save_state(state)
            return 1

    devices = state.setdefault("devices", {})
    seeding = state.get("seeded") is None
    new, rebound, dup = [], [], []

    by_ip: dict[str, list[str]] = {}
    for ip, mac, vendor in rows:
        by_ip.setdefault(ip, []).append(mac)
        rec = devices.get(mac)
        if rec is None:
            devices[mac] = {
                "first_seen": now(), "last_seen": now(), "ip": ip,
                "vendor": vendor, "label": "", "ports": [],
                # Seeding auto-accepts everything present on the first run, so
                # day one is not an alert storm nobody reads.
                "accepted": seeding,
            }
            if not seeding:
                new.append((ip, mac, vendor))
        else:
            if rec.get("ip") != ip:
                rebound.append((mac, rec.get("ip"), ip))
                rec["ip"] = ip
            rec["last_seen"] = now()
            if vendor and vendor != "unknown":
                rec["vendor"] = vendor

    for ip, macs in by_ip.items():
        if len(macs) > 1:
            dup.append((ip, macs))

    # Gateway MAC change = someone is answering for the router. Always critical,
    # always bypasses quiet hours: this is an attack in progress, not a report.
    gw_ip = gateway_ip()
    gw_mac = next((m for i, m, _ in rows if i == gw_ip), None)
    known_gw = state.setdefault("gateway", {})
    if gw_mac and known_gw.get("mac") and known_gw["mac"] != gw_mac:
        notify("netwatch: GATEWAY MAC CHANGED",
               f"{gw_ip} was {known_gw['mac']}, now {gw_mac}.\n"
               f"This is the signature of ARP spoofing / a MITM, or the router "
               f"was genuinely replaced. Verify before trusting the LAN.",
               "urgent", "rotating_light", critical=True)
    if gw_mac:
        known_gw.update({"ip": gw_ip, "mac": gw_mac})

    if seeding:
        state["seeded"] = now()
        notify("netwatch: baseline seeded",
               f"{len(rows)} devices recorded and auto-accepted on {iface}.\n"
               f"From now on any unknown MAC raises an alert.\n"
               f"Review: netwatch status",
               "default", "dog")

    for ip, mac, vendor in new:
        hint = " — likely a phone/laptop (randomised MAC)" if is_randomised(mac) else ""
        notify(f"netwatch: NEW DEVICE {ip}",
               f"{mac}\n{vendor}{hint}\n\n"
               f"Accept it:  netwatch accept {mac} <label>\n"
               f"Inspect it: netdiag identify {ip}",
               "high", "eyes")
    for mac, old_ip, new_ip in rebound:
        notify("netwatch: device changed address",
               f"{mac}\n{old_ip} -> {new_ip}\n"
               f"Normal after a DHCP lease change; suspicious if this device "
               f"is supposed to hold a reservation.",
               "default", "arrows_counterclockwise")
    for ip, macs in dup:
        notify("netwatch: DUPLICATE IP",
               f"{ip} is claimed by {len(macs)} MACs:\n" + "\n".join(macs) +
               "\nEither an address conflict or ARP spoofing.",
               "high", "warning", critical=True)

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
        notify("netwatch: daily report needs attention",
               " · ".join(flags) + f"\n\nFull report: {path}",
               "default", "dog", critical="L2 LOOP" in flags)

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
    pending = [(m, d) for m, d in devices.items() if not d.get("accepted")]
    if pending:
        print(f"\nAWAITING ACCEPTANCE ({len(pending)}):")
        for mac, d in pending:
            tag = " [randomised]" if is_randomised(mac) else ""
            print(f"  {d.get('ip', '?'):<16} {mac}  {d.get('vendor', '?')}{tag}")
        print("\n  netwatch accept <mac> <label>")
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
