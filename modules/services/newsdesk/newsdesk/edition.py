"""Ranking, and turning a judged edition into the published artefacts.

Ranking is triage, not judgement. It exists to get roughly the right forty
items onto the reader's desk out of the ~1,100 that arrive in a week, and its
only clever part is the allocation: lanes take turns.

Round-robin across lanes is what stops the linux lane (23 sources, ~794
items/week) from eating an edition whole while the energy lane (~30/week) never
appears. Within a lane the best score wins; across lanes, everyone gets a turn.
Per-source caps then stop one prolific source owning its lane. Those two rules
together are the entire reason a 2/week newsletter can survive next to a
210/week aggregator.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import events, feeds, longform, site
from .collect import awakened_sources, stale_sources
from .db import now, state_dir, write_atomic

# Items whose feed gave us only a teaser get their article fetched, but only
# for the tiers where it is likely to pay off, and only a few per run.
ARTICLE_FETCH_MAX = 12
ARTICLE_FETCH_MIN_WORDS = 120

# max_age_days is not a tidiness setting — it is what makes this a NEWS digest.
# The first collect pulls each feed's whole history (2,274 items across 86
# feeds, going back years), and without a cutoff a 2025 release announcement
# competes for a slot against this morning's. Items with no publication date
# fall back to when we first saw them.
KINDS = {
    "brief": {
        "title": "Morning brief",
        "limit": 40,
        "exclude_lanes": ["release-radar"],
        "min_words": 0,
        "max_age_days": 10,
    },
    "brief-monday": {
        "title": "Morning brief",
        "limit": 44,
        "exclude_lanes": [],
        "min_words": 0,
        "max_age_days": 10,
    },
    "longread": {
        "title": "Weekend long-read",
        "limit": 14,
        "exclude_lanes": ["release-radar"],
        "min_words": 900,
        # More generous: a substantial essay is worth reading three weeks late,
        # and the rare sources this edition exists for publish monthly.
        "max_age_days": 30,
    },
}


def edition_id(kind: str, when: datetime | None = None) -> str:
    d = (when or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    return f"{d}-{kind}"


# --------------------------------------------------------------------------
# ranking
# --------------------------------------------------------------------------

def rank(con: sqlite3.Connection, kind: str, *, fetch_articles: bool = True,
         dry_run: bool = False) -> dict:
    spec = KINDS[kind]
    excluded = set(spec["exclude_lanes"])

    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=spec["max_age_days"])).isoformat()
    rows = con.execute(
        "SELECT i.*, s.tier, s.cap, s.weight, s.insecure_tls, s.note AS source_note"
        " FROM items i JOIN sources s ON s.name = i.source"
        " WHERE i.state = 'new' AND i.score IS NOT NULL AND s.enabled = 1"
        "   AND s.role = 'read'"   # signal sources are counted, never published
        " AND i.words >= ?"
        " AND COALESCE(i.published, i.first_seen) >= ?"
        " ORDER BY i.score * s.weight DESC",
        (spec["min_words"], cutoff)).fetchall()

    by_lane: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        if r["lane"] in excluded:
            continue
        by_lane.setdefault(r["lane"], []).append(r)

    taken_per_source: dict[str, int] = {}
    chosen: list[sqlite3.Row] = []
    lanes = sorted(by_lane)
    cursor = {lane: 0 for lane in lanes}

    # Round-robin: one pick per lane per pass, best-first within the lane.
    while len(chosen) < spec["limit"]:
        progressed = False
        for lane in lanes:
            if len(chosen) >= spec["limit"]:
                break
            items = by_lane[lane]
            i = cursor[lane]
            while i < len(items):
                cand = items[i]
                i += 1
                if taken_per_source.get(cand["source"], 0) >= cand["cap"]:
                    continue
                taken_per_source[cand["source"]] = taken_per_source.get(cand["source"], 0) + 1
                chosen.append(cand)
                progressed = True
                break
            cursor[lane] = i
        if not progressed:
            break

    # A dry run must leave the database exactly as it found it — that is what
    # makes it safe to run repeatedly while calibrating the profile.
    ids = [c["id"] for c in chosen]
    if ids and not dry_run:
        con.executemany("UPDATE items SET state='shortlisted' WHERE id=?",
                        [(i,) for i in ids])
        con.commit()

    if fetch_articles and not dry_run:
        _fill_thin_bodies(con, chosen)

    candidates = []
    for c in chosen:
        row = con.execute("SELECT body, summary FROM items WHERE id=?", (c["id"],)).fetchone()
        candidates.append({
            "id": c["id"],
            "lane": c["lane"],
            "source": c["source"],
            # The catalogue's per-source editorial policy, handed to the reader
            # so a source's standing instructions are DATA. Chris can retune
            # what a source is for by editing sources.json, with no prompt
            # change: "filter this forum for discussion, not build logs".
            "source_note": c["source_note"] or "",
            "tier": c["tier"],
            "title": c["title"],
            "url": c["url"],
            "published": c["published"],
            "score": c["score"],
            "signals": json.loads(c["signals"] or "[]"),
            "text": (row["body"] or row["summary"] or "")[:12000],
        })

    # Good reads ride along in the same file but are chosen from a separate
    # pool with no recency filter — see longform.py for why that matters.
    reads = [] if dry_run else longform.shortlist(con)

    # And the corroboration detector, which answers a different question
    # entirely: not "is this worth reading" but "are many independent sources
    # suddenly saying the same thing". See events.py.
    clusters = events.detect(con)

    return {
        "kind": kind,
        "edition": edition_id(kind),
        "considered": len(rows),
        "shortlisted": len(candidates),
        "candidates": candidates,
        "good_reads": reads,
        "events": clusters,
    }


def _fill_thin_bodies(con: sqlite3.Connection, chosen) -> None:
    """Fetch the article for shortlisted items whose feed gave only a teaser.

    Bounded on purpose: a slow site must not hold up an edition, and this is a
    nice-to-have, not the product.
    """
    budget = ARTICLE_FETCH_MAX
    for c in chosen:
        if budget <= 0:
            break
        if c["tier"] == "firehose":
            continue
        if (c["words"] or 0) >= ARTICLE_FETCH_MIN_WORDS:
            continue
        budget -= 1
        try:
            text = feeds.fetch_article_text(c["url"], insecure=bool(c["insecure_tls"]))
        except Exception:  # noqa: BLE001 — a missing body is not a failure
            continue
        if len(text.split()) > (c["words"] or 0):
            con.execute("UPDATE items SET body=?, words=? WHERE id=?",
                        (text, len(text.split()), c["id"]))
    con.commit()


# --------------------------------------------------------------------------
# publishing
# --------------------------------------------------------------------------

TOKEN = re.compile(r"\[nd:(\d+)\]")


def _short_date(iso: str | None) -> str:
    """`18 Aug` — enough to see at a glance whether this is today's news."""
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%-d %b")
    except ValueError:
        return ""


def _grade_links(item_id: int, edition: str, url: str, published: str | None,
                 archived: str | None = None) -> str:
    when = _short_date(published)
    stamp = f'<span class="when">{when}</span> · ' if when else ""
    # The source link routes through /news/r, which records the click and then
    # redirects. For a good read that click is the "I read it" signal that
    # retires it from rotation.
    arch = f' · <a href="/news/{archived}">archived</a>' if archived else ""
    return (f'{stamp}<a href="/news/r?i={item_id}">source</a>{arch} · '
            f'<a href="/news/g?e={edition}&i={item_id}&v=up">&#128077;</a> '
            f'<a href="/news/g?e={edition}&i={item_id}&v=down">&#128078;</a>')


def publish(con: sqlite3.Connection, kind: str, judged_md: str, *,
            web_dir: Path | None = None, space_dir: Path | None = None,
            cmark: str | None = None) -> dict:
    """Write the edition as markdown. `newsdesk render` turns it into a site.

    cmark is accepted and ignored — kept so the systemd unit and any caller
    from before the Zola migration keep working.
    """
    eid = edition_id(kind)
    spec = KINDS[kind]
    web = web_dir or (state_dir() / "web")
    space = space_dir or Path("/var/lib/silverbullet/Areas/Newsdesk")

    short = con.execute(
        "SELECT i.*, s.tier FROM items i JOIN sources s ON s.name=i.source"
        " WHERE i.state='shortlisted'").fetchall()
    by_id = {r["id"]: r for r in short}

    judged_md = (judged_md or "").strip()
    fell_back = False
    if not judged_md:
        # The reader failed or came back empty. Publish the ranked shortlist
        # with a banner rather than an empty page — a silent blank edition is
        # indistinguishable from "no news", which is the failure this whole
        # design is organised against.
        fell_back = True
        judged_md = _fallback_markdown(short)

    published_ids = [int(m) for m in TOKEN.findall(judged_md) if int(m) in by_id]

    # Good reads are not in `short` — they come from a separate pool — so any
    # token the news shortlist does not account for is resolved directly.
    extra = [int(m) for m in set(TOKEN.findall(judged_md)) if int(m) not in by_id]
    read_ids: list[int] = []
    if extra:
        rows = con.execute(
            "SELECT i.*, s.tier FROM items i JOIN sources s ON s.name=i.source"
            f" WHERE i.id IN ({','.join('?' * len(extra))})", extra).fetchall()
        for r in rows:
            by_id[r["id"]] = r
            if (r["words"] or 0) >= longform.MIN_WORDS:
                read_ids.append(r["id"])

    # SHOWN means he saw it in a published edition — not that the reader
    # considered it. A candidate the reader passed over was never in front of
    # him, so it must not be penalised. Only what actually appears counts.
    if read_ids:
        longform.mark_shown(con, read_ids)
        # Only the picks get archived. Text is cheap; the whole intake is not.
        for rid in read_ids:
            try:
                longform.archive(con, rid, web_dir=web)
            except Exception:  # noqa: BLE001 — never cost the edition
                pass

    # --- markdown for the space: tokens become plain links -----------------
    space_md = TOKEN.sub(
        lambda m: (f"([source]({by_id[int(m.group(1))]['url']})"
                   f"{' · ' + _short_date(by_id[int(m.group(1))]['published'])
                      if _short_date(by_id[int(m.group(1))]['published']) else ''}"
                   f" `nd:{m.group(1)}`)")
        if int(m.group(1)) in by_id else "",
        judged_md)

    stale = stale_sources(con)
    awake = awakened_sources(con)
    passed = [r for r in short if r["id"] not in published_ids]

    footer_md = _footer_markdown(stale, awake, passed, fell_back)
    space_page = (f"# {spec['title']} — {eid}\n\n"
                  f"{space_md}\n\n{footer_md}\n")

    # --- HTML: tokens become source + grading links ------------------------
    html_md = TOKEN.sub(
        lambda m: _grade_links(int(m.group(1)), eid, by_id[int(m.group(1))]["url"],
                               by_id[int(m.group(1))]["published"],
                               _archived_of(con, int(m.group(1))))
        if int(m.group(1)) in by_id else "",
        judged_md)
    # --- the TLDR, needed before the page is written -----------------------
    tldr = ""
    m = re.search(r"^TLDR:\s*(.+)$", judged_md, re.MULTILINE | re.IGNORECASE)
    if m:
        tldr = m.group(1).strip()
    if not tldr:
        tldr = (f"{len(published_ids)} item(s) from {len(short)} shortlisted."
                if published_ids else "Nothing worth your time today.")

    # --- content, not presentation ----------------------------------------
    # Zola owns permalinks, previous/next, the index, pagination, the feed and
    # the search index. This used to be ~110 lines of hand-rolled HTML that had
    # accreted one reasonable increment at a time; see site.py.
    site.write_edition(
        eid, spec["title"], eid.rsplit("-", 1)[0], tldr,
        f"{html_md}\n\n{footer_md}",
        n_published=len(published_ids), n_reads=len(read_ids),
        had_event="what happened" in judged_md.lower())

    if space.parent.exists():
        try:
            write_atomic(space / f"{eid}.md", space_page)
        except OSError:
            pass  # the space is a convenience, not the product

    if published_ids:
        con.executemany(
            "UPDATE items SET state='published', edition=? WHERE id=?",
            [(eid, i) for i in published_ids])
    con.executemany("UPDATE items SET state='passed_over', edition=? WHERE id=?",
                    [(eid, r["id"]) for r in passed])
    con.execute(
        "INSERT OR REPLACE INTO editions"
        " (id, kind, created, n_short, n_published, judged, tldr)"
        " VALUES (?,?,?,?,?,?,?)",
        (eid, kind, now(), len(short), len(published_ids),
         0 if fell_back else 1, tldr))
    con.commit()

    return {"edition": eid, "published": len(published_ids),
            "shortlisted": len(short), "stale": len(stale),
            "fell_back": fell_back, "tldr": tldr,
            "url": f"{eid}/"}


def _archived_of(con: sqlite3.Connection, item_id: int) -> str | None:
    row = con.execute("SELECT archived_path FROM items WHERE id=?", (item_id,)).fetchone()
    return row["archived_path"] if row else None


def _fallback_markdown(short) -> str:
    lines = ["> **The reader did not complete.** This is the raw keyword ranking,",
             "> unjudged — expect noise. `journalctl -u newsdesk-*` has the detail.",
             ""]
    for r in short:
        lines.append(f"- **{r['title']}** [nd:{r['id']}] — {r['source']}"
                     f" · score {r['score']}")
    lines.append("")
    lines.append("TLDR: Reader failed — raw ranking published instead.")
    return "\n".join(lines)


def _footer_markdown(stale: list[dict], awake: list[dict], passed,
                     fell_back: bool) -> str:
    out = ["", "---", ""]
    if awake:
        out.append("### Back from the dead")
        out.append("")
        for a in awake:
            out.append(f"- **{a['name']}** ({a['lane']}) — {a['detail']} after"
                       " months of silence.")
        out.append("")
    if stale:
        out.append("### Sources that have gone quiet")
        out.append("")
        for s in stale:
            out.append(f"- **{s['name']}** ({s['lane']}) — {s['detail']}")
        out.append("")
    if passed and not fell_back:
        out.append(f"<details><summary>Not selected this edition ({len(passed)})"
                   "</summary>")
        out.append("")
        for r in sorted(passed, key=lambda r: -(r["score"] or 0)):
            # Linked and tracked. A click here is the strongest single signal
            # the system gets: it means the reader put something in front of
            # him that should have been an item, and he went and read it
            # anyway. See feedback.missed_clicks.
            out.append(f"- [{r['title']}](/news/r?i={r['id']}) — {r['source']}")
        out.append("")
        out.append("</details>")
        out.append("")
    return "\n".join(out)



