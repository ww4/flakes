# pool-autoremount — self-healing remount for storage pool members that drop
# off the USB bus.
#
# Covers BOTH mergerfs pools:
#   - fusion  (/mnt/primary/D1..D6  → /mnt/fusion)
#   - backup  (/mnt/backup/D1..D4   → /mnt/backup/all)
#
# Members are USB externals that fall off the bus under load and unmount
# *cleanly* (the `nofail` mount goes inactive, not "failed"), so the
# SystemdUnitFailed rule never sees them. This reconciler runs every 2 min,
# detects a missing member, and remounts it.
#
# Why the backup pool matters just as much (2026-08-16): after re-cabling the
# backup drives off the Genesys hub they all enumerated fine but NONE mounted —
# hot-plugging after boot does not trigger fstab mounts. mergerfs then fell back
# to the bare `/mnt/backup/D*` directories **on the root SSD**, so
# `/mnt/backup/all` reported 448G instead of 22T. Nothing spilled (the backup
# preflight refuses to run with a member missing) but backups stayed broken and
# nothing alerted, because this reconciler and the PoolMemberOffline rule both
# only knew about /mnt/primary. Both are generalised here.
#
# Note: mergerfs picks up a branch mounted underneath it live — `mnt-backup-all`
# was never restarted on 2026-08-16 and went from 448G to the full 22T the
# moment the four branch mounts came up. So remounting the member is sufficient;
# the pool mount does not need touching.
#
# Safety model:
#  - It only ever calls `systemctl start <mount>` (mounting replays the XFS
#    log — the designed, non-destructive recovery) and `touch`. It NEVER runs
#    xfs_repair or any destructive command. If the filesystem is too damaged to
#    mount, the start fails and the drive is left down for the PoolMemberOffline
#    / BackupPoolMemberOffline Grafana warning (which respects quiet hours).
#  - Write-test gate: success is only declared after recreating the
#    `.pool-member` sentinel succeeds, proving the remount is writable (not a
#    read-only / erroring remount).
#  - Flap cap: at most `maxPerDay` auto-remounts per drive per rolling 24 h.
#    Past that it stops remounting that drive — a disk that keeps dropping is
#    failing hardware, and silently remounting it would mask the warning.
#    Counters are per POOL per drive (both pools have a "D1"), so the state
#    files are named `<pool>-<drive>.remounts`. Legacy fusion-only files named
#    `<drive>.remounts` are migrated in place on first run.
#  - It pushes ntfy only on a *successful* auto-remount, at `low` priority, so
#    it never wakes anyone. The can't-fix and flapping cases are intentionally
#    left to the offline warnings.
#  - Maintenance: `touch /run/pool-autoremount.hold` to pause without stopping
#    the timer (e.g. when intentionally unmounting a drive), or
#    `systemctl stop pool-autoremount.timer`.
{ config, lib, pkgs, ... }:

let
  gromit-notify = import ./notify-pkg.nix { inherit pkgs; };

  # Each pool: id, branch base dir, mount-unit prefix, the mergerfs pool mount,
  # and its members. Adding a pool here is the only change needed.
  pools = [
    {
      id = "fusion";
      base = "/mnt/primary";
      unitPrefix = "mnt-primary";
      poolMount = "/mnt/fusion";
      members = [ "D1" "D2" "D3" "D4" "D5" "D6" ];
    }
    {
      id = "backup";
      base = "/mnt/backup";
      unitPrefix = "mnt-backup";
      poolMount = "/mnt/backup/all";
      members = [ "D1" "D2" "D3" "D4" ];
    }
  ];

  maxPerDay = 3;        # flap cap: auto-remounts per drive per rolling 24 h
  mountTimeout = 240;   # seconds allowed for a mount (covers XFS log replay)

  # Flatten to "pool|base|unitPrefix|poolMount|drive" tokens so the shell loop
  # stays a plain `for` (no while-read, which competes for stdin with systemctl).
  entries = lib.concatMap
    (p: map (m: "${p.id}|${p.base}|${p.unitPrefix}|${p.poolMount}|${m}") p.members)
    pools;

  pool-autoremount = pkgs.writeShellApplication {
    name = "pool-autoremount";
    runtimeInputs = [ pkgs.util-linux pkgs.systemd pkgs.coreutils gromit-notify ];
    text = ''
      STATE=/var/lib/pool-autoremount
      mkdir -p "$STATE"

      if [ -e /run/pool-autoremount.hold ]; then
        echo "maintenance hold present (/run/pool-autoremount.hold) — skipping"
        exit 0
      fi

      now=$(date +%s)
      window=$(( 24 * 3600 ))

      # One-time migration of the pre-2026-08 fusion-only state file names
      # (`D1.remounts` → `fusion-D1.remounts`), needed now that both pools have
      # a drive called D1. Deliberately NON-FATAL: this runs under `set -e`, and
      # a self-healing reconciler must never abort every remount for both pools
      # because a stale bookkeeping file could not be renamed.
      for legacy in "$STATE"/D[0-9].remounts; do
        [ -f "$legacy" ] || continue
        target="$STATE/fusion-$(basename "$legacy")"
        if [ ! -e "$target" ]; then
          mv "$legacy" "$target" || echo "WARNING: could not migrate $legacy → $target (continuing)"
        fi
      done

      for entry in ${lib.escapeShellArgs entries}; do
        IFS='|' read -r pool base unitPrefix poolMount d <<< "$entry"

        mp="$base/$d"
        unit="$unitPrefix-$d.mount"
        log="$STATE/$pool-$d.remounts"

        if mountpoint -q "$mp"; then
          continue
        fi

        echo "$pool/$d: $mp is NOT mounted — evaluating auto-remount"

        # Flap cap: keep only successful-remount timestamps from the last 24 h.
        recent=0
        if [ -f "$log" ]; then
          tmp=$(mktemp)
          while read -r ts; do
            [ -n "$ts" ] || continue
            if [ $(( now - ts )) -lt "$window" ]; then
              echo "$ts" >> "$tmp"
              recent=$(( recent + 1 ))
            fi
          done < "$log"
          mv "$tmp" "$log"
        fi

        if [ "$recent" -ge ${toString maxPerDay} ]; then
          echo "$pool/$d: flap cap reached ($recent auto-remounts in 24 h) — leaving down for the offline warning"
          continue
        fi

        # Warn (journal only) if the bare mountpoint accumulated files during
        # the outage — mergerfs may have written onto the nvme root dir; those
        # get shadowed by the mount and should be cleaned up manually later.
        if [ -n "$(ls -A "$mp" 2>/dev/null)" ]; then
          echo "$pool/$d: WARNING — $mp is non-empty while unmounted; stray files may have landed on the ROOT fs and will be shadowed by the remount"
        fi

        echo "$pool/$d: attempting 'systemctl start $unit'"
        if ! timeout ${toString mountTimeout} systemctl start "$unit"; then
          echo "$pool/$d: systemctl start failed (device not ready / unreadable) — leaving down for the warning"
          continue
        fi

        if ! mountpoint -q "$mp"; then
          echo "$pool/$d: start returned 0 but $mp is still not a mountpoint — leaving down"
          continue
        fi

        # Write-test + restore the sentinel the preflights check.
        if ! touch "$mp/.pool-member" 2>/dev/null; then
          echo "$pool/$d: remounted but NOT writable — unmounting and backing off"
          systemctl stop "$unit" || true
          continue
        fi

        echo "$now" >> "$log"
        count=$(( recent + 1 ))
        echo "$pool/$d: auto-remounted OK (occurrence $count of ${toString maxPerDay} in 24 h)"
        gromit-notify "Pool drive auto-remounted" \
          "$pool/$d ($mp) dropped off the bus and was automatically remounted — occurrence $count of ${toString maxPerDay} allowed in 24 h. $poolMount is whole again." \
          low "floppy_disk,white_check_mark" || true
      done
    '';
  };
in
{
  environment.systemPackages = [ pool-autoremount ];

  systemd.services.pool-autoremount = {
    description = "Auto-remount storage pool members that dropped off the bus";
    after = [ "local-fs.target" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${pool-autoremount}/bin/pool-autoremount";
    };
  };

  systemd.timers.pool-autoremount = {
    description = "Periodic storage pool auto-remount check";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "3min";
      OnUnitActiveSec = "2min";
      AccuracySec = "30s";
    };
  };

  systemd.tmpfiles.rules = [
    "d /var/lib/pool-autoremount 0755 root root - -"
  ];
}
