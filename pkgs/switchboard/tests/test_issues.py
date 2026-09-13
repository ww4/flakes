import asyncio
import json
from pathlib import Path

import pytest

from switchboard import intents, issues, sources
from switchboard.config import Settings


def test_spoken_orders_critical_first_and_caps() -> None:
    found = [issues.Issue("warning", "w1"), issues.Issue("critical", "c1"), issues.Issue("warning", "w2"),
             issues.Issue("warning", "w3"), issues.Issue("warning", "w4")]
    ordered = sorted(found, key=lambda i: i.rank)
    text = issues.spoken(ordered, limit=3)
    assert text == "There are 5 issues. Critical: c1. Warning: w1. Warning: w2. And 2 more."
    assert issues.spoken([]) == ""
    assert issues.spoken([issues.Issue("warning", "x")]) == "There is 1 issue. Warning: x."


def test_sentinel_active_reads_state_and_headline(tmp_path: Path) -> None:
    inc = tmp_path / "incidents"; inc.mkdir()
    (inc / "comin-deploy-1789248250.txt").write_text("[comin-deploy] comin_last_deployment_failed\n\nmore\n")
    state = {"checks": {
        "comin-deploy": {"consecutive": 3, "active": True, "last_escalated": 1},
        "failed-units": {"consecutive": 0, "active": False, "last_escalated": 0},
        "drive-smart-failed": {"consecutive": 2, "active": True, "last_escalated": 1},
        "selftest": {"consecutive": 1, "active": True, "last_escalated": 1},
    }}
    (tmp_path / "state.json").write_text(json.dumps(state))
    s = Settings(state_dir=tmp_path, sentinel_state=tmp_path / "state.json", sentinel_incidents=inc)
    found = sorted(issues.sentinel_active(s), key=lambda i: i.rank)
    assert [(i.severity, i.text) for i in found] == [
        ("critical", "sentinel drive smart failed"),
        ("warning", "sentinel comin deploy: comin_last_deployment_failed"),
    ]


def test_missing_sentinel_state_is_a_source_error(tmp_path: Path) -> None:
    s = Settings(state_dir=tmp_path, sentinel_state=tmp_path / "nope.json")
    with pytest.raises(sources.SourceError):
        issues.sentinel_active(s)


def test_temps_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(_s):
        return sources.Temps(cpu_c=76.0, nvme_c=40.0, drives={"sda": 59.0, "sdb": 44.0, "sdc": 51.0})
    monkeypatch.setattr(sources, "temps", fake)
    found = asyncio.run(issues.temps(Settings()))
    assert [(i.severity, i.text) for i in found] == [
        ("warning", "CPU at 76 degrees, warning"),
        ("critical", "drive sda at 59 degrees, critical"),
        ("warning", "drive sdc at 51 degrees, warning"),
    ]


def test_current_reports_a_dead_reader_as_a_warning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def boom(_s):
        raise sources.SourceError("down")
    async def none(_s):
        return []
    monkeypatch.setattr(issues, "alertmanager", boom)
    monkeypatch.setattr(issues, "units_and_pool", none)
    monkeypatch.setattr(issues, "temps", none)
    monkeypatch.setattr(issues, "sentinel_active", lambda s: [])
    found = asyncio.run(issues.current(Settings(state_dir=tmp_path)))
    assert [(i.severity, i.text) for i in found] == [("warning", "couldn't read the alert manager")]


def test_issues_intent_routes() -> None:
    assert intents.route("are there any issues") == "issues"
    assert intents.route("what's wrong") == "issues"
    assert intents.route("any warnings") == "issues"
    assert intents.route("what happened overnight") == "incidents"   # history stays separate
