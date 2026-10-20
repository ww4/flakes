# briefing — the dashboard's one wide "what's going on" card.
#
# Chris asked for a single large card on the Home tab that summarises the
# daybook or current homelab issues, "sort of how we have the switchboard set
# up — there are some issues", and when there is nothing, offers something
# worth reading instead.
#
# It joins three things nothing else joins:
#
#   sentinel   /var/lib/sentinel/state.json   what is open right now
#   daybook    the SilverBullet space          what today is about
#   newsdesk   last-publish.json               what to read
#
# All three are read-only, all three are optional, and the card renders
# whatever it can get. Output goes to /var/lib/briefing/public/card.html,
# which nginx serves as an exact-match location on the dashboard vhost (see
# modules/services/homepage.nix) so the iframe is same-origin.
#
# ⚠️ Runs as the same user that owns those state directories. It writes
# nothing but its own output, and ProtectSystem=strict keeps that true.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.briefing;
  briefing = pkgs.callPackage ./package.nix { };
  bin = "${briefing}/bin/briefing";
in
{
  options.services.briefing = {
    enable = lib.mkEnableOption "the dashboard briefing card";

    user = lib.mkOption {
      type = lib.types.str;
      default = "claude";
      description = ''
        User to run as. Must be able to READ the sentinel state directory, the
        SilverBullet space and the newsdesk state directory — on gromit all
        three are owned by this user already.
      '';
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/briefing";
      description = "Where the rendered card is written.";
    };

    publicDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/briefing/public";
      description = ''
        Directory nginx serves the card out of. Kept separate from stateDir so
        that making the web root readable never implies exposing anything else
        the service might later keep.
      '';
    };

    sentinelState = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/sentinel/state.json";
      description = "sentinel's state file; `active` checks become the issue list.";
    };

    sentinelIncidents = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/sentinel/incidents";
      description = ''
        Evidence directory. The newest `<check>-<epoch>.txt` supplies the one
        line of detail shown beside an open issue. Files here are pruned on a
        retention policy, so a long-open condition often has none left — that
        is handled, not an error.
      '';
    };

    spaceDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/silverbullet";
      description = ''
        The SilverBullet space. Only `Journal/Day/<today>` is read, and only
        its `## Plan` section — the rest of that page is calendar, an
        explicitly FYI-only homelab block, and carryover.
      '';
    };

    projectsDir = lib.mkOption {
      type = lib.types.str;
      default = "/home/claude/project-board/projects";
      description = ''
        The project board's source of truth — one <slug>.json per project,
        written by the agent with ordinary file tools (see
        modules/agent/project-board.nix, which is emphatic about why there is
        no API). Read-only here.

        ⚠️ This lives under the agent's 0700 home, which is exactly why this
        unit runs as that user and writes its output somewhere nginx can
        reach, rather than nginx reading the source directly.
      '';
    };

    boardUrl = lib.mkOption {
      type = lib.types.str;
      default = "https://digest.rosemaryacres.com/board/";
      description = "Where the project card's rows link to — the full board.";
    };

    publishFile = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/newsdesk/last-publish.json";
      description = "newsdesk's end-of-run summary; supplies the reading teaser.";
    };

    newsUrl = lib.mkOption {
      type = lib.types.str;
      default = "https://digest.rosemaryacres.com/news";
      description = "Base URL the edition's relative `url` is resolved against.";
    };

    notesUrl = lib.mkOption {
      type = lib.types.str;
      default = "https://notes.rosemaryacres.com";
      description = "Where the Today panel links to.";
    };

    sentinelUrl = lib.mkOption {
      type = lib.types.str;
      default = "https://digest.rosemaryacres.com/sentinel/";
      description = "Where the issue count links to — the browsable incident log.";
    };

    interval = lib.mkOption {
      type = lib.types.str;
      default = "*:0/5";
      description = ''
        Refresh cadence. Five minutes: the sentinel ticks every two, and an
        open condition appearing on the card within one tick of a tea break is
        the point of it.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ briefing ];

    systemd.services.briefing-card = {
      description = "briefing — render the dashboard card";
      environment = {
        BRIEFING_SENTINEL_STATE = cfg.sentinelState;
        BRIEFING_SENTINEL_INCIDENTS = cfg.sentinelIncidents;
        BRIEFING_SPACE = cfg.spaceDir;
        BRIEFING_PUBLISH = cfg.publishFile;
        BRIEFING_NEWS_URL = cfg.newsUrl;
        BRIEFING_NOTES_URL = cfg.notesUrl;
        BRIEFING_SENTINEL_URL = cfg.sentinelUrl;
        BRIEFING_PUBLIC_DIR = cfg.publicDir;
      };
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        StateDirectory = "briefing";
        TimeoutStartSec = "2min";
        ProtectSystem = "strict";
        ReadWritePaths = [ cfg.stateDir ];
        NoNewPrivileges = true;
        # World-readable: nginx runs as its own user and must read the card.
        UMask = "0022";
      };
      script = "${bin} card";
    };

    # The Lab tab's project card. Separate unit from briefing-card because the
    # sources are unrelated: this one reads only the board's JSON files, and a
    # failure to read the SilverBullet space should not blank the projects.
    systemd.services.briefing-projects = {
      description = "briefing — render the project board card";
      environment = {
        BRIEFING_PROJECTS = cfg.projectsDir;
        BRIEFING_BOARD_URL = cfg.boardUrl;
        BRIEFING_PUBLIC_DIR = cfg.publicDir;
      };
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        StateDirectory = "briefing";
        TimeoutStartSec = "2min";
        ProtectSystem = "strict";
        ReadWritePaths = [ cfg.stateDir ];
        NoNewPrivileges = true;
        UMask = "0022";
      };
      script = "${bin} projects";
    };

    systemd.timers.briefing-projects = {
      description = "briefing project card refresh";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        # Project files change when the agent edits them, which is rarely and
        # unpredictably; the board's own renderer watches them with a .path
        # unit. Ten minutes is plenty for a glance card and keeps the
        # percentages honest without a second inotify watch.
        OnCalendar = "*:0/10";
        Persistent = true;
        RandomizedDelaySec = "30s";
        OnBootSec = "2min";
      };
    };

    systemd.timers.briefing-card = {
      description = "briefing card refresh";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.interval;
        Persistent = true;
        RandomizedDelaySec = "20s";
        # So the card exists shortly after a deploy rather than 404ing until
        # the next tick.
        OnBootSec = "90s";
      };
    };
  };
}
