"""Detecting a story by CORROBORATION rather than by any single article.

Chris's framing, which is the specification:

    "if we suddenly invade a country, you're going to hear about that from
    every news source out there, but I don't need any specific article to tell
    me that. I just want to know that it happened... not necessarily that any
    one story would fit the filter, but the amount of stories talking about the
    same thing should matter for something."

Everything else in the newsdesk asks "is this item worth reading?". This asks a
different question — "are a lot of independent people suddenly saying the same
thing?" — and no individual item has to be any good for the answer to be yes.

WHY THIS NEEDED NEW SOURCES, NOT JUST NEW CODE
----------------------------------------------
The first prototype found 758 "bursting" terms in 48 hours: 'touch',
'resource', 'skip content'. Coincidence between unrelated articles. Tuning
helped, but the real finding came from checking whether the signal existed at
all: of 525 items published over three days, exactly ONE source mentioned the
event Chris had noticed, and every other regex hit was the word "surge" or
"rally" turning up in a pickled-beet post and a 3D-printing article.

The catalogue was built out of essayists, engineering blogs and technical
newsletters — all deliberately slow. It is good at signal and structurally
blind to events.

Hence `role = 'signal'`: sources ingested purely to be COUNTED. They are never
published, never enter a lane, never become a good read, and never appear in
the not-selected list. Their only job is to make convergence visible. It is a
pleasing recycling of exactly what was thrown away — Bitcoin Magazine was cut
for being 70 items a week of price and personality, and being 70 items a week
of price and personality is what makes it good substrate.

WHAT THE DETECTOR LEARNED FROM THE PROTOTYPE
--------------------------------------------
  * Match on TITLES, not bodies. A body mentions everything; a title is about
    one thing. This alone removed most of the noise.
  * Count DISTINCT SOURCES, never items. One prolific outlet posting six
    variations of its own headline is not corroboration, and the aggregators
    would win every day otherwise.
  * Compare against a trailing baseline. "bitcoin" is in headlines every day
    and is never news.
"""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

WINDOW_HOURS = 36
BASELINE_DAYS = 21

# A story needs this many INDEPENDENT sources before it is a story.
MIN_SOURCES = 4
# ...and must be this much more talked-about than its own baseline.
MIN_LIFT = 3.0
# Terms this common are furniture, whatever the arithmetic says.
MAX_BASELINE_SHARE = 0.25
# Never hand the reader more than this; two real events in a day is a lot.
MAX_CLUSTERS = 2

TOKEN = re.compile(r"[a-z0-9][a-z0-9'&$.-]{2,}")

STOP = set("""
the a an and or but if then than that this these those of in on at to for with from by is are was
were be been being have has had do does did will would can could should may might must shall it its
as not no nor so such about into over under after before between during you your we our they their
them us he she his her one two new now more most other some any all out up down off again further
here there both each few too very just also like get got make made use used using said says say
what when where which who whom why how new news week month year today yesterday tomorrow first last
next best top big small good great time way day days people world report reports according amid
says say new update updates live latest video watch photos opinion analysis
""".split())


def _terms(title: str) -> set[str]:
    words = [w.strip(".'-&$") for w in TOKEN.findall((title or "").lower())]
    # Purely numeric tokens are worthless and actively harmful: "$72,000"
    # tokenises to "72" and "000", and "000" then clusters a bitcoin rally
    # with "$10,000 teaching aide bonuses" and "70,000 doses of Ervebo".
    words = [w for w in words
             if w and w not in STOP and len(w) > 2 and any(c.isalpha() for c in w)]
    out = set(words)
    # Bigrams catch the specific thing ("short squeeze", "debt buybacks") that
    # a unigram alone would leave ambiguous.
    out |= {f"{a} {b}" for a, b in zip(words, words[1:])}
    return out


def detect(con: sqlite3.Connection, *, window_hours: int = WINDOW_HOURS,
           min_sources: int = MIN_SOURCES, now: datetime | None = None) -> list[dict]:
    """Return the clusters of items that many sources are talking about."""
    now = now or datetime.now(timezone.utc)
    w_start = (now - timedelta(hours=window_hours)).isoformat()
    b_start = (now - timedelta(days=BASELINE_DAYS)).isoformat()

    rows = con.execute(
        "SELECT i.id, i.source, i.lane, i.title, i.url, i.published"
        " FROM items i JOIN sources s ON s.name = i.source"
        " WHERE s.enabled = 1 AND i.published >= ? AND i.title != ''",
        (b_start,)).fetchall()

    window = [r for r in rows if r["published"] >= w_start]
    baseline = [r for r in rows if r["published"] < w_start]
    if len(window) < min_sources:
        return []

    win_sources: dict[str, set[str]] = defaultdict(set)
    win_items: dict[str, list] = defaultdict(list)
    for r in window:
        for t in _terms(r["title"]):
            win_sources[t].add(r["source"])
            win_items[t].append(r)

    base_sources: dict[str, set[str]] = defaultdict(set)
    for r in baseline:
        for t in _terms(r["title"]):
            base_sources[t].add(r["source"])

    n_base_sources = len({r["source"] for r in baseline}) or 1
    win_days = max(window_hours / 24.0, 0.1)

    bursting: list[tuple[float, int, str]] = []
    for term, srcs in win_sources.items():
        if len(srcs) < min_sources:
            continue
        base = base_sources.get(term, set())
        if len(base) / n_base_sources > MAX_BASELINE_SHARE:
            continue  # everyday furniture
        rate_now = len(srcs) / win_days
        rate_base = len(base) / BASELINE_DAYS
        lift = rate_now / rate_base if rate_base else rate_now * 10
        if lift >= MIN_LIFT:
            bursting.append((lift, len(srcs), term))

    # Greedy clustering: take the term with the widest agreement, claim its
    # items, repeat. Deterministic, and easy to explain when it misfires —
    # which matters more here than cleverness.
    bursting.sort(key=lambda x: (-x[1], -x[0]))
    burst_terms = {t for _, _, t in bursting}
    claimed: set[int] = set()
    clusters: list[dict] = []
    for lift, nsrc, term in bursting:
        if len(clusters) >= MAX_CLUSTERS:
            break
        seed = [r for r in win_items[term] if r["id"] not in claimed]
        if len({r["source"] for r in seed}) < min_sources:
            continue

        # Refine membership. A single shared word is far too loose: seeding on
        # "treasury" pulled a bitcoin story, an Iran sanctions story and a
        # retirement-accounts story into one cluster. A real event has a
        # VOCABULARY, so build the signature from terms common to the seed and
        # require each item to match the seed term plus at least one more.
        counts: dict[str, int] = {}
        for r in seed:
            for t in _terms(r["title"]) & burst_terms:
                counts[t] = counts.get(t, 0) + 1
        signature = {t for t, n in counts.items()
                     if t != term and n >= max(2, len(seed) * 0.3)}
        items = [r for r in seed if _terms(r["title"]) & signature] if signature else []

        sources = {r["source"] for r in items}
        if len(sources) < min_sources:
            continue
        for r in items:
            claimed.add(r["id"])
        also = sorted(signature,
                      key=lambda t: -sum(1 for r in items if t in _terms(r["title"])))
        clusters.append({
            "term": term,
            "also": also[:6],
            "lift": round(lift, 1),
            "sources": sorted(sources),
            "n_sources": len(sources),
            "items": [{"id": r["id"], "source": r["source"], "title": r["title"],
                       "url": r["url"], "published": r["published"]}
                      for r in sorted(items, key=lambda r: r["published"], reverse=True)][:12],
        })
    return clusters
