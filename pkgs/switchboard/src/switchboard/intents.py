"""The fast path: recognise a handful of questions and answer them from live
data in well under a second, so the common calls never wait on an LLM.

`route()` is pure (text -> Intent name) and is what the unit tests pin down.
`answer()` runs the matching reader and phrases the result for a voice —
short sentences, no symbols, units spelled so piper reads them naturally.
Anything route() does not recognise returns None and falls through to the
slow path (agent.py).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from . import sources, standing
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
    ("incidents", re.compile(r"\b(incident|anything (wrong|broken|happen)|what (happened|broke|went wrong)|alerts?)\b")),
    # "home lab" (two words) is how whisper spells it; "how's the home lab
    # doing" went to the slow path on 2026-09-12 — 16 s for the status answer.
    ("status",    re.compile(r"\b(status|health|how(?:'s| are| is| are we| am i) (?:things|everything|it going|we doing|the (?:box|server|home ?lab|pool|system)|gromit)|everything (?:ok|okay|fine|alright)|all good)\b")),
    ("temps",     re.compile(r"\b(temp|temperature|how hot|thermal|cool|warm|drives? temp)\w*")),
    ("disk",      re.compile(r"\b(disk|storage|space|room|full|free|capacity|pool)\b")),
    ("time",      re.compile(r"\b(what time|the time|what day|the date|today'?s date)\b")),
]

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
            return name
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


async def _time(s: Settings) -> str:
    now = dt.datetime.now().astimezone()
    return now.strftime("It is %-I:%M %p on %A, %B %-d.")


async def _help(s: Settings) -> str:
    return ("You can ask for status, incidents, temperatures, disk space, or the time. "
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
    "incidents": _incidents,
    "temps": _temps,
    "disk": _disk,
    "time": _time,
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
    "Prometheus has no filesystem data for the paths I watch.",
    "You can ask for status, incidents, temperatures, disk space, or the time.",
    "Anything else I will pass to the agent, which takes a little longer.",
    "Say goodbye to hang up.",
    "Goodbye.",
    "Hi Chris. What would you like to know?",
    "I didn't catch that.",
    "I couldn't look that up right now.",
    "The agent couldn't answer that just now.",
]

# Intents whose answers the prewarm timer pre-renders (read-only, cheap).
LIVE_INTENTS: list[str] = ["status", "temps", "disk", "incidents"]
