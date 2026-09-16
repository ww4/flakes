"""The rules: what the facts in profile.json mean for a station.

profile.json holds only what was MEASURED per track (netradio profile — a
few hours of listening for the whole library). This file turns those facts
into verdicts, and is evaluated every time the scanner or the DJ reads the
profile — so changing a threshold, or adding a filter over facts already
measured, takes effect the same night with no re-listening. A rule that
needs a new fact adds it to `Facts` in profile.py and is measured on the
next pass (the cache key is the file, so only the field's absence triggers
re-analysis if you bump PROFILE_VERSION there).

Add a filter: write a function over (path, facts, title) that returns the
tag names it asserts, and list it in RULES. Tags are what consumers act on:

  talk        keep the track off every station (the scanner drops it)
  head_talk   starts with chatter: the DJ doesn't fade the previous song in
              over it, and gives the previous song a short exit
  tail_talk   ends with chatter: the DJ lets it finish, no crossfade
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Verdict:
    talk: bool
    head_talk: bool
    tail_talk: bool
    reason: str


# Thresholds from the labelled set (see the profile.py docstring). Both must
# hold: the classifier hears speech and no music, AND nobody is holding notes.
TALK_FRAMES_MIN = 0.45
PITCH_STABLE_MAX = 0.15

TITLE_HINT = re.compile(r"\b(intro|introduction|introducing|talking intro|spoken|banter|"
                        r"announcer|band intro|stage talk|monolog|applause|tuning)\b", re.I)


def is_talk(talk_frames: float, pitch_stable: float) -> bool:
    return talk_frames >= TALK_FRAMES_MIN and pitch_stable < PITCH_STABLE_MAX


def rule_talk(path: str, f: dict, title: str) -> list[str]:
    """The first minute is speech (a whole talk track, OR a song with a long
    spoken intro — either way not wanted on a radio station)."""
    return ["talk"] if is_talk(f.get("talk_frames", 0.0), f.get("pitch_stable", 1.0)) else []


def rule_edges(path: str, f: dict, title: str) -> list[str]:
    tags = []
    if is_talk(f.get("head_talk_frames", 0.0), f.get("head_pitch_stable", 1.0)):
        tags.append("head_talk")
    if is_talk(f.get("tail_talk_frames", 0.0), f.get("tail_pitch_stable", 1.0)):
        tags.append("tail_talk")
    return tags


RULES = [rule_talk, rule_edges]


def evaluate(path: str, facts: dict, title: str = "") -> Verdict:
    tags: set[str] = set()
    for rule in RULES:
        tags.update(rule(path, facts, title))
    talk = "talk" in tags
    if talk:
        reason = f"speech {facts.get('talk_frames', 0):.2f}, held notes {facts.get('pitch_stable', 0):.2f}"
        if TITLE_HINT.search(title or ""):
            reason += ", title agrees"
    else:
        reason = "music"
    return Verdict(talk=talk, head_talk="head_talk" in tags and not talk,
                   tail_talk="tail_talk" in tags and not talk, reason=reason)


def apply_overrides(verdicts: dict[str, Verdict], overrides: dict[str, str]) -> dict[str, Verdict]:
    """A hand-written {path: "talk" | "music"} wins over the rules; the edge
    hints are kept either way."""
    for p, kind in overrides.items():
        v = verdicts.get(p, Verdict(False, False, False, ""))
        verdicts[p] = Verdict(kind == "talk", v.head_talk, v.tail_talk, f"override: {kind}")
    return verdicts
