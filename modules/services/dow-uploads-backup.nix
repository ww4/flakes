# Local mirror of the Drive On Wood uploads bucket (DO Spaces → gromit).
#
# WHY: forum.driveonwood.com stores 12 years of user photos in the DO Spaces
# bucket `dow`. Measured 2026-09-02: 46.3 GB across 194,504 objects —
# original/ 27.8 GB (53,680 objects, irreplaceable) and optimized/ 18.5 GB
# (140,817 thumbnails Discourse can regenerate).
#
# ⚠️ THAT BUCKET HAD EXACTLY ONE COPY. The nightly Discourse backup is
# DATABASE-ONLY (`backup_with_uploads = false`), so the .sql.gz files in
# `dow-discourse-backups` restore the forum with every image missing. And the
# bucket has NO versioning — `get-bucket-versioning` is permitted and returns
# no Status field — while a lifecycle rule DOES expire the tombstone/ prefix.
# So the one mechanism acting on that data makes deletions permanent, and
# nothing reverses them.
#
# This pulls a full copy onto the fusion pool, which puts it inside the
# existing restic critical-paths (local + B2) and therefore gives it version
# history for free. A separate direct-to-Backblaze sync is the third leg and
# is deliberately NOT here — it needs its own B2 key (see the note at the end).
#
# ── SAFETY: why this is not a naive `rclone sync` ──────────────────────────
#
# `rclone sync` DELETES at the destination to match the source. Pointed at a
# bucket we are protecting *because it might get wiped*, a plain sync on a
# timer is a mirror of the disaster, not a backup — the next run would faithfully
# reproduce the deletion locally. Three guards:
#
#   1. --max-delete 500 — a mass deletion upstream ABORTS the run instead of
#      replicating it. 500 is comfortably above normal churn (a busy week on
#      this forum is single digits) and far below a catastrophe.
#   2. --backup-dir — anything deleted or overwritten is MOVED into a dated
#      archive/ directory rather than destroyed, so even an under-the-threshold
#      deletion is recoverable from the local copy alone.
#   3. restic snapshots the whole tree nightly, so there is point-in-time
#      recovery behind both of the above.
#
# ── SAFETY: never sync into a dead mountpoint ─────────────────────────────
#
# If /mnt/fusion fails to mount, the mountpoint is a bare directory on the root
# SSD (449 GB, 85% full). Syncing 46 GB into it would fill the root filesystem
# and write a fake tree that restic would then happily back up. The preflight
# reads the directory rather than trusting `mountpoint` — a dropped mergerfs
# branch leaves the mount *listed* while reads fail (see the XFS zombie-mount
# incident, 2026-08-17).
{ config, pkgs, ... }:

let
  root = "/mnt/fusion/dow-uploads";
in
{
  systemd.services.dow-uploads-sync = {
    description = "Mirror the DOW uploads bucket (DO Spaces) to the fusion pool";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    unitConfig.RequiresMountsFor = "/mnt/fusion";

    serviceConfig = {
      Type = "oneshot";
      # Runs as claude because /run/secrets/digitalocean is claude-owned 0400.
      # umask 0022 so root's restic run can read the tree.
      User = "claude";
      Group = "media";
      UMask = "0022";
      RuntimeDirectory = "dow-uploads-sync";
      RuntimeDirectoryMode = "0700";
      # Generous: the FIRST run pulls 46 GB / 194k objects. Later runs diff.
      TimeoutStartSec = "6h";
    };

    script = ''
      set -euo pipefail
      # findutils is NOT optional here: the report and the archive prune both
      # use `find`, which lives in findutils rather than coreutils. Without it
      # the unit pulls 46 GB successfully and then FAILS under `set -e` on the
      # reporting step — a green build that dies at runtime. Caught by reading
      # the generated script's PATH, not by the build.
      PATH=${pkgs.rclone}/bin:${pkgs.coreutils}/bin:${pkgs.gnugrep}/bin:${pkgs.findutils}/bin:$PATH

      # ---- preflight: is the pool actually alive? (read it, don't trust mount) ----
      if [ "$(ls -A /mnt/fusion 2>/dev/null | head -5 | wc -l)" -lt 3 ]; then
        echo "FATAL: /mnt/fusion is empty or unreadable — refusing to sync."
        echo "       A bare mountpoint would put 46 GB on the root SSD and let"
        echo "       restic back up a fake tree. Fix the pool first."
        exit 1
      fi

      mkdir -p ${root}/current ${root}/archive

      # ---- rclone config, written to tmpfs, never to the nix store ----
      set -a; . /run/secrets/digitalocean; set +a
      CONF="$RUNTIME_DIRECTORY/rclone.conf"
      umask 077
      cat > "$CONF" <<EOF
      [dospaces]
      type = s3
      provider = DigitalOcean
      access_key_id = $DO_SPACES_KEY_ID
      secret_access_key = $DO_SPACES_SECRET
      endpoint = ''${DO_SPACES_ENDPOINT#https://}
      acl = private
      EOF
      umask 022

      STAMP="$(date +%F)"
      echo "=== dow-uploads sync $STAMP ==="

      rclone sync dospaces:dow ${root}/current \
        --config "$CONF" \
        --backup-dir ${root}/archive/"$STAMP" \
        --max-delete 500 \
        --fast-list \
        --transfers 8 \
        --checkers 16 \
        --retries 3 \
        --stats 5m \
        --stats-one-line \
        --log-level INFO

      # ---- report what we hold, so a silent shrink is visible in the journal ----
      n=$(find ${root}/current -type f | wc -l)
      sz=$(du -sh ${root}/current | cut -f1)
      echo "RESULT: ${root}/current holds $n files, $sz"
      if [ "$n" -lt 150000 ]; then
        echo "WARNING: object count is well below the 194,504 seen on 2026-09-02."
        echo "         Investigate before trusting this copy."
      fi

      # ---- prune the deleted/overwritten archive after 90 days ----
      find ${root}/archive -mindepth 1 -maxdepth 1 -type d -mtime +90 \
        -exec rm -rf {} + 2>/dev/null || true
    '';
  };

  systemd.timers.dow-uploads-sync = {
    description = "Daily mirror of the DOW uploads bucket";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      # 04:15 — after both restic runs (02:30 local, 03:00 B2) so a day's sync
      # is captured by the FOLLOWING night's snapshot rather than racing it.
      OnCalendar = "04:15";
      Persistent = true;
      RandomizedDelaySec = "10m";
    };
  };

  # THIRD LEG, NOT YET WIRED: a direct DO Spaces → Backblaze B2 sync, so one
  # copy exists that depends on neither DigitalOcean nor this house. It needs
  # its own B2 application key — the existing `restic-b2` secret is root-only
  # and probably scoped to the `gromit-restic` bucket, so it cannot write a new
  # one. Template is at ~/secrets-inbox/backblaze-dow.env; wire it once Chris
  # fills it in.
}
