# disk-io-watch — count kernel I/O errors and USB resets per device, publish
# them as Prometheus counters, and alert on a device that starts failing.
#
# WHY THIS EXISTS. On 2026-08-25 D6 (`sdm`, a USB enclosure) logged 1,762
# `DID_ERROR` reads in ~10 h — a real, sustained link fault — and NOTHING on the
# box noticed. There were zero USB bus resets, because the block layer retried
# and mostly won, so nothing escalated to the events pool-autoremount and the
# sentinel watch for. `pool_member_healthy` read 1 (it comes from a readdir, and
# readdirs succeeded), `df` was happy, bitcoind sat at progress=1.000000, and
# there were no failed units. The only thing that could find it was a hand-run
# `journalctl -k -b | grep -c 'sdm.*DID_ERROR'`.
#
# That is the quiet shape of the fault that later becomes a hard drop or a
# zombie mount. No check anywhere counted kernel I/O errors per device, so this
# module does exactly that and nothing else. See [[gromit-d6-drive]].
#
# METRICS (node_exporter textfile collector):
#   disk_io_errors_total{device}      block-layer "I/O error, dev sdX, sector N"
#   disk_scsi_errors_total{device}    SCSI "hostbyte=DID_ERROR" / "Result: ..."
#   usb_device_resets_total{port}     "usb 6-2: reset SuperSpeed USB device"
#   disk_io_watch_last_run_seconds    liveness — a stalled watcher is not silence
#
# All three are true counters, reset to 0 on reboot, which is what Prometheus
# expects. They are accumulated from the journal with a CURSOR, not by
# re-scanning `-b` every run: a full-boot scan costs more every hour the box
# stays up, and this runs every 5 minutes.
#
# ⚠️ KERNEL DEVICE NAMES ARE NOT STABLE. A USB drive that drops and re-enumerates
# comes back as a different `sdX`, so a device that failed and was then reseated
# leaves an ORPHANED series behind — the label names something that no longer
# exists. Verified on the first run: `sdf` showed 111 errors in a 47-minute
# window right after the 2026-08-24 22:08 boot and then vanished; that was D1
# before it was reseated, which came back under another letter. The counts are
# still honest (they are per-boot totals of things that really happened), but
# DO NOT read a device label here as "this disk, right now" — cross-check
# against `lsblk` before acting. Labelling by serial would fix it and is the
# obvious follow-up; it needs a udev/`lsblk` lookup per device and the journal
# only gives the kernel name, so it is deliberately out of scope here.
#
# ⚠️ A counter that only ever goes up is not the same as a working watcher. If
# the journal read fails, the run leaves the previous totals in place and does
# NOT write zeros — a blank series would read as "no errors" when it means "no
# data". disk_io_watch_last_run_seconds is what distinguishes the two.
{ config, lib, pkgs, ... }:

let
  gromit-notify = import ./notify-pkg.nix { inherit pkgs; };

  # Shared with pool-autoremount.nix and drive-temps.nix.
  textfileDir = "/var/lib/node-exporter-textfile";
  stateDir = "/var/lib/disk-io-watch";

  # Alerting is RELATIVE, per the standing preference: compare against the state
  # at the LAST notification, never against a fixed band. A device that is
  # already known-bad must not re-page every interval.
  #
  #   - first alert:  a device crosses `newFaultErrors` having been clean
  #   - re-alert:     only when its total has at least DOUBLED since the last
  #                   notification AND grown by at least `newFaultErrors`
  #
  # Doubling makes re-alerts logarithmic: a fault going 50 -> 50,000 produces
  # ~10 notifications, not 1,000. D6 at ~300 resets/hr would otherwise page
  # hourly forever.
  newFaultErrors = 50;

  # Quiet hours are absolute for this class. A failing disk is not fire, flood,
  # or electrical, so it waits for morning. Nothing here may pierce 22:00-07:00.
  notifyAfterHour = 7;
  notifyBeforeHour = 22;

  disk-io-watch = pkgs.writeShellApplication {
    name = "disk-io-watch";
    runtimeInputs = [ pkgs.systemd pkgs.coreutils pkgs.gnugrep pkgs.gawk gromit-notify ];
    text = ''
      mkdir -p "${stateDir}"
      CURSOR="${stateDir}/cursor"
      BOOTID="${stateDir}/bootid"
      TOTALS="${stateDir}/totals"     # "<metric>|<label> <count>" per line
      NOTIFIED="${stateDir}/notified" # totals as of the last notification

      now_boot=$(cat /proc/sys/kernel/random/boot_id)
      prev_boot=$(cat "$BOOTID" 2>/dev/null || echo "")

      # A reboot resets the kernel log AND the real-world counters, so the
      # cursor and the accumulated totals are both meaningless across one.
      # Start clean rather than carrying a phantom baseline forward.
      if [ "$now_boot" != "$prev_boot" ]; then
        echo "boot changed ($prev_boot -> $now_boot) — resetting counters"
        rm -f "$CURSOR" "$TOTALS" "$NOTIFIED"
        printf '%s' "$now_boot" > "$BOOTID"
      fi

      # Read only what is new. Without a cursor (first run this boot) read the
      # whole boot once, which is the only full scan we ever pay for.
      jargs=( -k -o short-unix --no-pager --show-cursor )
      if [ -s "$CURSOR" ]; then
        jargs+=( --after-cursor "$(cat "$CURSOR")" )
      else
        jargs+=( -b )
      fi

      chunk=$(mktemp)
      # shellcheck disable=SC2064
      trap "rm -f '$chunk'" EXIT
      if ! journalctl "''${jargs[@]}" > "$chunk" 2>/dev/null; then
        # ⚠️ Do NOT publish on a failed read. Writing zeros here would turn
        # "the lookup failed" into "there are no errors" — the exact
        # empty-result ambiguity that has bitten this homelab repeatedly.
        echo "journalctl read failed — leaving previous totals in place"
        exit 0
      fi

      # `--show-cursor` appends "-- cursor: s=..." as the LAST line. Save it for
      # the next run, then drop it so it can't be parsed as a log line.
      newcur=$(grep -oP '^-- cursor: \K.*' "$chunk" || true)
      grep -v '^-- cursor: ' "$chunk" > "$chunk.body" || true
      mv -f "$chunk.body" "$chunk"

      # --- count this chunk ------------------------------------------------
      # Block layer: "blk_update_request: I/O error, dev sdm, sector 12345"
      # SCSI:        "sd 6:0:0:0: [sdm] tag#0 ... hostbyte=DID_ERROR"
      # USB:         "usb 6-2: reset SuperSpeed USB device number 5 using xhci_hcd"
      delta=$(mktemp)
      # shellcheck disable=SC2064
      trap "rm -f '$chunk' '$delta'" EXIT
      {
        grep -oP 'I/O error, dev \K[a-z0-9]+' "$chunk" \
          | awk '{print "disk_io_errors_total|device|" $1}' || true
        grep -oP '\[\K[a-z0-9]+(?=\].*DID_ERROR)' "$chunk" \
          | awk '{print "disk_scsi_errors_total|device|" $1}' || true
        grep -oP 'usb \K[0-9]+-[0-9.]+(?=: reset )' "$chunk" \
          | awk '{print "usb_device_resets_total|port|" $1}' || true
      } | sort | uniq -c | awk '{print $2, $1}' > "$delta"

      # --- accumulate into persisted totals ---------------------------------
      touch "$TOTALS"
      merged=$(mktemp)
      # shellcheck disable=SC2064
      trap "rm -f '$chunk' '$delta' '$merged'" EXIT
      awk '
        NR==FNR { t[$1] += $2; next }
                { t[$1] += $2 }
        END     { for (k in t) print k, t[k] }
      ' "$TOTALS" "$delta" | sort > "$merged"
      mv -f "$merged" "$TOTALS"

      # NOT `[ -n "$newcur" ] && ...` — as a trailing statement under `set -e`
      # a false test would exit the script before it ever publishes.
      if [ -n "$newcur" ]; then
        printf '%s' "$newcur" > "$CURSOR"
      fi

      # --- publish ----------------------------------------------------------
      # Atomic same-directory rename; node_exporter never sees a partial file.
      # Exactly one sample per name+label pair — a duplicate fails the WHOLE
      # textfile collector, not just this file (learned the hard way).
      if [ -d "${textfileDir}" ] && mtmp=$(mktemp "${textfileDir}/.disk-io-watch.prom.XXXXXX"); then
        {
          echo "# HELP disk_io_errors_total Block-layer I/O errors seen in the kernel log this boot."
          echo "# TYPE disk_io_errors_total counter"
          echo "# HELP disk_scsi_errors_total SCSI DID_ERROR results seen in the kernel log this boot."
          echo "# TYPE disk_scsi_errors_total counter"
          echo "# HELP usb_device_resets_total USB device resets seen in the kernel log this boot."
          echo "# TYPE usb_device_resets_total counter"
          # TOTALS lines are "<metric>|<label>|<value> <count>".
          awk '{ split($1, p, "|"); printf "%s{%s=\"%s\"} %s\n", p[1], p[2], p[3], $2 }' "$TOTALS"
          echo "# HELP disk_io_watch_last_run_seconds Unix time of the last successful run. Staleness here means the watcher stopped, not that errors stopped."
          echo "# TYPE disk_io_watch_last_run_seconds gauge"
          echo "disk_io_watch_last_run_seconds $(date +%s)"
        } > "$mtmp"
        chmod 0644 "$mtmp"
        mv -f "$mtmp" "${textfileDir}/disk-io-watch.prom" || rm -f "$mtmp"
      fi

      # --- alert ------------------------------------------------------------
      hour=$(date +%-H)
      if [ "$hour" -lt ${toString notifyAfterHour} ] || [ "$hour" -ge ${toString notifyBeforeHour} ]; then
        echo "quiet hours (hour=$hour) — metrics published, notification suppressed"
        exit 0
      fi

      touch "$NOTIFIED"
      msg=$(awk -v thresh=${toString newFaultErrors} '
        NR==FNR { seen[$1] = $2; next }
        {
          key = $1; cur = $2; last = (key in seen ? seen[key] : 0)
          if (cur < thresh) next
          # First alert for a previously-clean device, or a doubling since the
          # last one. Both require thresh new events, so noise cannot creep up.
          if (last == 0 || (cur >= last * 2 && cur - last >= thresh)) {
            split(key, p, "|")
            printf "%s %s: %d (was %d)\n", p[3], p[1], cur, last
          }
        }
      ' "$NOTIFIED" "$TOTALS")

      if [ -n "$msg" ]; then
        gromit-notify "Disk I/O errors climbing" \
          "Kernel is logging I/O errors. This is the quiet fault shape that precedes a drop or zombie mount.

$msg

Check: journalctl -k -b | grep -E 'DID_ERROR|I/O error'" \
          default "warning,floppy_disk" || true
        # Only rebaseline the devices we actually mentioned, so a quiet device
        # that crosses the threshold later still gets its own first alert.
        cp -f "$TOTALS" "$NOTIFIED"
      fi
    '';
  };
in
{
  systemd.tmpfiles.rules = [
    "d ${stateDir} 0750 root root - -"
  ];

  systemd.services.disk-io-watch = {
    description = "Count kernel disk I/O errors and USB resets per device";
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${disk-io-watch}/bin/disk-io-watch";
    };
  };

  systemd.timers.disk-io-watch = {
    description = "Periodic disk I/O error census";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "3min";
      OnUnitActiveSec = "5min";
      # Not Persistent: the counters are per-boot by construction, so there is
      # nothing to catch up on after downtime.
      AccuracySec = "30s";
    };
  };
}
