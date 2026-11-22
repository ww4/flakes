"""Mini split tool tests: the CLI gets an argument LIST, bad input never reaches
the unit, a request is validated whole before any part is sent, and a failing
or unreachable unit is an error, never a quiet success."""

from __future__ import annotations

import pytest

from homelab_mcp import minisplit


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


STATE = '{"power": true, "mode": "heat", "setpoint": 71.0, "room_temp": 72.0, "unit": "F"}'


@pytest.fixture
def fake_cli(monkeypatch):
    calls: list[list[str]] = []
    box = {"proc": FakeProc(stdout=STATE)}

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return box["proc"]

    monkeypatch.setattr(minisplit.shutil, "which", lambda b: "/bin/" + b)
    monkeypatch.setattr(minisplit.subprocess, "run", fake_run)
    return calls, box


def test_status_uses_json_and_parses(fake_cli):
    calls, _ = fake_cli
    out = minisplit.status("minisplit")
    assert out["setpoint"] == 71.0
    assert calls == [["minisplit", "--json", "status"]]


def test_order_mode_and_temp_before_power_on(fake_cli):
    calls, _ = fake_cli
    minisplit.apply("minisplit", power=True, temp_f=72, mode="heat")
    assert [c[2:] for c in calls] == [["mode", "heat"], ["temp", "72"], ["on"]]


def test_power_off_goes_first(fake_cli):
    calls, _ = fake_cli
    minisplit.apply("minisplit", power=False, temp_f=None, mode="cool")
    assert [c[2:] for c in calls] == [["off"], ["mode", "cool"]]


@pytest.mark.parametrize("kw", [
    {"power": None, "temp_f": None, "mode": None},
    {"power": None, "temp_f": 700, "mode": None},
    {"power": None, "temp_f": 7, "mode": None},
    {"power": True, "temp_f": None, "mode": "turbo"},
    {"power": True, "temp_f": 72, "mode": "heat; reboot"},
])
def test_invalid_requests_send_nothing(fake_cli, kw):
    calls, _ = fake_cli
    with pytest.raises(minisplit.MiniSplitError):
        minisplit.apply("minisplit", **kw)
    assert calls == []


def test_cli_failure_is_an_error(fake_cli):
    _, box = fake_cli
    box["proc"] = FakeProc(returncode=2, stderr="minisplit: status failed: Network Error")
    with pytest.raises(minisplit.MiniSplitError, match="Network Error"):
        minisplit.status("minisplit")


def test_non_json_is_an_error(fake_cli):
    _, box = fake_cli
    box["proc"] = FakeProc(stdout="power ON")
    with pytest.raises(minisplit.MiniSplitError):
        minisplit.status("minisplit")


def _server(tmp_path, **kw):
    from homelab_mcp.config import Settings
    from homelab_mcp.server import build_server
    (tmp_path / "Inbox").mkdir()
    return build_server(Settings(space_root=tmp_path, **kw))


def test_tools_absent_by_default(tmp_path):
    names = {t.name for t in _server(tmp_path)._tool_manager.list_tools()}
    assert not any(n.startswith("minisplit_") for n in names)


def test_exactly_two_tools_when_enabled(tmp_path):
    names = {t.name for t in _server(tmp_path, minisplit_tools=True)._tool_manager.list_tools()}
    assert {n for n in names if n.startswith("minisplit_")} == {"minisplit_status", "minisplit_set"}
    # The cloud gate and raw DP access are never exposed from here.
    assert not any("cloud" in n or "raw" in n for n in names)
