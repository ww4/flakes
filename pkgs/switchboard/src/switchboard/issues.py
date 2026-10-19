"""What's wrong right now — spoken first, before the greeting's question.

Chris, 2026-09-13: "If something is at warning or critical stage, I want to
hear that first thing." The sources that already define warning/critical on
this box, in the order they're spoken (critical before warning):

  Alertmanager   firing alerts with a severity label (the river rules today)
  sentinel       state.json: a check is `active` while its condition holds and
                 is cleared the moment it stops — the live view, not the
                 incident history. Severity comes from the check config; the
                 newest incident file for that id supplies the headline.
  units / pool   failed units, unmounted pool members (critical: data is
                 unreachable)
  temps          CPU / NVMe / HDD against the thresholds the monitoring notes
                 carry (gromit-temp-monitoring)

Every reader is best-effort: one that cannot be read is reported as its own
warning ("couldn't read X"), never silently skipped — a dead source is not
a quiet system.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

import httpx

from . import sources
from .config import Settings
from .sources import SourceError

log = logging.getLogger(__name__)

# From memory `gromit-temp-monitoring` (verified against hardware).
TEMP_WARN = {"cpu": 75, "nvme": 65, "hdd": 50}
TEMP_CRIT = {"cpu": 85, "nvme": 75, "hdd": 58}

# sentinel check id -> severity (sentinel.nix); unknown ids are warnings.
SENTINEL_SEVERITY = {"drive-smart-failed": "critical"}
SENTINEL_SKIP = {"selftest", "agenttest", "acttest"}


@dataclass(frozen=True)
class Issue:
    severity: str    # "critical" | "warning"
    text: str        # one spoken sentence, no trailing period needed

    @property
    def rank(self) -> int:
        return 0 if self.severity == "critical" else 1


def _words(ident: str) -> str:
    return ident.replace("-", " ").replace("_", " ")


def spoken_detail(raw: str, cid: str) -> str:
    """A sentinel evidence line, reduced to something worth saying aloud.

    Chris, 2026-10-19: *"Hearing cryptic readouts is confusing and
    unnecessary."* He is right, and the phone is the worst place for it.
    Sentinel evidence lines come in two shapes. Some are written for a human:

        nginx is 'failed' (expected active) — EVERY vhost on the box is
        unreachable

    Others are a bare threshold expression, and they carry the check id twice
    before it:

        [swap-headroom] swap-headroom: 127.0.0.1:9100=7.9621

    Spoken, that second one becomes "one twenty seven dot zero dot zero dot
    one colon nine one zero zero equals seven point nine six two one", which
    tells him nothing he could act on and buries the part that matters — that
    swap headroom is the thing complaining.

    So: strip both id prefixes, then only keep what is left if it reads like a
    sentence. Otherwise say nothing and let the caller speak the check name
    alone. Same rule as the dashboard's briefing card
    (modules/services/briefing in ww4/flakes) — deliberately duplicated rather
    than shared, because a ten-line heuristic is not worth coupling a phone
    system to a web dashboard.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith(f"[{cid}]"):
        text = text[len(cid) + 2:]
    text = text.strip()
    prefix = f"{cid}:"
    if text.lower().startswith(prefix.lower()):
        text = text[len(prefix):]
    text = text.strip()
    # A sentence has a space in it and some length. A token like
    # "/=90.8903" has neither, and is not worth a second of phone time.
    if len(text) < 12 or " " not in text:
        return ""
    return text


# ---------------------------------------------------------------- readers

async def alertmanager(settings: Settings) -> list[Issue]:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{settings.alertmanager_url}/api/v2/alerts",
                                    params={"active": "true", "silenced": "false", "inhibited": "false"})
        resp.raise_for_status()
        alerts = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceError(f"alertmanager: {exc}") from exc
    out = []
    for a in alerts:
        labels = a.get("labels", {})
        sev = labels.get("severity", "warning")
        if sev not in ("warning", "critical"):
            continue
        name = _words(labels.get("alertname", "alert"))
        summary = (a.get("annotations") or {}).get("summary") or ""
        out.append(Issue(sev, f"{sev} alert, {name}" + (f": {summary}" if summary else "")))
    return out


def sentinel_active(settings: Settings) -> list[Issue]:
    try:
        state = json.loads(settings.sentinel_state.read_text())
    except (OSError, ValueError) as exc:
        raise SourceError(f"sentinel state: {exc}") from exc
    out = []
    for cid, st in (state.get("checks") or {}).items():
        if cid in SENTINEL_SKIP or not isinstance(st, dict) or not st.get("active"):
            continue
        sev = SENTINEL_SEVERITY.get(cid, "warning")
        headline = ""
        try:
            newest = max(settings.sentinel_incidents.glob(f"{cid}-*.txt"), key=lambda p: p.stat().st_mtime)
            first = newest.read_text(errors="replace").splitlines()[0]
            headline = spoken_detail(first, cid)
        except (ValueError, OSError, IndexError):
            pass
        out.append(Issue(sev, f"sentinel {_words(cid)}" + (f": {headline}" if headline else "")))
    return out


async def units_and_pool(settings: Settings) -> list[Issue]:
    failed, down = await asyncio.gather(sources.failed_units(settings), sources.inactive_units(settings, settings.pool_mount_units))
    out = []
    if failed:
        names = ", ".join(_words(u.rsplit(".", 1)[0]) for u in failed[:3])
        more = f" and {len(failed) - 3} more" if len(failed) > 3 else ""
        out.append(Issue("warning", f"{len(failed)} failed unit{'s' if len(failed) != 1 else ''}: {names}{more}"))
    if down:
        names = ", ".join(u.split("-")[-1].split(".")[0] for u in down)
        out.append(Issue("critical", f"pool drive{'s' if len(down) != 1 else ''} {names} not mounted"))
    return out


async def temps(settings: Settings) -> list[Issue]:
    t = await sources.temps(settings)
    out = []

    def check(label: str, kind: str, value: float | None):
        if value is None:
            return
        if value >= TEMP_CRIT[kind]:
            out.append(Issue("critical", f"{label} at {round(value)} degrees, critical"))
        elif value >= TEMP_WARN[kind]:
            out.append(Issue("warning", f"{label} at {round(value)} degrees, warning"))

    check("CPU", "cpu", t.cpu_c)
    check("NVMe", "nvme", t.nvme_c)
    for dev, deg in sorted(t.drives.items(), key=lambda kv: -kv[1]):
        check(f"drive {dev}", "hdd", deg)
    return out


# ---------------------------------------------------------------- summary

async def current(settings: Settings) -> list[Issue]:
    """Everything wrong right now, critical first. Reader failures become warnings."""
    issues: list[Issue] = []

    async def run(name: str, coro):
        try:
            issues.extend(await coro)
        except SourceError as exc:
            log.warning("issues: %s", exc)
            issues.append(Issue("warning", f"couldn't read {name}"))

    async def sentinel():
        return sentinel_active(settings)

    await asyncio.gather(
        run("the alert manager", alertmanager(settings)),
        run("the sentinel", sentinel()),
        run("system units", units_and_pool(settings)),
        run("temperatures", temps(settings)),
    )
    return sorted(issues, key=lambda i: i.rank)


def spoken(issues: list[Issue], limit: int = 4) -> str:
    """'There are 2 issues. Critical: ... Warning: ...' — or '' when clean."""
    if not issues:
        return ""
    n = len(issues)
    parts = [f"There {'is' if n == 1 else 'are'} {n} issue{'s' if n != 1 else ''}."]
    for i in issues[:limit]:
        parts.append(f"{i.severity.capitalize()}: {i.text}.")
    if n > limit:
        parts.append(f"And {n - limit} more.")
    return " ".join(parts)
