"""Notes by phone — dial 7, or "take a note" on the switchboard.

Two destinations, both things that already get picked up:
  for Chris  -> <space>/Inbox.md          (the daybook's inotify triage files it,
                                           verbatim copy kept in Inbox/Log)
  for Claude -> <space>/System/Agent Queue.md  (the #agent-request format
                                           homelab-mcp's request_work writes;
                                           the 09:00 daybook consumes it)

The recording is kept for 30 days under <state>/notes so a mis-heard word
can be recovered from the audio; the queue entry says where.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)

# "for Claude, ..." / "note for you: ..." / "Claude, ..." at the start of the
# transcript routes it to the agent queue; the marker itself is dropped.
_FOR_CLAUDE = re.compile(
    r"^\s*(?:(?:this is )?(?:a )?(?:note )?for (?:claude|you|the agent)[,.:;!]?\s*|claude[,:]\s*)",
    re.IGNORECASE,
)
# On the switchboard the note may arrive in the same breath as the trigger:
# "take a note: buy chain oil" / "remind me to call the vet".
_TRIGGER = re.compile(
    r"^\s*(?:(?:please )?(?:take|make|leave|save) a note(?: for (?:me|myself|claude|you))?|note to self|"
    r"remind me|remember)(?:\s+(?:that|to))?[,.:;]?\s*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Note:
    text: str
    for_claude: bool

    @property
    def recipient(self) -> str:
        return "Claude" if self.for_claude else "you"


def parse(transcript: str) -> Note | None:
    """Transcript -> Note, or None if nothing is left once the markers go."""
    t = _TRIGGER.match(transcript)
    # "take a note for Claude, ..." — the recipient rides inside the trigger.
    for_claude = bool(t and re.search(r"for (?:claude|you)\b", t.group(0), re.IGNORECASE))
    body = transcript[t.end():] if t else transcript
    m = _FOR_CLAUDE.match(body)
    if m:
        for_claude = True
        body = body[m.end():]
    body = " ".join(body.split()).strip(" .")
    if not body:
        return None
    return Note(text=body[0].upper() + body[1:] + ".", for_claude=for_claude)


def body_after_trigger(transcript: str) -> bool:
    """Did the caller say the note in the same utterance as the trigger?"""
    return _TRIGGER.sub("", transcript, count=1).strip() != ""


# ---------------------------------------------------------------- saving

def _stamp(now: dt.datetime) -> str:
    return now.strftime("%Y-%m-%d %-I:%M %p").replace("AM", "a.m.").replace("PM", "p.m.")


def keep_audio(settings: Settings, recording: Path, now: dt.datetime) -> Path | None:
    """Copy the call recording into <state>/notes (tmpfiles sweeps at 30 d)."""
    if not recording.exists():
        return None
    settings.notes_dir.mkdir(parents=True, exist_ok=True)
    dst = settings.notes_dir / f"{now.strftime('%Y%m%d-%H%M%S')}{recording.suffix}"
    shutil.copyfile(recording, dst)
    return dst


def save(settings: Settings, note: Note, *, audio: Path | None = None, now: dt.datetime | None = None) -> Path:
    """Append the note where it gets picked up; returns the page written."""
    now = now or dt.datetime.now()
    if note.for_claude:
        page = settings.space_dir / settings.queue_page
        title = " ".join(note.text.rstrip(".").split()[:8])
        lines = [
            "",
            f"- [ ] **{title}** #agent-request #whenever",
            f"    - filed {now.strftime('%Y-%m-%d %H:%M')} by phone",
            f"    - what: {note.text}",
        ]
        if audio is not None:
            lines.append(f"    - audio (30 days): {audio}")
        chunk = "\n".join(lines) + "\n"
    else:
        page = settings.space_dir / settings.inbox_page
        chunk = f"- 📞 {_stamp(now)} — {note.text}\n"
    page.parent.mkdir(parents=True, exist_ok=True)
    with page.open("a", encoding="utf-8") as fh:
        fh.write(chunk)
    log.info("note for %s -> %s: %r", note.recipient, page.name, note.text[:80])
    return page
