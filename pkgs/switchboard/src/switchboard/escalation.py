"""The physical-hazard tier rings a phone.

Chris, 2026-08-19: nothing network-related may wake him; fire, flood and an
electrical fault may. This is the mechanism for the second half. Alertmanager
routes alerts labelled `tier="physical"` to a loopback webhook served here;
each firing alert becomes an outbound call (outbound.call_and_say) that says
what is wrong and asks for a 1 to acknowledge. Acknowledged alerts stop
calling; unacknowledged ones ring again on every Alertmanager repeat (hourly
on that route) until they resolve or are acknowledged.

State: <state>/calls/<fingerprint>.json — {called, acked, alertname, ...}.
The dialplan's announce context POSTs /ack/<fingerprint> when 1 is pressed
(func_curl); `switchboard call` still works without an id.

Payload: Alertmanager's webhook JSON ({"alerts": [{status, labels,
annotations, fingerprint, startsAt}]}); Grafana's webhook contact point sends
the same shape, so a Grafana rule can use this too.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import outbound
from .config import Settings

log = logging.getLogger(__name__)


@dataclass
class CallRecord:
    fingerprint: str
    alertname: str
    called_at: float = 0.0
    calls: int = 0
    acked_at: float = 0.0
    resolved_at: float = 0.0
    extra: dict = field(default_factory=dict)


def _path(settings: Settings, fp: str) -> Path:
    return settings.calls_dir / f"{re.sub(r'[^A-Za-z0-9_-]', '_', fp)}.json"


def load(settings: Settings, fp: str) -> CallRecord | None:
    try:
        d = json.loads(_path(settings, fp).read_text())
        return CallRecord(**d)
    except (OSError, ValueError, TypeError):
        return None


def save(settings: Settings, rec: CallRecord) -> None:
    settings.calls_dir.mkdir(parents=True, exist_ok=True)
    p = _path(settings, rec.fingerprint)
    tmp = p.with_suffix(".json.part")
    tmp.write_text(json.dumps(rec.__dict__))
    tmp.replace(p)


# ---------------------------------------------------------------- phrasing

def spoken(alert: dict) -> str:
    labels = alert.get("labels") or {}
    ann = alert.get("annotations") or {}
    name = labels.get("alertname", "alert").replace("_", " ")
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)   # RiverFloodMajor -> River Flood Major
    sev = labels.get("severity", "critical")
    what = ann.get("summary") or ann.get("description") or ""
    what = " ".join(what.split())
    return f"This is Gromit with a {sev} alert. {name}. {what} Press 1 to acknowledge, or I will call again in an hour."


# ---------------------------------------------------------------- decisions

def should_call(rec: CallRecord | None, now: float, min_gap_s: float) -> str | None:
    """Reason to place a call, or None."""
    if rec is None:
        return "first firing"
    if rec.acked_at:
        return None
    if now - rec.called_at < min_gap_s:
        return None
    return "not acknowledged"


async def handle_payload(settings: Settings, payload: dict, now: float | None = None) -> list[str]:
    """Process one webhook delivery; returns a log line per alert."""
    now = now or time.time()
    out = []
    for alert in payload.get("alerts") or []:
        fp = alert.get("fingerprint") or json.dumps(alert.get("labels"), sort_keys=True)
        name = (alert.get("labels") or {}).get("alertname", "?")
        existing = load(settings, fp)
        rec = existing or CallRecord(fingerprint=fp, alertname=name)
        if alert.get("status") == "resolved":
            rec.resolved_at = now
            save(settings, rec)
            out.append(f"{name}: resolved")
            continue
        why = should_call(existing, now, settings.call_min_gap_s)
        if not why:
            out.append(f"{name}: no call ({'acknowledged' if rec.acked_at else 'called recently'})")
            continue
        try:
            await outbound.call_and_say(settings, spoken(alert), alert_id=fp)
        except Exception as exc:  # the spool or TTS failing must not kill the hook
            log.exception("escalation: call for %s failed", name)
            out.append(f"{name}: CALL FAILED: {exc}")
            continue
        rec.called_at = now
        rec.calls += 1
        rec.resolved_at = 0.0
        save(settings, rec)
        out.append(f"{name}: called ({why}, call #{rec.calls})")
    for line in out:
        log.info("escalation: %s", line)
    return out


def acknowledge(settings: Settings, fp: str, now: float | None = None) -> bool:
    rec = load(settings, fp)
    if rec is None:
        return False
    rec.acked_at = now or time.time()
    save(settings, rec)
    log.info("escalation: %s acknowledged by phone", rec.alertname)
    return True


# ---------------------------------------------------------------- the server

_STATUS = {200: "OK", 202: "Accepted", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large"}
MAX_BODY = 1 << 20


async def _respond(writer: asyncio.StreamWriter, code: int, body: str) -> None:
    data = body.encode()
    writer.write((f"HTTP/1.1 {code} {_STATUS.get(code, 'OK')}\r\nContent-Type: text/plain\r\n"
                  f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode() + data)
    await writer.drain()
    writer.close()


async def _handle(settings: Settings, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        writer.close()
        return
    lines = head.decode(errors="replace").split("\r\n")
    try:
        method, target, _ = lines[0].split(" ", 2)
    except ValueError:
        await _respond(writer, 400, "bad request line\n")
        return
    headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:] if ":" in l)}
    length = int(headers.get("content-length", "0") or 0)
    if length > MAX_BODY:
        await _respond(writer, 413, "too large\n")
        return
    body = await reader.readexactly(length) if length else b""

    if method == "POST" and target == "/alert":
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            await _respond(writer, 400, "not json\n")
            return
        lines_out = await handle_payload(settings, payload)
        await _respond(writer, 202, "\n".join(lines_out) + "\n")
    elif method == "POST" and target.startswith("/ack/"):
        fp = target[len("/ack/"):]
        await _respond(writer, 200 if acknowledge(settings, fp) else 404, "ok\n" if fp else "no id\n")
    elif method == "GET" and target == "/healthz":
        await _respond(writer, 200, "ok\n")
    else:
        await _respond(writer, 404 if method in ("GET", "POST") else 405, "no\n")


async def serve(settings: Settings) -> None:
    settings.calls_dir.mkdir(parents=True, exist_ok=True)
    server = await asyncio.start_server(lambda r, w: _handle(settings, r, w), settings.hook_host, settings.hook_port)
    log.info("escalation hook listening on %s:%d", settings.hook_host, settings.hook_port)
    async with server:
        await server.serve_forever()
