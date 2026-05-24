#!/usr/bin/env python3
"""Backfill river data from USGS Water Services into Prometheus.

Fetches stage (00065) and flow (00060) for the configured gauges over the
requested time range, writes OpenMetrics text-format with explicit Unix-
seconds timestamps, and prints the path so the caller can pipe it through
`promtool tsdb create-blocks-from openmetrics`.

Usage:
  riverwatch-backfill.py --gauge LPTK2 --start 2020-01-01 --end 2026-05-24 --freq uv
  riverwatch-backfill.py --gauge LPTK2 --start 1925-10-01 --end 1989-09-30 --freq dv --params 00060
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

# Per-gauge config — USGS site ID and flood thresholds (from NWPS gauge metadata).
# Thresholds are static per gauge and apply to the entire backfill period.
GAUGES = {
    "LPTK2": {
        "usgs": "03290500",
        "thresholds": {"action": 30.0, "minor": 33.0, "moderate": 43.0, "major": 49.0},
    },
    "GSTK2": {
        "usgs": "03290080",
        "thresholds": {},  # NWPS reports no flood thresholds for this gauge
    },
}

CATEGORIES = ["action", "minor", "moderate", "major", "no_flooding"]
PARAM_TO_METRIC = {
    "00065": ("riverwatch_stage_ft", "Current observed stage (ft)", 1.0),
    "00060": ("riverwatch_flow_kcfs", "Current observed flow (kcfs)", 1.0 / 1000.0),  # USGS gives cfs; we want kcfs
}


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def chunk_range(start: datetime, end: datetime, days: int):
    """Yield (chunk_start, chunk_end) tuples covering [start, end] in <=days windows."""
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=days), end)
        yield cur, nxt
        cur = nxt + timedelta(seconds=1)


def fetch_usgs(site: str, start: datetime, end: datetime, params: list[str], freq: str) -> dict:
    url = f"https://waterservices.usgs.gov/nwis/{freq}/"
    r = httpx.get(
        url,
        params={
            "sites": site,
            "startDT": start.strftime("%Y-%m-%d"),
            "endDT": end.strftime("%Y-%m-%d"),
            "parameterCd": ",".join(params),
            "format": "json",
            "siteStatus": "all",
        },
        timeout=300.0,
        follow_redirects=True,
    )
    r.raise_for_status()
    return r.json()


def active_category(stage_ft: float, thresholds: dict[str, float]) -> str:
    if not thresholds:
        return "no_flooding"
    if stage_ft >= thresholds.get("major", float("inf")):
        return "major"
    if stage_ft >= thresholds.get("moderate", float("inf")):
        return "moderate"
    if stage_ft >= thresholds.get("minor", float("inf")):
        return "minor"
    if stage_ft >= thresholds.get("action", float("inf")):
        return "action"
    return "no_flooding"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gauge", required=True, choices=GAUGES.keys())
    ap.add_argument("--start", required=True, help="ISO date YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="ISO date YYYY-MM-DD")
    ap.add_argument("--freq", choices=["iv", "dv"], default="iv",
                    help="USGS endpoint: iv=instantaneous (15-min), dv=daily values")
    ap.add_argument("--params", default="00065,00060",
                    help="comma-separated USGS parameter codes (00065=stage, 00060=flow)")
    ap.add_argument("--chunk-days", type=int, default=365,
                    help="days per USGS request — keep ≤365 for iv, can be larger for dv")
    ap.add_argument("--out", required=True, help="output OpenMetrics file path")
    args = ap.parse_args()

    gauge = args.gauge
    cfg = GAUGES[gauge]
    site = cfg["usgs"]
    thresholds = cfg["thresholds"]
    params = args.params.split(",")
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    samples_emitted = 0
    total_chunks = sum(1 for _ in chunk_range(start, end, args.chunk_days))
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    requested_params = set(params)
    will_compute_categories = "00065" in requested_params  # stage drives the category
    will_emit_thresholds = will_compute_categories and bool(thresholds)

    # NOTE on labels: we deliberately do NOT add the scrape-time labels
    # (instance, job) here. promtool 3.x has a quirk where some subset of
    # samples lose those labels during block creation, producing a phantom
    # series. The cleanest workaround is to never set them in the backfill
    # at all and unify in Grafana via `sum without(instance, job) (…)`.
    SCRAPE_LABELS = ""

    # Buffer all (timestamp, line) tuples so we can sort by timestamp at
    # the end — USGS returns separate time-series per parameter, so writing
    # in API order produces samples not in monotonic order, which promtool
    # rejects with "out of order sample".
    rows: list[tuple[int, str]] = []

    for i, (cs, ce) in enumerate(chunk_range(start, end, args.chunk_days), 1):
        print(f"[{gauge}/{args.freq}] chunk {i}/{total_chunks}: {cs.date()} → {ce.date()}",
              file=sys.stderr)
        try:
            data = fetch_usgs(site, cs, ce, params, args.freq)
        except Exception as e:
            print(f"  WARN: fetch failed: {e!r}", file=sys.stderr)
            continue

        ts_blocks = data.get("value", {}).get("timeSeries", []) or []
        for ts in ts_blocks:
            var_code = ts["variable"]["variableCode"][0]["value"]
            if var_code not in PARAM_TO_METRIC:
                continue
            metric_name, _, unit_factor = PARAM_TO_METRIC[var_code]

            for value_set in ts.get("values", []):
                for v in value_set.get("value", []):
                    try:
                        raw = float(v["value"])
                    except (ValueError, TypeError):
                        continue
                    if raw < -1000:  # USGS uses -999999 for missing
                        continue
                    scaled = raw * unit_factor
                    dt = parse_iso(v["dateTime"])
                    unix_ts = int(dt.timestamp())
                    rows.append((unix_ts, f'{metric_name}{{gauge="{gauge}"}} {scaled} {unix_ts}\n'))

                    if var_code == "00065" and will_compute_categories:
                        cat = active_category(scaled, thresholds)
                        for c in CATEGORIES:
                            val = 1 if c == cat else 0
                            rows.append((unix_ts, f'riverwatch_flood_category_active{{gauge="{gauge}",category="{c}"}} {val} {unix_ts}\n'))

    # Threshold boundary rows at start + end of the range
    if will_emit_thresholds:
        for cat, th in thresholds.items():
            rows.append((int(start.timestamp()),
                         f'riverwatch_flood_threshold_ft{{gauge="{gauge}",category="{cat}"}} {th} {int(start.timestamp())}\n'))
            rows.append((int(end.timestamp()),
                         f'riverwatch_flood_threshold_ft{{gauge="{gauge}",category="{cat}"}} {th} {int(end.timestamp())}\n'))

    # Sort by timestamp (stable — preserves order of same-timestamp rows).
    rows.sort(key=lambda x: x[0])
    samples_emitted = len(rows)

    with out_path.open("w") as f:
        # All # HELP / # TYPE headers up front — OpenMetrics requires them
        # before any data line of that metric family.
        for code, (mname, htext, _) in PARAM_TO_METRIC.items():
            if code in requested_params:
                f.write(f"# HELP {mname} {htext}\n")
                f.write(f"# TYPE {mname} gauge\n")
        if will_compute_categories:
            f.write("# HELP riverwatch_flood_category_active 1 if the gauge is currently in this category\n")
            f.write("# TYPE riverwatch_flood_category_active gauge\n")
        if will_emit_thresholds:
            f.write("# HELP riverwatch_flood_threshold_ft Stage threshold for this flood category\n")
            f.write("# TYPE riverwatch_flood_threshold_ft gauge\n")
        for _, line in rows:
            f.write(line)
        f.write("# EOF\n")

    print(f"[{gauge}/{args.freq}] wrote {samples_emitted} samples → {out_path}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
