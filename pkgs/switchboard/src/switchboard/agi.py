"""FastAGI server — Asterisk hands the call to us over TCP and we drive it.

Protocol (Asterisk -> us): a block of `agi_key: value` lines then a blank
line. Then we send one command per line and read one `NNN result=...` reply
per command. A hangup shows up as a `HANGUP` line, a -1 result, or a 511.
We do the minimum: ANSWER, STREAM FILE, RECORD FILE, HANGUP. Asterisk does
the endpointing for us (RECORD FILE's silence parameter), which is why there
is no VAD in this package; AudioSocket + a VAD is the upgrade path if
barge-in ever matters.

Per turn: record -> transcribe -> route -> (fast answer | agent with fillers)
-> render -> play. The whole thing is one coroutine per call.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from . import agent, audio, intents, outbound
from .config import Settings

log = logging.getLogger(__name__)

_RESULT = re.compile(r"^(\d{3}) result=(-?\d+)(.*)$")
_WHY = re.compile(r"\((\w+)\)")


class Hangup(Exception):
    """The far end went away; stop talking."""


@dataclass
class AgiReply:
    code: int
    result: int
    rest: str = ""


@dataclass
class Call:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    env: dict[str, str] = field(default_factory=dict)

    @property
    def id(self) -> str:
        # Asterisk ids look like "1789154049.9" — the dot would make
        # Path.with_suffix() eat ".9-0" instead of appending, so it goes.
        return self.env.get("agi_uniqueid", uuid.uuid4().hex).replace(".", "-")

    @property
    def caller(self) -> str:
        return self.env.get("agi_callerid", "unknown")

    async def handshake(self) -> None:
        while True:
            line = (await self.reader.readline()).decode(errors="replace").rstrip("\r\n")
            if not line:
                break
            key, _, value = line.partition(":")
            self.env[key.strip()] = value.strip()

    async def command(self, *parts: str) -> AgiReply:
        line = " ".join(parts)
        log.debug("-> %s", line)
        self.writer.write((line + "\n").encode())
        await self.writer.drain()
        while True:
            raw = (await self.reader.readline()).decode(errors="replace").rstrip("\r\n")
            log.debug("<- %s", raw)
            if raw == "":
                raise Hangup("connection closed")
            if raw == "HANGUP":
                raise Hangup("HANGUP")
            m = _RESULT.match(raw)
            if not m:
                continue   # multi-line 520 usage text etc.
            reply = AgiReply(int(m.group(1)), int(m.group(2)), m.group(3).strip())
            if reply.code == 511 or reply.result == -1:
                raise Hangup(raw)
            if reply.code != 200:
                log.warning("agi %r -> %s", line, raw)
            return reply

    async def answer(self) -> None:
        await self.command("ANSWER")

    async def play(self, prompt_no_ext: str) -> None:
        await self.command("STREAM FILE", prompt_no_ext, '""')

    # Asterisk names the file <path>.<format>: "wav16" -> ".wav16", not ".wav".
    # (2026-09-12: three calls hung up after the beep because converse()
    # looked for .wav and read "no file" as "caller gone".)
    RECORD_FORMAT = "wav16"

    async def record(self, path_no_ext: str, *, max_ms: int = 15000, silence_s: int = 2) -> str:
        """Returns why recording stopped: timeout | dtmf | hangup | writefile | silence."""
        r = await self.command("RECORD FILE", path_no_ext, self.RECORD_FORMAT, '"#"', str(max_ms), "0", "BEEP", f"s={silence_s}")
        m = _WHY.search(r.rest)   # e.g. "(timeout) endpos=12345"
        return m.group(1) if m else "unknown"

    async def hangup(self) -> None:
        try:
            await self.command("HANGUP")
        except Hangup:
            pass


# ---------------------------------------------------------------- the call

class Switchboard:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.background: set[asyncio.Task[None]] = set()

    def prompt(self, name: str) -> str:
        return str(self.s.prompts / name)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        call = Call(reader, writer)
        try:
            await call.handshake()
            log.info("call %s from %s", call.id, call.caller)
            await call.answer()
            await call.play(self.prompt("greeting"))
            await self.converse(call)
        except Hangup as exc:
            log.info("call %s: hangup (%s)", call.id, exc)
        except Exception:
            log.exception("call %s: unhandled", call.id)
            try:
                await call.play(self.prompt("sorry"))
            except Hangup:
                pass
        finally:
            await call.hangup()
            writer.close()

    async def converse(self, call: Call) -> None:
        empty = 0
        for turn in range(50):
            rec = self.s.inbox / f"{call.id}-{turn}"
            why = await call.record(str(rec))
            wav = rec.with_name(f"{rec.name}.{call.RECORD_FORMAT}")
            if why == "hangup":
                return
            if not wav.exists():
                # Never treat this as "caller gone": it is a bug or a permissions
                # problem, and a silent goodbye is how it hid twice.
                raise RuntimeError(f"RECORD FILE reported {why!r} but {wav} does not exist")
            text = await audio.transcribe(self.s, wav)
            wav.unlink(missing_ok=True)
            intent = intents.route(text)
            if intent == "empty":
                empty += 1
                if empty >= self.s.max_empty_turns:
                    await call.play(self.prompt("goodbye"))
                    return
                # First silence: maybe we missed it. Second: they're thinking —
                # say so, don't nag.
                await call.play(self.prompt("didnt-catch" if empty == 1 else "still-here"))
                continue
            empty = 0
            if intent is not None:
                reply = await intents.answer(self.s, intent)
            else:
                reply = await self.slow(call, text)
                if reply is None:
                    return   # went to call-back mode; the line has been released
            out = await audio.say(self.s, reply.text, self.s.outbox / f"{call.id}-{turn}", style=reply.style)  # type: ignore[arg-type]
            await call.play(str(out.with_name(out.name.removesuffix(out.suffix))))
            if reply.hangup:
                return
        await call.play(self.prompt("goodbye"))

    async def slow(self, call: Call, question: str) -> intents.Reply | None:
        """Ask the agent while keeping the caller company. Past hold_max_s,
        release the line and deliver the answer by calling back."""
        # Kick the agent off FIRST; the filler plays while it is already working.
        task = asyncio.create_task(agent.ask(self.s, question))
        started = time.monotonic()
        await call.play(self.prompt("one-moment"))
        while True:
            try:
                text = await asyncio.wait_for(asyncio.shield(task), timeout=self.s.filler_every_s)
                return intents.Reply(text=text)
            except asyncio.TimeoutError:
                pass
            except agent.AgentError as exc:
                log.warning("agent: %s", exc)
                return intents.Reply(text="The agent couldn't answer that just now.")
            if time.monotonic() - started >= self.s.hold_max_s:
                await call.play(self.prompt("callback"))
                self._later(self._callback(task, question))
                return None
            await call.play(self.prompt("still-working"))

    async def _callback(self, task: asyncio.Task[str], question: str) -> None:
        try:
            text = await task
        except agent.AgentError as exc:
            text = f"Sorry, the agent couldn't answer your question. {exc}"
        await outbound.call_and_say(self.s, f"You asked: {question}. {text}")

    def _later(self, coro: "asyncio.coroutines.Coroutine[None, None, None]") -> None:
        t = asyncio.create_task(coro)
        self.background.add(t)
        t.add_done_callback(self.background.discard)


# The static prompt set, rendered once at service start (cli render-prompts).
# The greeting comes from Settings (see cli.render_prompts).
PROMPTS: dict[str, str] = {
    "didnt-catch":   "Sorry, I didn't catch that. Try again after the tone.",
    "still-here":    "Still here. Go ahead whenever you're ready.",
    "one-moment":    "Let me look into that. One moment.",
    "still-working": "Still working on it.",
    "callback":      "This is taking a while. I'll call you back with the answer. Goodbye.",
    "sorry":         "Something went wrong on my end. Goodbye.",
    "goodbye":       "Goodbye.",
}


async def serve(settings: Settings) -> None:
    settings.inbox.mkdir(parents=True, exist_ok=True)
    settings.outbox.mkdir(parents=True, exist_ok=True)
    board = Switchboard(settings)
    server = await asyncio.start_server(board.handle, settings.agi_host, settings.agi_port)
    log.info("fastagi listening on %s:%d", settings.agi_host, settings.agi_port)
    async with server:
        await server.serve_forever()
