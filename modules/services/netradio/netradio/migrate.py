"""One-time changes to the RUNTIME config, applied on the box.

The station list lives in config/stations.json and is Chris's to edit, so a
seed change in the flake only reaches a fresh install. When a change should
reach an existing box too — splitting a station, adding a setting — it is
written here as a named, idempotent migration; the credentials oneshot runs
`netradio migrate` after seeding and records what ran in migrations.json.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from netradio.config import Config


def split_blues_jazz_soul(cfg: Config) -> str:
    """Blues, Jazz & Soul → Blues / Jazz / Soul & R&B (2026-09-16). Slots on
    the old station move to Blues."""
    stations = cfg.stations()
    mounts = {s["mount"] for s in stations}
    if "blues-jazz" not in mounts or {"blues", "jazz", "soul"} & mounts:
        return "nothing to do"
    old = next(s for s in stations if s["mount"] == "blues-jazz")
    i = stations.index(old)
    era = (old.get("base") or {}).get("era")
    def st(mount, name, genres):
        s = {"mount": mount, "name": name, "kind": "curated", "family": ["blues-jazz"], "base": {"genres": genres}}
        if era:
            s["base"]["era"] = era
        if old.get("breaks_every") is not None:
            s["breaks_every"] = old["breaks_every"]
        return s
    stations[i:i + 1] = [
        st("blues", "Blues", ["blues"]),
        st("jazz", "Jazz", ["jazz", "big band", "swing", "fusion", "bebop", "dixieland"]),
        st("soul", "Soul & R&B", ["soul and r&b", "soul", "r&b", "funk", "motown"]),
    ]
    cfg.save_stations(stations)
    slots = cfg.schedule()
    for s in slots:
        if s.get("station") == "blues-jazz":
            s["station"] = "blues"
    cfg.save_schedule(slots)
    return "split blues-jazz into blues / jazz / soul"


MIGRATIONS = [
    ("split-blues-jazz-soul", split_blues_jazz_soul),
]


def run(cfg: Config) -> list[str]:
    done = cfg._read("migrations.json", {})
    out = []
    for name, fn in MIGRATIONS:
        if name in done:
            continue
        result = fn(cfg)
        done[name] = result
        cfg._write("migrations.json", done)
        out.append(f"{name}: {result}")
        if result != "nothing to do":
            # the station list may have changed: rescan, and restart Liquidsoap
            # if the mounts did (the apply path unit decides)
            cfg.request("apply", {"reason": f"migration {name}"})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True, type=Path)
    args = ap.parse_args(argv)
    for line in run(Config(args.config)):
        print(line)
    return 0
