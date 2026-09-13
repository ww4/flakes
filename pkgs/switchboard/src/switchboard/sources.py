"""Read-only views of the box for the fast path. Nothing here changes state.

Each reader returns plain Python and raises SourceError on a lookup failure,
so an intent can tell "nothing wrong" from "could not look" — the two must
never be reported the same way (a dead Prometheus is not a cool CPU).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings

log = logging.getLogger(__name__)


class SourceError(RuntimeError):
    pass


# ---------------------------------------------------------------- prometheus

async def prom_query(settings: Settings, expr: str) -> list[tuple[dict[str, str], float]]:
    """Instant query -> [(labels, value)]. Empty list means the query matched
    nothing, which is a real answer; a transport/HTTP failure is SourceError."""
    try:
        async with httpx.AsyncClient(timeout=settings.prometheus_timeout_s) as client:
            resp = await client.get(f"{settings.prometheus_url}/api/v1/query", params={"query": expr})
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceError(f"prometheus: {exc}") from exc
    if body.get("status") != "success":
        raise SourceError(f"prometheus: {body.get('error', 'non-success status')}")
    out: list[tuple[dict[str, str], float]] = []
    for row in body["data"]["result"]:
        try:
            out.append((row["metric"], float(row["value"][1])))
        except (KeyError, ValueError, TypeError):
            continue
    return out


@dataclass(frozen=True)
class Temps:
    cpu_c: float | None
    nvme_c: float | None
    drives: dict[str, float]      # device -> °C, e.g. {"sda": 41.0}

    @property
    def hottest_drive(self) -> tuple[str, float] | None:
        return max(self.drives.items(), key=lambda kv: kv[1]) if self.drives else None


async def temps(settings: Settings) -> Temps:
    hw, drv = await asyncio.gather(
        prom_query(settings, 'node_hwmon_temp_celsius{sensor="temp1"}'),
        prom_query(settings, "gromit_drive_temp_celsius"),
    )
    cpu = nvme = None
    for labels, value in hw:
        chip = labels.get("chip", "")
        if "coretemp" in chip:
            cpu = value
        elif chip.startswith("nvme"):
            nvme = value
    return Temps(
        cpu_c=cpu,
        nvme_c=nvme,
        drives={lab.get("device", "?"): v for lab, v in drv},
    )


@dataclass(frozen=True)
class Disk:
    mountpoint: str
    avail_bytes: float
    size_bytes: float

    @property
    def free_fraction(self) -> float:
        return self.avail_bytes / self.size_bytes if self.size_bytes else 0.0


async def disks(settings: Settings) -> list[Disk]:
    sel = "|".join(settings.disk_paths)
    avail, size = await asyncio.gather(
        prom_query(settings, f'node_filesystem_avail_bytes{{mountpoint=~"{sel}"}}'),
        prom_query(settings, f'node_filesystem_size_bytes{{mountpoint=~"{sel}"}}'),
    )
    sizes = {lab["mountpoint"]: v for lab, v in size}
    out = [Disk(lab["mountpoint"], v, sizes.get(lab["mountpoint"], 0.0)) for lab, v in avail]
    # Keep the caller's ordering so "fusion first" holds in the spoken answer.
    order = {p: i for i, p in enumerate(settings.disk_paths)}
    return sorted(out, key=lambda d: order.get(d.mountpoint, 99))


# ---------------------------------------------------------------- systemd

async def _systemctl(settings: Settings, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        settings.systemctl_bin, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode not in (0, 1, 3, 4):
        # 0 ok; is-active/is-failed use 1/3/4 for inactive/unknown — all real answers.
        raise SourceError(f"systemctl {' '.join(args)}: exit {proc.returncode}: {err.decode(errors='replace').strip()}")
    return proc.returncode, out.decode(errors="replace")


async def failed_units(settings: Settings) -> list[str]:
    _, out = await _systemctl(settings, "--failed", "--no-legend", "--plain")
    return [line.split()[0] for line in out.splitlines() if line.strip()]


async def inactive_units(settings: Settings, units: list[str]) -> list[str]:
    """Which of `units` are NOT active. Uses one is-active call: output is one
    state per line in argument order."""
    if not units:
        return []
    _, out = await _systemctl(settings, "is-active", *units)
    states = out.split()
    if len(states) != len(units):
        raise SourceError(f"systemctl is-active returned {len(states)} states for {len(units)} units")
    return [u for u, s in zip(units, states) if s != "active"]


# ---------------------------------------------------------------- sentinel

@dataclass(frozen=True)
class Incident:
    kind: str          # "failed-units", "seeding-health", ...
    age_s: float
    headline: str

    @property
    def age_h(self) -> float:
        return self.age_s / 3600.0


def recent_incidents(settings: Settings, within_h: float = 24.0, now: float | None = None) -> list[Incident]:
    """Sentinel incident files newer than within_h, newest first.

    File names are <kind>-<epoch>.txt and the first line is the headline. A
    missing directory is a SourceError, not "no incidents".
    """
    d: Path = settings.sentinel_incidents
    if not d.is_dir():
        raise SourceError(f"sentinel incidents dir missing: {d}")
    now = time.time() if now is None else now
    found: list[Incident] = []
    for f in d.glob("*-*.txt"):
        stem = f.stem
        kind, _, epoch = stem.rpartition("-")
        if not epoch.isdigit():
            continue
        age = now - int(epoch)
        if age > within_h * 3600:
            continue
        try:
            first = f.read_text(errors="replace").splitlines()[0]
        except (OSError, IndexError):
            first = kind
        # Strip the "[kind] " prefix the sentinel writes.
        if first.startswith(f"[{kind}]"):
            first = first[len(kind) + 2:].strip()
        found.append(Incident(kind=kind, age_s=age, headline=first))
    found.sort(key=lambda i: i.age_s)
    return found


# ---------------------------------------------------------------- NWS forecast

@dataclass(frozen=True)
class Period:
    name: str            # "This Afternoon", "Tonight", "Monday", "Monday Night"
    date: str            # local date of startTime, YYYY-MM-DD
    daytime: bool
    temperature: int
    short: str           # "Mostly Sunny"
    pop: int | None      # chance of precipitation, percent


async def nws_forecast(settings: Settings) -> list[Period]:
    """The NWS gridpoint forecast periods, cached for forecast_cache_s.
    The cache lives in the state dir so the prewarm timer and calls share it."""
    cache = settings.state_dir / "cache" / "nws-forecast.json"
    try:
        if time.time() - cache.stat().st_mtime < settings.forecast_cache_s:
            return [Period(**d) for d in json.loads(cache.read_text())]
    except (OSError, ValueError, TypeError):
        pass
    try:
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "gromit-switchboard"}) as client:
            resp = await client.get(settings.nws_forecast_url)
        resp.raise_for_status()
        raw = resp.json()["properties"]["periods"]
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise SourceError(f"nws: {exc}") from exc
    periods = [
        Period(
            name=p["name"], date=p["startTime"][:10], daytime=bool(p["isDaytime"]),
            temperature=int(p["temperature"]), short=p["shortForecast"],
            pop=(p.get("probabilityOfPrecipitation") or {}).get("value"),
        )
        for p in raw
    ]
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".json.part")
    tmp.write_text(json.dumps([p.__dict__ for p in periods]))
    tmp.replace(cache)
    return periods


# ---------------------------------------------------------------- bitcoin (mempool backend)

@dataclass(frozen=True)
class Bitcoin:
    usd: int
    price_age_s: float
    height: int
    fee_fast: int      # sat/vB
    fee_hour: int
    fee_economy: int


async def bitcoin(settings: Settings) -> Bitcoin:
    async def get(path: str):
        resp = await client.get(f"{settings.mempool_url}{path}")
        resp.raise_for_status()
        return resp.json() if path != "/api/blocks/tip/height" else int(resp.text)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            prices, height, fees = await asyncio.gather(
                get("/api/v1/prices"), get("/api/blocks/tip/height"), get("/api/v1/fees/recommended"))
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceError(f"mempool: {exc}") from exc
    try:
        return Bitcoin(
            usd=int(prices["USD"]), price_age_s=max(0.0, time.time() - float(prices["time"])),
            height=int(height), fee_fast=int(fees["fastestFee"]), fee_hour=int(fees["hourFee"]),
            fee_economy=int(fees["economyFee"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceError(f"mempool: unexpected shape: {exc}") from exc
