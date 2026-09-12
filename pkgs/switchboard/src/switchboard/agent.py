"""The slow path: hand the question to the memory-loaded agent.

Same headless pattern as digest.nix / newsdesk: `claude -p` run as the claude
user from the docs-repo directory so its memory and playbooks load, on the
subscription OAuth (no API billing). Expect 15-60 s. The caller either waits
with filler prompts or — past hold_max_s — gets a call back (outbound.py).

The prompt makes it answer for a VOICE: a couple of plain sentences. It is
read-only by instruction, not by mechanism — the run inherits the claude
user's normal permission settings, so anything the agent could do in a
session it could do here. Keep that in mind before wiring this to a number
strangers can dial.
"""

from __future__ import annotations

import asyncio
import logging
import os

from .config import Settings

log = logging.getLogger(__name__)

_PROMPT = """\
You are answering a question by TELEPHONE. The caller is Chris, on a phone \
handset, and your answer will be read aloud by a text-to-speech engine.

Rules:
- Lead with the answer. At most two short sentences, under 35 words total —
  a long answer takes twenty seconds to play and the caller is holding a phone.
- Plain prose only.
- No markdown, no lists, no headings, no URLs, no code, no file paths.
- Spell numbers and units the way a person would say them.
- Read-only: look things up, do not change anything.
- If you cannot find out, say so in one sentence.

Where things live (go straight there — one or two commands, no exploring):
{hints}

Question: {question}
"""

# Measured 2026-09-12 on three real phone questions: without this list the
# agent spent 3-7 tool turns finding the data (8-52 s, and the smaller
# models sometimes gave up); with it, 2-4 turns (3-10 s) on every model.
# The model is not the lever; the turn count is. Overridable per deployment
# via settings.agent_hints (services.switchboard.agentHints).
DEFAULT_HINTS = """\
- failed units: systemctl --failed
- backups: journalctl -u restic-backups-critical-local -u restic-backups-critical-b2 --since yesterday
- calendar: /var/lib/pim/calendars/nextcloud/personal/*.ics (grep DTSTART/SUMMARY for the day)
- incidents: newest files in /var/lib/sentinel/incidents/
- weather forecast: curl -A gromit https://api.weather.gov/gridpoints/ILN/26,12/forecast (Owenton KY) -> properties.periods[].name/shortForecast/temperature
- weather alerts (NWS) and Ryan Hall's latest: sqlite3 /var/lib/wx/wx.db, tables nws_alert and extraction
- temps, disk, pool: Prometheus at http://127.0.0.1:9090/api/v1/query"""


class AgentError(RuntimeError):
    pass


async def ask(settings: Settings, question: str) -> str:
    env = dict(os.environ)
    env.setdefault("HOME", str(settings.claude_cwd.parent))
    env["CLAUDE_AUTONOMOUS"] = "1"   # reflection hook no-ops on headless runs
    hints = settings.agent_hints or DEFAULT_HINTS
    proc = await asyncio.create_subprocess_exec(
        settings.claude_bin, "-p", _PROMPT.format(question=question, hints=hints),
        "--output-format", "text",
        "--max-turns", str(settings.agent_max_turns),
        cwd=str(settings.claude_cwd),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=settings.claude_timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        raise AgentError(f"claude -p timed out after {settings.claude_timeout_s:.0f}s")
    if proc.returncode != 0:
        raise AgentError(f"claude -p exited {proc.returncode}: {err.decode(errors='replace')[-300:]}")
    text = " ".join(out.decode(errors="replace").split())
    if not text:
        raise AgentError("claude -p returned nothing")
    log.info("agent: %r", text[:200])
    return text
