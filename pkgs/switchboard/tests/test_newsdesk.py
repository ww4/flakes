import datetime as dt

from switchboard import intents, newsdesk

EDITION = """## What happened

**AI agents attacked RubyGems in May, and it stayed undisclosed until this week.** Four outlets carried the story. Nobody said so. [nd:20770]

## Bitcoin

- **Adam Gibson wants coinjoins to hide inside bets that look like ordinary payments.** In Babilonia, two parties fund a shared UTXO. On-chain it reads as a normal spend. [nd:20774]

- **A silent-payments light client cannot tell when its server drops a tweak.** Rob Segers benchmarked BlindBit. [nd:20775]

**Network** — nothing today. A Cloudflare CASB product launch.

## Linux & self-hosting

- **With tracking on, an LG smart TV inventories your LAN and reports every host name it finds.** Gamers Nexus' investigation. See [the video](https://example.com/x). [nd:20790]

**Agrarian** — nothing today.

## Worth knowing

- **Security** — LDK v0.2.6 fixes a channel-manager DoS. Upgrade if you run it.
- **Patch** — Bitcoin Core #36048 fixes walletnotify.

TLDR: OpenAI's test agents attacked RubyGems; Lustig on Treasuries.
"""


def test_parse_structure() -> None:
    ed = newsdesk.parse(EDITION, "2026-09-12-longread")
    assert ed.tldr == "OpenAI's test agents attacked RubyGems; Lustig on Treasuries."
    assert [i.lane for i in ed.items] == ["What happened", "Bitcoin", "Bitcoin", "Linux & self-hosting", "Worth knowing", "Worth knowing"]
    assert ed.items[0].headline.startswith("AI agents attacked RubyGems") and ed.items[0].ref == "nd:20770"
    assert ed.items[0].detail == "Four outlets carried the story. Nobody said so."
    assert ed.items[3].detail == "Gamers Nexus' investigation. See the video."      # link text kept, url dropped, ref dropped
    assert ed.items[4].headline == "Security: LDK v0.2.6 fixes a channel-manager DoS."
    assert ed.items[4].detail == "LDK v0.2.6 fixes a channel-manager DoS. Upgrade if you run it."
    assert ed.items[5].detail == ""                                                  # single sentence: nothing more to say
    assert ed.nothing == {"Network": "A Cloudflare CASB product launch.", "Agrarian": ""}


def test_headlines_text() -> None:
    ed = newsdesk.parse(EDITION, "2026-09-04-longread")   # a Friday well in the past: "Friday's", not "yesterday's"
    text = newsdesk.headlines(ed)
    assert text.startswith("Friday's long read, 6 stories. Press any key to stop me at a story. What happened: AI agents attacked RubyGems")
    assert "Bitcoin: Adam Gibson wants coinjoins" in text
    assert "Nothing today in Network and Agrarian." in text
    assert text.endswith("Say more about and a topic for the detail, or next to go through them.")


def test_spoken_name_relative() -> None:
    today = dt.date.today().isoformat()
    assert newsdesk.Edition(name=f"{today}-brief").spoken_name == "This morning's brief"
    assert newsdesk.Edition(name="garbage").spoken_name == "The latest edition"


def test_find_matches_whisperish_queries() -> None:
    ed = newsdesk.parse(EDITION, "x")
    best, close = newsdesk.find(ed, "the LG TV")
    assert best.ref == "nd:20790" and close == []
    best, close = newsdesk.find(ed, "coin joins and bets")
    assert best.ref == "nd:20774"
    best, close = newsdesk.find(ed, "silent payments")
    assert best.ref == "nd:20775"
    assert newsdesk.find(ed, "the weather in Ohio") == (None, [])
    assert newsdesk.find(ed, "") == (None, [])


def test_more_query_extraction_and_routing() -> None:
    assert newsdesk.more_query("More about the LG TV.") == "the LG TV"
    assert newsdesk.more_query("tell me more about coinjoins") == "coinjoins"
    assert newsdesk.more_query("details on Treasuries?") == "Treasuries"
    assert newsdesk.more_query("what about the RubyGems thing") == "the RubyGems thing"
    assert newsdesk.more_query("what's the weather") is None
    assert intents.route("more about the LG TV") == "news:more"
    assert intents.route("tell me more about silent payments") == "news:more"
    assert intents.route("next") == "news:next"
    assert intents.route("next story") == "news:next"
    assert intents.route("what's new") == "news"
    assert intents.route("read me the newsletter") == "news"
    assert intents.route("what's the status") == "status"      # "what about"-style phrasings must not steal these
    assert intents.route("what's the weather tomorrow") == "weather:tomorrow"


def test_segments_carry_item_indexes_and_the_stop_hint() -> None:
    ed = newsdesk.parse(EDITION, "2026-09-04-longread")
    segs = newsdesk.segments(ed)
    assert segs[0] == (None, "Friday's long read, 6 stories. Press any key to stop me at a story.")
    assert segs[1][0] == 0 and segs[1][1].startswith("What happened: AI agents")
    assert segs[2][0] == 1 and segs[2][1].startswith("Bitcoin: Adam Gibson")
    assert segs[3][0] == 2 and not segs[3][1].startswith("Bitcoin")      # same lane: no lane prefix
    assert segs[-1] == (None, "Say more about and a topic for the detail, or next to go through them.")
    assert newsdesk.headlines(ed) == " ".join(t for _, t in segs)
    assert intents.route("more") == "news:this"
    assert intents.route("tell me more") == "news:this"
    assert intents.route("more about the tv") == "news:more"


def test_stream_file_reports_the_interrupting_key() -> None:
    import asyncio
    from tests.test_sources_agi import drive

    key, call, fake = drive(["", "200 result=49 endpos=8000"], lambda c: c.play("/x", escape="0123456789*#"))
    assert key == "1" and fake.sent == ['STREAM FILE /x "0123456789*#"']
    key, call, fake = drive(["", "200 result=0 endpos=20155"], lambda c: c.play("/x", escape="0123456789*#"))
    assert key is None
