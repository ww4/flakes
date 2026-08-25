# pool-autoremount — self-healing remount for storage pool members that drop
# off the USB bus.
#
# Covers BOTH mergerfs pools:
#   - fusion  (/mnt/primary/D1..D6  → /mnt/fusion)
#   - backup  (/mnt/backup/D1..D4   → /mnt/backup/all)
#
# Members are USB externals that fall off the bus under load. The `nofail`
# mount goes inactive rather than "failed", so the SystemdUnitFailed rule never
# sees them. This reconciler runs every 2 min, detects a missing member, and
# remounts it.
#
# A member can drop in one of TWO shapes, and only the first self-heals with a
# plain `systemctl start`:
#   1. clean  — the mount goes away; `mountpoint` says no. This is what the
#               module originally assumed was the only case.
#   2. zombie — XFS shuts the filesystem down but the mount ENTRY SURVIVES.
#               `/proc/mounts` still lists it, `mountpoint` still says yes, and
#               statfs(2) still returns the cached superblock — so `df` and
#               node_exporter both report it present and fine. Only real I/O
#               (readdir/read) returns EIO. Nothing can mount over the corpse,
#               so `systemctl start` fails forever with "device not ready".
#
# Shape 2 is not hypothetical — see the 2026-08-17 incident note below.
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
# ⚠️ The 2026-08-17 zombie-mount incident, which this module now handles.
# At 05:27 fusion D6 dropped off the bus; XFS logged "Block device removal
# (0x20) detected … Shutting down filesystem" and left the mount in place. The
# reconciler noticed 37 s later and then failed 260 consecutive times over 9 h,
# every attempt dying at "device not ready", because it can only ever call
# `systemctl start` and nothing may mount over a live mountpoint. It needed
# exactly one `umount`. bitcoind (datadir on /mnt/fusion) crash-looped to
# start-limit-hit and took fulcrum + mempool-cookie-sync with it.
#
# Nothing alerted for those 9 h, because of a mutual-delegation hole:
#   - this reconciler's can't-fix path was deliberately SILENT, delegating to
#     the PoolMemberOffline Grafana rule;
#   - that rule counts `node_filesystem_size_bytes` mount entries, and the
#     zombie kept the count pinned at 6/6.
# `node_filesystem_device_error` stayed 0 too — statfs never errored. So no
# node_exporter-derived metric could have seen this. Both halves are fixed:
# the reconciler now probes with real I/O, clears zombies, publishes its own
# health metric, and escalates when it cannot fix something itself.
#
# ⚠️ The 2026-08-24 incident: the zombie handling above never ran.
# After gromit was physically relocated, fusion D6's USB cable faulted and a
# reseat left the same zombie shape as 2026-08-17. The reconciler logged
# "D6: /mnt/primary/D6 is NOT mounted" — never "ZOMBIE MOUNT" — then failed the
# plain `systemctl start` three times and hit the flap cap. Two distinct bugs,
# both fixed here:
#
#   1. The zombie branch was gated behind `mountpoint -q`, which STATS the path.
#      On a shut-down XFS that stat returns EIO, so `mountpoint -q` said "not
#      mounted" and the zombie branch was skipped entirely. The detector was
#      defeated by the very fault it was written to catch. Now `findmnt` reads
#      the kernel mount table instead, which cannot fail on dead I/O.
#
#   2. Recovery needs a `-o nouuid` fallback. `umount -l` is lazy: it detaches
#      the mountpoint but defers releasing the superblock until the last
#      reference drops, and mergerfs holding the branch keeps it alive. XFS then
#      rejects the remount with "Filesystem has duplicate UUID … - can't mount".
#      Manual recovery on 2026-08-24 needed exactly that flag.
#
# The old failure string, "device not ready / unreadable", was actively
# misleading in both cases — the device was healthy and enumerated the whole
# time. It now points at the kernel log instead of blaming the hardware.
#
# Note: mergerfs picks up a branch mounted underneath it live — `mnt-backup-all`
# was never restarted on 2026-08-16 and went from 448G to the full 22T the
# moment the four branch mounts came up. So remounting the member is sufficient;
# the pool mount does not need touching.
#
# Safety model:
#  - It calls `systemctl start <mount>` (mounting replays the XFS log — the
#    designed, non-destructive recovery), `touch`, and — only for a confirmed
#    zombie — `umount -l`. It NEVER runs xfs_repair or any destructive command.
#    If the filesystem is too damaged to mount, the start fails and the drive is
#    left down, now with an escalation push rather than silence.
#  - `umount -l` is reached ONLY when the member is mounted AND a real read
#    returned a definite error. An ambiguous read (timeout — could be a slow
#    spin-up rather than a dead device) is explicitly NOT unmounted: it is
#    reported and left for the next run. Never take a destructive action on an
#    ambiguous signal; a member that is merely slow must not be torn down.
#  - Health is probed with `ls -A` (readdir), not `mountpoint`/statfs. That
#    choice is the whole point: the 2026-08-17 zombie satisfied both of the
#    cheap checks while every actual read returned EIO.
#  - "Is it mounted?" is answered by `findmnt` (/proc/self/mountinfo), never by
#    `mountpoint`. See the 2026-08-24 note below: `mountpoint` stats the path,
#    and that stat itself can return EIO, so it reports a zombie as absent.
#  - Write-test gate: success is only declared after recreating the
#    `.pool-member` sentinel succeeds, proving the remount is writable (not a
#    read-only / erroring remount).
#  - Flap cap: at most `maxPerDay` auto-remounts per drive per rolling 24 h.
#    Past that it stops remounting that drive — a disk that keeps dropping is
#    failing hardware, and silently remounting it would mask the warning.
#    Counters are per POOL per drive (both pools have a "D1"), so the state
#    files are named `<pool>-<drive>.remounts`. Legacy fusion-only files named
#    `<drive>.remounts` are migrated in place on first run.
#  - Notifications: `low` priority on a successful auto-remount (never wakes
#    anyone), and `default` priority ONCE per episode after `alertAfter`
#    consecutive failed recoveries (~8 min). The escalation fires on the
#    transition only, not every 2 min, per the polite-polling ethos. Delegating
#    the can't-fix case to a Grafana rule is what produced 9 h of silence on
#    2026-08-17; this module now reports its own failures.
#  - It publishes `pool_member_healthy{pool,member}` to the node_exporter
#    textfile collector, derived from the same real-I/O probe. This is the only
#    trustworthy pool-health signal — the node_filesystem_* series cannot
#    distinguish a working member from a zombie.
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
  readTimeout = 20;     # seconds a mounted member gets to answer a readdir
  alertAfter = 4;       # consecutive failed recoveries (~8 min) before escalating

  # Shared with drive-temps.nix; node_exporter reads *.prom from here.
  textfileDir = "/var/lib/node-exporter-textfile";

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

      # --- health metric ------------------------------------------------
      # Accumulated during the sweep and published atomically at exit, so
      # node_exporter never reads a half-written file. Skipped entirely when
      # nothing was probed (e.g. the maintenance-hold early exit), so a hold
      # leaves the last good sample in place instead of blanking the series.
      METRIC_TMP=""
      METRIC_N=0
      if [ -d "${textfileDir}" ]; then
        if METRIC_TMP=$(mktemp "${textfileDir}/.pool-autoremount.prom.XXXXXX"); then
          {
            echo "# HELP pool_member_healthy 1 if the pool member is mounted and answers real I/O, 0 otherwise."
            echo "# TYPE pool_member_healthy gauge"
          } > "$METRIC_TMP"
        else
          METRIC_TMP=""
        fi
      fi

      # Emit EXACTLY ONE sample per member per run: Prometheus rejects a
      # textfile with duplicate name+label pairs, and one bad file fails the
      # whole textfile collector. So this is called only at terminal outcomes —
      # healthy (1), successful remount (1), or note_failure (0) — never
      # speculatively on the way into a recovery attempt.
      record_health() {  # record_health <pool> <member> <0|1>
        [ -n "$METRIC_TMP" ] || return 0
        printf 'pool_member_healthy{pool="%s",member="%s"} %s\n' "$1" "$2" "$3" >> "$METRIC_TMP"
        METRIC_N=$(( METRIC_N + 1 ))
      }

      publish_health() {
        [ -n "$METRIC_TMP" ] || return 0
        if [ "$METRIC_N" -gt 0 ]; then
          chmod 0644 "$METRIC_TMP"
          # Same-directory rename => atomic swap for node_exporter's reader.
          mv -f "$METRIC_TMP" "${textfileDir}/pool-autoremount.prom" || rm -f "$METRIC_TMP"
        else
          rm -f "$METRIC_TMP"
        fi
      }
      trap publish_health EXIT

      # `mountpoint -q` STATS the path, and a shut-down XFS returns EIO from
      # that stat — so the zombie the module exists to catch read as "not
      # mounted" and skipped the zombie branch entirely (2026-08-24, below).
      # findmnt parses /proc/self/mountinfo, a pure kernel table, and never
      # touches the filesystem, so it stays truthful when every I/O is failing.
      is_mounted() {  # is_mounted <path>
        findmnt -rno TARGET "$1" > /dev/null 2>&1
      }

      # After `umount -l` the kernel may still hold the shut-down superblock:
      # the unmount is LAZY — it detaches the mountpoint but defers releasing
      # the superblock until the last reference drops, and mount.fuse.mergerfs
      # holding the branch is enough to keep it alive. XFS then refuses to mount
      # the very same filesystem again:
      #     XFS (sdm1): Filesystem has duplicate UUID <uuid> - can't mount
      # That is neither corruption nor a real collision, and `-o nouuid` is the
      # documented remedy. Gated on having cleared the zombie OURSELVES in this
      # run, so the check can only ever be skipped against a corpse we created —
      # never against a genuinely duplicated filesystem.
      # Source and type come from FSTAB (`findmnt -s`), not from
      # `systemctl show -p What`: for an active unit the latter reports the
      # RESOLVED device (/dev/sdm1) rather than the stable by-label/by-uuid path,
      # and after a re-enumeration that letter is exactly what went stale. The
      # fstab entry always names the persistent symlink, which points at whatever
      # device the drive came back as.
      try_nouuid_mount() {  # try_nouuid_mount <pool> <member> <mp>
        local src fstype
        src=$(findmnt -sn -o SOURCE "$3" 2>/dev/null || true)
        fstype=$(findmnt -sn -o FSTYPE "$3" 2>/dev/null || true)
        [ -n "$src" ] || return 1
        [ "$fstype" = "xfs" ] || return 1
        [ -e "$src" ] || return 1
        echo "$1/$2: remount rejected after clearing the zombie — the lazily-unmounted superblock is still pinned; retrying with '-o nouuid'"
        mount -t xfs -o nouuid "$src" "$3"
      }

      # Count a failed recovery and escalate exactly once per episode. The
      # counter is cleared whenever the member is healthy again, so a new
      # outage gets a new notification.
      # Every give-up path funnels through here, so this is also the single
      # place an unhealthy sample is recorded — see the note on record_health.
      note_failure() {  # note_failure <pool> <member> <failfile> <reason>
        record_health "$1" "$2" 0
        n=1
        if [ -f "$3" ]; then n=$(( $(cat "$3" 2>/dev/null || echo 0) + 1 )); fi
        echo "$n" > "$3"
        echo "$1/$2: $4 (consecutive failed recoveries: $n)"
        if [ "$n" -eq ${toString alertAfter} ]; then
          gromit-notify "Pool member DOWN — auto-recovery is failing" \
            "$1/$2 has resisted $n consecutive automatic recovery attempts. Reason: $4. The pool is degraded and this needs hands — check 'journalctl -u pool-autoremount'." \
            default "warning,floppy_disk" || true
        fi
      }

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
        fails="$STATE/$pool-$d.failures"

        # --- health gate ---------------------------------------------------
        # A mount entry is NOT proof the filesystem works (see the zombie case
        # in the header), so probe with a real readdir rather than trusting
        # `mountpoint`. Three outcomes, only one of which is destructive:
        #   healthy — mounted and answers   -> nothing to do
        #   zombie  — mounted, definite I/O error -> clear it, then remount
        #   slow    — mounted, no answer in time  -> AMBIGUOUS, leave it alone
        # Tracks whether WE cleared a zombie for this member in this run; gates
        # the `-o nouuid` fallback below.
        cleared_zombie=0

        if is_mounted "$mp"; then
          # Capture the probe status explicitly: `rc=$?` after a bare `if` would
          # read 0, because an `if` whose branch is not taken exits zero.
          rc=0
          timeout ${toString readTimeout} ls -A "$mp" > /dev/null 2>&1 || rc=$?
          if [ "$rc" -eq 0 ]; then
            rm -f "$fails"
            record_health "$pool" "$d" 1
            continue
          fi
          if [ "$rc" -eq 124 ]; then
            # Could be a drive spinning up under load rather than a dead one.
            # Tearing down a merely-slow member would cause the very outage
            # this module exists to prevent, so back off and retry next run.
            note_failure "$pool" "$d" "$fails" \
              "mounted but did not answer a readdir within ${toString readTimeout}s — ambiguous (slow vs dead), NOT unmounting"
            continue
          fi
          echo "$pool/$d: ZOMBIE MOUNT — $mp is mounted but unreadable (rc=$rc); the filesystem was shut down under a live mount. Clearing with 'umount -l' so it can be remounted."
          if ! umount -l "$mp"; then
            note_failure "$pool" "$d" "$fails" \
              "zombie mount detected but 'umount -l' failed — cannot recover automatically"
            continue
          fi
          cleared_zombie=1
          echo "$pool/$d: zombie mount cleared — proceeding to remount"
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
          note_failure "$pool" "$d" "$fails" \
            "flap cap reached ($recent auto-remounts in 24 h) — refusing to remount again; this drive is failing"
          continue
        fi

        # Warn (journal only) if the bare mountpoint accumulated files during
        # the outage — mergerfs may have written onto the nvme root dir; those
        # get shadowed by the mount and should be cleaned up manually later.
        if [ -n "$(ls -A "$mp" 2>/dev/null)" ]; then
          echo "$pool/$d: WARNING — $mp is non-empty while unmounted; stray files may have landed on the ROOT fs and will be shadowed by the remount"
        fi

        echo "$pool/$d: attempting 'systemctl start $unit'"
        nouuid_used=0
        if ! timeout ${toString mountTimeout} systemctl start "$unit"; then
          # Only a zombie we just cleared can leave a pinned superblock behind,
          # so that is the single case worth a second attempt.
          if [ "$cleared_zombie" -eq 1 ] && try_nouuid_mount "$pool" "$d" "$mp"; then
            nouuid_used=1
          else
            # Deliberately does NOT claim the device is unreadable: on
            # 2026-08-24 this string sent the on-call reader after a healthy
            # drive when the real blocker was a mount the kernel had refused.
            note_failure "$pool" "$d" "$fails" \
              "'systemctl start $unit' failed — the device may be absent, or the kernel refused the mount; check 'journalctl -k' for its reason before assuming bad hardware"
            continue
          fi
        fi

        if ! is_mounted "$mp"; then
          note_failure "$pool" "$d" "$fails" \
            "start returned 0 but $mp is still not a mountpoint"
          continue
        fi

        # Write-test + restore the sentinel the preflights check.
        if ! touch "$mp/.pool-member" 2>/dev/null; then
          systemctl stop "$unit" || true
          note_failure "$pool" "$d" "$fails" \
            "remounted but NOT writable — unmounted again and backing off"
          continue
        fi

        rm -f "$fails"
        record_health "$pool" "$d" 1
        echo "$now" >> "$log"
        count=$(( recent + 1 ))
        # A `-o nouuid` recovery is NOT equivalent to a normal one: it is a
        # manual mount outside the unit, and the pinned corpse survives until
        # reboot, so any further drop of this member will fail the plain fstab
        # mount the same way. Say so rather than reporting a clean heal.
        nouuid_note=""
        if [ "$nouuid_used" -eq 1 ]; then
          nouuid_note=" NOTE: recovered with '-o nouuid' because the previous superblock was still pinned — this is a manual mount and the stale superblock persists until a REBOOT, so another drop before then will not self-heal."
          echo "$pool/$d: recovered via '-o nouuid' — a reboot is needed to clear the stale superblock"
        fi

        echo "$pool/$d: auto-remounted OK (occurrence $count of ${toString maxPerDay} in 24 h)"
        gromit-notify "Pool drive auto-remounted" \
          "$pool/$d ($mp) dropped off the bus and was automatically remounted — occurrence $count of ${toString maxPerDay} allowed in 24 h. $poolMount is whole again.$nouuid_note" \
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
