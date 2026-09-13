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
    usd_24h_ago: int | None   # from the backend's hourly price history
    height: int
    fee_fast: int      # sat/vB
    fee_hour: int
    fee_economy: int

    @property
    def change_24h_pct(self) -> float | None:
        if not self.usd_24h_ago:
            return None
        return (self.usd - self.usd_24h_ago) / self.usd_24h_ago * 100


@dataclass(frozen=True)
class BitcoinStats:
    ath_usd: int
    ath_date: str            # YYYY-MM-DD
    retarget_date: str       # ISO 8601 UTC from the backend
    retarget_change_pct: float
    retarget_blocks: int
    nodes: int | None        # reachable nodes, or None if btcnodes was unreachable
    nodes_age_s: float | None


async def _mempool_get(client: httpx.AsyncClient, settings: Settings, path: str, params: dict | None = None):
    resp = await client.get(f"{settings.mempool_url}{path}", params=params)
    resp.raise_for_status()
    return int(resp.text) if path == "/api/blocks/tip/height" else resp.json()


async def bitcoin(settings: Settings) -> Bitcoin:
    """Price (+24 h ago), tip and fees — everything local, ~0.1 s."""
    day_ago = int(time.time()) - 86400
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            prices, height, fees, hist = await asyncio.gather(
                _mempool_get(client, settings, "/api/v1/prices"),
                _mempool_get(client, settings, "/api/blocks/tip/height"),
                _mempool_get(client, settings, "/api/v1/fees/recommended"),
                _mempool_get(client, settings, "/api/v1/historical-price", {"currency": "USD", "timestamp": day_ago}),
            )
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceError(f"mempool: {exc}") from exc
    try:
        pts = hist.get("prices") or []
        usd_24h = int(pts[0]["USD"]) if pts and pts[0].get("USD") else None
        return Bitcoin(
            usd=int(prices["USD"]), price_age_s=max(0.0, time.time() - float(prices["time"])), usd_24h_ago=usd_24h,
            height=int(height), fee_fast=int(fees["fastestFee"]), fee_hour=int(fees["hourFee"]),
            fee_economy=int(fees["economyFee"]),
        )
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise SourceError(f"mempool: unexpected shape: {exc}") from exc


async def _btcnodes(settings: Settings) -> tuple[int, float] | None:
    """(reachable nodes, age of the snapshot) — cached; None if the site is down."""
    cache = settings.state_dir / "cache" / "btcnodes.json"
    try:
        if time.time() - cache.stat().st_mtime < settings.nodes_cache_s:
            d = json.loads(cache.read_text())
            return int(d["total_nodes"]), time.time() - float(d["timestamp"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, headers={"User-Agent": "gromit-switchboard"}) as client:
            resp = await client.get(settings.btcnodes_url)
        resp.raise_for_status()
        snap = resp.json()["results"][0]
        d = {"total_nodes": int(snap["total_nodes"]), "timestamp": float(snap["timestamp"])}
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        log.warning("btcnodes: %s", exc)
        return None
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".json.part"); tmp.write_text(json.dumps(d)); tmp.replace(cache)
    return d["total_nodes"], time.time() - d["timestamp"]


async def bitcoin_stats(settings: Settings) -> BitcoinStats:
    """ATH (from the backend's full daily history), next difficulty adjustment, node count."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            hist, diff = await asyncio.gather(
                _mempool_get(client, settings, "/api/v1/historical-price", {"currency": "USD"}),
                _mempool_get(client, settings, "/api/v1/difficulty-adjustment"),
            )
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceError(f"mempool: {exc}") from exc
    nodes = await _btcnodes(settings)
    try:
        pts = [p for p in hist["prices"] if p.get("USD")]
        top = max(pts, key=lambda p: p["USD"])
        import datetime as _dt
        return BitcoinStats(
            ath_usd=int(top["USD"]), ath_date=_dt.datetime.fromtimestamp(int(top["time"]), _dt.timezone.utc).date().isoformat(),
            retarget_date=diff["estimatedRetargetDate"] if isinstance(diff["estimatedRetargetDate"], str)
                          else _dt.datetime.fromtimestamp(diff["estimatedRetargetDate"] / 1000, _dt.timezone.utc).isoformat(),
            retarget_change_pct=float(diff["difficultyChange"]), retarget_blocks=int(diff["remainingBlocks"]),
            nodes=nodes[0] if nodes else None, nodes_age_s=nodes[1] if nodes else None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceError(f"mempool: unexpected shape: {exc}") from exc


# ---------------------------------------------------------------- ntfy notifications

@dataclass(frozen=True)
class Notification:
    time: float
    title: str
    message: str
    priority: int    # ntfy 1-5, 3 = default


async def ntfy_recent(settings: Settings, hours: float | None = None) -> list[Notification]:
    """Messages on the topic in the last N hours, newest first (ntfy's poll API)."""
    hours = hours or settings.notifications_hours
    if not settings.ntfy_user:
        raise SourceError("ntfy: no subscriber credential in the environment")
    try:
        async with httpx.AsyncClient(timeout=5.0, auth=(settings.ntfy_user, settings.ntfy_pass)) as client:
            resp = await client.get(f"{settings.ntfy_url}/{settings.ntfy_topic}/json", params={"poll": "1", "since": f"{int(hours)}h"})
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise SourceError(f"ntfy: {exc}") from exc
    out = []
    for line in resp.text.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("event") != "message":
            continue
        out.append(Notification(time=float(d.get("time", 0)), title=d.get("title") or "", message=d.get("message") or "",
                                priority=int(d.get("priority") or 3)))
    return sorted(out, key=lambda n: -n.time)
