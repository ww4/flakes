"""The project board, reduced to one card.

The board at digest.rosemaryacres.com/board/ is the full surface; this is the
glance. Chris asked for "a list of projects, percentage complete, and blocking
next step if possible" — all three come straight out of the project files, so
nothing here is derived cleverly:

    percent   done steps / total steps
    next      the card's own `next` field, which is written as the thing that
              has to happen for the project to move
    status    the card's own `status`

⚠️ A project IS a JSON file (modules/agent/project-board.nix is emphatic about
this, and about why there is no API). This module only ever READS them.

⚠️ `updated` is the file's MTIME, never a field — same rule as the board. A
timestamp the agent has to remember to bump is one that lies the first time it
forgets, and "nothing has touched this in days" is exactly what the card is
for.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

# Order the card lists them in. Blocked first because a blocked project is the
# only one on the board that wants a decision from Chris rather than more work
# from the agent; done last because it is finished and still listed only
# because nothing reconciles card status automatically.
STATUS_ORDER = {"blocked": 0, "active": 1, "paused": 2, "done": 3}

# Steps that count as finished when computing a percentage.
DONE_STATES = {"done"}


def load(projects_dir, *, now: float | None = None) -> list[dict]:
    """Every project file, newest-first within its status band.

    A file that cannot be read or parsed is SKIPPED, not faked: a card that
    silently invents a 0% project would be worse than one that is short.
    """
    now = now if now is not None else time.time()
    directory = Path(projects_dir)
    out = []
    try:
        files = sorted(directory.glob("*.json"))
    except OSError:
        return []

    for path in files:
        try:
            data = json.loads(path.read_text())
            mtime = path.stat().st_mtime
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        out.append(_summarise(data, path.stem, mtime, now))

    out.sort(key=lambda p: (STATUS_ORDER.get(p["status"], 9), -p["mtime"]))
    return out


def _summarise(data: dict, slug: str, mtime: float, now: float) -> dict:
    steps = [s for s in (data.get("steps") or []) if isinstance(s, dict)]
    total = len(steps)
    done = sum(1 for s in steps if str(s.get("state", "")).lower() in DONE_STATES)
    status = str(data.get("status") or "active").strip().lower()

    # ⚠️ A project with NO steps is 0%, not 100%. The empty-list average is the
    # classic place to accidentally report everything as finished.
    percent = round(100 * done / total) if total else 0

    return {
        "slug": slug,
        "title": str(data.get("title") or slug).strip(),
        "status": status,
        "done": done,
        "total": total,
        "percent": percent,
        "next": _next_step(data, steps, status),
        "mtime": mtime,
        "quiet_days": max(0.0, (now - mtime) / 86400.0),
    }


def _next_step(data: dict, steps: list[dict], status: str) -> str:
    """What has to happen next, preferring the card's own sentence.

    `next` is written for a human and usually names the decision or the
    command. When it is missing, the first unfinished step's name is the
    honest substitute. A finished project gets nothing — inventing a next
    step for something that is done is how a board starts lying.
    """
    if status == "done":
        return ""
    text = str(data.get("next") or "").strip()
    if text:
        return text
    for step in steps:
        if str(step.get("state", "")).lower() not in DONE_STATES:
            name = str(step.get("name") or "").strip()
            if name:
                return name
    return ""
