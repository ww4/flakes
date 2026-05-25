# SnapRAID — file-level parity protection for the mergerfs media pool.
#
# How it composes with mergerfs:
#
#   sdf  /mnt/primary/D1   xfs  ┐
#   sdg  /mnt/primary/D2   xfs  ├─→ mergerfs → /mnt/fusion   (apps see this)
#   sdk  /mnt/primary/D3   xfs  ┘                              (added post-NTFS-triage)
#   NEW  /mnt/parity1      xfs  ────→ snapraid.parity         (NOT in mergerfs)
#
# SnapRAID reads the data disks directly (NOT through /mnt/fusion) and writes
# XOR parity into a single file on /mnt/parity1. Failure of any one data disk
# is recoverable file-by-file via `snapraid fix` once a replacement disk is
# in place — surviving disks stay readable through the rebuild.
#
# Sync runs nightly at 04:00 (after media-mirror's 04:00 Sunday weekly job is
# offset because sync is a no-op when nothing changed). Scrub runs Mondays at
# 05:00 covering 12% of data each pass with a 10-day minimum age, giving full
# array coverage every ~8 weeks.
#
# ACTIVATION — set enable=true once:
#   1. /mnt/parity1 is mounted with the new parity drive (≥ size of the
#      largest data disk; currently sdg at 9.1 TB → 10 TB parity disk is
#      the minimum).
#   2. Each data disk has the snapraid.content file path's parent dir
#      writable, and /var/lib/snapraid exists.
#   3. First run is `snapraid sync` manually — it builds parity from
#      scratch, takes hours on a TB-scale array. Only then enable the timer.
#
# Adding data disks later:
#   - New disk must be ≤ the parity disk size.
#   - Add an entry to dataDisks, add its content file to contentFiles,
#     then `snapraid sync`.
#
# Upgrading parity to a larger disk later:
#   - cp -p /mnt/parity1/snapraid.parity → new disk, cmp to verify,
#     remount new disk at /mnt/parity1, `snapraid scrub` to confirm.
#   - No parity recalculation — the file is the same bytes.
{ config, lib, pkgs, ... }:

{
  services.snapraid = {
    enable = false;  # flip to true once parity drive is in place

    # Data disks: the underlying filesystems that mergerfs unifies as
    # /mnt/fusion. Add D3-onward as you reformat sdk and reclaim the
    # decom Hitachis (3× HUA723030, still reliable enterprise drives).
    dataDisks = {
      d1 = "/mnt/primary/D1";  # sdf 7.3 TB Seagate Expansion
      d2 = "/mnt/primary/D2";  # sdg 9.1 TB WD WD100EMAZ
      d3 = "/mnt/primary/D3";  # sdb 2.7 TB Hitachi HUA723030 (ex-decom, 2026-05-25)
      d4 = "/mnt/primary/D4";  # sdd 2.7 TB Hitachi HUA723030 (ex-decom)
      d5 = "/mnt/primary/D5";  # sde 2.7 TB Hitachi HUA723030 (ex-decom)
      d6 = "/mnt/primary/D6";  # sdk 3.6 TB WD My Book (ex-NTFS-Backup, 2026-05-25)
    };

    # Single 10 TB parity disk to start. Add a second entry for 2-disk
    # fault tolerance once the array grows past ~5 data disks.
    parityFiles = [
      "/mnt/parity1/snapraid.parity"
    ];

    # Content (database) files. SnapRAID requires copies ≥ number of parity
    # disks. One on persistent local storage + one per data disk gives
    # full redundancy against any single drive loss.
    contentFiles = [
      "/var/lib/snapraid/snapraid.content"
      "/mnt/primary/D1/snapraid.content"
      "/mnt/primary/D2/snapraid.content"
      "/mnt/primary/D3/snapraid.content"
      "/mnt/primary/D4/snapraid.content"
      "/mnt/primary/D5/snapraid.content"
      "/mnt/primary/D6/snapraid.content"
    ];

    exclude = [
      "*.unrecoverable"
      "/tmp/"
      "lost+found/"
      ".pool-member"                  # mergerfs drive-presence sentinel
      "/arr/downloads/incomplete/"    # transient *arr download chunks
      ".snapraid.content*"            # the content files themselves
    ];

    sync.interval = "*-*-* 04:00:00";   # daily at 04:00

    scrub = {
      interval = "Mon *-*-* 05:00:00";  # weekly Monday at 05:00
      plan = 12;                        # scrub 12% of array per run
      olderThan = 10;                   # skip blocks scrubbed in last 10 days
    };

    touchBeforeSync = true;             # normalize sub-second mtimes first
  };

  # Persistent local content-file directory.
  systemd.tmpfiles.rules = [
    "d /var/lib/snapraid 0755 root root - -"
  ];

  # Notify on sync/scrub failure via the existing notify-failure@ template.
  systemd.services.snapraid-sync.onFailure =
    lib.mkIf config.services.snapraid.enable [ "notify-failure@%N.service" ];
  systemd.services.snapraid-scrub.onFailure =
    lib.mkIf config.services.snapraid.enable [ "notify-failure@%N.service" ];
}
