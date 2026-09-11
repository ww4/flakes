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

Question: {question}
"""


class AgentError(RuntimeError):
    pass


async def ask(settings: Settings, question: str) -> str:
    env = dict(os.environ)
    env.setdefault("HOME", str(settings.claude_cwd.parent))
    env["CLAUDE_AUTONOMOUS"] = "1"   # reflection hook no-ops on headless runs
    proc = await asyncio.create_subprocess_exec(
        settings.claude_bin, "-p", _PROMPT.format(question=question),
        "--output-format", "text",
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
