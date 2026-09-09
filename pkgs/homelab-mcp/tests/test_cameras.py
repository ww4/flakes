"""Camera-tool tests.

The behaviours that matter are the SAFETY ones: an unreachable NVR must be an
error rather than an empty all-clear, a site must be allowlisted, and the CLI
must be invoked as an argument list so a camera name can never become shell.
"""

from __future__ import annotations

import json

import pytest

from homelab_mcp import cameras
from homelab_mcp.config import Settings
from homelab_mcp.server import build_server


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def fake_cli(monkeypatch):
    """Capture the argv the CLI would be invoked with."""
    calls: list[list[str]] = []
    box = {"proc": FakeProc(stdout='{"site":"craigmyle","total":1,"online":1}')}

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return box["proc"]

    monkeypatch.setattr(cameras.shutil, "which", lambda b: "/bin/" + b)
    monkeypatch.setattr(cameras.subprocess, "run", fake_run)
    return calls, box


def test_status_passes_json_flag_and_parses(fake_cli):
    calls, _ = fake_cli
    out = cameras.camera_status("blueiris", "craigmyle", only_down=True)
    assert out["site"] == "craigmyle"
    argv = calls[0]
    assert argv[0] == "blueiris"
    assert "--json" in argv and "offline" in argv
    # An argument LIST, never a string: no shell is ever constructed.
    assert all(isinstance(a, str) for a in argv)


def test_cams_when_not_only_down(fake_cli):
    calls, _ = fake_cli
    cameras.camera_status("blueiris", "craigmyle", only_down=False)
    assert "cams" in calls[0] and "offline" not in calls[0]


def test_unreachable_nvr_raises_rather_than_returning_empty(fake_cli):
    """The whole point. 'NVR down' and 'all cameras fine' must never look alike."""
    _, box = fake_cli
    box["proc"] = FakeProc(
        returncode=1, stderr="blueiris: http://x/json unreachable: Connection refused")
    with pytest.raises(cameras.CameraError) as exc:
        cameras.camera_status("blueiris", "craigmyle", only_down=True)
    assert "unreachable" in str(exc.value)


def test_missing_binary_is_an_error(monkeypatch):
    monkeypatch.setattr(cameras.shutil, "which", lambda b: None)
    with pytest.raises(cameras.CameraError):
        cameras.camera_status("blueiris", "craigmyle", only_down=True)


def test_non_json_output_is_an_error(fake_cli):
    _, box = fake_cli
    box["proc"] = FakeProc(stdout="  all cameras online")
    with pytest.raises(cameras.CameraError):
        cameras.camera_status("blueiris", "craigmyle", only_down=True)


@pytest.mark.parametrize("bad", ["", "a" * 40, "Cam1; rm -rf /", "../../etc",
                                 "Cam 1", "$(id)"])
def test_bad_camera_identifiers_rejected(fake_cli, bad):
    with pytest.raises(cameras.CameraError):
        cameras.mute("blueiris", "craigmyle", bad, "reason", 30)
    with pytest.raises(cameras.CameraError):
        cameras.unmute("blueiris", "craigmyle", bad)


@pytest.mark.parametrize("days", [0, -1, 366, 100000])
def test_mute_duration_bounded(fake_cli, days):
    """No indefinite mutes from the MCP: that needs a deliberate act at a keyboard."""
    with pytest.raises(cameras.CameraError):
        cameras.mute("blueiris", "craigmyle", "Cam26", "dead", days)


def test_mute_never_passes_forever(fake_cli):
    calls, box = fake_cli
    box["proc"] = FakeProc(stdout="  muted Cam26 for 30 days")
    cameras.mute("blueiris", "craigmyle", "Cam26", "awaiting replacement", 30)
    assert "--forever" not in calls[0]
    assert "--days" in calls[0]


def test_long_reason_truncated(fake_cli):
    calls, box = fake_cli
    box["proc"] = FakeProc(stdout="ok")
    cameras.mute("blueiris", "craigmyle", "Cam26", "x" * 5000, 30)
    assert max(len(a) for a in calls[0]) <= 200


def test_unmute_of_unmuted_camera_is_information_not_failure(fake_cli):
    _, box = fake_cli
    box["proc"] = FakeProc(returncode=1, stdout="  Cam26 was not muted")
    assert "was not muted" in cameras.unmute("blueiris", "craigmyle", "Cam26")


# --------------------------------------------------------------- tool surface


def _settings(**kw):
    return Settings(space_root=kw.pop("space_root"), **kw)


def test_camera_tools_absent_by_default(tmp_path):
    (tmp_path / "Inbox").mkdir()
    mcp = build_server(_settings(space_root=tmp_path))
    names = {t.name for t in mcp._tool_manager.list_tools()}
    assert not any(n.startswith("camera_") for n in names)
    assert "get_context" in names


def test_camera_tools_present_when_enabled(tmp_path):
    (tmp_path / "Inbox").mkdir()
    mcp = build_server(_settings(space_root=tmp_path, camera_tools=True))
    names = {t.name for t in mcp._tool_manager.list_tools()}
    assert {"camera_status", "camera_mute", "camera_unmute"} <= names


def test_no_imagery_tool_is_ever_exposed(tmp_path):
    """Snapshots stay on the box. Status is equipment metadata; frames are not."""
    (tmp_path / "Inbox").mkdir()
    mcp = build_server(_settings(space_root=tmp_path, camera_tools=True))
    names = {t.name for t in mcp._tool_manager.list_tools()}
    assert not any("snapshot" in n or "image" in n for n in names)


def test_unlisted_site_rejected(tmp_path, fake_cli):
    (tmp_path / "Inbox").mkdir()
    mcp = build_server(_settings(space_root=tmp_path, camera_tools=True,
                                 camera_sites=["craigmyle"]))
    fn = mcp._tool_manager._tools["camera_status"].fn
    with pytest.raises(cameras.CameraError) as exc:
        fn(site="someone-else")
    # The allowlist itself must not leak: knowing WHICH customers exist is
    # exactly what an unauthenticated prober would want.
    assert "craigmyle" not in str(exc.value)


def test_default_site_used_when_none_given(tmp_path, fake_cli):
    calls, _ = fake_cli
    (tmp_path / "Inbox").mkdir()
    mcp = build_server(_settings(space_root=tmp_path, camera_tools=True))
    fn = mcp._tool_manager._tools["camera_status"].fn
    fn()
    assert "craigmyle" in calls[0]


def test_status_tool_defaults_to_only_down(tmp_path, fake_cli):
    calls, _ = fake_cli
    (tmp_path / "Inbox").mkdir()
    mcp = build_server(_settings(space_root=tmp_path, camera_tools=True))
    mcp._tool_manager._tools["camera_status"].fn()
    assert "offline" in calls[0]


def test_mute_records_an_audit_line(tmp_path, fake_cli, monkeypatch):
    """Muting is a write, so it is auditable like every other write.

    Asserted on the audit call rather than via caplog: the audit logger sets
    propagate=False (so records never reach the root logger pytest captures),
    which would make a caplog assertion fail even though the line IS emitted.
    """
    _, box = fake_cli
    box["proc"] = FakeProc(stdout="  muted Cam26 for 30 days")
    (tmp_path / "Inbox").mkdir()
    seen: list[tuple] = []
    monkeypatch.setattr("homelab_mcp.audit.record_write",
                        lambda *a: seen.append(a))
    mcp = build_server(_settings(space_root=tmp_path, camera_tools=True))
    mcp._tool_manager._tools["camera_mute"].fn(camera="Cam26", reason="dead")
    assert seen and seen[0][0] == "camera_mute"
    assert "Cam26" in seen[0][1]
    # The REASON must not be audited: it is free text from the caller, and the
    # log is meant to be safe to read over someone's shoulder.
    assert not any("dead" in str(x) for x in seen[0])


def test_status_result_is_json_serialisable(fake_cli):
    _, box = fake_cli
    box["proc"] = FakeProc(stdout=json.dumps(
        {"site": "craigmyle", "total": 26, "online": 25,
         "cameras": [{"name": "My Camera 26", "short": "Cam26",
                      "online": False, "fps": 1.47,
                      "error": "Missing address or device"}]}))
    out = cameras.camera_status("blueiris", "craigmyle", only_down=True)
    json.dumps(out)
    assert out["cameras"][0]["short"] == "Cam26"
