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
# 1. The .path unit fires on an ADD, a REMOVE *and* an in-place EDIT.
#    ⚠️ This comment said the opposite until 2026-10-06, and that was wrong --
#    asserted from "PathChanged on a directory watches the directory inode"
#    without testing it. Measured: two in-place rewrites of an existing project
#    file each triggered a render within the same second, with the directory's
#    own mtime UNCHANGED both times. inotify on a directory reports events for
#    the files in it, which is the whole point of watching a directory.
#    So the timer is NOT the refresh mechanism. Its real job is the relative
#    times -- "quiet 3d", "touched 5h ago" move with the clock, not with the
#    files, and without it a board left alone would keep claiming "touched 1m
#    ago" forever. That alone is worth the hourly tick.
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

  claude-run = import ./claude-run-pkg.nix { inherit pkgs; };

  # The prompt starts from board-sync's findings, not from the 5,900-line board,
  # so the model spends its tokens on judgement rather than on reading.
  boardReconcilePrompt = pkgs.writeText "board-reconcile.md" ''
    Reconcile the project board against the `open-loops` memory board, then stop.

    `board-sync --apply` has already run and fixed every card STATUS it could
    derive mechanically. Its report, including sections that look like projects
    but have no card, is at /tmp/board-sync-report.txt. Read that first.

    Your job is the part a regex cannot do:

    1. **A `next` that is no longer true.** The common case is one naming PRs
       that have since merged — check with `prs` before believing a card.
       One card showed three merged PRs as a blocker on Chris for 14 days.
    2. **Steps that disagree with reality.** A card with every step `done` and
       status `active` is the shape that went unnoticed for 13 days.
    3. **Cards with no section, and sections with no card.** A card with no
       `loop` pointer is board-only and fine; say so and move on. For a
       candidate section that is clearly a real project, create the card.
    4. **Prune `open-loops`.** Move resolved sections below the
       `## 🗄️ RESOLVED` fence. ⚠️ Only a section with NO `⏳` / `OPEN:` /
       `OWED` / `still open` residue — archiving a mixed section hides live
       work. Verify by line-count conservation, not by eye: that file was
       corrupted once by an index-based slice that matched a backticked
       mention in its own preamble.

    ⚠️ Do not invent a `next` you cannot source from the section or a PR state.
    A confident wrong value is worse than a stale one, because a stale one
    still shows under the board's own quiet marker.

    Change nothing outside `~/project-board/projects/` and the `open-loops`
    memory file. Finish with one line starting `TLDR:`.
  '';

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
      # Renders once on boot and on the switch that first enables it. Without
      # this the page does not exist until the hour ticks over, so the vhost
      # serves a 404 from deploy until then -- which is exactly what happened on
      # 2026-10-06: #399 deployed at 09:13 and /board/ 404'd, because the .path
      # unit only fires on a CHANGE and the directory was already there.
      # A status surface whose first impression is a 404 does not get trusted.
      wantedBy = [ "multi-user.target" ];
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
    # ── keeping the board honest ──────────────────────────────────────────
    # The board had 3 cards while `open-loops` had 139 open sections: it is a
    # second place to write, and a second place always loses to the first. So
    # the board is DERIVED from open-loops rather than maintained beside it.
    #
    # Two tiers, deliberately NOT three. A cheap-model triage tier was proposed
    # to judge whether a diff is substantive; that question is answerable by
    # grep, and a cheap model that wrongly answers "spurious, drop it" produces
    # exactly nothing, which nothing notices. A guess does not belong where a
    # grep works.

    # Tier 1 — mechanical, no model, STATUS ONLY. Refuses to touch an unchanged
    # file: the board has no `updated` field by design and uses MTIME, so
    # rewriting every card each run would make every project look freshly worked.
    systemd.services.board-sync = {
      description = "Derive project-board card status from the open-loops board";
      unitConfig.ConditionPathExists = "/home/claude/.local/bin/board-sync";
      serviceConfig = {
        Type = "oneshot";
        User = "claude";
      };
      # Never fails the unit: a drifted card is a report, not an incident, and a
      # red unit here would train Chris to ignore the board.
      script = "/home/claude/.local/bin/board-sync --apply || true";
    };

    systemd.paths.board-sync = {
      description = "Re-derive card status when the open-loops board changes";
      wantedBy = [ "multi-user.target" ];
      pathConfig = {
        PathModified = "/home/claude/.claude/projects/-home-claude-nixos-homelab-improvements/memory/open-loops.md";
        # An editing session writes that file many times a minute. 30 minutes of
        # debounce: it costs no tokens either way, but the churn makes the file
        # harder to read while it is still being edited.
        TriggerLimitIntervalSec = "30min";
        TriggerLimitBurst = 1;
      };
    };

    # Tier 2 — the judgement pass. Weekly, no diff trigger, because none of it
    # is urgent and all of it is judgement: status drift, a `next` naming merged
    # PRs, cards with no section, and pruning resolved sections below the fence.
    systemd.services.board-reconcile = {
      description = "Weekly reconcile of the project board against open-loops";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      unitConfig.ConditionPathExists = "/home/claude/.local/bin/board-sync";
      serviceConfig = {
        Type = "oneshot";
        User = "claude";
        TimeoutStartSec = "25min";
        Environment = [
          "PATH=/etc/profiles/per-user/claude/bin:/run/current-system/sw/bin:/usr/bin:/bin"
          "CLAUDE_AUTONOMOUS=1"
        ];
      };
      script = ''
        set -uo pipefail
        /home/claude/.local/bin/board-sync --candidates > /tmp/board-sync-report.txt 2>&1 || true
        set +o errexit
        out="$(${claude-run}/bin/claude-run 20m ${boardReconcilePrompt})"
        rc=$?
        set -o errexit
        if [ "$rc" -eq 2 ]; then
          echo "board-reconcile: SKIPPED — Claude login expired" >&2
          exit 0
        elif [ "$rc" -ne 0 ]; then
          echo "board-reconcile: claude-run exited $rc — stderr above" >&2
          exit 0
        fi
        printf '%s\n' "$out" | tail -40
      '';
    };

    systemd.timers.board-reconcile = {
      description = "Weekly board/open-loops reconcile";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = "Sun 05:30";
        RandomizedDelaySec = "20min";
        Persistent = true;
        Unit = "board-reconcile.service";
      };
    };

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
