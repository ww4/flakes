"""Outbound: make a handset ring and speak a message.

Asterisk call files — drop a text file into /var/spool/asterisk/outgoing and
Asterisk originates the call. Written to a temp name in the same directory
and renamed in, because the spool is polled and a half-written file would be
picked up. This is the mechanism behind both "I'll call you back" from the
agent path and any future escalation (the sentinel's fire/flood/electrical
tier is the only thing allowed to ring a phone overnight — nothing network).
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path

from . import audio
from .config import Settings

log = logging.getLogger(__name__)


def call_file(channel: str, prompt: Path, *, retries: int = 2, wait_s: int = 30) -> str:
    """Contents of a call file that rings `channel` and plays `prompt`
    (a path WITHOUT extension, as Asterisk's Playback wants it)."""
    return (
        f"Channel: {channel}\n"
        f"MaxRetries: {retries}\n"
        f"RetryTime: 60\n"
        f"WaitTime: {wait_s}\n"
        f"Context: switchboard-announce\n"
        f"Extension: s\n"
        f"Priority: 1\n"
        f"Setvar: MESSAGE={prompt}\n"
    )


def spool(settings: Settings, contents: str) -> Path:
    outgoing = settings.asterisk_outgoing
    if not outgoing.is_dir():
        raise FileNotFoundError(f"asterisk outgoing spool missing: {outgoing}")
    fd, tmp = tempfile.mkstemp(prefix=".call-", dir=outgoing)
    with os.fdopen(fd, "w") as fh:
        fh.write(contents)
    final = outgoing / f"switchboard-{int(time.time() * 1000)}.call"
    os.rename(tmp, final)
    log.info("spooled %s", final)
    return final


async def call_and_say(settings: Settings, text: str, channel: str | None = None) -> Path:
    """Render `text` and ring `channel` (default: the callback handset)."""
    stamp = int(time.time())
    wav = await audio.say(settings, text, settings.outbox / f"announce-{stamp}")
    prompt = wav.with_suffix("")   # Asterisk adds the extension itself
    return spool(settings, call_file(channel or settings.callback_channel, prompt))
