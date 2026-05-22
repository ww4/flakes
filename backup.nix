# Backup configuration — restic to a local repo and offsite Backblaze B2.
#
# Scope: critical-tier data only — small, irreplaceable application state.
# Media lives on /mnt/fusion and is handled separately (a weekly rsync mirror
# to the backup pool — not yet configured).
#
# Secrets are kept out of git, in root-only files on the host:
#   /var/lib/restic/password  - repo encryption passphrase (shared by both repos)
#   /var/lib/restic/b2-env    - B2_ACCOUNT_ID / B2_ACCOUNT_KEY for the B2 repo
{ config, lib, pkgs, ... }:

let
  # Irreplaceable application state. Postgres is captured via the nightly
  # pg_dump SQL files in /var/backup/postgresql, not the live data directory.
  criticalPaths = [
    # Application state.
    "/var/lib/nextcloud"
    "/var/lib/audiobookshelf"
    "/var/lib/tandoor-recipes"
    "/var/lib/jellyfin"
    "/var/backup/postgresql"

    # Nextcloud external storage (the /Bitcoin and /Fusion mounts). Live data
    # not covered by /var/lib/nextcloud. /mnt/fusion/nextcloud is ~199 GB.
    "/mnt/fusion/Bitcoin"
    "/mnt/fusion/nextcloud"

    # Home folder — irreplaceable personal data only. Caches, VM images, and
    # downloaded media are deliberately left out; see criticalExclude.
    "/home/chris/projects"          # source repos + relocated personal files
    "/home/chris/Pictures"
    "/home/chris/Documents"
    "/home/chris/Desktop"
    "/home/chris/Videos"
    "/home/chris/Music/Recordings"  # personal recordings (not the whole Music dir)
    "/home/chris/Downloads"         # catch-all; the big media item is excluded
    "/home/chris/.bitcoin/wallets"  # wallet only — not the blockchain
  ];

  # Regenerable junk — transcode scratch, caches, logs — and large downloaded
  # media that belongs on the media tier, not the offsite critical backup.
  criticalExclude = [
    "/var/lib/jellyfin/transcodes"
    "/var/lib/jellyfin/cache"
    "/var/lib/jellyfin/log"
    "/home/chris/Downloads/timberframing" # 59 GB downloaded video course
  ];

  # Snapshot retention: 7 daily, 4 weekly, 6 monthly.
  pruneOpts = [
    "--keep-daily 7"
    "--keep-weekly 4"
    "--keep-monthly 6"
  ];

  # Structural integrity check after each run (uses the local cache).
  checkOpts = [ "--with-cache" ];
in
{
  # restic CLI available for manual restore / inspection.
  environment.systemPackages = [ pkgs.restic ];

  services.restic.backups = {
    # Local repository on the backup drive pool — fast restores, survives an
    # nvme failure. NOTE: when the media rsync mirror is configured it MUST
    # exclude /mnt/backup/all/restic so a --delete sync cannot wipe this repo.
    critical-local = {
      repository = "/mnt/backup/all/restic";
      passwordFile = "/var/lib/restic/password";
      paths = criticalPaths;
      exclude = criticalExclude;
      initialize = true;
      inherit pruneOpts checkOpts;
      timerConfig = {
        OnCalendar = "02:30";
        Persistent = true;
      };
    };

    # Offsite repository on Backblaze B2 — survives fire / theft / ransomware.
    critical-b2 = {
      repository = "b2:gromit-restic";
      passwordFile = "/var/lib/restic/password";
      environmentFile = "/var/lib/restic/b2-env";
      paths = criticalPaths;
      exclude = criticalExclude;
      initialize = true;
      inherit pruneOpts checkOpts;
      timerConfig = {
        OnCalendar = "03:00";
        Persistent = true;
      };
    };
  };

  # Don't let restic write into a bare mountpoint if the backup pool failed
  # to mount.
  systemd.services.restic-backups-critical-local.unitConfig.RequiresMountsFor =
    "/mnt/backup/all";
}
