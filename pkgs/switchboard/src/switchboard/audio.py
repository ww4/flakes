"""Speech in, speech out. Both directions shell out — whisper via its HTTP
server (model stays loaded between calls), piper via its CLI (fast enough to
start per utterance), sox for the resampling glue between phone-rate audio
and what the models want.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import httpx

from .config import Settings

log = logging.getLogger(__name__)

WHISPER_RATE_HZ = 16000
# Below this many seconds of phone-rate audio there is nothing to transcribe:
# a RECORD FILE that hit its timeout on a dead line writes a 44-byte header,
# and whisper-server answers 400 to that.
MIN_UTTERANCE_S = 0.3
PHONE_RATE_HZ = 8000


class AudioError(RuntimeError):
    pass


async def _run(*argv: str, stdin: bytes | None = None) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(stdin)
    if proc.returncode != 0:
        raise AudioError(f"{argv[0]} exited {proc.returncode}: {err.decode(errors='replace').strip()}")
    return out


async def resample(settings: Settings, src: Path, dst: Path, rate_hz: int) -> None:
    """Any input sox understands -> 16-bit signed mono PCM wav at rate_hz."""
    await _run(settings.sox_bin, str(src), "-r", str(rate_hz), "-c", "1", "-b", "16", "-e", "signed-integer", str(dst))


# ---------------------------------------------------------------- STT

async def transcribe(settings: Settings, wav: Path) -> str:
    """Phone-rate wav -> text via whisper-server's /inference endpoint.
    Returns "" for a recording too short to hold a word."""
    if wav.stat().st_size < 44 + int(MIN_UTTERANCE_S * PHONE_RATE_HZ * 2):
        log.info("stt: recording too short (%d bytes), skipping", wav.stat().st_size)
        return ""
    wav16 = wav.with_suffix(".16k.wav")
    await resample(settings, wav, wav16, WHISPER_RATE_HZ)
    try:
        async with httpx.AsyncClient(timeout=settings.whisper_timeout_s) as client:
            with wav16.open("rb") as fh:
                resp = await client.post(
                    f"{settings.whisper_url}/inference",
                    files={"file": (wav16.name, fh, "audio/wav")},
                    data={"response_format": "json", "temperature": "0.0", "prompt": settings.whisper_prompt},
                )
        resp.raise_for_status()
        text = str(resp.json().get("text", "")).strip()
    finally:
        wav16.unlink(missing_ok=True)
    text = _clean_transcript(text)
    log.info("stt: %r", text)
    return text


_BRACKETED = re.compile(r"[\[(][^\])]*[\])]")   # "[BLANK_AUDIO]", "(silence)", "[Music]"


def _clean_transcript(text: str) -> str:
    """whisper labels non-speech in brackets; a turn that is only that is empty."""
    return _BRACKETED.sub("", text).strip()


# ---------------------------------------------------------------- TTS

async def say(settings: Settings, text: str, dst: Path) -> Path:
    """text -> Asterisk-playable wav at dst (8 kHz s16 mono by default).

    piper emits 22.05 kHz; a second sox pass brings it to phone rate. dst may
    be given without an extension (Asterisk's STREAM FILE convention) — the
    .wav is appended here.
    """
    if dst.suffix != ".wav":
        dst = dst.with_name(dst.name + ".wav")
    dst.parent.mkdir(parents=True, exist_ok=True)
    raw = dst.with_suffix(".piper.wav")
    await _run(
        settings.piper_bin,
        "--model", str(settings.piper_voice),
        "--output_file", str(raw),
        stdin=text.encode(),
    )
    try:
        await resample(settings, raw, dst, settings.out_rate_hz)
    finally:
        raw.unlink(missing_ok=True)
    return dst
