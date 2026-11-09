import datetime as dt
from pathlib import Path

from switchboard import intents, notes
from switchboard.config import Settings


def test_parse_routes_by_recipient_and_strips_markers() -> None:
    n = notes.parse("For Claude, look at the qbit seed guard tomorrow.")
    assert n is not None and n.for_claude and n.text == "Look at the qbit seed guard tomorrow."
    n = notes.parse("claude: check the D6 drive")
    assert n is not None and n.for_claude and n.text == "Check the D6 drive."
    n = notes.parse("buy chain oil and a bar")
    assert n is not None and not n.for_claude and n.text == "Buy chain oil and a bar."
    n = notes.parse("this is a note for you: the shop light flickers")
    assert n is not None and n.for_claude and n.text == "The shop light flickers."
    assert notes.parse("for claude") is None
    assert notes.parse("   ") is None


def test_parse_drops_the_switchboard_trigger() -> None:
    n = notes.parse("take a note: call the vet Monday")
    assert n is not None and not n.for_claude and n.text == "Call the vet Monday."
    n = notes.parse("Remind me to move the sprinkler.")
    assert n is not None and n.text == "Move the sprinkler."
    n = notes.parse("take a note for Claude, the mirror drift alert is noisy")
    assert n is not None and n.for_claude and n.text == "The mirror drift alert is noisy."
    assert notes.body_after_trigger("take a note") is False
    assert notes.body_after_trigger("take a note: x") is True


def test_route_note_intent() -> None:
    assert intents.route("take a note") == "note"
    assert intents.route("Take a note: buy milk.") == "note"
    assert intents.route("remind me to call mom") == "note"
    assert intents.route("note to self, the gate squeaks") == "note"
    assert intents.route("what's the status") == "status"


def test_save_for_chris_appends_to_inbox(tmp_path: Path) -> None:
    s = Settings(state_dir=tmp_path, space_dir=tmp_path / "space")
    (tmp_path / "space").mkdir()
    (tmp_path / "space" / "Inbox.md").write_text("*(empty)*\n")
    now = dt.datetime(2026, 9, 12, 16, 50)
    page = notes.save(s, notes.Note(text="Buy chain oil.", for_claude=False), now=now)
    assert page.name == "Inbox.md"
    assert page.read_text() == "*(empty)*\n- 📞 2026-09-12 4:50 p.m. — Buy chain oil.\n"


def test_save_for_claude_uses_the_request_queue_format(tmp_path: Path) -> None:
    s = Settings(state_dir=tmp_path, space_dir=tmp_path / "space")
    now = dt.datetime(2026, 9, 12, 16, 50)
    audio = tmp_path / "notes" / "20260912-165000.wav16"
    page = notes.save(s, notes.Note(text="Look at the qbit seed guard tomorrow, it flagged twice.", for_claude=True), audio=audio, now=now)
    assert page == tmp_path / "space" / "System" / "Agent Queue.md"
    body = page.read_text()
    assert "- [ ] **Look at the qbit seed guard tomorrow, it** #agent-request #whenever" in body
    assert "    - filed 2026-09-12 16:50 by phone" in body
    assert "    - what: Look at the qbit seed guard tomorrow, it flagged twice." in body
    assert f"    - audio (30 days): {audio}" in body


def test_keep_audio_copies_into_notes_dir(tmp_path: Path) -> None:
    s = Settings(state_dir=tmp_path)
    rec = tmp_path / "in" / "x.wav16"
    rec.parent.mkdir()
    rec.write_bytes(b"\x00" * 100)
    dst = notes.keep_audio(s, rec, dt.datetime(2026, 9, 12, 16, 50, 3))
    assert dst == tmp_path / "notes" / "20260912-165003.wav16" and dst.read_bytes() == b"\x00" * 100
    assert notes.keep_audio(s, tmp_path / "missing.wav16", dt.datetime.now()) is None
