"""LAYER 2, part two — turning a transcript into fields a rules engine can act on.

`claude -p` reads the transcript and fills in a fixed JSON shape. The output is
deliberately STRUCTURED rather than prose: Layer 3 has to compare today's
answer against yesterday's to notice that Ryan is escalating, and you cannot
diff two paragraphs of English for that.

The context window handed to the model includes the previous few extractions,
which is what lets it thread `system_id` across days. Without that thread there
is no escalation signal at all, only a series of unrelated opinions.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess

from .db import now
from .geo import fence_score

HAZARD_ALIASES = {
    "tornadoes": "tornado", "tornadic": "tornado",
    "damaging winds": "damaging wind", "wind": "damaging wind",
    "flooding": "flash flood", "flash flooding": "flash flood",
    "snow": "significant snow", "heavy snow": "significant snow",
    "ice": "ice storm", "freezing rain": "ice storm",
}


def build_prompt(base: str, video: dict, transcript: str,
                 previous: list[dict]) -> str:
    prior = ""
    if previous:
        lines = []
        for p in previous:
            lines.append(
                f"- {p.get('created_at','')[:10]} \"{p.get('title','')}\" -> "
                f"system_id={p.get('system_id')} hazards={p.get('hazards')} "
                f"window={p.get('window_start')}..{p.get('window_end')} "
                f"escalation={p.get('escalation')}")
        prior = ("\n\n## Previous videos (for system_id threading and escalation)\n\n"
                 + "\n".join(lines))
    return (f"{base}{prior}\n\n## This video\n\n"
            f"Title: {video.get('title','')}\n"
            f"Duration: {video.get('duration','?')} s\n\n"
            f"Transcript:\n\n{transcript}\n")


def parse_response(text: str) -> dict:
    """Pull the JSON object out of whatever the model actually returned.

    Models add fences and prefaces even when told not to. Failing the whole
    extraction over a stray ```json would silently drop a day of forecasts, so
    this is forgiving about the wrapper and strict about the contents.
    """
    body = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", body, re.S)
    if fenced:
        body = fenced.group(1).strip()
    if not body.startswith("{"):
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"no JSON object in model output: {text[:200]!r}")
        body = body[start:end + 1]
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("model output was not a JSON object")
    return data


def normalise(data: dict) -> dict:
    hazards = []
    for h in data.get("hazards") or []:
        key = str(h).strip().lower()
        hazards.append(HAZARD_ALIASES.get(key, key))
    window = data.get("window") or {}
    if not isinstance(window, dict):
        window = {}
    regions = [str(r).strip() for r in (data.get("regions") or []) if str(r).strip()]
    escalation = str(data.get("escalation") or "new").strip().lower()
    if escalation not in {"up", "down", "flat", "new"}:
        escalation = "new"
    confidence = str(data.get("confidence") or "hedged").strip().lower()
    if confidence not in {"confident", "hedged", "speculative"}:
        confidence = "hedged"
    return {
        "system_id": (str(data.get("system_id")).strip()
                      if data.get("system_id") else None),
        "hazards": sorted(set(hazards)),
        "regions": regions,
        "window_start": window.get("start") or None,
        "window_end": window.get("end") or None,
        "confidence": confidence,
        "escalation": escalation,
        "summary": str(data.get("summary") or "").strip(),
        "quotes": [str(q).strip() for q in (data.get("quotes") or []) if str(q).strip()],
        "fence_score": fence_score(regions),
    }


def previous_extractions(con: sqlite3.Connection, limit: int = 4) -> list[dict]:
    rows = con.execute(
        "SELECT e.*, v.title FROM extraction e JOIN video v ON v.id = e.video_id"
        " ORDER BY e.created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def run_claude(prompt: str, *, claude: str = "claude", timeout: int = 600) -> str:
    proc = subprocess.run([claude, "-p", prompt], capture_output=True,
                          text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def store(con: sqlite3.Connection, video_id: str, fields: dict, raw: str) -> None:
    con.execute(
        "INSERT INTO extraction (video_id, created_at, system_id, hazards, regions,"
        " window_start, window_end, confidence, escalation, fence_score, summary,"
        " quotes, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(video_id) DO UPDATE SET"
        " created_at=excluded.created_at, system_id=excluded.system_id,"
        " hazards=excluded.hazards, regions=excluded.regions,"
        " window_start=excluded.window_start, window_end=excluded.window_end,"
        " confidence=excluded.confidence, escalation=excluded.escalation,"
        " fence_score=excluded.fence_score, summary=excluded.summary,"
        " quotes=excluded.quotes, raw=excluded.raw",
        (video_id, now(), fields["system_id"], json.dumps(fields["hazards"]),
         json.dumps(fields["regions"]), fields["window_start"], fields["window_end"],
         fields["confidence"], fields["escalation"], fields["fence_score"],
         fields["summary"], json.dumps(fields["quotes"]), raw[:20000]))
    con.commit()
