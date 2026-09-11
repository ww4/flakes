"""Runtime configuration — environment variables, prefix SWITCHBOARD_.

Everything here has a default that works on gromit when the NixOS module is
in place; the CLI can override any of it for a bench test
(e.g. SWITCHBOARD_PIPER_VOICE=/tmp/voice.onnx switchboard say "hi" out.wav).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SWITCHBOARD_", extra="ignore")

    # Where recordings, rendered replies and the pre-rendered prompt set live.
    # Asterisk must be able to read this (it plays files from here) and write
    # into <state_dir>/in (RECORD FILE lands there).
    state_dir: Path = Path("/var/lib/switchboard")

    # --- speech-to-text: whisper.cpp's whisper-server, kept warm as a unit ---
    whisper_url: str = "http://127.0.0.1:8778"
    whisper_timeout_s: float = 60.0
    # Vocabulary hint: whisper biases towards words it has just "heard", so
    # the box's proper nouns go in here or base.en turns "Gromit" into "from it".
    whisper_prompt: str = (
        "Gromit switchboard. Fusion pool, backup pool, sentinel, comin, Immich, "
        "Jellyfin, Nextcloud, Forgejo, Vaultwarden, mempool, bitcoind, qBittorrent, "
        "Prometheus, Grafana, Tailscale, restic, NVMe, CPU."
    )

    # --- text-to-speech: piper CLI + a voice model (.onnx with .onnx.json beside it) ---
    piper_bin: str = "piper"
    piper_voice: Path = Path("/var/lib/switchboard/voice/en_US-lessac-medium.onnx")
    # Rate Asterisk expects for a plain .wav prompt (8 kHz signed 16-bit mono).
    # Bump to 16000 and write .sln16 if the phones negotiate G.722.
    out_rate_hz: int = 8000

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
    # How long the caller waits on the line before we switch to "I'll call you
    # back" mode. Filler prompts play every filler_every_s meanwhile.
    hold_max_s: float = 75.0
    filler_every_s: float = 12.0

    # --- FastAGI listener ---
    agi_host: str = "127.0.0.1"
    agi_port: int = 4573
    # Consecutive empty turns (silence / nothing transcribed) before hanging up.
    max_empty_turns: int = 2

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
