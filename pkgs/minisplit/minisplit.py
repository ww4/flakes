"""minisplit: local control of the Turbro mini split (a Tuya OEM unit).

Talks to the unit directly on the LAN with tinytuya over Tuya protocol 3.5,
with no app and no cloud. The device ID and local key come from a JSON file
written once by a Smart Life QR login (see modules/services/minisplit.nix).
Its "devices" list holds one entry with "id" and "local_key".

Every write is READ BACK and compared. A command that the unit silently
ignored exits non-zero instead of printing success.

Data points, measured on the live unit 2026-11-22 (cloud schema plus a read):
  1 power (bool) · 2 setpoint · 3 room temp (both x10, unit per DP 19)
  4 mode: cold|hot|wet|wind|auto · 5 fan (auto, ...) · 8 eco · 13 display
  light · 14 child lock · 19 unit "F"/"C" · 102 sleep
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import tinytuya

DEFAULT_DEVICE_FILE = os.path.expanduser("~/.config/minisplit/tuya-devices.json")

# What a person types -> what the unit calls it.
MODES = {"cool": "cold", "heat": "hot", "dry": "wet", "fan": "wind", "auto": "auto"}
MODE_NAMES = {v: k for k, v in MODES.items()}

# Accepted setpoint range in °F. Deliberately narrower than the cloud schema
# (16.0-90.0, unit-agnostic). A typo like "7" or "700" is refused here rather
# than sent to a heat pump.
MIN_F, MAX_F = 60.0, 86.0


class MiniSplitError(RuntimeError):
    pass


def connect() -> tinytuya.Device:
    path = os.environ.get("MINISPLIT_DEVICE_FILE", DEFAULT_DEVICE_FILE)
    host = os.environ.get("MINISPLIT_HOST")
    if not host:
        raise MiniSplitError("MINISPLIT_HOST is not set")
    try:
        with open(path) as f:
            dev = json.load(f)["devices"][0]
    except (OSError, KeyError, IndexError, json.JSONDecodeError) as exc:
        raise MiniSplitError(f"cannot read device file {path}: {exc.__class__.__name__}") from exc
    d = tinytuya.Device(dev["id"], host, dev["local_key"],
                        version=float(os.environ.get("MINISPLIT_PROTOCOL", "3.5")))
    d.set_socketTimeout(5)
    d.set_socketRetryLimit(2)
    return d


def _check(resp: Any, what: str) -> dict:
    if not isinstance(resp, dict) or "Error" in resp:
        err = resp.get("Error") if isinstance(resp, dict) else repr(resp)
        raise MiniSplitError(f"{what} failed: {err}")
    return resp


def read_dps(d: tinytuya.Device) -> dict:
    dps = _check(d.status(), "status").get("dps")
    if not dps:
        raise MiniSplitError("status returned no data points")
    return dps


def summarize(dps: dict) -> dict:
    def tenths(v: Any) -> float | None:
        return None if v is None else v / 10
    return {
        "power": dps.get("1"),
        "mode": MODE_NAMES.get(dps.get("4"), dps.get("4")),
        "setpoint": tenths(dps.get("2")),
        "room_temp": tenths(dps.get("3")),
        "unit": dps.get("19"),
        "fan": dps.get("5"),
        "eco": dps.get("8"),
        "sleep": dps.get("102"),
        "display_light": dps.get("13"),
        "child_lock": dps.get("14"),
    }


def write(d: tinytuya.Device, dp: str, value: Any) -> dict:
    _check(d.set_value(int(dp), value), f"set DP {dp}")
    for _ in range(3):  # the unit can take a moment to report the new state
        time.sleep(1.5)
        dps = read_dps(d)
        if dps.get(dp) == value:
            return dps
    raise MiniSplitError(f"unit did not accept DP {dp}={value!r}; it reports {dps.get(dp)!r}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="minisplit", description=__doc__.splitlines()[0])
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show current state")
    sub.add_parser("on", help="power on")
    sub.add_parser("off", help="power off")
    t = sub.add_parser("temp", help=f"set the setpoint in °F ({MIN_F:g}-{MAX_F:g})")
    t.add_argument("degrees", type=float, help="rounded to a whole degree")
    m = sub.add_parser("mode", help="set the operating mode")
    m.add_argument("mode", choices=sorted(MODES))
    sub.add_parser("raw", help="dump every data point (debugging)")
    args = p.parse_args(argv)

    try:
        d = connect()
        if args.cmd == "status":
            dps = read_dps(d)
        elif args.cmd == "raw":
            print(json.dumps(read_dps(d), indent=2, sort_keys=True))
            return 0
        elif args.cmd in ("on", "off"):
            dps = write(d, "1", args.cmd == "on")
        elif args.cmd == "temp":
            if not MIN_F <= args.degrees <= MAX_F:
                raise MiniSplitError(f"{args.degrees:g} °F is outside {MIN_F:g}-{MAX_F:g}")
            current = read_dps(d)
            if current.get("19") != "F":
                raise MiniSplitError(f"unit is set to {current.get('19')!r}, not F; refusing to guess")
            dps = write(d, "2", int(round(args.degrees)) * 10)  # schema step is 10 = 1.0 degree
        elif args.cmd == "mode":
            dps = write(d, "4", MODES[args.mode])
        else:  # pragma: no cover - argparse enforces the choices
            raise MiniSplitError(f"unknown command {args.cmd}")
    except MiniSplitError as exc:
        print(f"minisplit: {exc}", file=sys.stderr)
        return 2

    s = summarize(dps)
    if args.json:
        print(json.dumps(s))
    else:
        u = s["unit"] or "?"
        print(f"power {'ON' if s['power'] else 'off'} · mode {s['mode']} · "
              f"set {s['setpoint']:g}°{u} · room {s['room_temp']:g}°{u} · fan {s['fan']}")
        flags = [k for k in ("eco", "sleep", "child_lock") if s[k]]
        if flags:
            print("on: " + ", ".join(flags))
    return 0


if __name__ == "__main__":
    sys.exit(main())
