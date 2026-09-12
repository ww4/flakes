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
from typing import Literal

import httpx

from .config import Settings

log = logging.getLogger(__name__)

WHISPER_RATE_HZ = 16000
# Below this many seconds of phone-rate audio there is nothing to transcribe:
# a RECORD FILE that hit its timeout on a dead line writes a 44-byte header,
# and whisper-server answers 400 to that.
MIN_UTTERANCE_S = 0.3
PHONE_RATE_HZ = 16000   # RECORD FILE ... wav16


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


async def resample(settings: Settings, src: Path, dst: Path, rate_hz: int, *, raw: bool = False) -> None:
    """Any input sox understands -> 16-bit signed mono PCM at rate_hz.
    raw=True writes headerless samples (Asterisk's .sln16); else a wav."""
    out_type = ["-t", "raw"] if raw else []
    await _run(settings.sox_bin, str(src), "-r", str(rate_hz), "-c", "1", "-b", "16", "-e", "signed-integer", *out_type, str(dst))


# ---------------------------------------------------------------- STT

async def transcribe(settings: Settings, wav: Path) -> str:
    """Phone-rate wav -> text via whisper-server's /inference endpoint.
    Returns "" for a recording too short to hold a word."""
    if wav.stat().st_size < 44 + int(MIN_UTTERANCE_S * PHONE_RATE_HZ * 2):
        log.info("stt: recording too short (%d bytes), skipping", wav.stat().st_size)
        return ""
    wav16 = wav.with_name(wav.name + ".16k.wav")   # not with_suffix: ".wav16" would be replaced
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

Style = Literal["conversational", "announce"]


async def say(settings: Settings, text: str, dst: Path, style: Style = "conversational") -> Path:
    """text -> Asterisk-playable audio at dst (16 kHz .sln16 by default).
    style picks the backend: conversational -> settings.tts, announce ->
    settings.announce_tts (falls back to tts).

    piper emits 22.05 kHz; a second sox pass brings it to out_rate_hz. dst is
    given without an extension (Asterisk's STREAM FILE convention) — the
    extension is appended here.
    """
    ext = "." + settings.out_ext
    if dst.suffix != ext:
        dst = dst.with_name(dst.name + ext)
    dst.parent.mkdir(parents=True, exist_ok=True)
    raw = dst.with_name(dst.name + ".tts.wav")
    backend = (settings.announce_tts or settings.tts) if style == "announce" else settings.tts
    if backend == "kokoro":
        await _kokoro(settings, text, raw)
    else:
        await _run(
            settings.piper_bin,
            "--model", str(settings.piper_voice),
            "--length_scale", str(settings.piper_length_scale),
            "--output_file", str(raw),
            stdin=text.encode(),
        )
    try:
        await resample(settings, raw, dst, settings.out_rate_hz, raw=(settings.out_ext.startswith("sln")))
    finally:
        raw.unlink(missing_ok=True)
    return dst


async def _kokoro(settings: Settings, text: str, dst: Path, voice: str | None = None) -> None:
    """Kokoro-FastAPI: POST /v1/audio/speech -> 24 kHz wav bytes."""
    body = {
        "model": "kokoro",
        "voice": voice or settings.kokoro_voice,
        "input": text,
        "response_format": "wav",
        "speed": settings.kokoro_speed,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.kokoro_timeout_s) as client:
            resp = await client.post(f"{settings.kokoro_url}/v1/audio/speech", json=body)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise AudioError(f"kokoro: {exc}") from exc
    dst.write_bytes(resp.content)


async def say_kokoro_voice(settings: Settings, text: str, voice: str, dst: Path) -> Path:
    """say() pinned to one Kokoro voice — the audition renderer."""
    ext = "." + settings.out_ext
    if dst.suffix != ext:
        dst = dst.with_name(dst.name + ext)
    dst.parent.mkdir(parents=True, exist_ok=True)
    raw = dst.with_name(dst.name + ".tts.wav")
    await _kokoro(settings, text, raw, voice=voice)
    try:
        await resample(settings, raw, dst, settings.out_rate_hz, raw=(settings.out_ext.startswith("sln")))
    finally:
        raw.unlink(missing_ok=True)
    return dst


# Kokoro voice ids are <accent><gender>_<name>: a=American b=British, f/m.
_ACCENT = {"a": "American", "b": "British"}
_GENDER = {"f": "female", "m": "male"}


def kokoro_audition_script(voice: str, n: int) -> str:
    prefix, _, name = voice.partition("_")
    who = f"{_ACCENT.get(prefix[:1], '')} {_GENDER.get(prefix[1:2], '')}".strip()
    return (
        f"Hi, my name is {name.capitalize()}, voice number {n}. I am a Kokoro 82 million parameter model, "
        f"{who}, native rate 24 kilohertz, played here at 16. ... "
        "This is the Gromit switchboard. What would you like to know? ... "
        "CPU 34 degrees. NVMe 29 degrees. The hottest spinning drive is S D B at 44 degrees, across 8 drives. ... "
        "Any key for the next voice, star to repeat, pound to hang up."
    )
