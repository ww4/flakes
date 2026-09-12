"""Speech in, speech out. Both directions shell out — whisper via its HTTP
server (model stays loaded between calls), piper via its CLI (fast enough to
start per utterance), sox for the resampling glue between phone-rate audio
and what the models want.
"""

from __future__ import annotations

import asyncio
import hashlib
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
        payload = wav16.read_bytes()
        resp = await _first_up(
            settings, settings.whisper_urls, "/inference",
            files={"file": (wav16.name, payload, "audio/wav")},
            data={"response_format": "json", "temperature": "0.0",
                  **({"prompt": settings.whisper_prompt} if settings.whisper_prompt else {})},
        )
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

# Sentence boundaries for the cache: split after . ! ? followed by whitespace.
# "..." (a pause in the audition scripts) stays inside a sentence.
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=\S)")
# 250 ms of silence at 16 kHz s16 mono, between cached sentences.
_GAP_S = 0.25


def sentences(text: str) -> list[str]:
    return [t for t in _SENTENCE.split(text.strip()) if t]


def _backend_for(settings: Settings, style: Style) -> str:
    return (settings.announce_tts or settings.tts) if style == "announce" else settings.tts


def _voice_key(settings: Settings, backend: str) -> str:
    if backend == "kokoro":
        return f"kokoro-{settings.kokoro_voice}-{settings.kokoro_speed}"
    return f"piper-{settings.piper_voice.stem}-{settings.piper_length_scale}"


async def render(settings: Settings, text: str, backend: str, dst_raw: Path) -> None:
    """One backend call: text -> 16 kHz s16 mono raw PCM at dst_raw."""
    tmp = dst_raw.with_name(dst_raw.name + ".tts.wav")
    if backend == "kokoro":
        await _kokoro(settings, text, tmp)
    else:
        await _run(
            settings.piper_bin,
            "--model", str(settings.piper_voice),
            "--length_scale", str(settings.piper_length_scale),
            "--output_file", str(tmp),
            stdin=text.encode(),
        )
    try:
        await resample(settings, tmp, dst_raw, settings.out_rate_hz, raw=True)
    finally:
        tmp.unlink(missing_ok=True)


async def say(settings: Settings, text: str, dst: Path, style: Style = "conversational") -> Path:
    """text -> Asterisk-playable audio at dst (16 kHz .sln16 by default).

    style picks the backend: conversational -> settings.tts, announce ->
    settings.announce_tts (falls back to tts). dst is given without an
    extension (Asterisk's STREAM FILE convention); it is appended here.

    Sentence cache: each sentence is rendered once per (backend, voice) and
    kept under <state>/cache; a reply is the byte-concatenation of its
    sentences with a short gap. Raw PCM makes that free, and most fast-path
    replies are fixed sentences plus one with a number in it — so a typical
    call renders one sentence, not four. (Chris, 2026-09-12: "pre-render a
    bunch of the common phrases".)
    """
    ext = "." + settings.out_ext
    if dst.suffix != ext:
        dst = dst.with_name(dst.name + ext)
    dst.parent.mkdir(parents=True, exist_ok=True)
    backend = _backend_for(settings, style)
    if not settings.tts_cache or settings.out_ext != "sln16":
        await render(settings, text, backend, dst)
        return dst

    cache = settings.cache_dir / _voice_key(settings, backend)
    cache.mkdir(parents=True, exist_ok=True)
    gap = b"\x00" * int(_GAP_S * settings.out_rate_hz * 2)
    parts: list[bytes] = []
    misses = 0
    for sent in sentences(text):
        key = cache / (hashlib.sha1(sent.encode()).hexdigest() + ext)
        if not key.exists():
            tmp = key.with_name(key.name + ".part")
            await render(settings, sent, backend, tmp)
            tmp.replace(key)   # atomic: a concurrent call never sees a half-written entry
            misses += 1
        parts.append(key.read_bytes())
    log.info("tts %s: %d sentences, %d rendered", backend, len(parts), misses)
    dst.write_bytes(gap.join(parts))
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
    resp = await _first_up(settings, settings.kokoro_urls, "/v1/audio/speech", json=body, timeout=settings.kokoro_timeout_s)
    dst.write_bytes(resp.content)


async def _first_up(settings: Settings, bases: list[str], path: str, *, timeout: float | None = None, **post: object) -> httpx.Response:
    """POST to the first base URL that accepts the connection.

    A refused/unreachable host (wallace powered off) moves on within
    connect_timeout_s; an HTTP error from a host that IS up is final — it
    would be the same request failing everywhere.
    """
    t = httpx.Timeout(timeout or settings.whisper_timeout_s, connect=settings.connect_timeout_s)
    errors: list[str] = []
    for base in bases:
        try:
            async with httpx.AsyncClient(timeout=t) as client:
                resp = await client.post(f"{base}{path}", **post)  # type: ignore[arg-type]
            resp.raise_for_status()
            if base != bases[0]:
                log.info("using fallback %s (%s)", base, "; ".join(errors))
            return resp
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            errors.append(f"{base}: {type(exc).__name__}")
            continue
        except httpx.HTTPError as exc:
            raise AudioError(f"{base}{path}: {exc}") from exc
    raise AudioError(f"no backend reachable for {path}: " + "; ".join(errors))


async def say_kokoro_voice(settings: Settings, text: str, voice: str, dst: Path) -> Path:
    """say() pinned to one Kokoro voice, uncached — the audition renderer."""
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
