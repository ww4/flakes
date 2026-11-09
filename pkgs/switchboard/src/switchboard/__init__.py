"""switchboard — dial 0 and talk to the homelab.

Pipeline for one turn of a call:

    Asterisk RECORD FILE (16 kHz wav16)
      -> stt.transcribe   (sox 8k->16k, whisper-server /inference)
      -> intents.route    (fast, deterministic readers over Prometheus / systemd / sentinel)
         or agent.ask     (slow: `claude -p`, memory-loaded, spoken-answer prompt)
      -> tts.say          (piper 22 kHz -> sox 16 kHz raw .sln16)
    Asterisk STREAM FILE

The same pipeline is exposed on the CLI (`switchboard turn in.wav out.wav`,
`switchboard ask "text"`) so every stage can be exercised without a phone.
"""

__version__ = "0.1.0"
