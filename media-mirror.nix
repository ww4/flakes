# media-mirror — guarded weekly mirror of the media pool to the backup pool.
#
# See media-mirror.sh for the tool itself. This module packages it, schedules
# the weekly sync + graveyard prune, and wires the drive preflight into the
# local restic backup (whose repo also lives on the backup pool).
{ config, lib, pkgs, ... }:

let
  gromit-notify = import ./notify-pkg.nix { inherit pkgs; };

  media-mirror = pkgs.writeShellApplication {
    name = "media-mirror";
    runtimeInputs = with pkgs; [
      rsync
      coreutils
      findutils
      util-linux # mountpoint
      gnugrep
      gnused
      gromit-notify
    ];
    # SC2001: the sed calls operate on streams, not single variables, so the
    # ${var//search/replace} suggestion does not apply.
    excludeShellChecks = [ "SC2001" ];
    text = builtins.readFile ./media-mirror.sh;
  };
in
{
  environment.systemPackages = [ media-mirror ];

  # State + log directory. World-readable so the deletion review list and
  # `media-mirror status` work without sudo (contents are just file paths).
  systemd.tmpfiles.rules = [
    "d /var/lib/media-mirror      0755 root root -"
    "d /var/lib/media-mirror/logs 0755 root root -"
  ];

  # Weekly additive sync (queues any deletions for review — never deletes).
  systemd.services.media-mirror-sync = {
    description = "Mirror /mnt/fusion to the backup pool (additive; queues deletions)";
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${media-mirror}/bin/media-mirror sync";
    };
  };
  systemd.timers.media-mirror-sync = {
    description = "Weekly media-mirror sync";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "Sun 04:00";
      Persistent = true;
    };
  };

  # Daily graveyard prune (removes snapshots past the retention window).
  systemd.services.media-mirror-prune-graveyard = {
    description = "Prune media-mirror graveyard snapshots past retention";
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${media-mirror}/bin/media-mirror prune-graveyard";
    };
  };
  systemd.timers.media-mirror-prune-graveyard = {
    description = "Daily media-mirror graveyard prune";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "daily";
      Persistent = true;
    };
  };

  # The local restic repo lives on the backup pool, so gate that backup on the
  # same drive-presence check (merges with the definition in backup.nix).
  services.restic.backups.critical-local.backupPrepareCommand =
    "${media-mirror}/bin/media-mirror preflight backup";

  # Alert if either restic backup fails.
  systemd.services.restic-backups-critical-local.onFailure =
    [ "notify-failure@%N.service" ];
  systemd.services.restic-backups-critical-b2.onFailure =
    [ "notify-failure@%N.service" ];
}
