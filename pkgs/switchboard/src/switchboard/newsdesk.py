"""The newsletter by phone — the latest newsdesk edition in two layers.

  "what's new" / "the news"   -> edition name, story count, then each lane's
                                 bold headline sentence (what the judge wrote)
  "more about <words>"        -> that item's full edition paragraph
  "next"                      -> the next item's paragraph, in edition order

Everything is text the newsdesk already wrote (edition.reader.md +
last-publish.json), so no agent; the pre-warm timer renders it all once per
edition and the phone plays it from the cache. Chris, 2026-09-13: latest
edition only, Heart's voice throughout, no third "whole article" level yet.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Item:
    lane: str
    headline: str      # the bold sentence, no markdown
    detail: str        # the paragraph after it (may be empty for worth-knowing lines)
    ref: str = ""      # nd:NNNN

    @property
    def spoken_detail(self) -> str:
        return f"{self.headline} {self.detail}".strip() if self.detail else self.headline


@dataclass
class Edition:
    name: str                        # "2026-09-12-longread"
    tldr: str = ""
    items: list[Item] = field(default_factory=list)
    nothing: dict[str, str] = field(default_factory=dict)   # lane -> reason

    @property
    def spoken_name(self) -> str:
        m = re.match(r"(\d{4}-\d{2}-\d{2})-(\w+)", self.name)
        if not m:
            return "The latest edition"
        day = dt.date.fromisoformat(m.group(1))
        kind = {"brief": "brief", "longread": "long read"}.get(m.group(2), m.group(2))
        today = dt.date.today()
        when = "This morning's" if day == today else ("Yesterday's" if (today - day).days == 1 else day.strftime("%A's"))
        return f"{when} {kind}"


# ---------------------------------------------------------------- parsing

_LANE = re.compile(r"^## (.+?)\s*$")
_BULLET = re.compile(r"^- \*\*(.+?)\*\*\s*(.*)$")
_LEAD = re.compile(r"^\*\*(.+?)\*\*\s*(.*)$")                      # "What happened" lead paragraph
_NOTHING = re.compile(r"^\*\*(.+?)\*\*\s*—\s*nothing today\.?\s*(.*)$", re.IGNORECASE)
_WORTH = re.compile(r"^- \*\*(.+?)\*\*\s*—\s*(.*)$")               # "- **Security** — text"
_REF = re.compile(r"\s*\[nd:(\d+)\]")
_TLDR = re.compile(r"^TLDR:\s*(.*)$")


def _clean(text: str) -> str:
    text = _REF.sub("", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)     # markdown links -> text
    text = re.sub(r"https?://\S+", "", text)
    text = text.replace("**", "").replace("`", "").replace("*", "")
    return " ".join(text.split()).strip()


def parse(text: str, name: str = "") -> Edition:
    ed = Edition(name=name)
    lane = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if m := _LANE.match(line):
            lane = m.group(1).strip()
            continue
        if m := _TLDR.match(line):
            ed.tldr = _clean(m.group(1))
            continue
        if m := _NOTHING.match(line):
            ed.nothing[m.group(1).strip()] = _clean(m.group(2))
            continue
        if lane.lower() == "worth knowing" and (m := _WORTH.match(line)):
            body = _clean(m.group(2))
            first = re.split(r"(?<=[.!?])\s", body, maxsplit=1)[0]
            ed.items.append(Item(lane=lane, headline=f"{m.group(1).strip()}: {first}", detail=body if body != first else ""))
            continue
        m = _BULLET.match(line) or _LEAD.match(line)
        if m:
            ref = _REF.search(line)
            ed.items.append(Item(lane=lane, headline=_clean(m.group(1)), detail=_clean(m.group(2)), ref=f"nd:{ref.group(1)}" if ref else ""))
    return ed


def load(settings: Settings) -> Edition | None:
    try:
        text = settings.newsdesk_edition.read_text()
    except OSError:
        return None
    name = ""
    try:
        name = json.loads(settings.newsdesk_publish.read_text()).get("edition", "")
    except (OSError, ValueError):
        pass
    return parse(text, name)


# ---------------------------------------------------------------- speaking

def segments(ed: Edition) -> list[tuple[int | None, str]]:
    """The headline pass as (item index or None, spoken text) pieces. The AGI
    plays them one at a time so a key press knows WHICH story it landed on;
    headlines() joins the same pieces for the bench and the pre-warm (same
    sentences, so the cache serves both)."""
    n = len(ed.items)
    out: list[tuple[int | None, str]] = [(None, f"{ed.spoken_name}, {n} stor{'y' if n == 1 else 'ies'}. Press any key to stop me at a story.")]
    lane = None
    for i, it in enumerate(ed.items):
        head = it.headline if it.headline.endswith((".", "!", "?")) else it.headline + "."
        if it.lane != lane:
            lane = it.lane
            out.append((i, f"{lane}: {head}"))
        else:
            out.append((i, head))
    if ed.nothing:
        lanes = list(ed.nothing)
        out.append((None, "Nothing today in " + (" and ".join(lanes) if len(lanes) <= 2 else ", ".join(lanes[:-1]) + ", and " + lanes[-1]) + "."))
    out.append((None, "Say more about and a topic for the detail, or next to go through them."))
    return out


def headlines(ed: Edition) -> str:
    return " ".join(text for _, text in segments(ed))


# ---------------------------------------------------------------- matching

_STOP = {"the", "a", "an", "and", "or", "of", "on", "in", "to", "for", "about", "with", "that", "this", "is", "are",
         "was", "it", "its", "one", "more", "tell", "me", "story", "thing", "detail", "details", "please", "what", "how", "why"}


def _tokens(text: str) -> set[str]:
    """Stemmed-ish (6-char prefix) content words. Two-letter words stay ("LG",
    "TV"), and adjacent pairs are also added joined, because whisper splits
    compounds the newsdesk writes as one word ("coin joins" / "coinjoins")."""
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) >= 2]
    toks = {w[:6] for w in words}
    toks.update((a + b)[:6] for a, b in zip(words, words[1:]))
    return toks


def find(ed: Edition, query: str) -> tuple[Item | None, list[Item]]:
    """Best item for the caller's words, plus runners-up close enough to ask about."""
    q = _tokens(query)
    if not q:
        return None, []
    scored = []
    for it in ed.items:
        hl = _tokens(it.headline)
        body = _tokens(it.detail)
        score = 3 * len(q & hl) + len(q & body)
        if score:
            scored.append((score, it))
    if not scored:
        return None, []
    scored.sort(key=lambda s: -s[0])
    best_score, best = scored[0]
    close = [it for sc, it in scored[1:3] if sc >= 0.7 * best_score]
    return best, close


_MORE = re.compile(r"^(?:(?:tell me |give me )?(?:some )?(?:more|details?|the detail)(?: about| on)?|(?:tell me |what) about|expand on|go deeper on)\s+(.+?)[.?!]?$", re.IGNORECASE)


def more_query(transcript: str) -> str | None:
    m = _MORE.match(transcript.strip())
    return m.group(1).strip() if m else None
