import asyncio
import json
from pathlib import Path

import httpx
import pytest

from switchboard import escalation, outbound
from switchboard.config import Settings


def test_spoken_message() -> None:
    a = {"labels": {"alertname": "RiverFloodMajor", "severity": "critical"},
         "annotations": {"summary": "Kentucky River at Lockport at MAJOR flood stage"}}
    assert escalation.spoken(a) == ("This is Gromit with a critical alert. River Flood Major. Kentucky River at Lockport at MAJOR "
                                    "flood stage Press 1 to acknowledge, or I will call again in an hour.")


def test_should_call_rules() -> None:
    now = 1_000_000.0
    assert escalation.should_call(None, now, 2700) == "first firing"
    rec = escalation.CallRecord("fp", "X", called_at=now - 100)
    assert escalation.should_call(rec, now, 2700) is None                 # called recently
    rec = escalation.CallRecord("fp", "X", called_at=now - 4000)
    assert escalation.should_call(rec, now, 2700) == "not acknowledged"   # repeat
    rec = escalation.CallRecord("fp", "X", called_at=now - 4000, acked_at=now - 3000)
    assert escalation.should_call(rec, now, 2700) is None                 # acked: never again


def test_call_file_carries_alert_id() -> None:
    text = outbound.call_file("PJSIP/101", Path("/x/announce-1"), alert_id="abc123")
    assert "Setvar: ALERTID=abc123" in text
    assert "Setvar: ALERTID=\n" in outbound.call_file("PJSIP/101", Path("/x/announce-1"))


def test_payload_calls_once_then_respects_ack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def fake_call(settings, text, channel=None, *, alert_id=""):
        calls.append((text, alert_id))
        return Path("/dev/null")

    monkeypatch.setattr(outbound, "call_and_say", fake_call)
    s = Settings(state_dir=tmp_path, call_min_gap_s=2700)
    firing = {"alerts": [{"status": "firing", "fingerprint": "fp1", "labels": {"alertname": "DriveTemperatureCritical", "severity": "critical"},
                          "annotations": {"summary": "drive sda at 60 degrees"}}]}
    now = 1_000_000.0
    assert asyncio.run(escalation.handle_payload(s, firing, now)) == ["DriveTemperatureCritical: called (first firing, call #1)"]
    assert asyncio.run(escalation.handle_payload(s, firing, now + 60)) == ["DriveTemperatureCritical: no call (called recently)"]
    assert asyncio.run(escalation.handle_payload(s, firing, now + 3700)) == ["DriveTemperatureCritical: called (not acknowledged, call #2)"]
    assert escalation.acknowledge(s, "fp1", now + 3800)
    assert asyncio.run(escalation.handle_payload(s, firing, now + 8000)) == ["DriveTemperatureCritical: no call (acknowledged)"]
    resolved = {"alerts": [dict(firing["alerts"][0], status="resolved")]}
    assert asyncio.run(escalation.handle_payload(s, resolved, now + 9000)) == ["DriveTemperatureCritical: resolved"]
    assert len(calls) == 2 and calls[0][1] == "fp1" and "drive sda at 60 degrees" in calls[0][0]
    assert not escalation.acknowledge(s, "unknown")


def test_call_failure_does_not_kill_the_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*a, **k):
        raise RuntimeError("spool missing")

    monkeypatch.setattr(outbound, "call_and_say", boom)
    s = Settings(state_dir=tmp_path)
    out = asyncio.run(escalation.handle_payload(s, {"alerts": [{"status": "firing", "fingerprint": "f", "labels": {"alertname": "X"}}]}))
    assert out == ["X: CALL FAILED: spool missing"]
    assert escalation.load(s, "f") is None   # not recorded as called, so the next delivery retries


def test_http_server_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def fake_call(settings, text, channel=None, *, alert_id=""):
        calls.append(alert_id)
        return Path("/dev/null")

    monkeypatch.setattr(outbound, "call_and_say", fake_call)

    async def run():
        s = Settings(state_dir=tmp_path, hook_port=0)
        server = await asyncio.start_server(lambda r, w: escalation._handle(s, r, w), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server, httpx.AsyncClient(timeout=5) as c:
            base = f"http://127.0.0.1:{port}"
            r = await c.post(f"{base}/alert", json={"alerts": [{"status": "firing", "fingerprint": "zz", "labels": {"alertname": "T"}}]})
            assert r.status_code == 202 and "T: called" in r.text
            assert (await c.post(f"{base}/ack/zz")).status_code == 200
            assert (await c.post(f"{base}/ack/nope")).status_code == 404
            assert (await c.get(f"{base}/healthz")).status_code == 200
            assert (await c.post(f"{base}/alert", content=b"{not json")).status_code == 400
            assert (await c.get(f"{base}/whatever")).status_code == 404
        assert escalation.load(s, "zz").acked_at > 0
        return calls

    assert asyncio.run(run()) == ["zz"]
