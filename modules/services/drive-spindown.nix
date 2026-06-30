# Idle spin-down for the backup-pool USB drives (the 4 WD Elements at
# /mnt/backup/D1-4). These drives are touched only by sporadic batch jobs —
# the daily overnight prune/gyb (~00:00-02:00) and the weekly Sun media-mirror +
# bub-mirror syncs. The rest of the time they have zero block I/O, so they
# should sleep. Letting them spin down drops their temperature (and wear) hard.
#
# Why this is needed (and wasn't before): these drives have a firmware idle
# timer and used to spin down on their own between syncs. drive-temps.nix polls
# smartctl every 5 min, and a SMART read is an ATA command that RESETS the
# firmware idle timer — so once a drive is awake (a reboot, a backup job) it can
# never reach its own spin-down timeout and just spins 24/7, heating up. The
# exporter uses `-n standby` so it never *wakes* a sleeping drive, but it does
# hold an already-spinning one awake.
#
# hd-idle fixes that: it decides idleness from /proc/diskstats (real block I/O),
# which SMART reads do NOT touch — so it spins the drive down on a true I/O-idle
# timeout regardless of the 5-min poll. Once hd-idle parks a drive, the
# exporter's `-n standby` check sees standby and leaves it asleep. The two
# cooperate.
#
# Safety: the default idle time is set to 0 (never) so ONLY the four named
# backup drives are ever spun down — the active fusion pool, the SSD and the
# boot NVMe keep spinning. Worst-case failure mode (an ID stops matching, or the
# USB bridge ignores the spin-down command) is simply "a backup drive isn't
# parked" — never "an active drive got parked".
{ config, lib, pkgs, ... }:

let
  # The 4 backup-pool drives, by stable serial-bearing by-id. Device letters
  # (sdh..sdk) shuffle across reboots and USB re-plugs on this box, so we never
  # hardcode them — we resolve these symlinks to the current /dev node at start.
  backupDriveIds = [
    "usb-WD_Elements_25A3_575832324439303132343737-0:0"
    "usb-WD_Elements_25A3_57583532444330364C304B32-0:0"
    "usb-WD_Elements_25A3_575835314438394844563632-0:0"
    "usb-WD_Elements_25A3_575832324443303254584B36-0:0"
  ];
  # 10 min of no block I/O -> spin down. Mirrors the drives' original firmware
  # idle behaviour (the one PR #50's polling was defeating). Short enough to cool
  # quickly after a job, long enough not to bounce mid-sync.
  idleSecs = 600;
in
{
  systemd.services.drive-spindown = {
    description = "Spin down the idle backup-pool USB drives (hd-idle, by-id, default-off)";
    wantedBy = [ "multi-user.target" ];
    after = [ "local-fs.target" ];
    path = [ pkgs.coreutils ];
    serviceConfig = {
      Type = "simple";
      Restart = "always";
      RestartSec = 10;
    };
    # Build the arg list at start: default idle 0 (protect every other disk),
    # then a per-disk override for each backup drive that is currently present.
    # `-c scsi` issues the SCSI STOP UNIT command, which USB-SATA bridges honour
    # (the default ATA spin-down is often swallowed by the bridge). `-d` keeps
    # hd-idle in the foreground and logs spin-up/down events to the journal.
    script = ''
      # NOTE: drive-temps.nix activity-gates these same drives (it only SMART-
      # reads them while they're doing block I/O), so its 5-min poll can no
      # longer wake a parked drive out from under hd-idle. (hd-idle tracks only
      # block I/O, so a SMART-induced wake would otherwise leave it wrongly
      # believing the drive is still parked and it would never re-issue STOP.)
      args=( -i 0 )
      for id in ${lib.concatStringsSep " " backupDriveIds}; do
        link="/dev/disk/by-id/$id"
        if [ -e "$link" ]; then
          dev="$(basename "$(readlink -f "$link")")"
          echo "drive-spindown: $id -> $dev (idle ${toString idleSecs}s)"
          args+=( -a "$dev" -i ${toString idleSecs} -c scsi )
        else
          echo "drive-spindown: $id not present, skipping"
        fi
      done
      exec ${pkgs.hd-idle}/bin/hd-idle -d "''${args[@]}"
    '';
  };
}
