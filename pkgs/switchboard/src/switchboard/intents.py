"""The fast path: recognise a handful of questions and answer them from live
data in well under a second, so the common calls never wait on an LLM.

`route()` is pure (text -> Intent name) and is what the unit tests pin down.
`answer()` runs the matching reader and phrases the result for a voice —
short sentences, no symbols, units spelled so piper reads them naturally.
Anything route() does not recognise returns None and falls through to the
slow path (agent.py).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from . import issues, sources, standing
from .config import Settings
from .sources import SourceError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reply:
    text: str
    hangup: bool = False
    style: str = "conversational"   # "announce" -> the announcement voice (audio.say)


# Ordered: first match wins. Patterns are matched against the lowercased,
# punctuation-stripped transcript. Keep "goodbye" ahead of anything that
# could contain "bye" incidentally.
_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("goodbye",   re.compile(r"\b(goodbye|good bye|bye|hang up|that'?s all|thanks? (that'?s )?(all|it))\b")),
    ("note",      re.compile(r"^(please )?((take|make|leave|save) a note|note to self|remind me|remember (that|to))\b")),
    ("hello",     re.compile(r"^(hi|hello|hey|hey there|good (morning|afternoon|evening))( there)?( gromit| switchboard)?$")),
    ("help",      re.compile(r"\b(help|what can (you|i) (do|ask|say)|options|menu)\b")),
    ("notifications", re.compile(r"\b(notifications?|ntfy|pushes|what (have|did) you (sent|send|pushed|push)( me)?)\b")),
    ("issues",    re.compile(r"\b(issues?|problems?|what'?s wrong|warnings?|critical)\b")),
    ("incidents", re.compile(r"\b(incident|anything (wrong|broken|happen)|what (happened|broke|went wrong)|alerts?)\b")),
    # "home lab" (two words) is how whisper spells it; "how's the home lab
    # doing" went to the slow path on 2026-09-12 — 16 s for the status answer.
    ("status",    re.compile(r"\b(status|health|how(?:'s| are| is| are we| am i) (?:things|everything|it going|we doing|the (?:box|server|home ?lab|pool|system)|gromit)|everything (?:ok|okay|fine|alright)|all good)\b")),
    ("temps",     re.compile(r"\b(temp|temperature|how hot|thermal|cool|warm|drives? temp)\w*")),
    ("disk",      re.compile(r"\b(disk|storage|space|room|full|free|capacity|pool)\b")),
    ("time",      re.compile(r"\b(what time|the time|what day|the date|today'?s date)\b")),
    # Bitcoin: the specific questions before the general price rule; "stats" first.
    ("btc-stats", re.compile(r"\b(bitcoin|btc) (stat(istic)?s|summary|rundown|numbers|report|overview)\b|\b(all|everything) (about|on) bitcoin\b")),
    ("btc-ath",   re.compile(r"\b(all[- ]time high|ath|record high|highest (ever|price))\b")),
    ("btc-diff",  re.compile(r"\bdifficulty\b|\bretarget\b")),
    ("btc-nodes", re.compile(r"\bnodes?\b.*\b(online|running|reachable|are there)\b|\bhow many nodes\b|\bnode count\b")),
    ("btc-fees",  re.compile(r"\b(fee|fees|sat(s|oshi)?s? per (v?byte|vb))\b")),
    ("btc-block", re.compile(r"\b(block ?height|latest block|current block|what block|tip)\b")),
    ("btc-price", re.compile(r"\b(bitcoin|btc|coin price|price of bitcoin)\b")),
    ("weather",   re.compile(r"\b(weather|forecast|rain|snow|storms?|how hot (is it going|will it)|temperature (today|tomorrow|outside))\b")),
]

# Weather scope: which periods to read. "today" also covers tonight; nothing
# said means both days (Chris, 2026-09-13: "tomorrow" was getting both).
_SCOPE_TOMORROW = re.compile(r"\btomorrow\b")
_SCOPE_TODAY = re.compile(r"\b(today|tonight|this (afternoon|evening|morning)|right now|currently|outside)\b")


def weather_scope(normalised: str) -> str:
    if _SCOPE_TOMORROW.search(normalised):
        return "tomorrow"
    if _SCOPE_TODAY.search(normalised):
        return "today"
    return "both"

_PUNCT = re.compile(r"[^\w\s']")


def normalise(text: str) -> str:
    return _PUNCT.sub(" ", text.lower()).strip()


# Anything this short that matched no rule is a clipped word ("Go." for
# "Goodbye" cut off by the silence window, 2026-09-12) — not a question worth
# 10 s of agent time. Treated as an empty turn.
_FRAGMENT_MAX_CHARS = 3


def route(text: str) -> str | None:
    """Transcript -> intent name, or None for the slow path."""
    t = normalise(text)
    if not t:
        return "empty"
    for name, pat in _RULES:
        if pat.search(t):
            return f"weather:{weather_scope(t)}" if name == "weather" else name
    if len(t) <= _FRAGMENT_MAX_CHARS:
        return "empty"
    q = standing.match(standing_questions(), t)
    if q is not None:
        return f"standing:{q.name}"
    return None


_standing_cache: list["standing.StandingQuestion"] | None = None


def standing_questions() -> list["standing.StandingQuestion"]:
    """The configured standing questions (Settings.standing_json or the default)."""
    global _standing_cache
    if _standing_cache is None:
        raw = Settings().standing_json
        if raw:
            import json
            _standing_cache = [standing.StandingQuestion(**d) for d in json.loads(raw)]
        else:
            _standing_cache = list(standing.DEFAULT_STANDING)
    return _standing_cache


# ---------------------------------------------------------------- phrasing

def _c(value: float | None) -> str:
    return "unknown" if value is None else f"{round(value)} degrees"


def _tb(b: float) -> str:
    tb = b / 1e12
    if tb >= 1:
        return f"{tb:.1f} terabytes"
    return f"{round(b / 1e9)} gigabytes"


def _unit_name(unit: str) -> str:
    """'mnt-primary-D3.mount' -> 'mount unit mnt primary D3'; 'foo.service' -> 'foo'."""
    name, _, kind = unit.rpartition(".")
    spoken = name.replace("-", " ").replace("_", " ")
    return spoken if kind == "service" else f"{kind} unit {spoken}"


def _list(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _hours(h: float) -> str:
    if h < 1:
        return f"{max(1, round(h * 60))} minutes ago"
    if h < 2:
        return "about an hour ago"
    return f"about {round(h)} hours ago"


# ---------------------------------------------------------------- handlers

async def _status(s: Settings) -> str:
    failed, down = await sources.failed_units(s), await sources.inactive_units(s, s.pool_mount_units)
    try:
        incidents = sources.recent_incidents(s, within_h=24)
    except SourceError as exc:
        log.warning("status: %s", exc)
        incidents = None
    parts: list[str] = []
    if not failed and not down:
        parts.append("Everything is green. No failed units and all six pool drives are mounted.")
    else:
        if failed:
            parts.append(f"{len(failed)} failed unit{'s' if len(failed) != 1 else ''}: {_list([_unit_name(u) for u in failed])}.")
        else:
            parts.append("No failed units.")
        if down:
            parts.append(f"Pool drive{'s' if len(down) != 1 else ''} {_list([u.split('-')[-1].split('.')[0] for u in down])} not mounted.")
        else:
            parts.append("All six pool drives are mounted.")
    if incidents is None:
        parts.append("I could not read the sentinel incident log.")
    elif incidents:
        parts.append(f"The sentinel logged {len(incidents)} incident{'s' if len(incidents) != 1 else ''} in the last day. Ask me about incidents for details.")
    return " ".join(parts)


_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")


def _clean(text: str, words: int) -> str:
    text = _EMOJI.sub("", text)
    text = re.sub(r"https?://\S+", "", text)              # nobody wants a URL read out
    text = " ".join(text.replace("*", "").replace("`", "").split())
    parts = text.split(" ")
    return " ".join(parts[:words]) + ("…" if len(parts) > words else "")


def _ago(seconds: float) -> str:
    m = round(seconds / 60)
    if m < 2:
        return "just now"
    if m < 60:
        return f"{m} minutes ago"
    h = round(m / 60)
    return "an hour ago" if h == 1 else f"{h} hours ago"


def _notifications_text(notes: list["sources.Notification"], hours: float, now: float, limit: int = 5) -> str:
    if not notes:
        return f"No notifications in the last {round(hours)} hours."
    head = [f"{len(notes)} notification{'s' if len(notes) != 1 else ''} in the last {round(hours)} hours."]
    for n in notes[:limit]:
        title = _clean(n.title, 12)
        body = _clean(n.message, 25)
        urgent = "Urgent. " if n.priority >= 4 else ""
        head.append(f"{_ago(now - n.time)}: {urgent}{title}. {body}".rstrip(". ") + ".")
    if len(notes) > limit:
        head.append(f"And {len(notes) - limit} more.")
    return " ".join(head)


async def _notifications(s: Settings) -> str:
    import time as _time
    return _notifications_text(await sources.ntfy_recent(s), s.notifications_hours, _time.time())


async def _issues(s: Settings) -> str:
    text = issues.spoken(await issues.current(s))
    return text or "No warnings or criticals right now."


async def _incidents(s: Settings) -> str:
    incidents = sources.recent_incidents(s, within_h=24)
    if not incidents:
        return "Nothing from the sentinel in the last 24 hours."
    head = incidents[:3]
    lines = [f"{i.kind.replace('-', ' ')}, {_hours(i.age_h)}: {i.headline}" for i in head]
    more = f" And {len(incidents) - 3} more." if len(incidents) > 3 else ""
    return f"{len(incidents)} in the last day. " + " ".join(lines) + more


async def _temps(s: Settings) -> str:
    t = await sources.temps(s)
    parts = [f"CPU {_c(t.cpu_c)}", f"NVMe {_c(t.nvme_c)}"]
    if t.drives:
        dev, deg = t.hottest_drive  # type: ignore[misc]
        parts.append(f"hottest spinning drive is {dev} at {round(deg)} degrees, across {len(t.drives)} drives")
    else:
        parts.append("no drive temperatures reported")
    return ". ".join(parts) + "."


async def _disk(s: Settings) -> str:
    ds = await sources.disks(s)
    if not ds:
        return "Prometheus has no filesystem data for the paths I watch."
    bits = []
    for d in ds:
        name = {"/": "the root filesystem", "/mnt/fusion": "the fusion pool", "/mnt/backup/all": "the backup pool"}.get(d.mountpoint, d.mountpoint)
        bits.append(f"{name} has {_tb(d.avail_bytes)} free, {round(d.free_fraction * 100)} percent")
    return ". ".join(bits) + "."


def _period_sentence(p: "sources.Period") -> str:
    """'Tonight: slight chance of showers then partly cloudy, low around 61, 20 percent chance of rain.'"""
    short = p.short.lower().replace(" and ", " and ").replace("chance", "chance of")
    short = re.sub(r"chance of of", "chance of", short)
    hi_lo = f"{'high' if p.daytime else 'low'} {'near' if p.daytime else 'around'} {p.temperature}"
    pop = f", {p.pop} percent chance of rain" if p.pop else ""
    return f"{p.name}: {short}, {hi_lo}{pop}."


def _weather_text(periods: list["sources.Period"], scope: str, today: str, tomorrow: str) -> str:
    want = {"today": [today], "tomorrow": [tomorrow], "both": [today, tomorrow]}[scope]
    chosen = [p for p in periods if p.date in want]
    if not chosen:
        return "I don't have a forecast for that day yet."
    return " ".join(_period_sentence(p) for p in chosen)


def _weather(scope: str) -> Handler:
    async def handler(s: Settings) -> str:
        periods = await sources.nws_forecast(s)
        today = dt.date.today()
        return _weather_text(periods, scope, today.isoformat(), (today + dt.timedelta(days=1)).isoformat())
    return handler


def _dollars(n: int) -> str:
    return f"{n:,} dollars"


def _price_sentence(b: "sources.Bitcoin") -> str:
    age = round(b.price_age_s / 60)
    when = "just now" if age < 1 else ("a minute ago" if age == 1 else f"{age} minutes ago")
    move = ""
    if b.change_24h_pct is not None:
        pct = b.change_24h_pct
        move = f", {'up' if pct >= 0 else 'down'} {abs(pct):.1f} percent over the last 24 hours"
    return f"Bitcoin is {_dollars(b.usd)}, as of {when}{move}."


async def _btc_price(s: Settings) -> str:
    return _price_sentence(await sources.bitcoin(s))


def _spoken_date(iso_date: str) -> str:
    d = dt.date.fromisoformat(iso_date[:10])
    return d.strftime("%B %-d, %Y")


def _ath_sentence(b: "sources.Bitcoin", st: "sources.BitcoinStats", today: dt.date) -> str:
    days = (today - dt.date.fromisoformat(st.ath_date)).days
    below = (st.ath_usd - b.usd) / st.ath_usd * 100
    ago = "today" if days == 0 else ("yesterday" if days == 1 else f"{days} days ago")
    rel = f" Bitcoin is {below:.0f} percent below it." if below >= 0.5 else " Bitcoin is at a new high."
    return f"The all-time high is {_dollars(st.ath_usd)}, set on {_spoken_date(st.ath_date)}, {ago}.{rel}"


def _difficulty_sentence(st: "sources.BitcoinStats") -> str:
    when = dt.datetime.fromisoformat(st.retarget_date.replace("Z", "+00:00")).astimezone()
    direction = "up" if st.retarget_change_pct >= 0 else "down"
    return (f"The next difficulty adjustment is expected {when.strftime('%A, %B %-d')}, in {st.retarget_blocks:,} blocks, "
            f"{direction} {abs(st.retarget_change_pct):.1f} percent.")


def _nodes_sentence(st: "sources.BitcoinStats") -> str:
    if st.nodes is None:
        return "I couldn't reach the node count right now."
    return f"{st.nodes:,} reachable bitcoin nodes are online."


async def _btc_ath(s: Settings) -> str:
    b, st = await asyncio.gather(sources.bitcoin(s), sources.bitcoin_stats(s))
    return _ath_sentence(b, st, dt.date.today())


async def _btc_diff(s: Settings) -> str:
    return _difficulty_sentence(await sources.bitcoin_stats(s))


async def _btc_nodes(s: Settings) -> str:
    return _nodes_sentence(await sources.bitcoin_stats(s))


async def _btc_stats(s: Settings) -> str:
    """Everything, in the order Chris asked: price and 24 h move, ATH, difficulty, nodes, then block height and fees."""
    b, st = await asyncio.gather(sources.bitcoin(s), sources.bitcoin_stats(s))
    return " ".join([
        _price_sentence(b), _ath_sentence(b, st, dt.date.today()), _difficulty_sentence(st), _nodes_sentence(st),
        f"The block height is {b.height:,}.", await _btc_fees(s),
    ])


async def _btc_block(s: Settings) -> str:
    b = await sources.bitcoin(s)
    return f"The block height is {b.height:,}."


async def _btc_fees(s: Settings) -> str:
    b = await sources.bitcoin(s)
    if b.fee_fast == b.fee_economy:
        return f"Fees are {b.fee_fast} sat per byte across the board."
    return f"Fees: {b.fee_fast} sat per byte for next block, {b.fee_hour} within the hour, {b.fee_economy} economy."


async def _time(s: Settings) -> str:
    now = dt.datetime.now().astimezone()
    return now.strftime("It is %-I:%M %p on %A, %B %-d.")


async def _help(s: Settings) -> str:
    return ("You can ask for status, issues, recent notifications, temperatures, disk space, the weather today or tomorrow, bitcoin, or the time. "
            "Anything else I will pass to the agent, which takes a little longer. Say goodbye to hang up.")


async def _goodbye(s: Settings) -> str:
    return "Goodbye."


async def _hello(s: Settings) -> str:
    return "Hi Chris. What would you like to know?"


async def _empty(s: Settings) -> str:
    return "I didn't catch that."


Handler = Callable[[Settings], Awaitable[str]]

_HANDLERS: dict[str, Handler] = {
    "status": _status,
    "issues": _issues,
    "notifications": _notifications,
    "incidents": _incidents,
    "temps": _temps,
    "disk": _disk,
    "time": _time,
    "btc-price": _btc_price,
    "btc-block": _btc_block,
    "btc-fees": _btc_fees,
    "btc-ath": _btc_ath,
    "btc-diff": _btc_diff,
    "btc-nodes": _btc_nodes,
    "btc-stats": _btc_stats,
    "weather:today": _weather("today"),
    "weather:tomorrow": _weather("tomorrow"),
    "weather:both": _weather("both"),
    "help": _help,
    "goodbye": _goodbye,
    "hello": _hello,
    "empty": _empty,
}


async def answer(settings: Settings, intent: str) -> Reply:
    handler = _HANDLERS[intent]
    try:
        text = await handler(settings)
    except SourceError as exc:
        # Say that the lookup failed. Never let a failed read sound like good news.
        log.warning("intent %s: %s", intent, exc)
        text = "I couldn't look that up right now."
    return Reply(text=text, hangup=(intent == "goodbye"), style=("announce" if intent == "time" else "conversational"))


# Sentences the fast path says verbatim — pre-warmed into the TTS cache at
# unit start (cli prewarm) so the first caller of the day doesn't pay for them.
# Numeric sentences ("CPU 34 degrees.") are rendered on demand and cached too.
FIXED_PHRASES: list[str] = [
    "Everything is green. No failed units and all six pool drives are mounted.",
    "No failed units.",
    "All six pool drives are mounted.",
    "I could not read the sentinel incident log.",
    "Nothing from the sentinel in the last 24 hours.",
    "No warnings or criticals right now.",
    "Prometheus has no filesystem data for the paths I watch.",
    "You can ask for status, issues, recent notifications, temperatures, disk space, the weather today or tomorrow, bitcoin, or the time.",
    "Anything else I will pass to the agent, which takes a little longer.",
    "Say goodbye to hang up.",
    "Goodbye.",
    "Hi Chris. What would you like to know?",
    "I didn't catch that.",
    "I couldn't look that up right now.",
    "The agent couldn't answer that just now.",
]

# Intents whose answers the prewarm timer pre-renders (read-only, cheap).
LIVE_INTENTS: list[str] = ["status", "issues", "notifications", "temps", "disk", "incidents", "weather:today", "weather:tomorrow", "weather:both", "btc-price", "btc-block", "btc-fees", "btc-ath", "btc-diff", "btc-nodes", "btc-stats"]
