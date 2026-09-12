import asyncio
from pathlib import Path

import pytest

from switchboard import agi, outbound, sources
from switchboard.config import Settings


# ---------------------------------------------------------------- sentinel

def test_recent_incidents_parses_and_orders(tmp_path: Path) -> None:
    now = 1_800_000_000
    (tmp_path / "failed-units-1799990000.txt").write_text("[failed-units] comin.service failed\n\ndetails\n")
    (tmp_path / "seeding-health-1799999000.txt").write_text("[seeding-health] qBittorrent firewalled\n")
    (tmp_path / "disk-space-1799000000.txt").write_text("[disk-space] old, outside window\n")
    (tmp_path / "notes.md").write_text("ignored")
    s = Settings(state_dir=tmp_path, sentinel_incidents=tmp_path)
    found = sources.recent_incidents(s, within_h=24, now=now)
    assert [i.kind for i in found] == ["seeding-health", "failed-units"]
    assert found[0].headline == "qBittorrent firewalled"
    assert found[1].age_s == 10_000


def test_missing_incident_dir_is_an_error_not_empty(tmp_path: Path) -> None:
    s = Settings(state_dir=tmp_path, sentinel_incidents=tmp_path / "nope")
    with pytest.raises(sources.SourceError):
        sources.recent_incidents(s)


# ---------------------------------------------------------------- AGI protocol

class FakeAsterisk:
    """Scripted far end: canned reply lines, records what we sent.
    Built INSIDE the running loop — StreamReader needs one on 3.13."""

    def __init__(self, lines: list[str]) -> None:
        self.reader = asyncio.StreamReader()
        self.reader.feed_data("".join(l + "\n" for l in lines).encode())
        self.reader.feed_eof()
        self.sent: list[str] = []

        class W:
            def write(_, data: bytes) -> None:
                self.sent.append(data.decode().rstrip("\n"))

            async def drain(_) -> None:
                pass

            def close(_) -> None:
                pass

        self.writer = W()


def drive(lines: list[str], fn):
    """Run fn(call) against a scripted Asterisk; returns (result, fake)."""

    async def run():
        fake = FakeAsterisk(lines)
        call = agi.Call(fake.reader, fake.writer)  # type: ignore[arg-type]
        await call.handshake()
        return await fn(call), call, fake

    return asyncio.run(run())


def test_handshake_and_record_reason() -> None:
    async def fn(call: agi.Call) -> str:
        await call.answer()
        return await call.record("/tmp/x", max_ms=5000, silence_s=2)

    why, call, fake = drive([
        "agi_request: agi://127.0.0.1", "agi_uniqueid: 1757614000.42", "agi_callerid: 101", "",
        "200 result=0", "200 result=0 (timeout) endpos=8000",
    ], fn)
    assert call.id == "1757614000-42" and call.caller == "101"   # dot sanitised (Path.with_suffix trap)
    assert why == "timeout"
    assert fake.sent == ["ANSWER", 'RECORD FILE /tmp/x wav16 "#" 5000 0 BEEP s=2']


def test_hangup_line_raises() -> None:
    with pytest.raises(agi.Hangup):
        drive(["", "HANGUP"], lambda call: call.play("/x"))


def test_dead_channel_511_raises() -> None:
    with pytest.raises(agi.Hangup):
        drive(["", "511 Command Not Permitted on a dead channel"], lambda call: call.play("/x"))


# ---------------------------------------------------------------- call files

def test_call_file_shape(tmp_path: Path) -> None:
    text = outbound.call_file("PJSIP/101", Path("/var/lib/switchboard/out/announce-1"))
    assert "Channel: PJSIP/101" in text
    assert "Context: switchboard-announce" in text
    assert "Setvar: MESSAGE=/var/lib/switchboard/out/announce-1" in text
    assert not text.rstrip().endswith(".wav")


def test_spool_is_atomic_rename(tmp_path: Path) -> None:
    s = Settings(state_dir=tmp_path, asterisk_outgoing=tmp_path)
    final = outbound.spool(s, "Channel: X\n")
    assert final.suffix == ".call" and final.read_text() == "Channel: X\n"
    assert not list(tmp_path.glob(".call-*")), "temp file must be renamed away"


def test_spool_refuses_missing_dir(tmp_path: Path) -> None:
    s = Settings(state_dir=tmp_path, asterisk_outgoing=tmp_path / "missing")
    with pytest.raises(FileNotFoundError):
        outbound.spool(s, "x")


# ---------------------------------------------------------------- audio guards

def test_tiny_recording_skips_whisper(tmp_path: Path) -> None:
    """A 44-byte header-only wav (dead-line RECORD FILE) must not reach whisper."""
    from switchboard import audio

    wav = tmp_path / "empty.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 40)
    s = Settings(state_dir=tmp_path, whisper_url="http://127.0.0.1:1")   # nothing listens; would fail if called
    assert asyncio.run(audio.transcribe(s, wav)) == ""


def test_record_missing_file_is_an_error_not_a_hangup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RECORD FILE says 'timeout' but nothing is on disk: raise, don't quietly end the call."""
    import asyncio

    from switchboard import agi as agi_mod

    s = Settings(state_dir=tmp_path)
    board = agi_mod.Switchboard(s)

    async def fake_record(self, path_no_ext, **kw):
        return "timeout"

    monkeypatch.setattr(agi_mod.Call, "record", fake_record)

    async def run():
        fake = FakeAsterisk([])   # inside the loop: StreamReader needs one on 3.13
        call = agi_mod.Call(fake.reader, fake.writer)  # type: ignore[arg-type]
        s.inbox.mkdir(parents=True)
        await board.converse(call)

    with pytest.raises(RuntimeError, match="does not exist"):
        asyncio.run(run())


def test_record_extension_matches_format() -> None:
    assert agi.Call.RECORD_FORMAT == "wav16"
    # the AGI command and the on-disk name must agree
    from switchboard import audio
    assert audio.PHONE_RATE_HZ == 16000
