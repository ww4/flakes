"""Entry point: `roku-api` serves the JSON front end; `roku-find` prints what
SSDP can see, which is the first thing to run when something is not answering.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading

from roku import discover
from roku.api import Device, serve
from roku.ecp import Roku, RokuError

log = logging.getLogger("roku")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="", help="the Roku's address; discovered when omitted")
    ap.add_argument("--port", type=int, default=8060, help="ECP port on the device")
    ap.add_argument("--listen", default="127.0.0.1")
    ap.add_argument("--api-port", type=int, default=8793)
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--allow-power", action="store_true",
                    help="permit PowerOn/PowerOff; off by default, because it is "
                         "one request from a dark television somebody is watching")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stdout)

    device = Device(args.host, args.port, args.timeout)
    # Say at startup what was configured and what was found: an address nobody
    # typed is the thing you want in the journal when it later stops working.
    if args.host:
        log.info("Roku at %s:%d (configured)", args.host, args.port)
    else:
        hits = discover.find()
        log.info("discovery found %d Roku(s)%s", len(hits),
                 ": " + ", ".join(h["host"] for h in hits) if hits else "")
    log.info("power keys %s", "allowed" if args.allow_power else "disabled")

    serve(device, args.listen, args.api_port, args.allow_power)
    log.info("roku api on %s:%d", args.listen, args.api_port)

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    # A polling wait rather than a bare wait(): the same lesson as netradio's
    # speaker, where an untimed wait did not come back from a C-level acquire.
    while not stop.wait(1.0):
        pass
    return 0


def find(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="List the Rokus that answer SSDP.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--timeout", type=float, default=4.0)
    args = ap.parse_args(argv)
    hits = discover.find(timeout=args.timeout)
    if args.json:
        print(json.dumps(hits, indent=1))
        return 0 if hits else 1
    if not hits:
        print("No Roku answered. A suspended box can be silent to SSDP and still\n"
              "answer ECP — try `curl http://<address>:8060/query/device-info`.")
        return 1
    for h in hits:
        try:
            info = Roku(h["host"], h["port"]).status()
            print(f"{h['host']}:{h['port']}  {info['name']} ({info['model']}) — {info['power']}")
        except RokuError as e:
            print(f"{h['host']}:{h['port']}  answered SSDP but not ECP: {e}")
    return 0
