# switchboard — dial 0, talk to the homelab.
#
# Three units:
#   whisper-server    whisper.cpp HTTP server with the model loaded once
#                     (loopback :8778). Loading base.en per utterance would
#                     cost more than the transcription does.
#   switchboard-agi   the FastAGI server Asterisk hands "0" calls to
#                     (loopback :4573). Runs as the `claude` user: the slow
#                     path is `claude -p` on the subscription OAuth in
#                     /home/claude, same as digest.nix / newsdesk.
#   (prompts)         ExecStartPre renders the fixed prompt set with piper
#                     so a voice change is one rebuild + restart.
#
# Directory contract with Asterisk (pkgs/switchboard/src/switchboard/config.py):
#   /var/lib/switchboard/in       Asterisk WRITES recordings here (RECORD FILE)
#   /var/lib/switchboard/out      switchboard writes replies, Asterisk reads
#   /var/lib/switchboard/prompts  the fixed prompt set, Asterisk reads
# `in` is setgid asterisk and group-writable; the AGI unit joins the asterisk
# group (SupplementaryGroups, not a change to the claude user) to read what
# Asterisk recorded, and owns the directory so it can delete afterwards.
#
# What the slow path can do: whatever the claude user can do in a session.
# The prompt says read-only; nothing enforces it. Only handsets on this LAN /
# tailnet can dial 0 (asterisk.nix), which is the actual boundary. Do not put
# a DID in front of this without adding a real gate.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.switchboard;
  switchboard = pkgs.callPackage ../../pkgs/switchboard { inherit (cfg) voice; };
  stateDir = "/var/lib/switchboard";
  # As an attrset, NOT a serviceConfig.Environment list: NixOS quotes these,
  # whereas a bare `Environment=K=v with spaces` splits on whitespace and the
  # greeting shipped as the single word "This" (2026-09-11 — the same trap as
  # [[systemd-environment-splits-on-whitespace]], found by playing the file).
  env = {
    SWITCHBOARD_STATE_DIR = stateDir;
    SWITCHBOARD_WHISPER_URLS = builtins.toJSON (cfg.remoteWhisperUrls ++ [ "http://127.0.0.1:${toString cfg.whisperPort}" ]);
    SWITCHBOARD_KOKORO_URLS = builtins.toJSON (cfg.remoteKokoroUrls ++ [ "http://127.0.0.1:8880" ]);
    SWITCHBOARD_AGI_PORT = toString cfg.agiPort;
    SWITCHBOARD_CALLBACK_CHANNEL = cfg.callbackChannel;
    SWITCHBOARD_GREETING = cfg.greeting;
    SWITCHBOARD_PIPER_LENGTH_SCALE = toString cfg.pace;
    SWITCHBOARD_TTS = cfg.tts;
    SWITCHBOARD_ANNOUNCE_TTS = cfg.announceTts;
    SWITCHBOARD_KOKORO_VOICE = cfg.kokoroVoice;
    SWITCHBOARD_KOKORO_AUDITION = builtins.toJSON cfg.kokoroAudition;   # pydantic parses a JSON list
  };
in
{
  options.services.switchboard = {
    enable = lib.mkEnableOption "the voice switchboard (whisper + intents/agent + piper behind Asterisk)";

    whisperPort = lib.mkOption { type = lib.types.port; default = 8778; };

    # Remote inference, tried BEFORE the local copies (which stay running as
    # the fallback). wallace: hosts/wallace/switchboard-inference.nix.
    remoteWhisperUrls = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "http://100.66.171.120:8778" ];
    };
    remoteKokoroUrls = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "http://100.66.171.120:8880" ];
    };
    agiPort = lib.mkOption { type = lib.types.port; default = 4573; };

    whisperThreads = lib.mkOption {
      type = lib.types.int;
      default = 3;
      description = "CPU threads for whisper-server. The box has 4; leave one for everything else.";
    };

    voice = lib.mkOption {
      type = lib.types.enum (lib.attrNames (import ../../pkgs/switchboard/voices.nix {
        inherit (pkgs) lib fetchurl runCommand piper-tts sox jq;
      }).voices);
      default = "lessac-medium";
      description = "Piper voice (pkgs/switchboard/voices.nix). Dial 9 to audition them all from a handset.";
    };

    tts = lib.mkOption {
      type = lib.types.enum [ "piper" "kokoro" ];
      default = "piper";
      description = "Speech backend. kokoro = open-notebook's Kokoro-FastAPI container (nicer prosody, ~5x slower to render).";
    };

    announceTts = lib.mkOption {
      type = lib.types.enum [ "piper" "kokoro" ];
      default = cfg.tts;
      description = "Backend for announcements: outbound `switchboard call` and the time/date intent. Chris: piper lessac-high has an announcement flavour; Kokoro is conversational.";
    };

    kokoroVoice = lib.mkOption {
      type = lib.types.str;
      default = "af_heart";
      description = "Kokoro voice id when tts = kokoro. Dial 8 to audition.";
    };

    kokoroAudition = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      # hexgrad's grades, best first: heart A, bella A-, nicole/emma B-, rest C+.
      default = [ "af_heart" "af_bella" "af_nicole" "bf_emma" "am_fenrir" "am_michael" "am_puck" "af_aoede" "af_kore" "af_sarah" ];
      description = "Kokoro voices rendered for the dial-8 audition (at boot, by switchboard-audition-kokoro), in the order played.";
    };

    pace = lib.mkOption {
      type = lib.types.float;
      default = 1.0;
      description = "piper length_scale: 1.0 = the voice's trained pace, 0.9 = 10% brisker. Some voices are trained slow.";
    };

    greeting = lib.mkOption {
      type = lib.types.str;
      default = "This is the Gromit switchboard. What would you like to know?";
      description = "Spoken when the switchboard picks up.";
    };

    # Exposed for asterisk.nix: the rendered audition samples (dial 9) and
    # how many there are (known at eval — no import-from-derivation).
    auditionDir = lib.mkOption {
      type = lib.types.path;
      readOnly = true;
      default = switchboard.audition;
    };
    auditionCount = lib.mkOption {
      type = lib.types.int;
      readOnly = true;
      default = lib.length switchboard.catalogue.order;
    };

    callbackChannel = lib.mkOption {
      type = lib.types.str;
      default = "PJSIP/101";
      description = "Handset to ring for call-backs and `switchboard call`.";
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ switchboard ];   # `switchboard ask/say/hear/turn/call` from a shell

    systemd.tmpfiles.rules = [
      "d ${stateDir}          0755 claude asterisk -"
      "d ${stateDir}/in       2775 claude asterisk 1d"   # recordings are deleted after transcription; 1d is the safety net
      "d ${stateDir}/out      0755 claude asterisk 1d"
      "d ${stateDir}/prompts  0755 claude asterisk -"
      "d ${stateDir}/audition-kokoro 0755 claude asterisk -"
      # Rendered-sentence cache: entries unused for 30 days are swept.
      "d ${stateDir}/cache    0755 claude asterisk 30d"
    ];

    # Kokoro can't be rendered at build time (no network in the sandbox), so
    # the dial-8 samples are made here: a oneshot after the container is up,
    # off the switchboard's critical path. ~12 voices x ~25 s of audio at
    # ~0.75x realtime = a few minutes after boot before 8 has anything to play.
    systemd.services.switchboard-audition-kokoro = {
      description = "Render the Kokoro voice audition samples (dial 8)";
      wantedBy = [ "multi-user.target" ];
      after = [ "docker-open-notebook-kokoro.service" "network.target" ];
      wants = [ "docker-open-notebook-kokoro.service" ];
      environment = env;
      serviceConfig = {
        Type = "oneshot";
        User = "claude";
        ExecStart = "${switchboard}/bin/switchboard audition-kokoro";
        # The container takes a while to answer after it starts; retry rather than fail once.
        Restart = "on-failure";
        RestartSec = 30;
        TimeoutStartSec = "20min";
      };
    };

    # Every few minutes, run the live fast-path intents and render whatever
    # sentences are new into the cache (Chris, 2026-09-12: "have the answer
    # already ready … update with just the delta"). The call-time lookup is
    # still live; this just means the numbers it produces are usually already
    # rendered. Off the AGI unit's critical path.
    systemd.services.switchboard-prewarm = {
      description = "Pre-render the switchboard's likely answers into the TTS cache";
      after = [ "switchboard-agi.service" ];
      environment = env // { HOME = "/home/claude"; };
      serviceConfig = {
        Type = "oneshot";
        User = "claude";
        SupplementaryGroups = [ "asterisk" ];
        ExecStart = "${switchboard}/bin/switchboard prewarm --live";
        Nice = 10;
        UMask = "0022";
      };
    };
    systemd.timers.switchboard-prewarm = {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "1min";
        OnUnitActiveSec = "3min";
        RandomizedDelaySec = "30s";
      };
    };

    systemd.services.whisper-server = {
      description = "whisper.cpp server (speech-to-text for the switchboard)";
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        ExecStart = lib.concatStringsSep " " [
          "${pkgs.whisper-cpp}/bin/whisper-server"
          "--host 127.0.0.1" "--port ${toString cfg.whisperPort}"
          "-m ${switchboard.whisperModel}"
          "-t ${toString cfg.whisperThreads}"
        ];
        DynamicUser = true;
        Restart = "on-failure";
        RestartSec = 5;
        Nice = 5;
        # Hardening: it takes audio on loopback and returns text; nothing else.
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        NoNewPrivileges = true;
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" ];
        MemoryMax = "2G";
      };
    };

    systemd.services.switchboard-agi = {
      description = "switchboard FastAGI server (Asterisk -> whisper -> intents/agent -> piper)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" "whisper-server.service" ];
      wants = [ "whisper-server.service" ];
      # The claude profile so the slow path's `claude -p` resolves with its
      # OAuth credentials, exactly as digest.nix does. Setting PATH here
      # replaces the one NixOS derives from `path`, which is why the package
      # wraps its own tool paths (sox/piper/systemctl) instead of relying on it.
      environment = env // {
        HOME = "/home/claude";
        PATH = lib.mkForce "/etc/profiles/per-user/claude/bin:/run/current-system/sw/bin";
        CLAUDE_AUTONOMOUS = "1";
      };
      serviceConfig = {
        User = "claude";
        SupplementaryGroups = [ "asterisk" ];
        WorkingDirectory = "/home/claude/nixos-homelab-improvements";
        # Render the fixed prompt set before listening. Cheap (~4 s), and
        # guarantees the greeting matches the voice model in this build.
        # Only the prompts here (cached after the first render). The fixed-phrase
        # pre-warm belongs to the timer below — with Kokoro on gromit's own CPU,
        # prompts + pre-warm blew systemd's 90 s start timeout on three deploys
        # in a row (2026-09-12), each time failing the unit twice before it came up.
        ExecStartPre = "${switchboard}/bin/switchboard render-prompts";
        TimeoutStartSec = "5min";
        ExecStart = "${switchboard}/bin/switchboard agi";
        Restart = "on-failure";
        RestartSec = 3;
        UMask = "0022";
      };
    };
  };
}
