"""LAYER 2, part three — handing the trend to the newsdesk.

Chris was explicit that he does not want a second daily brief: weather trends
belong in the news feed he already reads. So wx does not publish anything of
its own. It writes markdown into a corpus directory and the newsdesk ingests it
as an ordinary source on a `weather` lane, judged and published alongside
everything else.

⚠️ THE 300-WORD CONTRACT. newsdesk/corpus.py drops any post under MIN_WORDS —
it is a guard against scrape wreckage, and it cannot tell our short post from a
truncated one. A rendered post that falls under it disappears silently, which
is the worst possible failure: the pipeline reports success and the item never
appears. MIN_WORDS is mirrored here on purpose, the excerpt is sized to clear
it with room, and there is a test that asserts a representative post does.
**If newsdesk's floor changes, this must change with it.**
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

# Mirrors newsdesk/corpus.py MIN_WORDS. Change both together.
NEWSDESK_MIN_WORDS = 300
# Transcript evidence included in the post. Sized so that summary + fields +
# quotes + excerpt clears the floor for even a terse extraction.
EXCERPT_WORDS = 220


def _excerpt(transcript: str, regions: list[str], words: int = EXCERPT_WORDS) -> str:
    """A window of the transcript, centred on where he talks about our region.

    The point is evidence, not padding: when the extraction says "northern
    Kentucky, Thursday", this is the passage that either backs that up or shows
    the auto-captions mangled a place name.
    """
    text = re.sub(r"\s+", " ", transcript or "").strip()
    if not text:
        return ""
    tokens = text.split()
    anchor = 0
    lowered = text.lower()
    for region in regions or []:
        idx = lowered.find(str(region).strip().lower())
        if idx > 0:
            anchor = len(text[:idx].split())
            break
    start = max(0, anchor - words // 3)
    return " ".join(tokens[start:start + words])


def render_post(*, video: dict, fields: dict, transcript: str,
                spc_note: str = "") -> str:
    published = video.get("published") or datetime.now(timezone.utc).isoformat()
    day = str(published)[:10]
    title = video.get("title") or "Ryan Hall forecast"
    url = f"https://www.youtube.com/watch?v={video['id']}"

    hazards = ", ".join(fields.get("hazards") or []) or "none named"
    regions = ", ".join(fields.get("regions") or []) or "none named out loud"
    window = " to ".join(x for x in (fields.get("window_start"),
                                     fields.get("window_end")) if x) or "no dates given"

    quotes = "\n\n".join(f"> {q}" for q in (fields.get("quotes") or []))

    parts = [
        "---",
        f"title: {title}",
        f"date: {day}",
        f"url: {url}",
        "---",
        "",
        f"# {title}",
        "",
        fields.get("summary") or "",
        "",
        "## What he is forecasting",
        "",
        f"- **Hazards:** {hazards}",
        f"- **Regions he named:** {regions}",
        f"- **Window:** {window}",
        f"- **His confidence:** {fields.get('confidence') or 'unknown'}",
        f"- **Versus his last video on this system:** {fields.get('escalation') or 'new'}",
    ]
    if spc_note:
        parts += ["", f"- **Against the official outlook:** {spc_note}"]
    if quotes:
        parts += ["", "## In his words", "", quotes]
    excerpt = _excerpt(transcript, fields.get("regions") or [])
    if excerpt:
        parts += [
            "", "## Transcript excerpt", "",
            "Auto-captions, lightly flattened — included as evidence for the "
            "extraction above, since he narrates maps and place names are the "
            "part most often mangled.", "",
            excerpt,
        ]
    return "\n".join(parts).rstrip() + "\n"


def write_post(corpus_dir: str | Path, video: dict, body: str) -> Path:
    d = Path(corpus_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"ryan-{video['id']}.md"
    path.write_text(body, encoding="utf-8")
    return path


def word_count(body: str) -> int:
    """Words as newsdesk counts them: after front matter and the leading H1."""
    text = body
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            text = text[end + 4:]
    text = re.sub(r"\A\s*#[^\n]*\n(\*[^\n]*\*\n)?", "", text).strip()
    return len(text.split())
