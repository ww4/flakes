# WX — severe weather alerting, and mining Ryan Hall for lead time.
#
# Design: ww4/nixos-homelab-improvements docs/weather-watch-research.md (docs #20).
#
# Three layers, three latencies, three geofences, three output routes:
#
#   LAYER 1  wx-alerts   NWS api.weather.gov, HIS COORDINATES, every 3 minutes.
#                        The only layer permitted to pierce quiet hours.
#   LAYER 2  wx-ryan     Ryan Hall's transcripts -> a `weather` lane in the
#                        newsdesk. Northern Kentucky. Twice a day.
#   LAYER 3  wx-ryan     The rule between them: push an ad-hoc ntfy only when
#                        Ryan is AHEAD of the official SPC outlook, or is
#                        escalating against his own previous video.
#
# THE LOAD-BEARING RULE: Ryan Hall is never the warning layer. A tornado
# warning cannot wait on a YouTube upload, a caption pass and an LLM call —
# which is exactly why Layer 1 is a separate unit on a 3-minute timer that
# depends on nothing but NWS.
#
# WHAT MAY WAKE HIM (Chris, 2026-09-07): tornado warning, flash flood
# emergency, extreme wind warning. He was explicit that a SEVERE THUNDERSTORM
# WARNING IS NOT ON THAT LIST. Everything else — including everything Ryan Hall
# ever says — is held until 07:00 and delivered as one consolidated summary.
#
# ⚠️ PII. cfg.locationFile points at a sops secret holding his home
# coordinates. They are never committed in plaintext, never logged, and never
# put in a notification body. ww4/flakes is PUBLIC on GitHub.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.wx;

  gromit-notify = import ../notify-pkg.nix { inherit pkgs; };

  wx = pkgs.callPackage ./package.nix { inherit gromit-notify; };

  # Absolute store path everywhere, never via PATH — the wx-ryan unit overrides
  # PATH wholesale so `claude -p` finds its OAuth credentials, and
  # `Environment=PATH=` silently defeats the systemd `path` option.
  bin = "${wx}/bin/wx";

  # Evaluated against the source tree, so it becomes true the moment Chris
  # commits the encrypted file — no module change needed to turn layer 1 on.
  secretPresent = builtins.pathExists ../../../secrets/wx-location.json;

  # ⚠️ An ATTRSET rendered via `environment`, NOT a list via
  # `serviceConfig.Environment`. NixOS quotes every assignment it renders from
  # this option; the raw list is passed through verbatim. `userAgent` contains a
  # space, so as a list entry systemd split it at the space: WX_USER_AGENT was
  # silently set to the truncated `(gromit-wx,` and the remainder was logged as
  # "Invalid environment assignment, ignoring". Nothing would have surfaced
  # that — the variable WAS set, just to a malformed User-Agent, which is
  # precisely what NWS throttles. Found 2026-09-09, deployed broken.
  commonEnv = {
    WX_STATE = cfg.stateDir;
    WX_CORPUS_DIR = cfg.corpusDir;
    WX_PROMPT = "${wx}/share/wx/extract-prompt.md";
    WX_USER_AGENT = cfg.userAgent;
    WX_LOCATION_FILE = cfg.locationFile;
  };
in
{
  options.services.wx = {
    enable = lib.mkEnableOption "the weather watch (NWS alerts + Ryan Hall lead time)";

    user = lib.mkOption {
      type = lib.types.str;
      default = "claude";
      description = ''
        User to run as. Must be the one whose `claude` CLI holds subscription
        OAuth credentials — the extraction stage runs `claude -p` the same way
        the newsdesk's reader does.
      '';
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/wx";
      description = "Database, transcripts, and the rendered corpus.";
    };

    corpusDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/wx/corpus";
      description = ''
        Markdown the newsdesk ingests as its `weather` lane. wx publishes
        nothing itself: Chris asked for weather trends to arrive in the news
        feed he already reads, not in a second daily brief.
      '';
    };

    locationFile = lib.mkOption {
      type = lib.types.str;
      # Written literally rather than read back out of config.sops.secrets:
      # an option default that reads `config` makes this module's evaluation
      # depend on sops-nix having already evaluated, for no gain. This is where
      # sops-nix puts it.
      default = "/run/secrets/wx-location";
      description = ''
        Path to the decrypted file holding `latitude` and `longitude`.

        ⚠️ PII — his home coordinates, used to match the actual NWS warning
        POLYGON rather than the county. Owen County is large and a warning over
        its eastern third is not about his house.
      '';
    };

    userAgent = lib.mkOption {
      type = lib.types.str;
      default = "(gromit-wx, chris.saenz@broadlinc.com)";
      description = ''
        NWS asks every API client for a contactable identifier and will
        throttle anonymous traffic. Contains no coordinates.
      '';
    };

    alertsSchedule = lib.mkOption {
      type = lib.types.str;
      default = "*:0/3";
      description = ''
        LAYER 1 polling cadence. Three minutes is deliberately faster than the
        polite-polling default that governs everything else on this box: a
        tornado warning carries about ten minutes of lead time, and NWS
        publishes this API expressly to be polled. It is also the one path here
        where latency is the entire product.
      '';
    };

    ryanSchedule = lib.mkOption {
      type = lib.types.str;
      default = "*-*-* 07,15,20:30:00";
      description = ''
        LAYER 2/3. He uploads once or twice a day, typically early afternoon
        Eastern; these three runs catch the morning's video, the afternoon's,
        and anything late. Never more often — the caption endpoint returns 429
        after roughly four pulls.
      '';
    };

    morningSchedule = lib.mkOption {
      type = lib.types.str;
      default = "*-*-* 07:05:00";
      description = ''
        Flush whatever quiet hours held, as ONE message. After 07:00, never
        before: a morning-facing job scheduled inside quiet hours is the exact
        mistake netwatch made when it woke him at 06:45.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ wx ];

    # The agent adds the reference; Chris adds the value — the standing split for
    # every secret on this box, and the reason there is no plaintext coordinate
    # anywhere in this repo. He creates it with:
    #
    #   sops secrets/wx-location.json
    #   {"latitude": "38.xxxxxxx", "longitude": "-84.xxxxxxx"}
    #
    # ⚠️ Declared only if the file EXISTS, and warned about loudly if it does
    # not. The alternative — an unconditional declaration — makes
    # `nixos-rebuild build` fail on a missing secret, which would block the
    # deploy of everything else on the box for a file only Chris can create.
    # The failure is not swallowed: `wx alerts` exits non-zero with a clear
    # message every three minutes until the secret is there, so this cannot go
    # quietly missing.
    sops.secrets = lib.mkIf secretPresent {
      "wx-location" = {
        sopsFile = ../../../secrets/wx-location.json;
        format = "json";
        key = "";             # the whole file, not one key out of it
        owner = cfg.user;
        mode = "0400";
      };
    };

    warnings = lib.optional (!secretPresent) ''
      services.wx: secrets/wx-location.json does not exist, so layer 1 (NWS
      alerts) cannot run — it has no point to query. Create it with:
        sops secrets/wx-location.json
      containing {"latitude": "...", "longitude": "..."}.
      Layers 2 and 3 (Ryan Hall -> the newsdesk) work without it.
    '';

    # ---------------------------------------------------------------- LAYER 1
    systemd.services.wx-alerts = {
      description = "wx — NWS alerts for this point (layer 1)";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      environment = commonEnv;
      # ⚠️ NOT CONFIGURED YET IS NOT A FAILURE. Without this condition the unit
      # exits 1 every three minutes for as long as the location secret is
      # missing — 81 failures in the first eight hours after deploy — and each
      # sentinel sweep re-escalates a unit that is behaving exactly as designed.
      # I chose "fail loudly" so a missing secret could not go quiet, and got a
      # repeating false alarm about a condition only Chris can clear. A
      # condition check makes systemd SKIP the unit and leave it inactive rather
      # than failed, so the state stays visible (`wx status`, the eval warning,
      # the skip line in the journal) without crying wolf on a 3-minute loop.
      unitConfig.ConditionPathExists = cfg.locationFile;
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        StateDirectory = "wx";
        TimeoutStartSec = "2min";
        # It talks to one public API and writes one SQLite file.
        ProtectSystem = "strict";
        ReadWritePaths = [ cfg.stateDir ];
        PrivateDevices = true;
        NoNewPrivileges = true;
      };
      script = "${bin} alerts";
    };

    systemd.timers.wx-alerts = {
      description = "wx alert polling";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.alertsSchedule;
        # NOT Persistent: replaying a missed poll after an outage would
        # re-evaluate warnings that have since expired. The next run in three
        # minutes is the catch-up.
        Persistent = false;
        RandomizedDelaySec = "20s";
      };
    };

    # ------------------------------------------------------------- LAYER 2/3
    systemd.services.wx-ryan = {
      description = "wx — Ryan Hall transcripts, extraction and trend rules (layers 2+3)";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      # No ConditionPathExists here: layers 2 and 3 are the half that works
      # without the secret, and they must keep running while it is missing.
      environment = commonEnv // {
        HOME = "/home/${cfg.user}";
        # Mirrors newsdesk/digest: the interactive claude env, so `claude -p`
        # finds its subscription OAuth credentials rather than an API key.
        # mkForce because NixOS already defines PATH for every unit from the
        # `path` option — overriding it is the point, and without mkForce the
        # two definitions conflict rather than one winning silently.
        PATH = lib.mkForce "/etc/profiles/per-user/${cfg.user}/bin:/run/current-system/sw/bin:/usr/bin:/bin";
        # Headless run — the agent's Stop reflection hook must no-op rather
        # than derail an extraction into doing /retro work.
        CLAUDE_AUTONOMOUS = "1";
      };
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        StateDirectory = "wx";
        WorkingDirectory = "/home/${cfg.user}";
        # The corpus is read by the newsdesk running as the same user, but born
        # group-writable keeps it consistent with how that service's state is
        # handled everywhere else.
        UMask = "0002";
        TimeoutStartSec = "30min";
      };
      script = ''
        set -uo pipefail
        # Non-zero here means "could not do the job" (channel unreachable), not
        # "nothing happened". A quiet day exits 0.
        ${bin} ryan --ytdlp ${pkgs.yt-dlp}/bin/yt-dlp
      '';
    };

    systemd.timers.wx-ryan = {
      description = "wx Ryan Hall polling";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.ryanSchedule;
        Persistent = true;
        RandomizedDelaySec = "6m";
      };
    };

    # ------------------------------------------------ the quiet-hours release
    systemd.services.wx-morning = {
      description = "wx — deliver what quiet hours held, as one summary";
      environment = commonEnv;
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        StateDirectory = "wx";
        TimeoutStartSec = "2min";
        ProtectSystem = "strict";
        ReadWritePaths = [ cfg.stateDir ];
        NoNewPrivileges = true;
      };
      script = "${bin} morning";
    };

    systemd.timers.wx-morning = {
      description = "wx overnight release";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.morningSchedule;
        Persistent = true;
        RandomizedDelaySec = "2m";
      };
    };
  };
}
