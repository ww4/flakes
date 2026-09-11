"""switchboard <command>

  ask "text"               text in, spoken-style text out (fast path or agent)
  say "text" OUT           render text to an Asterisk-rate wav
  hear IN.wav              transcribe a wav (any rate) via whisper-server
  turn IN.wav OUT          the full round trip: hear -> ask -> say
  call "text" [CHANNEL]    ring a handset and speak the text (call file)
  render-prompts           (re)render the static prompt set into <state>/prompts
  agi                      run the FastAGI server (the systemd unit)

Every stage that a call goes through can be exercised here without a phone;
`turn` is the bench test for the whole pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import agent, agi, audio, intents, outbound
from .config import Settings


async def ask_text(settings: Settings, text: str, *, allow_agent: bool = True) -> intents.Reply:
    intent = intents.route(text)
    if intent is not None:
        return await intents.answer(settings, intent)
    if not allow_agent:
        return intents.Reply(text="(no fast-path intent matched; agent disabled)")
    return intents.Reply(text=await agent.ask(settings, text))


async def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="switchboard", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ask"); a.add_argument("text"); a.add_argument("--no-agent", action="store_true")
    s = sub.add_parser("say"); s.add_argument("text"); s.add_argument("out", type=Path)
    h = sub.add_parser("hear"); h.add_argument("wav", type=Path)
    t = sub.add_parser("turn"); t.add_argument("wav", type=Path); t.add_argument("out", type=Path); t.add_argument("--no-agent", action="store_true")
    c = sub.add_parser("call"); c.add_argument("text"); c.add_argument("channel", nargs="?")
    sub.add_parser("render-prompts")
    sub.add_parser("agi")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings()

    if args.cmd == "ask":
        reply = await ask_text(settings, args.text, allow_agent=not args.no_agent)
        print(reply.text)
    elif args.cmd == "say":
        print(await audio.say(settings, args.text, args.out))
    elif args.cmd == "hear":
        print(await audio.transcribe(settings, args.wav))
    elif args.cmd == "turn":
        text = await audio.transcribe(settings, args.wav)
        print(f"heard: {text}", file=sys.stderr)
        reply = await ask_text(settings, text, allow_agent=not args.no_agent)
        print(f"reply: {reply.text}", file=sys.stderr)
        print(await audio.say(settings, reply.text, args.out))
    elif args.cmd == "call":
        print(await outbound.call_and_say(settings, args.text, args.channel))
    elif args.cmd == "render-prompts":
        settings.prompts.mkdir(parents=True, exist_ok=True)
        prompts = {"greeting": settings.greeting, **agi.PROMPTS}
        for name, text in prompts.items():
            print(await audio.say(settings, text, settings.prompts / name))
    elif args.cmd == "agi":
        await agi.serve(settings)
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(_main(sys.argv[1:])))
    except KeyboardInterrupt:
        sys.exit(130)
    except (audio.AudioError, agent.AgentError, FileNotFoundError) as exc:
        print(f"switchboard: {exc}", file=sys.stderr)
        sys.exit(1)
