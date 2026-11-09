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
    # A machine token, not a sentence: the check NAME is spoken, the token is
    # not. Chris 2026-10-19: "Hearing cryptic readouts is confusing and
    # unnecessary."
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
        ("warning", "sentinel comin deploy"),
    ]


def test_sentinel_speaks_prose_headlines_but_not_metric_dumps(tmp_path: Path) -> None:
    """The two shapes a sentinel evidence line actually takes, both real.

    The threshold lines are verbatim from /var/lib/sentinel/incidents on
    2026-10-19; spoken, the swap one reads "one twenty seven dot zero dot
    zero dot one colon nine one zero zero equals seven point nine six two
    one" and tells him nothing.
    """
    inc = tmp_path / "incidents"; inc.mkdir()
    (inc / "swap-headroom-1791335986.txt").write_text(
        "[swap-headroom] swap-headroom: 127.0.0.1:9100=7.9621\n")
    (inc / "disk-space-1791301015.txt").write_text(
        "[disk-space] disk-space: /=90.8903\n")
    (inc / "vhost-health-1791834915.txt").write_text(
        "[vhost-health] vhost-health: nginx is 'failed' (expected active) "
        "— EVERY vhost on the box is unreachable\n")
    state = {"checks": {
        "swap-headroom": {"active": True, "last_escalated": 1},
        "disk-space": {"active": True, "last_escalated": 1},
        "vhost-health": {"active": True, "last_escalated": 1},
    }}
    (tmp_path / "state.json").write_text(json.dumps(state))
    s = Settings(state_dir=tmp_path, sentinel_state=tmp_path / "state.json", sentinel_incidents=inc)
    spoken = {i.text for i in issues.sentinel_active(s)}

    # Metric dumps: the name only, and NOTHING of the expression.
    assert "sentinel swap headroom" in spoken
    assert "sentinel disk space" in spoken
    for text in spoken:
        assert "127.0.0.1" not in text
        assert "9100" not in text
        assert "90.8903" not in text
        assert "=" not in text

    # Prose: kept, with both id prefixes stripped.
    prose = next(t for t in spoken if t.startswith("sentinel vhost health:"))
    assert "nginx is 'failed'" in prose
    assert "vhost-health:" not in prose


def test_spoken_detail_rules() -> None:
    assert issues.spoken_detail("[disk-space] disk-space: /=90.8903", "disk-space") == ""
    assert issues.spoken_detail("[claude-login] claude-login", "claude-login") == ""
    assert issues.spoken_detail("", "x") == ""
    kept = issues.spoken_detail(
        "[oom] oom: the kernel killed two processes overnight", "oom")
    assert kept == "the kernel killed two processes overnight"


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
