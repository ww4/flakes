"""Runtime configuration — environment variables, prefix SWITCHBOARD_.

Everything here has a default that works on gromit when the NixOS module is
in place; the CLI can override any of it for a bench test
(e.g. SWITCHBOARD_PIPER_VOICE=/tmp/voice.onnx switchboard say "hi" out.wav).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SWITCHBOARD_", extra="ignore")

    # Where recordings, rendered replies and the pre-rendered prompt set live.
    # Asterisk must be able to read this (it plays files from here) and write
    # into <state_dir>/in (RECORD FILE lands there).
    state_dir: Path = Path("/var/lib/switchboard")

    # --- speech-to-text: whisper.cpp's whisper-server, kept warm as a unit ---
    # Tried in order; the first that answers wins. wallace (5900X) first when
    # it is on, gromit's own copy otherwise — so wallace being off costs
    # latency, not the phone. Set as a JSON list in the environment.
    whisper_urls: list[str] = Field(default_factory=lambda: ["http://127.0.0.1:8778"])
    whisper_timeout_s: float = 60.0
    # How long to wait for a remote to accept the connection before moving on.
    connect_timeout_s: float = 2.0
    # Vocabulary hint: whisper biases towards words it has just "heard", so
    # the box's proper nouns go in here or base.en turns "Gromit" into "from it".
    whisper_prompt: str = (
        "Gromit switchboard. Fusion pool, backup pool, sentinel, comin, Immich, "
        "Jellyfin, Nextcloud, Forgejo, Vaultwarden, mempool, bitcoind, qBittorrent, "
        "Prometheus, Grafana, Tailscale, restic, NVMe, CPU."
    )

    # What the switchboard says when it picks up. Terse two-word greetings
    # sounded abrupt on the first real call; module option services.switchboard.greeting.
    greeting: str = "This is the Gromit switchboard. What would you like to know?"

    # --- text-to-speech backend: "piper" (CLI, ~0.15x realtime on this CPU)
    # or "kokoro" (open-notebook's Kokoro-FastAPI container, OpenAI-style
    # /v1/audio/speech, ~0.75x realtime — nicer prosody, slower) ---
    tts: Literal["piper", "kokoro"] = "piper"
    # Announcements (outbound `switchboard call`, the time/date intent) can use
    # a different backend: Chris, 2026-09-12 — Kokoro is conversational, piper
    # lessac-high "has an announcement flavor". Defaults to `tts`.
    announce_tts: Literal["piper", "kokoro", ""] = ""
    kokoro_urls: list[str] = Field(default_factory=lambda: ["http://127.0.0.1:8880"])
    kokoro_voice: str = "af_heart"
    kokoro_speed: float = 1.0
    kokoro_timeout_s: float = 90.0
    # Voices the dial-8 audition renders (ids from /v1/audio/voices).
    # hexgrad's own grades: heart A, bella A-, nicole/emma B-, the rest C+.
    # (adam is an F+, lewis a D+ — not worth a listen.)
    kokoro_audition: list[str] = Field(default_factory=lambda: [
        "af_heart", "af_bella", "af_nicole", "bf_emma",
        "am_fenrir", "am_michael", "am_puck", "af_aoede", "af_kore", "af_sarah",
    ])

    # --- piper: CLI + a voice model (.onnx with .onnx.json beside it) ---
    piper_bin: str = "piper"
    piper_voice: Path = Path("/var/lib/switchboard/voice/en_US-lessac-medium.onnx")
    # Speaking pace: piper's length_scale. 1.0 = as trained; <1 faster.
    piper_length_scale: float = 1.0
    # Wideband: 16 kHz raw signed-linear (.sln16). Asterisk plays it straight
    # to a G.722 handset and downsamples for a mu-law one, so this is never
    # worse than the 8 kHz .wav it replaced. (2026-09-11: 8 kHz sounded
    # "grinding" on the first real calls.)
    out_rate_hz: int = 16000
    out_ext: str = "sln16"

    sox_bin: str = "sox"

    # --- fast-path data sources ---
    prometheus_url: str = "http://127.0.0.1:9090"
    prometheus_timeout_s: float = 5.0
    sentinel_incidents: Path = Path("/var/lib/sentinel/incidents")
    systemctl_bin: str = "systemctl"
    # Fusion pool member mounts the status intent checks (mnt-primary-D1..D6).
    pool_mount_units: list[str] = Field(
        default_factory=lambda: [f"mnt-primary-D{i}.mount" for i in range(1, 7)]
    )
    disk_paths: list[str] = Field(
        default_factory=lambda: ["/mnt/fusion", "/mnt/backup/all", "/"]
    )

    # --- slow path: the memory-loaded agent ---
    claude_bin: str = "claude"
    claude_cwd: Path = Path("/home/claude/nixos-homelab-improvements")
    claude_timeout_s: float = 120.0
    # Tool-loop cap: a wandering answer is worse than "I couldn't find out".
    agent_max_turns: int = 6
    # "Where things live" for the phone prompt; empty = agent.DEFAULT_HINTS.
    agent_hints: str = ""
    # How long the caller waits on the line before we switch to "I'll call you
    # back" mode. Filler prompts play every filler_every_s meanwhile.
    hold_max_s: float = 75.0
    filler_every_s: float = 12.0

    # --- FastAGI listener ---
    agi_host: str = "127.0.0.1"
    agi_port: int = 4573
    # Consecutive empty turns (silence / nothing transcribed) before hanging
    # up. Three, not two: the call log showed silences that were Chris
    # reading or thinking, and one call ended on them (2026-09-11).
    max_empty_turns: int = 3

    # --- outbound: call files (Asterisk spool) ---
    asterisk_outgoing: Path = Path("/var/spool/asterisk/outgoing")
    # Channel to ring for "call me back" / escalations. PJSIP/<extension>.
    callback_channel: str = "PJSIP/101"

    @property
    def inbox(self) -> Path:
        return self.state_dir / "in"

    @property
    def outbox(self) -> Path:
        return self.state_dir / "out"

    @property
    def prompts(self) -> Path:
        return self.state_dir / "prompts"

    @property
    def kokoro_audition_dir(self) -> Path:
        return self.state_dir / "audition-kokoro"

    # Standing questions (standing.py): JSON list overriding the package
    # default when set (services.switchboard.standing).
    standing_json: str = ""

    @property
    def answers_dir(self) -> Path:
        return self.state_dir / "answers"

    @property
    def slowlog(self) -> Path:
        return self.state_dir / "slowlog.jsonl"

    # Rendered-sentence cache (see audio.say): <state>/cache/<backend>-<voice>/<sha1>.sln16
    tts_cache: bool = True

    @property
    def cache_dir(self) -> Path:
        return self.state_dir / "cache"
