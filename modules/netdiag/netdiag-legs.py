"""netdiag-legs — find which LEG of a network is failing, by simultaneity.

Built 2026-09-27. The Craigmyle diagnosis established that five cameras dropping
within ~20 seconds of each other, repeatedly, means ONE shared upstream rather
than five faults. That inference was made by hand from an NVR log. This makes it
a measurement, on any network, without needing an NVR.

THE IDEA. Probe every target once per round. Record which targets fail in each
round. Then ask, for each PAIR, how often they fail TOGETHER. Devices sharing a
switch, an uplink, a powerline adapter or a PoE budget fail in the same round;
devices that merely happen to be sick fail in different rounds. Clustering on
co-failure recovers the physical topology from nothing but timing — which is
exactly the thing nobody has a diagram of.

WHY TCP CONNECT, NOT PING.
  * It needs no privilege at all: no raw socket, no CAP_NET_RAW, no setuid
    fping. So this runs as an ordinary user on marcus, on site, immediately.
  * It tests the SERVICE, not just the IP stack. A camera whose stream has died
    while its stack still answers ICMP is a real failure mode, and a ping-based
    test scores it healthy.
  * ICMP is also the first thing rate-limited or deprioritised by a switch under
    load, which produces "loss" that is an artefact of the measurement.

⚠️ READ THE TIMING CAVEAT. One round per interval means simultaneity is only
resolved to the interval. Two devices behind different switches that both drop
within the same 5-second round look simultaneous. Shorten the interval when the
distinction matters, and treat co-failure as evidence of a shared upstream, not
proof of which one — pair it with `netdiag switchport` (which port a MAC is on)
or `netdiag plc` (is there a powerline adapter in the path).
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import socket
import sys
import time

# Ports tried in order. 80 first because almost every camera, AP and switch
# answers it; 554 because an RTSP-only camera may not serve HTTP at all.
DEFAULT_PORTS = (80, 554, 443, 22)


def parse_targets(items: list[str]) -> list[tuple[str, str]]:
    """Accept `1.2.3.4`, `label=1.2.3.4`, or `label=1.2.3.4,1.2.3.5`.

    The label is free text and only ever used for display and grouping, so a
    guess about which building something is in costs nothing and helps read the
    output.
    """
    out: list[tuple[str, str]] = []
    for raw in items:
        label, _, addrs = raw.partition("=")
        if not addrs:
            label, addrs = "", label
        for a in addrs.split(","):
            a = a.strip()
            if a:
                out.append((a, label.strip() or "-"))
    return out


def load_file(path: str) -> list[str]:
    """One target per line: `1.2.3.4 label`, `#` comments ignored."""
    items = []
    with open(path) as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            items.append(f"{parts[1]}={parts[0]}" if len(parts) > 1 else parts[0])
    return items


async def probe(addr: str, ports: tuple, timeout: float) -> bool:
    """True if anything answers. A REFUSED connection counts as ALIVE.

    That is deliberate and easy to get backwards: a TCP reset proves a host is
    present and processing packets, which is the question being asked. Only a
    timeout or an unreachable means the leg is down.
    """
    for port in ports:
        try:
            fut = asyncio.open_connection(addr, port)
            reader, writer = await asyncio.wait_for(fut, timeout)
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.TimeoutError):
                pass
            return True
        except (ConnectionRefusedError, ConnectionResetError):
            return True
        except (asyncio.TimeoutError, OSError):
            continue
    return False


async def one_round(targets: list, ports: tuple, timeout: float) -> set:
    """Probe everything concurrently; return the set of addresses that failed."""
    results = await asyncio.gather(
        *(probe(a, ports, timeout) for a, _ in targets))
    return {a for (a, _), ok in zip(targets, results) if not ok}


def cluster(cofail: dict, fails: dict, targets: list, floor: float) -> list:
    """Group targets that fail together more often than `floor` of the time.

    Single-link agglomeration on the conditional co-failure rate. Deliberately
    simple: the output is a hint that sends someone to look at a switch, and a
    more sophisticated clustering would imply a confidence the data does not
    carry.
    """
    addrs = [a for a, _ in targets if fails.get(a)]
    parent = {a: a for a in addrs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(addrs):
        for b in addrs[i + 1:]:
            # KEY MUST BE SORTED, exactly as it was stored. Looking it up in
            # iteration order instead silently returned 0 for any pair whose
            # natural order differs from its string order — and for dotted
            # quads that is most pairs, because "192.168.1.213" sorts BEFORE
            # "192.168.1.67". Three addresses failing in lockstep in every
            # single round were reported as a group of two (2026-09-27). A
            # grouping tool that under-groups is worse than none: it argues
            # against the shared-upstream conclusion it exists to find.
            both = cofail.get(tuple(sorted((a, b))), 0)
            # JACCARD, and it must be symmetric. The obvious
            # `both / min(fails)` is wrong in the exact case this site has: a
            # chronically flapping camera is down so often that any OTHER
            # camera's rare drop lands inside one of its outages, scoring 1.0
            # and absorbing it into a group they do not share. Craigmyle has
            # New Shop East flapping ~20x/day, so that false grouping would
            # have been the normal result. Penalising the union instead means a
            # shared upstream (both down, together, nearly always) scores high
            # while coincidental overlap scores low.
            rate = both / max(1, fails[a] + fails[b] - both)
            if rate >= floor:
                parent[find(a)] = find(b)
    groups: dict = collections.defaultdict(list)
    for a in addrs:
        groups[find(a)].append(a)
    return [g for g in groups.values() if len(g) > 1]


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="netdiag-legs",
        description="Find which network leg is failing, by co-failure timing.")
    ap.add_argument("targets", nargs="*",
                    help="addr, label=addr, or label=addr1,addr2")
    ap.add_argument("-f", "--file", help="file of targets, one per line")
    ap.add_argument("-m", "--minutes", type=float, default=15.0)
    ap.add_argument("-i", "--interval", type=float, default=5.0,
                    help="seconds per round (bounds simultaneity resolution)")
    ap.add_argument("-t", "--timeout", type=float, default=2.0)
    ap.add_argument("-p", "--ports", default=",".join(map(str, DEFAULT_PORTS)))
    ap.add_argument("--floor", type=float, default=0.6,
                    help="co-failure rate to call a shared leg (0-1)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    items = list(args.targets)
    if args.file:
        items += load_file(args.file)
    targets = parse_targets(items)
    if not targets:
        ap.error("no targets given (positional, or -f FILE)")
    seen = set()
    targets = [t for t in targets if not (t[0] in seen or seen.add(t[0]))]

    try:
        ports = tuple(int(p) for p in args.ports.split(",") if p.strip())
    except ValueError:
        ap.error("--ports must be a comma-separated list of numbers")
    for a, _ in targets:
        try:
            socket.inet_pton(socket.AF_INET, a)
        except OSError:
            pass                      # hostnames are fine; resolution is tested

    label = dict(targets)
    rounds = max(1, int(args.minutes * 60 / args.interval))
    fails: dict = collections.Counter()
    cofail: dict = collections.Counter()
    events: list = []
    done = 0

    if not args.json:
        print(f"  {len(targets)} targets, {rounds} rounds of {args.interval}s "
              f"(~{args.minutes:.0f} min). Ctrl-C to stop early and still get "
              f"the report.")
        print(f"  probing tcp/{','.join(map(str, ports))}; a REFUSED port "
              f"counts as alive.\n")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        for r in range(rounds):
            t0 = time.time()
            down = loop.run_until_complete(one_round(targets, ports, args.timeout))
            done = r + 1
            if down:
                events.append({"at": time.strftime("%H:%M:%S"),
                               "down": sorted(down)})
                for a in down:
                    fails[a] += 1
                ds = sorted(down)
                for i, a in enumerate(ds):
                    for b in ds[i + 1:]:
                        cofail[tuple(sorted((a, b)))] += 1
                if not args.json:
                    names = ", ".join(f"{a}({label[a]})" if label[a] != "-" else a
                                      for a in ds)
                    print(f"  {time.strftime('%H:%M:%S')}  DOWN {len(ds):>2}: {names}")
            slack = args.interval - (time.time() - t0)
            if slack > 0 and r + 1 < rounds:
                time.sleep(slack)
    except KeyboardInterrupt:
        if not args.json:
            print(f"\n  stopped early after {done} rounds\n")
    finally:
        loop.close()

    groups = cluster(cofail, fails, targets, args.floor)
    report = {
        "rounds": done, "interval": args.interval,
        "targets": [{"addr": a, "label": label[a], "fails": fails.get(a, 0),
                     "loss_pct": round(100 * fails.get(a, 0) / max(1, done), 1)}
                    for a, _ in targets],
        "shared_legs": [sorted(g) for g in groups],
        "events": events[-200:],
    }
    if args.json:
        print(json.dumps(report, indent=1))
        return 0

    print(f"\n  == {done} rounds over {done * args.interval / 60:.1f} min ==")
    bad = [t for t in report["targets"] if t["fails"]]
    if not bad:
        print("  no failures on any target — nothing dropped during the window.")
        print("  ⚠️  That is NOT an all-clear for an intermittent fault; it means "
              "this window missed it. Run longer, or when it is happening.")
        return 0
    print(f"  {'TARGET':<16} {'LABEL':<14} {'LOSS':>6}  FAILS")
    for t in sorted(bad, key=lambda x: -x["fails"]):
        print(f"  {t['addr']:<16} {t['label'][:14]:<14} "
              f"{t['loss_pct']:>5.1f}%  {t['fails']}")

    if groups:
        print()
        for g in groups:
            print(f"  ⚠ THESE {len(g)} FAIL TOGETHER — one shared upstream, not "
                  f"{len(g)} faults:")
            for a in g:
                print(f"      {a}  ({label[a]})")
        print("\n  Next: `netdiag switchport <mac>` for the port each one lands "
              "on, and\n  `netdiag plc <iface>` — a powerline adapter in the "
              "shared path is\n  invisible to every IP-level probe.")
    else:
        print("\n  No co-failure grouping: these look like INDEPENDENT faults, "
              "so a\n  shared switch or link is unlikely to be the cause.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        os._exit(0)
