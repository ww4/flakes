# PIM — plain-text calendar hub for the scheduling assistant (2026-07-09,
# docs repo: docs/scheduling-assistant-research.md).
#
# Architecture: a vdir (one .ics file per event — git-diffable plain text,
# usable with zero daemons) at /var/lib/pim/calendars/, synced two-way by
# vdirsyncer with Nextcloud (cloud.rosemaryacres.com) — the working calendar.
# Chris arranges things in the Nextcloud UI / on his phone (DAVx⁵); the agent
# reads and writes events through the vdir (khal or icalendar).
#
# NO GOOGLE LEG (removed 2026-07-09, Chris: "we actually don't need the Google
# thing, I have DAVx⁵ on my phone doing the syncing already"). Dropping it also
# fixed a real papercut: the Google pair's `password.fetch` cat'd a secret file
# that never existed, so a bare `pim-vdirsyncer sync` aborted the WHOLE run and
# you had to say `sync nextcloud` explicitly. Bare `sync` now just works.
#
# Todos deliberately do NOT live here — they're markdown in the SilverBullet
# space. This module is calendar-only.
#
# INERT UNTIL SECRET + DISCOVERY EXIST — by design, so the module deploys
# safely before the one-time manual steps, without unit failures (sentinel
# watches failed units): pim-sync-nextcloud needs /run/secrets/nextcloud-caldav
# (a Nextcloud app password, sops) AND a one-time `pim-vdirsyncer discover
# nextcloud`. Both are done as of 2026-07-09; the leg is live.
#
# Everything runs as the claude user — the agent is the primary consumer, and
# vdirsyncer's status dir is mutable agent-side state.
{ config, lib, pkgs, ... }:

let
  pimDir = "/var/lib/pim";
  ncUser = "chris"; # Nextcloud login that owns the working calendar (confirmed 2026-07-09)
  ncSecret = "/run/secrets/nextcloud-caldav"; # declared below (sops, owner=claude)

  vdirsyncerConf = pkgs.writeText "vdirsyncer.conf" ''
    [general]
    status_path = "${pimDir}/status/"

    # ---- Nextcloud: the working calendar (two-way) ----
    [pair nextcloud]
    a = "local_nextcloud"
    b = "remote_nextcloud"
    collections = ["from b"]
    conflict_resolution = "b wins"
    metadata = ["displayname", "color"]

    [storage local_nextcloud]
    type = "filesystem"
    path = "${pimDir}/calendars/nextcloud/"
    fileext = ".ics"

    [storage remote_nextcloud]
    type = "caldav"
    url = "https://cloud.rosemaryacres.com/remote.php/dav/"
    username = "${ncUser}"
    password.fetch = ["command", "cat", "${ncSecret}"]
  '';

  khalConf = pkgs.writeText "khal.conf" ''
    [calendars]

    [[nextcloud]]
    path = ${pimDir}/calendars/nextcloud/*
    type = discover

    [locale]
    timeformat = %H:%M
    dateformat = %Y-%m-%d
    longdateformat = %Y-%m-%d
    datetimeformat = %Y-%m-%d %H:%M
    longdatetimeformat = %Y-%m-%d %H:%M
  '';

  # Convenience wrappers so humans and prompts don't need -c flags.
  pimVdirsyncer = pkgs.writeShellScriptBin "pim-vdirsyncer" ''
    exec ${pkgs.vdirsyncer}/bin/vdirsyncer -c ${vdirsyncerConf} "$@"
  '';
  agenda = pkgs.writeShellScriptBin "agenda" ''
    # agenda            -> next 7 days
    # agenda list today 14d --json ... -> raw khal passthrough
    if [ $# -eq 0 ]; then
      exec ${pkgs.khal}/bin/khal -c ${khalConf} list now 7d
    fi
    exec ${pkgs.khal}/bin/khal -c ${khalConf} "$@"
  '';

  mkSyncUnit = pair: extraConditions: {
    description = "vdirsyncer sync (${pair})";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    # Inert (Condition* skip, NOT failure) until the manual setup steps exist.
    unitConfig.ConditionPathExists = extraConditions ++ [
      # discovery has been run for this pair
      "${pimDir}/status/${pair}.collections"
    ];
    serviceConfig = {
      Type = "oneshot";
      User = "claude";
      Group = "users";   # claude has no own group — primary group is users
      ExecStart = "${pimVdirsyncer}/bin/pim-vdirsyncer sync ${pair}";
      TimeoutStartSec = "10min";
    };
  };
in
{
  # The Nextcloud app password (value added by Chris 2026-07-09). The agent
  # runs vdirsyncer, so it owns the decrypted path — same split as the
  # Cloudflare token.
  sops.secrets."nextcloud-caldav" = {
    sopsFile = ../../secrets/nextcloud-caldav.yaml;
    key = "nextcloud-caldav";
    owner = "claude";
    mode = "0400";
  };

  environment.systemPackages = [ pkgs.vdirsyncer pkgs.khal pimVdirsyncer agenda ];

  systemd.tmpfiles.rules = [
    "d ${pimDir}                     0750 claude users  - -"
    "d ${pimDir}/calendars           0750 claude users  - -"
    "d ${pimDir}/calendars/nextcloud 0750 claude users  - -"
    "d ${pimDir}/status              0750 claude users  - -"
    # retire the Google leg's leftovers (2026-07-09)
    "R ${pimDir}/calendars/google    - - - - -"
    "r ${pimDir}/google-token.json   - - - - -"
  ];

  systemd.services.pim-sync-nextcloud = mkSyncUnit "nextcloud" [ ncSecret ];

  systemd.timers.pim-sync-nextcloud = {
    description = "vdirsyncer sync (nextcloud), every 10 min";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "*:0/10";
      RandomizedDelaySec = "60";
      Persistent = false; # missed syncs are caught by the next tick
    };
  };
}
