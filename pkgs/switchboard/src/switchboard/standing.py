"""Standing questions: answers that change slowly, pre-answered and stored.

"Did the backup run", "what's on my schedule", "what happened recently" are
slow-path questions whose answers don't change every three minutes — Chris,
2026-09-12: pre-render them, store them, and only update when they change.

Each StandingQuestion names the files that feed its answer (`watch`). A
timer calls refresh() every few minutes; it re-asks the agent only when the
fingerprint of those files moved, the date rolled over (for `daily`
questions), or `max_age_s` passed. The stored text is rendered into the
sentence cache at the same time, so a matching call plays instantly.

At call time a stored answer older than STALE_PREFIX_S is prefixed with
"As of about N hours ago." — the caller should know they are hearing a
cached answer. No stored answer yet -> the normal slow path, which stores
its result for next time.
"""

from __future__ import annotations

import datetime as dt
import glob
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from . import agent, audio
from .config import Settings

log = logging.getLogger(__name__)

STALE_PREFIX_S = 2 * 3600


class StandingQuestion(BaseModel):
    name: str
    # Regexes matched (search) against the normalised transcript.
    patterns: list[str]
    # The curated question the agent is asked — better than whatever the
    # caller mumbled, and stable, so the answer text (and its audio) is too.
    ask: str
    # Globs whose mtime/count fingerprint decides whether to re-ask.
    watch: list[str] = Field(default_factory=list)
    # "Today" questions: the date is part of the fingerprint.
    daily: bool = False
    # Re-ask anyway after this long (sources like a journal have no mtime).
    max_age_s: int = 6 * 3600


DEFAULT_STANDING: list[StandingQuestion] = [
    StandingQuestion(
        name="backups",
        patterns=[r"\bbackups?\b", r"\brestic\b"],
        ask="Did the restic backups (local and B2) run last night, and did they succeed?",
        watch=[],                # journal has no file to watch; runs at ~02:30 and ~03:00
        daily=True, max_age_s=4 * 3600,
    ),
    StandingQuestion(
        name="schedule",
        patterns=[r"\b(schedule|calendar|appointments?|agenda)\b", r"what('s| is) (on )?(for )?today\b", r"\bplan for today\b"],
        ask="What is on my calendar today, and what is the next upcoming event after today?",
        watch=["/var/lib/pim/calendars/nextcloud/personal/*.ics"],
        daily=True, max_age_s=12 * 3600,
    ),
    StandingQuestion(
        name="recent",
        patterns=[r"\b(recently|lately)\b", r"what('s| has| is) (been )?(happen|going on)", r"\bany(thing)? new\b"],
        ask="What has happened on gromit in the last day: sentinel incidents, deploys, anything notable? Two sentences.",
        watch=["/var/lib/sentinel/incidents/*.txt", "/var/lib/comin/*"],
        max_age_s=3 * 3600,
    ),
    StandingQuestion(
        name="forecast",
        patterns=[r"\b(weather|forecast|rain|snow|storms?)\b"],
        ask="What is the weather forecast for the rest of today and tomorrow?",
        watch=["/var/lib/wx/wx.db"],
        daily=True, max_age_s=2 * 3600,
    ),
    StandingQuestion(
        name="ryan-hall",
        patterns=[r"\bryan\b"],
        ask="What did Ryan Hall say in his latest video? Two sentences.",
        watch=["/var/lib/wx/wx.db"],
        max_age_s=6 * 3600,
    ),
]


@dataclass(frozen=True)
class Stored:
    text: str
    ts: float
    fingerprint: str

    @property
    def age_s(self) -> float:
        return time.time() - self.ts


# ---------------------------------------------------------------- store

def _path(settings: Settings, name: str) -> Path:
    return settings.answers_dir / f"{name}.json"


def load(settings: Settings, name: str) -> Stored | None:
    p = _path(settings, name)
    try:
        d = json.loads(p.read_text())
        return Stored(text=d["text"], ts=float(d["ts"]), fingerprint=d.get("fingerprint", ""))
    except (OSError, ValueError, KeyError):
        return None


def store(settings: Settings, name: str, text: str, fingerprint: str) -> Stored:
    settings.answers_dir.mkdir(parents=True, exist_ok=True)
    s = Stored(text=text, ts=time.time(), fingerprint=fingerprint)
    tmp = _path(settings, name).with_suffix(".json.part")
    tmp.write_text(json.dumps({"text": s.text, "ts": s.ts, "fingerprint": s.fingerprint}))
    tmp.replace(_path(settings, name))
    return s


# ---------------------------------------------------------------- fingerprint

def fingerprint(q: StandingQuestion, now: float | None = None) -> str:
    """Hash of (count, newest mtime) per watch glob, plus the date for daily
    questions. Cheap: a few stat() calls, no agent."""
    h = hashlib.sha1()
    for pattern in q.watch:
        files = glob.glob(pattern)
        newest = max((os.stat(f).st_mtime for f in files), default=0.0)
        h.update(f"{pattern}:{len(files)}:{newest:.0f};".encode())
    if q.daily:
        h.update(dt.datetime.fromtimestamp(now or time.time()).date().isoformat().encode())
    return h.hexdigest()


def needs_refresh(q: StandingQuestion, stored: Stored | None, now: float | None = None) -> str | None:
    """Why this question should be re-asked, or None if the stored answer stands."""
    if stored is None:
        return "no stored answer"
    if stored.fingerprint != fingerprint(q, now):
        return "sources changed"
    if (now or time.time()) - stored.ts > q.max_age_s:
        return "max age"
    return None


# ---------------------------------------------------------------- refresh

async def refresh(settings: Settings, questions: list[StandingQuestion], *, force: bool = False) -> list[str]:
    """Re-ask what needs it; store + render. Returns the names refreshed."""
    done: list[str] = []
    for q in questions:
        why = "forced" if force else needs_refresh(q, load(settings, q.name))
        if not why:
            continue
        log.info("standing %s: refreshing (%s)", q.name, why)
        try:
            text = await agent.ask(settings, q.ask)
        except agent.AgentError as exc:
            log.warning("standing %s: %s", q.name, exc)
            continue
        store(settings, q.name, text, fingerprint(q))
        # Into the sentence cache now, so the first caller doesn't render it.
        scratch = settings.outbox / f"standing-{q.name}"
        await audio.say(settings, text, scratch)
        for f in settings.outbox.glob(f"standing-{q.name}.*"):
            f.unlink()
        done.append(q.name)
    return done


# ---------------------------------------------------------------- serving

def spoken(stored: Stored) -> str:
    """The stored answer, prefixed with its age once that matters."""
    if stored.age_s < STALE_PREFIX_S:
        return stored.text
    hours = round(stored.age_s / 3600)
    when = "about an hour ago" if hours <= 1 else f"about {hours} hours ago"
    return f"As of {when}. {stored.text}"


def match(questions: list[StandingQuestion], normalised: str) -> StandingQuestion | None:
    for q in questions:
        if any(re.search(p, normalised) for p in q.patterns):
            return q
    return None


# ---------------------------------------------------------------- slow-path log

_STOP = {"the", "a", "an", "is", "are", "was", "were", "do", "does", "did", "what", "whats", "what's",
         "how", "my", "me", "i", "you", "your", "it", "of", "on", "in", "for", "to", "and", "please",
         "can", "could", "tell", "about", "there", "any", "anything", "right", "now", "today", "tonight"}


def slow_key(question: str) -> str:
    """Normalise a slow-path question so rephrasings group together:
    lowercase, no punctuation, stopwords out, words sorted."""
    words = re.sub(r"[^\w\s']", " ", question.lower()).split()
    return " ".join(sorted(w for w in words if w not in _STOP)) or question.lower().strip()


def log_slow(settings: Settings, question: str, seconds: float) -> None:
    settings.slowlog.parent.mkdir(parents=True, exist_ok=True)
    with settings.slowlog.open("a") as fh:
        fh.write(json.dumps({"ts": time.time(), "q": question, "key": slow_key(question), "s": round(seconds, 1)}) + "\n")


def slow_report(settings: Settings, days: float = 7.0) -> list[tuple[str, int, float, str]]:
    """[(key, count, mean seconds, an example question)] for the window, most frequent first."""
    cutoff = time.time() - days * 86400
    agg: dict[str, list[tuple[float, str]]] = {}
    try:
        for line in settings.slowlog.read_text().splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("ts", 0) < cutoff:
                continue
            agg.setdefault(d["key"], []).append((float(d.get("s", 0)), d.get("q", "")))
    except OSError:
        return []
    rows = [(k, len(v), sum(s for s, _ in v) / len(v), v[-1][1]) for k, v in agg.items()]
    return sorted(rows, key=lambda r: -r[1])


def candidates(settings: Settings, questions: list[StandingQuestion], *, min_hits: int = 3, days: float = 7.0) -> list[tuple[str, int, str]]:
    """Repeated slow-path questions no standing question already covers."""
    out = []
    for key, n, _, example in slow_report(settings, days):
        if n >= min_hits and match(questions, re.sub(r"[^\w\s']", " ", example.lower())) is None:
            out.append((key, n, example))
    return out
