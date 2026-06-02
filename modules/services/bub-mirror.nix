# bub-mirror — pull bub's /mnt/fusion to /mnt/backup/all/rick-offsite/
# with hardlink-dedup against the existing local media-mirror.
#
# Companion to media-mirror.nix. media-mirror dumps gromit's /mnt/fusion
# directly into /mnt/backup/all (no subdir), so /mnt/backup/all/Movies/X
# is gromit's mirror of /mnt/fusion/Movies/X. bub-mirror pulls Bub's
# /mnt/fusion into a sibling rick-offsite/ subdir using rsync
# --link-dest=/mnt/backup/all, so any bub file that has a same-path
# same-size same-mtime peer in gromit's mirror becomes a hardlink instead
# of a duplicate copy.
#
# Combined storage ≈ size(media-mirror) + size(Rick-unique content) + ε.
# Hardlinks work because /mnt/backup/all uses mergerfs category.create=epmfs
# (existing-path most-free-space) — both copies of a shared file land on
# the same underlying disk where hardlinks are supported.
#
# Connection: bub is reached on Tailscale at 100.112.10.93:4089 with
# user chris. root@gromit needs an SSH key authorized on bub:chris's
# authorized_keys (generate once with `ssh-keygen` as root, copy via
# the two-hop path; documented in BACKUP-ARCHITECTURE.md).
#
# Run weekly, Sundays 06:00 — two hours after media-mirror's 04:00, so
# the local mirror is settled and visible as link-dest targets.
{ config, lib, pkgs, ... }:

let
  gromit-notify = import ./notify-pkg.nix { inherit pkgs; };

  # Phase 1 tool: hardlink Rick's files that gromit already has, CO-LOCATED on
  # the master's branch (direct on-branch ln — no mergerfs link(), so EXDEV is
  # impossible). Metadata-only, never copies. See bub-link-pass.sh.
  bubLinkPass = pkgs.writeShellApplication {
    name = "bub-link-pass";
    runtimeInputs = with pkgs; [ openssh coreutils util-linux ];
    excludeShellChecks = [ "SC2010" "SC2012" ];
    # No errexit: `find` over bub returns non-zero on permission-denied dirs
    # (expected — some of Rick's files aren't readable) yet still lists every
    # readable file. The script handles failures explicitly (|| / if), so a
    # non-zero find must NOT abort it. Matches how it was developed/tested.
    bashOptions = [ "nounset" "pipefail" ];
    text = builtins.readFile ./bub-link-pass.sh;
  };

  bubMirror = pkgs.writeShellApplication {
    name = "bub-mirror";
    runtimeInputs = with pkgs; [
      rsync
      openssh
      coreutils
      util-linux
      gnugrep
      gromit-notify
      bubLinkPass
    ];
    excludeShellChecks = [ "SC2001" ];
    text = ''
      # bub-mirror — pull bub:/mnt/fusion → /mnt/backup/all/rick-offsite/
      # with --link-dest hardlink dedup against /mnt/backup/all media-mirror.
      set -o errexit -o nounset -o pipefail -o errtrace

      # Serialize against media-mirror and any manual pool job: the backup
      # drives share one self-powered USB hub that trips over-current if
      # several are driven hard at once. This lock (shared with media-mirror's
      # serialize_pool) ensures only one heavy job hits the drives at a time;
      # wait up to 6h for an in-flight sync rather than piling on.
      exec 9>/run/lock/backup-pool.lock
      flock -w 21600 9 || { echo "bub-mirror: backup pool busy >6h, aborting"; exit 1; }

      BUB_USER=chris
      BUB_HOST=100.112.10.93
      BUB_PORT=4089
      SRC_PATH=/mnt/fusion/
      DST=/mnt/backup/all/rick-offsite/
      # --link-dest is destination-relative: rsync checks LINK_DEST/<same
      # relative path> as the destination file. media-mirror.sh dumps
      # gromit's /mnt/fusion contents directly at the root of
      # /mnt/backup/all (so /mnt/backup/all/Movies/X.mkv IS gromit's mirror
      # of that movie). So LINK_DEST=/mnt/backup/all makes rsync compare
      # bub:/mnt/fusion/Movies/X.mkv against /mnt/backup/all/Movies/X.mkv
      # and hardlink when they match.
      LINK_DEST=/mnt/backup/all
      SSH_KEY=/root/.ssh/id_ed25519
      STATE=/var/lib/bub-mirror
      LOGDIR="$STATE/logs"

      mkdir -p "$STATE" "$LOGDIR"

      # Excludes: tier-3 stuff, restic repos, transient/scratch, recovery
      # output, Windows/Linux trash dirs, OS metadata, the symlink that
      # would cause an rsync loop.
      EXCLUDES=(
        --exclude=/pinchflat
        --exclude=/arr
        --exclude=/restic
        --exclude=/.graveyard
        --exclude=/shows
        --exclude=/recup_dir.\*
        --exclude=/\$RECYCLE.BIN
        --exclude="/System Volume Information"
        --exclude=/.Trash-1000
        --exclude=.pool-member
      )

      notify() { gromit-notify "$1" "$2" "''${3:-default}" "''${4:-}" || true; }
      trap 'notify "bub-mirror ERROR" "Unexpected failure — journalctl -u bub-mirror-sync" urgent rotating_light' ERR

      [ "$(id -u)" -eq 0 ] || { echo "bub-mirror: must run as root"; exit 1; }
      [ -f "$SSH_KEY" ] || { echo "bub-mirror: ssh key missing at $SSH_KEY"; exit 1; }

      # Backup pool preflight (reuse media-mirror's sentinel check pattern).
      for mp in /mnt/backup/D1 /mnt/backup/D2 /mnt/backup/D3 /mnt/backup/D4 ; do
        if ! mountpoint -q "$mp" || [ ! -f "$mp/.pool-member" ]; then
          notify "bub-mirror ABORTED — drive offline" \
            "A backup pool member is missing ($mp). No files were changed." \
            urgent rotating_light
          echo "preflight failed: $mp"; exit 1
        fi
      done

      mkdir -p "$DST"

      ts=$(date +%Y-%m-%d_%H%M%S)
      log="$LOGDIR/sync-$ts.log"

      echo "bub-mirror: pulling $BUB_HOST:$SRC_PATH → $DST"
      echo "            link-dest=$LINK_DEST (hardlink dedup against media-mirror)"

      # Two-pass sync. The old single `rsync --link-dest` silently fell back to
      # a full COPY whenever mergerfs placed the rick-offsite copy on a
      # different branch than the master (hardlink EXDEV) — duplicating ~16% of
      # the overlap. We split the work so a wasteful copy is impossible:
      #
      #   Phase 1 (link pass): hardlink every file gromit already has, placed
      #   DIRECTLY on the master's branch (no mergerfs link(), so no EXDEV).
      #   Metadata-only — no data moves, so it can't stress the USB hub.
      #
      #   Phase 2 (copy pass): rsync pulls ONLY Rick-unique content.
      #   --compare-dest makes rsync SKIP anything already in the media-mirror
      #   (the overlap Phase 1 hardlinked), so it can never duplicate it.
      #   --size-only: immutable media, ignore mtime drift.

      echo "phase 1: hardlinking overlap (co-located on master's branch, no copies)"
      DRYRUN=0 bub-link-pass .

      echo "phase 2: pulling Rick-unique content (--compare-dest skips the overlap)"
      rc=0
      rsync \
        -aH --info=progress2,stats2 \
        --size-only \
        --partial --partial-dir=.rsync-partial \
        --compare-dest="$LINK_DEST" \
        --log-file="$log" --log-file-format='%i %n%L' \
        "''${EXCLUDES[@]}" \
        -e "ssh -i $SSH_KEY -p $BUB_PORT -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10" \
        "$BUB_USER@$BUB_HOST:$SRC_PATH" "$DST" || rc=$?
      # Tolerate 23 (partial — unreadable source files on bub) and 24 (source
      # files vanished mid-run); anything else is a real failure.
      if [ "$rc" -ne 0 ] && [ "$rc" -ne 23 ] && [ "$rc" -ne 24 ]; then
        echo "phase 2 rsync failed: exit $rc"; exit "$rc"
      fi
      [ "$rc" -eq 0 ] || echo "phase 2 rsync exit $rc (partial — some bub files unreadable; non-fatal)"

      # Tally Rick-unique files actually transferred (itemize '>f...' lines).
      copied=$(grep -cE '>f' "$log" 2>/dev/null || true)
      notify "bub-mirror OK" \
        "rick-offsite synced. Overlap hardlinked (phase 1); Rick-unique copied: ''${copied:-0} files." \
        low floppy_disk
      echo "bub-mirror done (itemized copy log: $log)"
    '';
  };
in
{
  # bubMirror is the scheduled job; bubLinkPass is also exposed for manual ops
  # (e.g. `sudo DRYRUN=1 bub-link-pass "TV Shows/Some Show"` to preview dedup).
  environment.systemPackages = [ bubMirror bubLinkPass ];

  systemd.tmpfiles.rules = [
    "d /var/lib/bub-mirror      0755 root root - -"
    "d /var/lib/bub-mirror/logs 0755 root root - -"
    # rick-offsite root — created here so first run finds it ready.
    "d /mnt/backup/all/rick-offsite 0755 root root - -"
  ];

  # Weekly pull from bub. Sundays 06:00 — two hours after media-mirror's
  # 04:00 so the local mirror is settled and visible as link-dest targets.
  systemd.services.bub-mirror-sync = {
    description = "Pull bub /mnt/fusion → /mnt/backup/all/rick-offsite (hardlink-deduped)";
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${bubMirror}/bin/bub-mirror";
    };
    # RequiresMountsFor belongs in [Unit], not [Service] — in serviceConfig it
    # was silently ignored, so the backup pool dependency never applied.
    unitConfig.RequiresMountsFor = "/mnt/backup/all";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
  };

  systemd.timers.bub-mirror-sync = {
    description = "Weekly bub → rick-offsite mirror";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "Sun 06:00";
      Persistent = true;
    };
  };
}
