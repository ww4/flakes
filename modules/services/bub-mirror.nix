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

  bubMirror = pkgs.writeShellApplication {
    name = "bub-mirror";
    runtimeInputs = with pkgs; [
      rsync
      openssh
      coreutils
      util-linux
      gnugrep
      gromit-notify
    ];
    excludeShellChecks = [ "SC2001" ];
    text = ''
      # bub-mirror — pull bub:/mnt/fusion → /mnt/backup/all/rick-offsite/
      # with --link-dest hardlink dedup against /mnt/backup/all media-mirror.
      set -o errexit -o nounset -o pipefail -o errtrace

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

      # rsync via SSH, hardlink dedup against local media-mirror, archive +
      # hardlinks-preserved + partial-on-fail + bandwidth-friendly opts.
      # --link-dest: rsync looks for matching files at the same relative path
      # under LINK_DEST and hardlinks instead of copying. Mergerfs's epmfs
      # placement ensures the hardlink target is on the same branch.
      # --size-only: match files by size alone, ignoring mtime. Bub's media is
      # immutable (a finished movie never changes size), and its mtimes differ
      # from our independently-acquired copies — without this, the default
      # size+mtime check treats every content-identical file as "changed" and
      # re-pulls it over the network just to hardlink it (wasteful). size-only
      # also makes future runs immune to mtime drift triggering a re-pull storm.
      rsync \
        -aH --info=progress2,stats2 \
        --size-only \
        --partial --partial-dir=.rsync-partial \
        --link-dest="$LINK_DEST" \
        "''${EXCLUDES[@]}" \
        -e "ssh -i $SSH_KEY -p $BUB_PORT -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10" \
        "$BUB_USER@$BUB_HOST:$SRC_PATH" "$DST" \
        2>&1 | tee "$log"

      # Tally: how many files were hardlinked (no transfer) vs new bytes copied.
      hl_count=$(grep -cE "^h[fL]" "$log" || true)
      copied=$(grep -E "Number of regular files transferred" "$log" | grep -oE "[0-9,]+" | tail -1 | tr -d ',' || true)
      bytes=$(grep -E "Total transferred file size" "$log" | grep -oE "[0-9,]+ bytes" | head -1 || true)

      notify "bub-mirror OK" \
        "Pulled bub → rick-offsite. Hardlinked: $hl_count. Copied: ''${copied:-0} new files (''${bytes:-0})." \
        low floppy_disk
      echo "bub-mirror done"
    '';
  };
in
{
  environment.systemPackages = [ bubMirror ];

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
      # Network + the backup pool need to be up.
      RequiresMountsFor = "/mnt/backup/all";
    };
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
