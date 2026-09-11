import pytest

from switchboard import intents


@pytest.mark.parametrize(
    "text,intent",
    [
        ("What's the status?", "status"),
        ("how is everything", "status"),
        ("is everything okay", "status"),
        ("how hot is the CPU", "temps"),
        ("drive temperatures please", "temps"),
        ("how much disk space is left", "disk"),
        ("is the pool full", "disk"),
        ("anything wrong?", "incidents"),
        ("what happened overnight", "incidents"),
        ("any alerts", "incidents"),
        ("what time is it", "time"),
        ("what can you do", "help"),
        ("thanks, that's all", "goodbye"),
        ("goodbye", "goodbye"),
        ("", "empty"),
        ("[BLANK_AUDIO]", None),   # audio.py strips this before routing; raw it is a word
        ("did the immich backup finish last night", None),
        ("what is the mempool doing", None),
    ],
)
def test_route(text: str, intent: str | None) -> None:
    assert intents.route(text) == intent


def test_goodbye_beats_status() -> None:
    # "that's all" must not be swallowed by a later rule.
    assert intents.route("status is fine, that's all, bye") == "goodbye"


def test_normalise_strips_punctuation() -> None:
    assert intents.normalise("What's UP?!") == "what's up"


def test_spoken_units() -> None:
    assert intents._tb(9.46e12) == "9.5 terabytes"
    assert intents._tb(69e9) == "69 gigabytes"
    assert intents._unit_name("mnt-primary-D3.mount") == "mount unit mnt primary D3"
    assert intents._unit_name("comin.service") == "comin"
    assert intents._list(["a"]) == "a"
    assert intents._list(["a", "b", "c"]) == "a, b and c"
    assert intents._hours(0.2) == "12 minutes ago"
    assert intents._hours(5.4) == "about 5 hours ago"


def test_answer_reports_lookup_failure(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A failed read must never come out sounding like good news."""
    import asyncio

    from switchboard.config import Settings

    async def boom(_s):
        raise intents.SourceError("prometheus down")

    monkeypatch.setitem(intents._HANDLERS, "temps", boom)
    reply = asyncio.run(intents.answer(Settings(state_dir=tmp_path), "temps"))
    assert "couldn't look that up" in reply.text
    assert not reply.hangup
