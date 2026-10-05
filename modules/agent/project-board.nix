# PROJECT BOARD — a status surface for work that runs longer than a session.
#
# The gap this fills: on a multi-day project there is no way to find out where
# things stand. Asking the agent gets you whatever its context happens to hold,
# and the task board in memory is a wall of prose across every project at once.
#
# From Clanker Therapy 2 (Jupiter Extras, 2026-10-02), Chris describing the
# week he had just finished:
#
#   "there was a period of a valley of despair that I got myself in ... there
#    was just so much time where we were turning on backend stuff and
#    establishing goals where I wasn't, I really didn't have any idea how far
#    along, how close we were. And I could ask it, but, you know, that only
#    knows what it knows."
#
# His conclusion was to have the bot build a dashboard FIRST, before the work.
# That is this.
#
# ── WHY IT IS THIS DUMB ─────────────────────────────────────────────────────
# A project IS a JSON file in the agent's home that the agent edits with
# ordinary file tools. There is no API and no mutation CLI, because the same
# conversation covers what happens when you give an agent a structured task
# tool: both hosts had watched bots "use it and set it up and then totally stop
# using it", and both had landed on one plain file the agent maintains, with
# Chris adding that Beads cost "20, 30% of their time managing Beads". So the
# only commands are `render` and `check`, and both only READ.
#
# "Updated" is the file's MTIME, never a field. A timestamp the agent has to
# remember to bump is one that lies the first time it forgets — and the quiet
# marker ("active, but nothing has touched this in 48h") exists precisely to
# show that nothing has been happening, so it cannot be sourced from a field
# that only changes when something does.
#
# ── TWO THINGS TO KNOW OPERATIONALLY ────────────────────────────────────────
# 1. ⚠️ The .path unit catches a project file being ADDED OR REMOVED, not
#    edited. PathChanged on a directory watches the directory inode, and
#    rewriting a file in place does not touch it. That is why the timer below
#    exists and is not optional. The timer is also what keeps "quiet 3d" and
#    "touched 5h ago" true: those move with the clock, not with the files.
# 2. The projects live in /home/claude (the agent owns them; it is 0700, so
#    nginx cannot read it) and the page is rendered into /var/lib/project-board
#    (nginx can). That split is why the unit runs as the agent and writes
#    somewhere else, rather than nginx reading the source of truth directly.
#
# Served at digest.rosemaryacres.com/board/ — merged into the digest vhost the
# way sentinel.nix merges /sentinel/, so it inherits that host's TLS and source
# gate and needs no DNS record, no certificate and no new origin.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.projectBoard;

  board = pkgs.writeShellApplication {
    name = "project-board";
    runtimeInputs = [ pkgs.python3 ];
    text = ''
      export PROJECT_BOARD_PROJECTS="''${PROJECT_BOARD_PROJECTS:-${cfg.projectsDir}}"
      export PROJECT_BOARD_WEB="''${PROJECT_BOARD_WEB:-${cfg.webDir}}"
      export PROJECT_BOARD_STALE_HOURS="''${PROJECT_BOARD_STALE_HOURS:-${toString cfg.staleHours}}"
      exec python3 ${renderer} "$@"
    '';
  };

  # The renderer is the test output, not the source file — the netradio pattern
  # (modules/services/netradio/default.nix:378). What the unit runs only exists
  # if the 23 renderer tests passed, so a board that would have silently dropped
  # an unreadable project, or rendered a blank page, cannot deploy.
  renderer = pkgs.runCommand "project-board.py"
    {
      nativeBuildInputs = [ pkgs.python3 ];
    }
    ''
      PROJECT_BOARD_PY=${./project-board.py} \
        python3 -m unittest discover -s ${./tests} -t ${./tests} \
        -p 'test_project_board.py' -v
      cp ${./project-board.py} $out
    '';
in
{
  options.services.projectBoard = {
    enable = lib.mkEnableOption "the agent's project status board";

    user = lib.mkOption {
      type = lib.types.str;
      default = "claude";
      description = "User that owns the project files and renders the page.";
    };

    projectsDir = lib.mkOption {
      type = lib.types.str;
      default = "/home/claude/project-board/projects";
      description = ''
        Directory of <slug>.json project files — the source of truth, written
        by the agent by hand. Under its home on purpose: these are the agent's
        working notes, and nothing but the renderer should read them.
      '';
    };

    webDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/project-board/web";
      description = "Where index.html is written for nginx to serve.";
    };

    staleHours = lib.mkOption {
      type = lib.types.int;
      default = 48;
      description = ''
        How long an `active` project may go untouched before the page marks it
        quiet. Only `active` is ever marked: paused and done are quiet on
        purpose, and a marker that fires on a normal state stops being read.
      '';
    };

    renderInterval = lib.mkOption {
      type = lib.types.str;
      default = "hourly";
      description = ''
        OnCalendar for the backstop re-render. NOT optional: the .path unit
        below cannot see an in-place edit, and the relative times on the page
        ("quiet 3d") go stale with the clock rather than with the files.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ board ];

    systemd.tmpfiles.rules = [
      "d ${cfg.projectsDir} 0755 ${cfg.user} users - -"
    ];

    systemd.services.project-board-render = {
      description = "Render the agent's project board";
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        StateDirectory = "project-board";   # /var/lib/project-board, agent-owned
        ExecStart = "${lib.getExe board} render";
        # A render is a directory listing and some string building. If it has
        # not finished in a minute something is wrong, and hanging would make
        # the path unit queue triggers behind it.
        TimeoutStartSec = "60s";
      };
    };

    # Catches a project being added or removed. See the header: this does NOT
    # fire on an in-place edit, which is what the timer is for.
    systemd.paths.project-board-render = {
      description = "Re-render the project board when a project is added or removed";
      wantedBy = [ "multi-user.target" ];
      pathConfig = {
        PathChanged = cfg.projectsDir;
        # A burst of file writes must not queue a dozen renders.
        TriggerLimitIntervalSec = "15s";
        TriggerLimitBurst = 5;
      };
    };

    systemd.timers.project-board-render = {
      description = "Keep the project board's relative times honest";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.renderInterval;
        Persistent = true;
        RandomizedDelaySec = "2m";
      };
    };

    # Merged into the digest vhost (modules/agent/digest.nix), exactly as
    # sentinel.nix merges /sentinel/: same TLS, same source gate, no new DNS.
    services.nginx.virtualHosts."digest.rosemaryacres.com".locations = {
      "= /board".extraConfig = "return 301 /board/;";
      "/board/" = {
        alias = "${cfg.webDir}/";
        extraConfig = "index index.html;";
      };
    };
  };
}
