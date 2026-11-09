"""Runtime configuration — environment variables, prefix SWITCHBOARD_.

Everything here has a default that works on gromit when the NixOS module is
in place; the CLI can override any of it for a bench test
(e.g. SWITCHBOARD_PIPER_VOICE=/tmp/voice.onnx switchboard say "hi" out.wav).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
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
    # Vocabulary hint for whisper. EMPTY on purpose: with a word list, base.en
    # stopped mid-sentence when it couldn't fit a listed word ("through the
    # Gra" for "through the Gromit switchboard", 2026-09-12, both hosts,
    # reproducible). The routing never depended on proper nouns anyway.
    whisper_prompt: str = ""

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

    # --- issues, spoken before the greeting's question (issues.py) ---
    alertmanager_url: str = "http://127.0.0.1:9093"
    sentinel_state: Path = Path("/var/lib/sentinel/state.json")
    # If the check takes longer than this the caller hears the plain greeting
    # rather than dead air; the issues then come out when asked.
    issues_timeout_s: float = 2.5

    # --- notifications: the ntfy topic the phone app subscribes to. The topic
    # is write-only for anonymous readers (#169), so reading needs the
    # subscriber credential ntfy-provision generates on the box. The units get
    # it via EnvironmentFile (read as root, like Homepage does); the names
    # below are that file's.
    ntfy_url: str = "http://localhost:8090"
    ntfy_topic: str = "gromit-alerts"
    ntfy_user: str = Field(default="", validation_alias=AliasChoices("SWITCHBOARD_NTFY_USER", "HOMEPAGE_VAR_NTFY_USER"))
    ntfy_pass: str = Field(default="", validation_alias=AliasChoices("SWITCHBOARD_NTFY_PASS", "HOMEPAGE_VAR_NTFY_PASS"))
    notifications_hours: float = 24.0

    # --- the newsletter (newsdesk.py): the latest edition, as the newsdesk wrote it ---
    newsdesk_edition: Path = Path("/var/lib/newsdesk/edition.reader.md")
    newsdesk_publish: Path = Path("/var/lib/newsdesk/last-publish.json")

    # --- recipes: Tandoor's API on loopback (services: hm.tandoor). Django
    # wants the vhost's Host header; the token is read-scoped (sops).
    tandoor_url: str = "http://127.0.0.1:8080"
    tandoor_host: str = "recipes.rosemaryacres.com"
    tandoor_token: str = Field(default="", validation_alias=AliasChoices("SWITCHBOARD_TANDOOR_TOKEN", "TANDOOR_API_TOKEN"))
    recipes_index_ttl_s: float = 3600.0    # the local name/keyword/food index (recipes.index)

    # --- bitcoin: the local mempool.space backend (services/mempool.nix). The
    # node has no fiat price; mempool's backend polls a price feed every few
    # minutes and serves it here along with the tip and fee estimates.
    mempool_url: str = "http://127.0.0.1:8081"

    # --- bitcoin spike watch (btcwatch.py): cheap detector on a timer, model
    # call only on a sustained new leg. Chris, 2026-09-18. ---
    # Regime-adaptive spike detector (validated across calm/grind/drop/crash
    # months). A move must be BOTH a >= btc_sigma_k outlier vs the trailing-24h
    # hourly-return volatility AND a real mean shift >= btc_min_move_pct — so it
    # self-scales and won't spam during a bull run's routine chop.
    btc_window_h: int = 3                    # hours the move is measured over
    btc_sigma_k: float = 3.0                 # move must exceed this many sigmas of recent churn
    btc_min_move_pct: float = 1.2            # ...and the 24h mean must have shifted at least this %
    btc_min_hours: int = 8                   # need this many hourly buckets before firing (cold start)

    # ── the STEP detector (added 2026-10-21) ──────────────────────────────
    # The sigma/disp test above measures a 3-HOUR window, so a fast step is
    # nearly invisible to it: on 2026-10-20 the price rose 2.20% in 42 minutes
    # and held, and the closest that test ever came was z=2.34 against a bar of
    # 3.0 one hour later. By +3h the window had slid past the step entirely and
    # the 3h return was NEGATIVE. Replayed over 135 h of real history it would
    # have fired zero times.
    #
    # Chris: "When we have a short duration spike like this, we should register
    # that as an input to the decision... But don't immediately report until we
    # see if the price stays up over the next period."
    #
    # So this arms a CANDIDATE and reports nothing. Confirmation happens later,
    # and only if the move was retained — which is what keeps a blip quiet.
    btc_step_pct: float = 1.5               # a move this big inside the window arms a candidate
    btc_step_window_s: float = 75 * 60      # ...measured over at most this long
    btc_step_confirm_s: float = 60 * 60     # wait this long before judging whether it held
    btc_step_retain_frac: float = 0.6       # this much of the move must still be present to fire
    btc_step_max_age_s: float = 6 * 3600    # abandon a candidate never confirmed by now

    # ── price sourcing (added 2026-10-30) ─────────────────────────────────
    # The price came only from the LOCAL mempool backend, and on 2026-10-28
    # 23:42 it stopped: bitcoind's disk left for Wallace, the stack came down,
    # and the watcher ran blind for 42 h while reporting success every tick.
    #
    # ⚠️ The coupling bought nothing. A Bitcoin node has no price in it --
    # mempool's /api/v1/prices is itself an aggregation of exchange data. So
    # going local took the SAME external number and added a hop through a
    # heavyweight service that can be in pieces on a workbench.
    #
    # Local stays primary so the normal case sends no external traffic
    # (polite-polling). Fallbacks are public, keyless, and agreed to within
    # 0.09% of each other and of mempool when all four were checked.
    btc_price_fallback: bool = True
    btc_source_switch_tolerance_pct: float = 0.5   # see sample_price: a venue
    # change is itself a small step, and the step detector's whole job is to
    # notice steps. A sample that both changes source AND jumps more than this
    # is dropped rather than recorded as a move.
    btc_stale_after_s: float = 3600         # warn ONCE when no good sample this long
    btc_reexplain_after_s: float = 12 * 3600  # a standing move re-explained no more often than this
    btc_history_keep_s: float = 26 * 3600
    btc_sample_min_gap_s: float = 480       # record a sample at least this often even if price is flat
    btc_explain_timeout_s: float = 150.0
    btc_notify: bool = True                 # one ntfy when a new move is explained (quiet-hours-gated)
    # Each explained move is also appended to a small rolling Atom feed on disk,
    # which the digest vhost already serves (/var/lib/digest is claude-owned and
    # published at digest.rosemaryacres.com). Newsdesk polls that URL as one
    # bitcoin-lane source and the judge curates the moves like any other feed —
    # so the "why" reaches the digest without a second model call or a DB write.
    # Empty btc_feed_path disables feed writing (tests, hosts without the digest).
    btc_feed_path: Path = Path("/var/lib/digest/btc-moves.xml")
    btc_feed_base_url: str = "https://digest.rosemaryacres.com/btc-moves.xml"
    btc_feed_keep: int = 20                  # entries retained in the rolling feed
    quiet_start_h: int = 22
    quiet_end_h: int = 7

    @property
    def ntfy_post_url(self) -> str:
        return f"{self.ntfy_url}/{self.ntfy_topic}"
    # Reachable-node count: bitnodes (redirects to btcnodes.io). The one
    # external call in the bitcoin set; cached an hour, absent if unreachable.
    btcnodes_url: str = "https://btcnodes.io/api/v1/snapshots/?limit=1"
    nodes_cache_s: float = 3600.0

    # --- weather: the NWS gridpoint for Owenton, KY (public, county-level) ---
    nws_forecast_url: str = "https://api.weather.gov/gridpoints/ILN/26,12/forecast"
    forecast_cache_s: float = 1800.0

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

    # --- escalation hook (escalation.py): Alertmanager -> loopback -> a phone call ---
    hook_host: str = "127.0.0.1"
    hook_port: int = 4575
    # Don't ring twice for the same alert inside this window even if
    # Alertmanager repeats sooner (the route repeats hourly).
    call_min_gap_s: float = 45 * 60

    @property
    def calls_dir(self) -> Path:
        return self.state_dir / "calls"

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

    # --- notes (notes.py): where "take a note" lands ---
    space_dir: Path = Path("/var/lib/silverbullet")
    inbox_page: str = "Inbox.md"                    # for Chris — triaged by the daybook
    queue_page: str = "System/Agent Queue.md"       # for Claude — the request_work format
    # Notes get a longer window than questions: up to two minutes, three
    # seconds of silence to end.
    note_max_ms: int = 120_000
    note_silence_s: int = 3

    @property
    def notes_dir(self) -> Path:
        return self.state_dir / "notes"

    # Rendered-sentence cache (see audio.say): <state>/cache/<backend>-<voice>/<sha1>.sln16
    tts_cache: bool = True

    @property
    def cache_dir(self) -> Path:
        return self.state_dir / "cache"
