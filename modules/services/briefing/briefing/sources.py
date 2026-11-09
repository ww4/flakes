"""Where the briefing card gets its three answers.

Each reader is deliberately total: it returns an empty result rather than
raising, because this card is decoration on a dashboard and a missing source
must never be able to blank the whole thing. What it must NOT do is invent —
an unreadable source returns nothing and the renderer says nothing about it.

The three sources, in the order the card prefers them:

  1. `sentinel` — what is actually wrong. /var/lib/sentinel/state.json, written
     by modules/services/sentinel.nix.
  2. `daybook` — what today is supposed to be about. The `## Plan` section of
     the SilverBullet space's Journal/Day page for today.
  3. `edition` — something to read, which is the fallback and also the thing
     Chris asked to be tempting.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

# ----------------------------------------------------------------- sentinel

# ⚠️ `active` in sentinel's state.json does NOT mean "the check fired on the
# last tick". Reading the sentinel loop: a check that fires sets active=True
# only once it has escalated, and active is cleared the moment the check stops
# firing. So active==True means "escalated and still not resolved" — an open
# condition — which is exactly what belongs on this card, and is why the
# consecutive counter is ignored here.
def sentinel(state_path, incident_dir, *, now=None) -> list[dict]:
    """Open sentinel conditions, freshest escalation first.

    Each entry carries `age_days` so a chronic latch reads as chronic. Three
    of these have been open for weeks on gromit; a card that shouts "3 things
    want attention!" every morning for a month becomes wallpaper, and the
    sentinel's own comments are emphatic that a muted channel is worse than no
    channel. The age is what keeps it honest.
    """
    now = now if now is not None else time.time()
    try:
        state = json.loads(Path(state_path).read_text())
    except (OSError, ValueError):
        return []

    checks = state.get("checks")
    if not isinstance(checks, dict):
        return []

    out = []
    for cid, st in checks.items():
        if not isinstance(st, dict) or not st.get("active"):
            continue
        escalated = st.get("last_escalated") or 0
        try:
            age = max(0.0, (now - float(escalated)) / 86400.0) if escalated else None
        except (TypeError, ValueError):
            age = None
        out.append({
            "id": str(cid),
            "label": _humanise(cid),
            "detail": _incident_detail(incident_dir, cid),
            "age_days": age,
            "escalated": escalated,
        })
    out.sort(key=lambda r: r["escalated"] or 0, reverse=True)
    return out


def _humanise(cid: str) -> str:
    text = str(cid).replace("-", " ").replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else "(unnamed check)"


def _incident_detail(incident_dir, cid: str) -> str:
    """First line of the newest evidence file for this check.

    Filenames are `<cid>-<epoch>.txt` (sentinel's render_page uses the same
    split). Evidence files are pruned on a retention policy, so a long-open
    condition routinely has none left — an empty string is a normal result
    here, not an error.
    """
    # ⚠️ The epoch tail must be ALL DIGITS. A bare `startswith(cid + "-")`
    # makes the check `disk` match `disk-space-200.txt` and borrow another
    # check's evidence — caught by a test, not by reading this.
    prefix = cid + "-"
    try:
        entries = []
        for name in os.listdir(incident_dir):
            if not name.endswith(".txt") or not name.startswith(prefix):
                continue
            tail = name[len(prefix):-4]
            if tail.isdigit():
                entries.append((int(tail), name))
    except OSError:
        return ""

    for _, name in sorted(entries, reverse=True):
        try:
            with open(os.path.join(incident_dir, name)) as fh:
                first = fh.readline().strip()
        except OSError:
            continue
        if first:
            return first
    return ""


# ------------------------------------------------------------------ daybook

# A plan item is a numbered markdown line. The daybook writes rich lines —
# bold lead, wiki links, emoji, issue links — and the card wants the headline
# only, so the lead **bold** span is preferred and everything else is stripped.
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_BOLD_LEAD = re.compile(r"\*\*(.+?)\*\*")
_WIKILINK = re.compile(r"\[\[([^\]|]+)\|([^\]]+)\]\]|\[\[([^\]]+)\]\]")
_MDLINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_TICKS = re.compile(r"`([^`]*)`")


def daybook(space_dir, *, today=None, limit=4) -> list[str]:
    """Today's plan headlines from Journal/Day/<date>.

    Only the `## Plan` section is read. The same page carries `### Calendar`,
    an explicitly FYI-only `### Homelab` block and `### Carryover`; pulling
    those in would put "not yours to act on" items on a card whose entire
    message is "here is what is yours today".
    """
    today = today or time.strftime("%Y-%m-%d")
    page = Path(space_dir) / "Journal" / "Day" / f"{today}.md"
    try:
        text = page.read_text()
    except OSError:
        return []

    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip().lower() == "## plan":
            start = index + 1
            break
    if start is None:
        return []

    items = []
    for line in lines[start:]:
        stripped = line.strip()
        # Any new heading ends the Plan section — including the `###`
        # subsections that follow it on the daybook's own pages.
        if stripped.startswith("#"):
            break
        match = _NUMBERED.match(line)
        if match:
            headline = _headline(match.group(1))
            if headline:
                items.append(headline)
        if len(items) >= limit:
            break
    return items


def _headline(raw: str) -> str:
    bold = _BOLD_LEAD.search(raw)
    text = bold.group(1) if bold else raw
    # A piped wikilink already carries its display text. An unpiped one is a
    # full space path (`Projects/Lock3 Website`), and the folder is noise on a
    # one-line headline — take the leaf.
    text = _WIKILINK.sub(
        lambda m: m.group(2) or (m.group(3) or "").rsplit("/", 1)[-1], text)
    text = _MDLINK.sub(r"\1", text)
    text = _TICKS.sub(r"\1", text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"\s+", " ", text).strip(" —-–:;,")
    return text.strip()


# ------------------------------------------------------------------ edition

def edition(publish_path, *, base_url) -> dict | None:
    """Today's newsdesk edition: a one-line TL;DR and a link to it.

    `last-publish.json` is written by the newsdesk at the end of a run and
    already contains a hand-sized summary, so nothing has to be re-derived
    from the edition markdown.

    ⚠️ Returns None when the run FELL BACK or published nothing. A teaser
    inviting Chris to read an edition that is empty or stale is worse than
    showing no teaser at all.
    """
    try:
        data = json.loads(Path(publish_path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    tldr = str(data.get("tldr") or "").strip()
    url = str(data.get("url") or "").strip()
    if not tldr or not url or data.get("fell_back"):
        return None
    try:
        published = int(data.get("published") or 0)
    except (TypeError, ValueError):
        published = 0
    if published <= 0:
        return None

    return {
        "tldr": tldr,
        "published": published,
        "edition": str(data.get("edition") or "").strip(),
        "url": base_url.rstrip("/") + "/" + url.lstrip("/"),
    }
