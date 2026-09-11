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
  switchboard = pkgs.callPackage ../../pkgs/switchboard { };
  stateDir = "/var/lib/switchboard";
  env = [
    "SWITCHBOARD_STATE_DIR=${stateDir}"
    "SWITCHBOARD_WHISPER_URL=http://127.0.0.1:${toString cfg.whisperPort}"
    "SWITCHBOARD_AGI_PORT=${toString cfg.agiPort}"
    "SWITCHBOARD_CALLBACK_CHANNEL=${cfg.callbackChannel}"
  ];
in
{
  options.services.switchboard = {
    enable = lib.mkEnableOption "the voice switchboard (whisper + intents/agent + piper behind Asterisk)";

    whisperPort = lib.mkOption { type = lib.types.port; default = 8778; };
    agiPort = lib.mkOption { type = lib.types.port; default = 4573; };

    whisperThreads = lib.mkOption {
      type = lib.types.int;
      default = 3;
      description = "CPU threads for whisper-server. The box has 4; leave one for everything else.";
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
    ];

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
      serviceConfig = {
        User = "claude";
        SupplementaryGroups = [ "asterisk" ];
        # The claude profile so the slow path's `claude -p` resolves with its
        # OAuth credentials, exactly as digest.nix does. This OVERRIDES the
        # systemd `path` option, which is why the package wraps its own tool
        # paths (sox/piper/systemctl) instead of relying on PATH.
        Environment = env ++ [
          "HOME=/home/claude"
          "PATH=/etc/profiles/per-user/claude/bin:/run/current-system/sw/bin"
          "CLAUDE_AUTONOMOUS=1"
        ];
        WorkingDirectory = "/home/claude/nixos-homelab-improvements";
        # Render the fixed prompt set before listening. Cheap (~4 s), and
        # guarantees the greeting matches the voice model in this build.
        ExecStartPre = "${switchboard}/bin/switchboard render-prompts";
        ExecStart = "${switchboard}/bin/switchboard agi";
        Restart = "on-failure";
        RestartSec = 3;
        UMask = "0022";
      };
    };
  };
}
