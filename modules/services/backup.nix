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
    "/var/lib/grafana"                 # dashboards + sqlite state DB
    "/var/lib/homepage"                # secrets.env — Homepage widget API keys (root 0600)
    "/var/lib/forgejo"                 # sqlite DB + bare repos + LFS objects + custom config
    "/var/lib/bitwarden_rs"            # Vaultwarden SQLite vault (NixOS module name is legacy)
    "/var/lib/paperless"               # OCR'd docs + sqlite index
    "/var/lib/uptime-kuma"             # monitor history + sqlite
    "/var/backup/postgresql"

    # Nextcloud external storage (the /Bitcoin and /Fusion mounts). Live data
    # not covered by /var/lib/nextcloud. /mnt/fusion/nextcloud is ~199 GB.
    "/mnt/fusion/Bitcoin"
    "/mnt/fusion/nextcloud"
    "/mnt/fusion/immich"               # Immich photo library

    # Home folder — irreplaceable personal data only. Caches, VM images, and
    # downloaded media are deliberately left out; see criticalExclude.
    "/home/chris/projects"             # source repos + relocated personal files
    "/home/chris/Pictures"
    "/home/chris/Documents"
    "/home/chris/Desktop"
    "/home/chris/Videos"
    "/home/chris/July 2025"            # homeschool materials
    "/home/chris/Music/Recordings"     # personal recordings (not the whole Music dir)
    "/home/chris/Downloads"            # catch-all; the big media item is excluded
    "/home/chris/gyb"                  # GYB Gmail archive (both accounts)
    # Wallets, keys and credentials — small, irreplaceable.
    "/home/chris/.sparrow"             # Bitcoin wallets (IRA Funds, Multisig)
    "/home/chris/.local/share/albyhub" # Alby Hub Lightning data
    "/home/chris/.ssh"                 # SSH keys
    "/home/chris/.gnupg"               # GPG keys
    "/home/chris/.config/Element"      # Matrix end-to-end encryption keys
  ];

  # Regenerable junk — transcode scratch, caches, logs — and large downloaded
  # media that belongs on the media tier, not the offsite critical backup.
  criticalExclude = [
    "/var/lib/jellyfin/transcodes"
    "/var/lib/jellyfin/cache"
    "/var/lib/jellyfin/log"
    "/var/lib/forgejo/log"             # forgejo logs — regenerable
    "/var/lib/forgejo/dump"            # manual admin dumps; would recursively grow the backup
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

  # Secrets via sops (migrated 2026-06-16). restic backups run as root, so the
  # passwordFile/environmentFile are read as root — default root:0400 is fine.
  sops.secrets."restic-password" = {
    sopsFile = ../../secrets/restic-password.yaml;
    key = "restic-password";
  };
  sops.secrets."restic-b2" = {
    sopsFile = ../../secrets/restic-b2.yaml;
    key = "restic-b2";
  };

  services.restic.backups = {
    # Local repository on the backup drive pool — fast restores, survives an
    # nvme failure. NOTE: when the media rsync mirror is configured it MUST
    # exclude /mnt/backup/all/restic so a --delete sync cannot wipe this repo.
    critical-local = {
      repository = "/mnt/backup/all/restic";
      passwordFile = config.sops.secrets."restic-password".path;
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
      passwordFile = config.sops.secrets."restic-password".path;
      environmentFile = config.sops.secrets."restic-b2".path;
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

  # ----- bub tier-1 push target (Flow 2 in BACKUP-ARCHITECTURE.md) -----
  #
  # bub uses SFTP to push its own restic snapshots into the same repo at
  # /mnt/backup/all/restic, tagged --host=bub --tag=bub-tier1. The repo is
  # owned root:restic, mode 2770, with default ACLs so new files inherit
  # group access. mergerfs + FUSE default_permissions on kernel 6.x doesn't
  # honor supplementary groups, so the SFTP-side user must have restic as
  # its PRIMARY group — hence a dedicated restic-push system user instead
  # of just adding chris to the restic group.

  users.groups.restic = {};

  users.users.restic-push = {
    isSystemUser = true;
    group = "restic";
    home = "/var/lib/restic-push";
    createHome = true;
    shell = pkgs.bashInteractive;             # nologin breaks SFTP via PAM
    openssh.authorizedKeys.keys = [
      # bub's /etc/bub-restic/ssh-key.pub — locked to SFTP only, no
      # forwarding, no shell, regardless of what the client requests.
      ''restrict,command="internal-sftp" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIO/llcItLigl8cl2ukc2vF/7v4TiiuTBl68Bb9gjscbP bub-restic@bub''
    ];
  };

  # chris gets a supplementary 'restic' group membership for manual ops
  # (e.g. `sg restic -c "restic snapshots"`). Supplementary alone won't
  # work over FUSE (see above), but it's fine for direct shell access.
  users.users.chris.extraGroups = [ "restic" ];

  # Own the repo perms idempotently on every activation. Runs after the
  # backup pool is mounted so the chgrp/chmod target actually exists.
  systemd.services.restic-repo-perms = {
    description = "Apply group + ACL perms on the restic repo for bub push";
    wantedBy = [ "multi-user.target" ];
    after = [ "mnt-backup-all.mount" ];
    requires = [ "mnt-backup-all.mount" ];
    unitConfig.RequiresMountsFor = "/mnt/backup/all";
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    path = with pkgs; [ coreutils acl findutils ];
    script = ''
      repo=/mnt/backup/all/restic
      [ -d "$repo" ] || exit 0
      chgrp -R restic "$repo"
      find "$repo" -type d -exec chmod 2770 {} +
      find "$repo" -type f -exec chmod 0660 {} +
      # config is conventionally read-only after init — keep group read.
      [ -f "$repo/config" ] && chmod 0640 "$repo/config" || true
      # Default ACL so any new entry (by any process) inherits group rwX.
      setfacl -R -d -m g:restic:rwX "$repo"
    '';
  };
}
