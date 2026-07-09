# PIM — plain-text calendar hub for the scheduling assistant (2026-07-09,
# docs repo: docs/scheduling-assistant-research.md).
#
# Architecture: a vdir (one .ics file per event — git-diffable plain text,
# usable with zero daemons) at /var/lib/pim/calendars/, synced by vdirsyncer:
#   - Nextcloud (cloud.rosemaryacres.com) — TWO-WAY. The working calendar:
#     Chris arranges things in the Nextcloud UI / phone; the agent reads and
#     writes events through the vdir (khal or icalendar).
#   - Google Calendar — READ-ONLY mirror (things periodically land there;
#     Chris decided read-only is fine). partial_sync=revert undoes any
#     accidental local edit to the Google mirror. NOTE: Google's CalDAV
#     rejects VTODO by protocol — todos never sync to Google. Todos live in
#     the SilverBullet space (markdown), not in any CalDAV store.
#
# INERT UNTIL SECRETS + DISCOVERY EXIST — by design, so this module deploys
# safely before the manual one-time steps are done, without unit failures
# (sentinel watches failed units):
#   pim-sync-nextcloud: needs /run/secrets/nextcloud-caldav (a Nextcloud app
#     password; Chris: Settings -> Security -> Devices & sessions -> new app
#     password, then `sops secrets/nextcloud-caldav.yaml`; the agent then
#     appends the sops.secrets declaration) AND a one-time
#     `pim-vdirsyncer discover nextcloud` (agent runs it; answers the
#     create-collection prompts).
#   pim-sync-google: additionally needs a Google Cloud OAuth "Desktop app"
#     client (enable the **CalDAV API**, not the Calendar API; publish the
#     consent screen to **Production** or refresh tokens die every 7 days —
#     the gyb lesson) and a one-time browser auth via SSH port-forward which
#     writes /var/lib/pim/google-token.json.
#
# Everything runs as the claude user — the agent is the primary consumer, and
# vdirsyncer's OAuth token file + status dir are mutable agent-side state.
{ config, lib, pkgs, ... }:

let
  pimDir = "/var/lib/pim";
  ncUser = "chris"; # Nextcloud login that owns the working calendar (confirmed 2026-07-09)
  ncSecret = "/run/secrets/nextcloud-caldav"; # declared below (sops, owner=claude)
  gClientId = "/run/secrets/google-oauth-client-id";
  gClientSecret = "/run/secrets/google-oauth-client-secret";

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

    # ---- Google: read-only mirror (events only; VTODO impossible) ----
    [pair google]
    a = "local_google"
    b = "remote_google"
    collections = ["from b"]
    partial_sync = "revert"

    [storage local_google]
    type = "filesystem"
    path = "${pimDir}/calendars/google/"
    fileext = ".ics"

    [storage remote_google]
    type = "google_calendar"
    token_file = "${pimDir}/google-token.json"
    client_id.fetch = ["command", "cat", "${gClientId}"]
    client_secret.fetch = ["command", "cat", "${gClientSecret}"]
    read_only = true
  '';

  khalConf = pkgs.writeText "khal.conf" ''
    [calendars]

    [[nextcloud]]
    path = ${pimDir}/calendars/nextcloud/*
    type = discover

    [[google]]
    path = ${pimDir}/calendars/google/*
    type = discover
    readonly = True

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
      Group = "claude";
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
    "d ${pimDir}                     0750 claude claude - -"
    "d ${pimDir}/calendars           0750 claude claude - -"
    "d ${pimDir}/calendars/nextcloud 0750 claude claude - -"
    "d ${pimDir}/calendars/google    0750 claude claude - -"
    "d ${pimDir}/status              0750 claude claude - -"
  ];

  systemd.services.pim-sync-nextcloud = mkSyncUnit "nextcloud" [ ncSecret ];
  systemd.services.pim-sync-google = mkSyncUnit "google" [
    gClientId
    gClientSecret
    "${pimDir}/google-token.json"
  ];

  systemd.timers.pim-sync-nextcloud = {
    description = "vdirsyncer sync (nextcloud), every 10 min";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "*:0/10";
      RandomizedDelaySec = "60";
      Persistent = false; # missed syncs are caught by the next tick
    };
  };
  systemd.timers.pim-sync-google = {
    description = "vdirsyncer sync (google, read-only), every 30 min";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "*:7/30";
      RandomizedDelaySec = "60";
      Persistent = false;
    };
  };
}
