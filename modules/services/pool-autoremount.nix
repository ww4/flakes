# pool-autoremount — self-healing remount for fusion pool members that drop
# off the USB bus.
#
# Two fusion members are USB externals (D1 Expansion Desk; the D6 slot has a
# history of WD My Book drops) that fall off the bus under load and unmount
# *cleanly* (the `nofail` mount goes inactive, not "failed"), so the
# SystemdUnitFailed rule never sees them. This reconciler runs every 2 min,
# detects a missing member, and remounts it.
#
# Safety model:
#  - It only ever calls `systemctl start <mount>` (mounting replays the XFS
#    log — the designed, non-destructive recovery) and `touch`. It NEVER runs
#    xfs_repair or any destructive command. If the filesystem is too damaged to
#    mount, the start fails and the drive is left down for the PoolMemberOffline
#    Grafana warning (which respects quiet hours) to surface.
#  - Write-test gate: success is only declared after recreating the
#    `.pool-member` sentinel succeeds, proving the remount is writable (not a
#    read-only / erroring remount).
#  - Flap cap: at most `maxPerDay` auto-remounts per drive per rolling 24 h.
#    Past that it stops remounting that drive — a disk that keeps dropping is
#    failing hardware, and silently remounting it would mask the warning.
#  - It pushes ntfy only on a *successful* auto-remount, at `low` priority, so
#    it never wakes anyone. The can't-fix and flapping cases are intentionally
#    left to the PoolMemberOffline warning.
#  - Maintenance: `touch /run/pool-autoremount.hold` to pause without stopping
#    the timer (e.g. when intentionally unmounting a drive), or
#    `systemctl stop pool-autoremount.timer`.
{ config, lib, pkgs, ... }:

let
  gromit-notify = import ./notify-pkg.nix { inherit pkgs; };

  members = [ "D1" "D2" "D3" "D4" "D5" "D6" ];
  maxPerDay = 3;        # flap cap: auto-remounts per drive per rolling 24 h
  mountTimeout = 240;   # seconds allowed for a mount (covers XFS log replay)

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

      for d in ${lib.concatStringsSep " " members}; do
        mp="/mnt/primary/$d"
        unit="mnt-primary-$d.mount"
        log="$STATE/$d.remounts"

        if mountpoint -q "$mp"; then
          continue
        fi

        echo "$d: $mp is NOT mounted — evaluating auto-remount"

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
          echo "$d: flap cap reached ($recent auto-remounts in 24 h) — leaving down for the PoolMemberOffline warning"
          continue
        fi

        # Warn (journal only) if the bare mountpoint accumulated files during
        # the outage — mergerfs may have written onto the nvme root dir; those
        # get shadowed by the mount and should be cleaned up manually later.
        if [ -n "$(ls -A "$mp" 2>/dev/null)" ]; then
          echo "$d: WARNING — $mp is non-empty while unmounted; stray files may have landed on the root fs and will be shadowed by the remount"
        fi

        echo "$d: attempting 'systemctl start $unit'"
        if ! timeout ${toString mountTimeout} systemctl start "$unit"; then
          echo "$d: systemctl start failed (device not ready / unreadable) — leaving down for the warning"
          continue
        fi

        if ! mountpoint -q "$mp"; then
          echo "$d: start returned 0 but $mp is still not a mountpoint — leaving down"
          continue
        fi

        # Write-test + restore the sentinel the backup preflight checks.
        if ! touch "$mp/.pool-member" 2>/dev/null; then
          echo "$d: remounted but NOT writable — unmounting and backing off"
          systemctl stop "$unit" || true
          continue
        fi

        echo "$now" >> "$log"
        count=$(( recent + 1 ))
        echo "$d: auto-remounted OK (occurrence $count of ${toString maxPerDay} in 24 h)"
        gromit-notify "Pool drive auto-remounted" \
          "$d ($mp) dropped off the bus and was automatically remounted — occurrence $count of ${toString maxPerDay} allowed in 24 h. /mnt/fusion is whole again." \
          low "floppy_disk,white_check_mark" || true
      done
    '';
  };
in
{
  environment.systemPackages = [ pool-autoremount ];

  systemd.services.pool-autoremount = {
    description = "Auto-remount fusion pool members that dropped off the bus";
    after = [ "local-fs.target" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${pool-autoremount}/bin/pool-autoremount";
    };
  };

  systemd.timers.pool-autoremount = {
    description = "Periodic fusion pool auto-remount check";
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
