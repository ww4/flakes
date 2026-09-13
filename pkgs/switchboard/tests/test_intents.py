import pytest

from switchboard import intents


@pytest.mark.parametrize(
    "text,intent",
    [
        ("What's the status?", "status"),
        ("how is everything", "status"),
        ("is everything okay", "status"),
        ("How's the home lab doing?", "status"),   # slow-path log, 2026-09-12 — 16 s for a status answer
        ("how's it going", "status"),
        ("how are we doing", "status"),
        ("how hot is the CPU", "temps"),
        ("drive temperatures please", "temps"),
        ("how much disk space is left", "disk"),
        ("is the pool full", "disk"),
        ("anything wrong?", "incidents"),
        ("what happened overnight", "incidents"),
        ("any alerts", "incidents"),
        ("what time is it", "time"),
        ("what's the bitcoin price", "btc-price"),
        ("how much is bitcoin right now", "btc-price"),
        ("what's the price of BTC", "btc-price"),
        ("what are fees like", "btc-fees"),
        ("what's the block height", "btc-block"),
        ("latest block", "btc-block"),
        ("what can you do", "help"),
        ("thanks, that's all", "goodbye"),
        ("goodbye", "goodbye"),
        ("", "empty"),
        ("Go.", "empty"),            # clipped "Goodbye" — a fragment, not a question
        ("Hello.", "hello"),
        ("hey there", "hello"),
        ("Hi", "hello"),             # short but a known word, not a fragment
        ("what", None),              # 4 chars: past the fragment cutoff, goes to the agent
        ("[BLANK_AUDIO]", None),   # audio.py strips this before routing; raw it is a word
        ("did the immich backup finish last night", "standing:backups"),   # standing question, pre-answered
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


def test_time_uses_announce_voice() -> None:
    import asyncio

    from switchboard.config import Settings

    reply = asyncio.run(intents.answer(Settings(), "time"))
    assert reply.style == "announce" and reply.text.startswith("It is ")
    assert asyncio.run(intents.answer(Settings(), "help")).style == "conversational"


def test_fixed_phrases_cover_the_handlers_verbatim_output() -> None:
    """Every fixed sentence the handlers emit should be in the pre-warm list."""
    for phrase in ["Goodbye.", "I didn't catch that.", "I couldn't look that up right now."]:
        assert phrase in intents.FIXED_PHRASES


def test_sentence_split_matches_how_answers_are_built() -> None:
    from switchboard import audio

    assert audio.sentences("CPU 34 degrees. NVMe 29 degrees. hottest drive is sdb.") == [
        "CPU 34 degrees.", "NVMe 29 degrees.", "hottest drive is sdb."]
    assert audio.sentences("Voice 1, Lessac. ... Press any key.") == ["Voice 1, Lessac.", "...", "Press any key."]
    assert audio.sentences("Goodbye.") == ["Goodbye."]


def test_every_setting_the_units_use_exists() -> None:
    """2026-09-12: an edit deleted `greeting` from Settings and the AGI unit
    crash-looped for nine minutes. Every attribute the entry points touch."""
    from switchboard import agi as agi_mod, cli as cli_mod, outbound as outbound_mod, standing as standing_mod, notes as notes_mod  # noqa: F401
    from switchboard.config import Settings

    s = Settings()
    for attr in ["greeting", "whisper_urls", "kokoro_urls", "tts", "announce_tts", "piper_voice", "piper_length_scale",
                 "kokoro_voice", "kokoro_speed", "kokoro_audition", "agent_hints", "agent_max_turns", "standing_json",
                 "space_dir", "inbox_page", "queue_page", "note_max_ms", "note_silence_s", "max_empty_turns",
                 "hold_max_s", "filler_every_s", "callback_channel", "asterisk_outgoing", "out_ext", "out_rate_hz"]:
        assert hasattr(s, attr), attr
    for prop in ["inbox", "outbox", "prompts", "cache_dir", "answers_dir", "slowlog", "notes_dir", "kokoro_audition_dir"]:
        assert isinstance(getattr(s, prop), type(s.state_dir)), prop
    # the prompt set renders from PROMPTS + greeting
    assert set(agi_mod.PROMPTS) >= {"didnt-catch", "still-here", "one-moment", "still-working", "callback", "sorry", "goodbye",
                                    "note-prompt", "note-go-ahead", "note-again", "note-empty"}


def test_bitcoin_phrasing(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import datetime as dt

    from switchboard import sources
    from switchboard.config import Settings

    async def fake(_s):
        return sources.Bitcoin(usd=77118, price_age_s=1200, usd_24h_ago=77344, height=966844, fee_fast=1, fee_hour=1, fee_economy=1)

    async def fake_stats(_s):
        return sources.BitcoinStats(ath_usd=124734, ath_date="2025-10-06", retarget_date="2026-09-19T05:31:10Z",
                                    retarget_change_pct=4.77, retarget_blocks=832, nodes=26895, nodes_age_s=600)

    monkeypatch.setattr(sources, "bitcoin", fake)
    monkeypatch.setattr(sources, "bitcoin_stats", fake_stats)
    s = Settings()
    assert asyncio.run(intents.answer(s, "btc-price")).text == "Bitcoin is 77,118 dollars, as of 20 minutes ago, down 0.3 percent over the last 24 hours."
    assert asyncio.run(intents.answer(s, "btc-block")).text == "The block height is 966,844."
    assert asyncio.run(intents.answer(s, "btc-fees")).text == "Fees are 1 sat per byte across the board."
    ath = intents._ath_sentence(asyncio.run(fake(s)), asyncio.run(fake_stats(s)), dt.date(2026, 9, 13))
    assert ath == "The all-time high is 124,734 dollars, set on October 6, 2025, 342 days ago. Bitcoin is 38 percent below it."
    assert asyncio.run(intents.answer(s, "btc-diff")).text.startswith("The next difficulty adjustment is expected Saturday, September 19, in 832 blocks, up 4.8 percent.")
    assert asyncio.run(intents.answer(s, "btc-nodes")).text == "26,895 reachable bitcoin nodes are online."
    everything = asyncio.run(intents.answer(s, "btc-stats")).text
    assert everything.startswith("Bitcoin is 77,118 dollars") and "all-time high" in everything and "difficulty" in everything \
        and "nodes" in everything and everything.endswith("across the board.")


def test_bitcoin_routing_specifics() -> None:
    assert intents.route("give me some bitcoin statistics") == "btc-stats"
    assert intents.route("bitcoin stats") == "btc-stats"
    assert intents.route("what's the all-time high") == "btc-ath"
    assert intents.route("when is the next difficulty adjustment") == "btc-diff"
    assert intents.route("how many nodes are online") == "btc-nodes"
    assert intents.route("what's bitcoin at") == "btc-price"


def test_nodes_unreachable_is_spoken(monkeypatch: pytest.MonkeyPatch) -> None:
    from switchboard import sources
    st = sources.BitcoinStats(ath_usd=1, ath_date="2025-01-01", retarget_date="2026-09-19T05:31:10Z",
                              retarget_change_pct=0.0, retarget_blocks=1, nodes=None, nodes_age_s=None)
    assert intents._nodes_sentence(st) == "I couldn't reach the node count right now."
