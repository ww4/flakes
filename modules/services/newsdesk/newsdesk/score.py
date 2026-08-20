"""Deterministic interest scoring.

This is podcast-triage's scorer, extracted so there is ONE definition instead of
two that drift. The maths is unchanged and deliberately so — it was tuned
against real transcripts:

  * density, not volume — divide by words/1000, or a rambling three-hour
    episode beats a tight technical one on sheer term count;
  * diminishing returns — a term said fifty times is not fifty times the signal
    of a term said once, it just means that is what the piece is ABOUT;
  * title matches count double — the title says what it is, the body says what
    it mentions.

The one newsdesk addition is `short_text_floor`: podcast transcripts are always
thousands of words, but an RSS item is often a 40-word teaser. Dividing by
0.04 thousand words turns a single keyword into a colossal score, so anything
below the floor is scored per-floor instead of per-actual-length. Without this,
the shortest items win every time — which is exactly backwards.
"""
from __future__ import annotations

SHORT_TEXT_FLOOR = 250  # words


def score_text(title: str, text: str, profile: dict, *,
               short_text_floor: int = SHORT_TEXT_FLOOR) -> tuple[float, list[str]]:
    """Return (score, top signal terms). Higher is more interesting."""
    interests = profile.get("interests", {})
    penalties = profile.get("penalties", {})

    blob = f"{title} {text}".lower()
    words = len(text.split())

    hits: dict[str, int] = {}
    pos = 0.0
    for term, weight in interests.items():
        n = blob.count(term.lower())
        if n:
            hits[term] = n
            pos += weight * (1 + (n - 1) ** 0.5)

    neg = 0.0
    for term, weight in penalties.items():
        n = blob.count(term.lower())
        if n:
            neg += weight * (1 + (n - 1) ** 0.5)

    effective_words = max(words, short_text_floor)
    density = (pos - neg) / (effective_words / 1000.0)

    tl = title.lower()
    title_bonus = sum(w * 2 for t, w in interests.items() if t.lower() in tl)
    title_malus = sum(w * 2 for t, w in penalties.items() if t.lower() in tl)

    top = sorted(hits.items(), key=lambda kv: -kv[1])[:8]
    return round(density + title_bonus - title_malus, 2), [f"{k}x{v}" for k, v in top]
