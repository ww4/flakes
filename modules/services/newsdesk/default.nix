# NEWSDESK — a personal news digest from esoteric RSS sources.
#
# Design + the reasoning behind the 86-source catalogue:
# ww4/nixos-homelab-improvements docs/newsdesk-research.md (docs PR #11).
#
# Four stages, three of which already existed on this box in another form:
#
#   collect  86 feeds -> SQLite. Conditional requests, per-source failure
#            isolation, and freshness bookkeeping.
#   rank     keyword density against a TUNABLE profile, then round-robin
#            across lanes so the linux firehose (~794 items/week) cannot eat
#            an edition while the energy lane (~30/week) never appears.
#   judge    `claude -p` reads the shortlist and rejects most of it. This is
#            the stage that earns the whole thing: the sources are on-topic
#            constantly and still mostly not worth his time, and no amount of
#            keyword work can tell substance from punditry.
#   publish  one HTML page, one SilverBullet page, one ntfy line.
#
# THE NOTIFICATION RULE. This is never urgent. One notification per edition,
# normal priority, and the script refuses to send one outside 07:00-22:00 even
# if a Persistent= catch-up fires the unit at three in the morning after the
# box was down. Nothing informational may ever pierce quiet hours.
#
# GRADING is optional and must stay that way — nothing nags, and an absent
# grade is never read as disapproval (see feedback.py, which is where that rule
# actually lives).
{ config, lib, pkgs, ... }:

let
  cfg = config.services.newsdesk;

  newsdesk = pkgs.callPackage ./package.nix { };

  # Called by absolute store path everywhere below, never via PATH. The edition
  # units override PATH wholesale (they need the claude profile so `claude -p`
  # finds its OAuth credentials), and `Environment=PATH=` silently defeats the
  # systemd `path` option — the exact shape of the #165 bug, where a tool was
  # on the interactive PATH and missing from every timer run.
  nd = "${newsdesk}/bin/newsdesk";

  # Shared by both edition units. `kind` is resolved at runtime for the brief
  # so that Monday picks up the release-radar lane (releases are not news;
  # batching them weekly keeps four days of editions cleaner).
  editionScript = kindExpr: ''
    set -uo pipefail
    export NEWSDESK_STATE=${cfg.stateDir}

    # Idempotent: adds sources new to the catalogue, refreshes their immutable
    # facts, and never touches the tuned cap/weight columns. A deploy must not
    # undo a week of his feedback.
    ${nd} seed \
      --catalogue ${newsdesk}/share/newsdesk/sources.json \
      --profile ${newsdesk}/share/newsdesk/interests.json || {
        echo "newsdesk: seeding failed — aborting this edition"
        exit 0
      }

    # His source edits FIRST — before anything is collected — so an
    # instruction written overnight takes effect this morning rather than
    # tomorrow. Failure here must not stop the edition.
    ${nd} sources --page ${cfg.spaceDir}/Sources.md || \
      echo "newsdesk: sources page sync failed — continuing"

    # Then grades, so the ranking reflects anything he clicked since the last
    # run.
    ${nd} grade --space ${cfg.spaceDir} || true

    # A failed poll is one source's problem, never the edition's.
    ${nd} collect || echo "newsdesk: collect returned non-zero — continuing"
    ${nd} ingest || echo "newsdesk: corpus ingest returned non-zero — continuing"

    kind=${kindExpr}
    n="$(${nd} rank --kind "$kind" | head -1)" || n=0
    echo "newsdesk: shortlisted $n for $kind"

    raw=${cfg.stateDir}/edition.raw.md
    : > "$raw"
    if [ "''${n:-0}" -eq 0 ]; then
      printf '%s\n' "Nothing new arrived since the last edition." > "$raw"
      printf '%s\n' "" >> "$raw"
      printf '%s\n' "TLDR: no new items." >> "$raw"
    ${lib.optionalString cfg.judge.enable ''
    else
      # An empty or failed reader is NOT fatal: publish falls back to the raw
      # ranking behind a loud banner, because a blank page is indistinguishable
      # from "no news" and that is the failure mode this whole design is
      # organised against.
      timeout ${cfg.judge.timeout} claude -p "$(cat ${newsdesk}/share/newsdesk/judge-prompt.md)" \
        > "$raw" 2>/dev/null \
        || echo "newsdesk: reader did not complete (see journalctl)"
    ''}
    fi

    out=${cfg.stateDir}/last-publish.json
    ${nd} publish --kind "$kind" --input "$raw" \
      --space ${cfg.spaceDir} --cmark ${pkgs.cmark-gfm}/bin/cmark-gfm > "$out" || {
        echo "newsdesk: publish failed"
        exit 1
      }

    published="$(${pkgs.jq}/bin/jq -r '.published // 0' "$out")"
    tldr="$(${pkgs.jq}/bin/jq -r '.tldr // ""' "$out")"

    # Quiet-hours guard. OnCalendar puts these at civil hours, but Persistent=
    # replays a missed run at boot — which can be 03:00 after an outage. No
    # informational notification may ever pierce quiet hours.
    hour="$(date +%-H)"
    if [ "$published" -gt 0 ] && [ "$hour" -ge ${toString cfg.notifyAfterHour} ] \
       && [ "$hour" -lt ${toString cfg.notifyBeforeHour} ]; then
      gromit-notify "Newsdesk — $kind" "$tldr
${cfg.editionUrl}" default "newspaper" "${cfg.editionUrl}"
    else
      echo "newsdesk: not notifying (published=$published hour=$hour)"
    fi
  '';

  # One definition of the edition units' execution environment, shared rather
  # than cross-referenced through `config`, which would make the two units'
  # evaluation depend on each other for no reason.
  editionServiceConfig = {
    Type = "oneshot";
    User = cfg.user;
    StateDirectory = "newsdesk";
    # Pages written into the SilverBullet space must be born group-writable or
    # the ACL mask locks the web UI out of them.
    UMask = "0002";
    WorkingDirectory = "/home/${cfg.user}";
    TimeoutStartSec = "45min";
    Environment = [
      "NEWSDESK_STATE=${cfg.stateDir}"
      "HOME=/home/${cfg.user}"
      # Mirrors digest.nix: the interactive claude env, so `claude -p` finds
      # its subscription OAuth credentials rather than hunting for an API key.
      "PATH=/etc/profiles/per-user/${cfg.user}/bin:/run/current-system/sw/bin:/usr/bin:/bin"
      # Marks this as a headless run so the agent's Stop reflection hook
      # no-ops — an edition must never be derailed into doing /retro work.
      "CLAUDE_AUTONOMOUS=1"
    ];
  };
in
{
  options.services.newsdesk = {
    enable = lib.mkEnableOption "the personal RSS news digest";

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/newsdesk";
      description = "State directory: the database, the tunable profile, the rendered pages.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "claude";
      description = ''
        User to run as. Must be the one whose `claude` CLI holds subscription
        OAuth credentials — the reader stage runs `claude -p` exactly the way
        the weekly digest does.
      '';
    };

    spaceDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/silverbullet/Areas/Newsdesk";
      description = ''
        SilverBullet folder for the edition pages. It is the second grading
        surface: `#good` / `#meh` on an item line works from the phone.
      '';
    };

    editionUrl = lib.mkOption {
      type = lib.types.str;
      default = "https://digest.rosemaryacres.com/news/";
      description = "Where the rendered edition is served; used in the notification.";
    };

    virtualHost = lib.mkOption {
      type = lib.types.str;
      default = "digest.rosemaryacres.com";
      description = ''
        Existing nginx vhost to hang /news/ off. Reusing the digest vhost means
        no new certificate and no new DNS record, and it inherits that host's
        source gate.
      '';
    };

    gradePort = lib.mkOption {
      type = lib.types.port;
      default = 8123;
      description = ''
        Loopback port for the grading endpoint. nginx proxies /news/g to it.
      '';
    };

    briefSchedule = lib.mkOption {
      type = lib.types.str;
      default = "Mon..Fri 07:00";
      description = "Weekday brief. Never earlier than the end of quiet hours.";
    };

    longreadSchedule = lib.mkOption {
      type = lib.types.str;
      default = "Sat 08:00";
      description = ''
        The weekend edition, where the rare substantial pieces are held for
        instead of competing with a release note on a Tuesday.
      '';
    };

    collectSchedule = lib.mkOption {
      type = lib.types.str;
      default = "*-*-* 01,07,13,19:15:00";
      description = ''
        Polling cadence, separate from the editions so the database is warm and
        conditional requests stay cheap. Four times a day across 86 feeds is
        polite; most responses should be 304.
      '';
    };

    tuneSchedule = lib.mkOption {
      type = lib.types.str;
      default = "Sun 09:00";
      description = "Weekly: apply bounded source tuning, propose term changes.";
    };

    notifyAfterHour = lib.mkOption {
      type = lib.types.int;
      default = 7;
      description = "Earliest hour a notification may be sent (quiet hours end).";
    };

    notifyBeforeHour = lib.mkOption {
      type = lib.types.int;
      default = 22;
      description = "Latest hour a notification may be sent (quiet hours begin).";
    };

    judge = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          Run the `claude -p` reader. Turning this off leaves collect + rank +
          publish, which is the calibration mode: the edition becomes the raw
          keyword ranking, which is what you want while tuning the profile.
        '';
      };

      timeout = lib.mkOption {
        type = lib.types.str;
        default = "20m";
        description = "Hard timeout for the reader. It failing is not fatal.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ newsdesk ];

    # NOTE: there are deliberately NO tmpfiles rules here any more. The units'
    # own StateDirectory=newsdesk creates /var/lib/newsdesk, and the app creates
    # web/ itself as it writes. The rules that used to be here named a group
    # that did not exist, failed silently, and left nginx pointing at a missing
    # directory — see gradeserver.py for the full postmortem.

    systemd.services.newsdesk-collect = {
      description = "Newsdesk — poll the feeds";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        StateDirectory = "newsdesk";
        TimeoutStartSec = "20min";
        Environment = [ "NEWSDESK_STATE=${cfg.stateDir}" "HOME=/home/${cfg.user}" ];
      };
      path = [ pkgs.coreutils ];
      script = ''
        set -uo pipefail
        ${nd} seed \
          --catalogue ${newsdesk}/share/newsdesk/sources.json \
          --profile ${newsdesk}/share/newsdesk/interests.json
        ${nd} sources --page ${cfg.spaceDir}/Sources.md || \
          echo "newsdesk: sources page sync failed — continuing"
        ${nd} collect
        ${nd} ingest
      '';
    };

    systemd.timers.newsdesk-collect = {
      description = "Newsdesk feed polling";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.collectSchedule;
        Persistent = true;
        RandomizedDelaySec = "8m";
      };
    };

    systemd.services.newsdesk-brief = {
      description = "Newsdesk — weekday brief";
      after = [ "network-online.target" "newsdesk-collect.service" ];
      wants = [ "network-online.target" ];
      serviceConfig = editionServiceConfig;
      # Monday picks up the release-radar lane; the rest of the week does not.
      script = editionScript ''"$([ "$(date +%u)" -eq 1 ] && echo brief-monday || echo brief)"'';
    };

    systemd.timers.newsdesk-brief = {
      description = "Newsdesk weekday brief";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.briefSchedule;
        Persistent = true;
        RandomizedDelaySec = "5m";
      };
    };

    systemd.services.newsdesk-longread = {
      description = "Newsdesk — weekend long-read";
      after = [ "network-online.target" "newsdesk-collect.service" ];
      wants = [ "network-online.target" ];
      serviceConfig = editionServiceConfig;
      script = editionScript "longread";
    };

    systemd.timers.newsdesk-longread = {
      description = "Newsdesk weekend long-read";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.longreadSchedule;
        Persistent = true;
        RandomizedDelaySec = "10m";
      };
    };

    systemd.services.newsdesk-tune = {
      description = "Newsdesk — ingest grades, tune sources, propose term changes";
      path = [ pkgs.coreutils ];
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        StateDirectory = "newsdesk";
        UMask = "0002";
        TimeoutStartSec = "10min";
        Environment = [ "NEWSDESK_STATE=${cfg.stateDir}" "HOME=/home/${cfg.user}" ];
      };
      script = ''
        set -uo pipefail
        ${nd} tune --space ${cfg.spaceDir}
      '';
    };

    systemd.timers.newsdesk-tune = {
      description = "Newsdesk weekly tuning";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.tuneSchedule;
        Persistent = true;
        RandomizedDelaySec = "15m";
      };
    };

    # The grading endpoint is a separate loopback service, NOT an nginx
    # access_log. That is the whole lesson of the 2026-08-20 outage: anything
    # nginx must OPEN at config-parse time can stop nginx from starting, and a
    # dead nginx is every vhost on the box, not just this one. `proxy_pass` to
    # 127.0.0.1 is something nginx can always do; if the grade service is down,
    # one link 502s and nothing else notices.
    systemd.services.newsdesk-grade = {
      description = "Newsdesk — grading endpoint (loopback)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];
      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        StateDirectory = "newsdesk";
        Restart = "on-failure";
        RestartSec = "5s";
        Environment = [ "NEWSDESK_STATE=${cfg.stateDir}" "HOME=/home/${cfg.user}" ];
        ExecStart = "${nd} serve --host 127.0.0.1 --port ${toString cfg.gradePort}";
        # It answers unauthenticated GETs, so give it the narrowest profile
        # that still lets it write one SQLite file.
        ProtectSystem = "strict";
        ReadWritePaths = [ cfg.stateDir ];
        ProtectHome = true;
        PrivateDevices = true;
        NoNewPrivileges = true;
        RestrictAddressFamilies = [ "AF_INET" "AF_UNIX" ];
        SystemCallFilter = [ "@system-service" ];
      };
    };

    services.nginx.virtualHosts.${cfg.virtualHost}.locations = {
      "= /news".extraConfig = "return 301 /news/;";
      "/news/" = {
        # `alias` is resolved per REQUEST, not at parse time, so a missing
        # directory here 404s instead of refusing to start. That asymmetry is
        # exactly why the log had to move and this can stay.
        alias = "${cfg.stateDir}/web/";
        extraConfig = "index index.html; autoindex on;";
      };
      # Exact matches win over the /news/ prefix above.
      "= /news/g".extraConfig = ''
        proxy_pass http://127.0.0.1:${toString cfg.gradePort};
        proxy_set_header Host $host;
      '';
      # Click-through: records that he opened the piece, then redirects to the
      # source. For a good read that click is the "I read it" signal.
      "= /news/r".extraConfig = ''
        proxy_pass http://127.0.0.1:${toString cfg.gradePort};
        proxy_set_header Host $host;
      '';
    };
  };
}
