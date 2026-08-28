# Drive temperature monitoring for gromit's many spinning disks (SATA + USB pool).
#
# node_exporter's hwmon collector already covers the CPU + NVMe, but the SATA and
# USB HDDs have no hwmon temp. This adds a tiny smartctl-based exporter that writes
# per-drive temps to node_exporter's textfile collector every few minutes.
#
# Key choice: `smartctl -n standby` so a spun-down drive is NOT woken just to read
# its temp. BUT the backup-pool WD Elements USB bridges don't report power state
# correctly, so `-n standby` fails to detect standby and the SMART read WAKES them
# — defeating drive-spindown.nix and cooking them. So those drives (matched by
# by-id) are read ONLY while actively spinning (block I/O since the last run, i.e. a
# backup job is writing); when idle they're skipped so they can sleep. An idle drive
# makes ~no heat, so there's nothing to monitor anyway.
{ config, lib, pkgs, ... }:
let
  textfileDir = "/var/lib/node-exporter-textfile";
  # The backup-pool USB drives (same by-id set as drive-spindown.nix). Letters
  # shuffle across reboots/replugs, so they're matched by stable by-id and
  # resolved to the current /dev name at run time. SMART-read only when active.
  backupDriveIds = [
    "usb-WD_Elements_25A3_575832324439303132343737-0:0"
    "usb-WD_Elements_25A3_57583532444330364C304B32-0:0"
    "usb-WD_Elements_25A3_575835314438394844563632-0:0"
    "usb-WD_Elements_25A3_575832324443303254584B36-0:0"
  ];
  driveTemps = pkgs.writeShellApplication {
    name = "drive-temps-export";
    runtimeInputs = with pkgs; [ smartmontools jq gawk coreutils util-linux ];
    text = ''
      out="${textfileDir}/drive_temps.prom"
      # Dot-prefixed temp name so a leftover can never be mistaken for a real
      # metric file, and so it sorts out of the way. The old name was
      # "$out.$$" — see the cleanup below for why that mattered.
      tmp="$(mktemp "${textfileDir}/.drive_temps.prom.XXXXXX")"
      # Per-device cumulative-I/O snapshot — used to tell whether a backup drive
      # is actively spinning before we risk waking it with a SMART read.
      state="${textfileDir}/.drive-iostat"
      newstate="$state.$$"
      : > "$newstate"

      # ⚠️ Always remove the temp file. Without this, every failed run left one
      # behind: 2,932 orphaned drive_temps.prom.<pid> files had accumulated by
      # 2026-08-27, one per run since 2026-08-11.
      cleanup() { rm -f "$tmp" "$newstate"; }
      trap cleanup EXIT

      # Sweep up the orphans left by the old "$out.$$" naming. Anchored to the
      # exact legacy shape (digits only) so it can never match drive_temps.prom
      # itself or any other module's file.
      find "${textfileDir}" -maxdepth 1 -type f \
        -regex '.*/drive_temps\.prom\.[0-9]+' -delete 2>/dev/null || true

      # Resolve the backup-pool by-ids to their current /dev names.
      backup_devs=""
      for id in ${lib.concatStringsSep " " backupDriveIds}; do
        link="/dev/disk/by-id/$id"
        if [ -e "$link" ]; then
          backup_devs="$backup_devs $(basename "$(readlink -f "$link")")"
        fi
      done

      {
        echo "# HELP gromit_drive_temp_celsius Drive temperature C (smartctl; standby/idle drives omitted)."
        echo "# TYPE gromit_drive_temp_celsius gauge"
        # SMART health/failure attributes — the real "is this drive failing?"
        # signals, from the SAME smartctl read (2026-08-10). smart_ok is the
        # overall self-assessment; the sector counts flag developing failure
        # before it becomes a hard fault.
        echo "# HELP gromit_drive_smart_ok SMART overall-health self-assessment (1=PASSED,0=FAILED)."
        echo "# TYPE gromit_drive_smart_ok gauge"
        echo "# HELP gromit_drive_reallocated_sectors SMART attr 5 raw (reallocated sectors)."
        echo "# TYPE gromit_drive_reallocated_sectors gauge"
        echo "# HELP gromit_drive_pending_sectors SMART attr 197 raw (current pending sectors)."
        echo "# TYPE gromit_drive_pending_sectors gauge"
        echo "# HELP gromit_drive_offline_uncorrectable SMART attr 198 raw (offline uncorrectable)."
        echo "# TYPE gromit_drive_offline_uncorrectable gauge"
        echo "# HELP gromit_drive_crc_errors SMART attr 199 raw (UDMA CRC errors — cable/link)."
        echo "# TYPE gromit_drive_crc_errors gauge"
        for dev in /dev/sd[a-z]; do
          [ -b "$dev" ] || continue
          name=$(basename "$dev")
          # Total sectors transferred (read+written). SMART reads don't bump
          # these, so it's a clean activity signal (the same one hd-idle uses).
          io=$(awk -v d="$name" '$3==d {print $6+$10; f=1} END{if(!f) print 0}' /proc/diskstats)
          printf '%s %s\n' "$name" "$io" >> "$newstate"
          # Backup-pool drives: skip the SMART read unless they've done I/O since
          # the last run — otherwise a read would wake a sleeping drive. The
          # standby-guard (-n standby) is applied ONLY to them: it protects a
          # sleeping backup drive, but some USB bridges (WD My Book/Elements)
          # misreport power state, so -n standby would ABORT the read on an
          # always-spinning FUSION drive and we'd get no SMART (this is why D6's
          # health was blank, 2026-08-10). Fusion/SSD drives are read without it.
          is_backup=0
          case " $backup_devs " in
            *" $name "*)
              is_backup=1
              prev=$(awk -v d="$name" '$1==d{print $2}' "$state" 2>/dev/null || true)
              if [ -z "''${prev:-}" ] || [ "$io" = "''${prev:-}" ]; then
                continue
              fi
              ;;
          esac
          nflag=""; [ "$is_backup" = 1 ] && nflag="-n standby"
          # USB bridges vary wildly in how smartctl must address the drive behind
          # them (WD My Book/Elements, Seagate Expansion, JMicron, Sunplus...).
          # Try device-type candidates until one returns REAL data (a temperature,
          # a SMART attribute table, or an overall-health verdict); the plain read
          # is last so a bridge that answers only as a SCSI enclosure doesn't win
          # over one that reaches the ATA drive. -H adds the health self-assessment.
          j=""
          for dt in "sat,auto" "sat" "usbjmicron" "usbjmicron,x" "usbsunplus" "usbprolific" "scsi" ""; do
            darg=""; [ -n "$dt" ] && darg="-d $dt"
            # ⚠️ BOUNDED. A drive that has stopped answering (D1 on 2026-08-27:
            # "Not Ready / Logical unit is in process of becoming ready",
            # commands ageing out at 361s) makes smartctl block for minutes, and
            # this loop tries EIGHT device-type candidates per drive. Runs were
            # already taking 3.5 min against a 5-minute timer, so one sick drive
            # is enough to make runs overlap and stack.
            #
            # `timeout` cannot kill a process wedged in uninterruptible D state,
            # and is not meant to here — it releases the SCRIPT, which is what
            # keeps the exporter's runtime bounded. A leftover D-state smartctl
            # clears when the kernel gives up on the device.
            # shellcheck disable=SC2086
            cand=$(timeout 20 smartctl $nflag -j -H -A -i $darg "$dev" 2>/dev/null) || true
            if printf '%s' "''${cand:-}" | jq -e '(.temperature.current!=null) or ((.ata_smart_attributes.table // [] | length)>0) or (.smart_status.passed!=null)' >/dev/null 2>&1; then
              j="$cand"; break
            fi
          done
          [ -n "''${j:-}" ] || continue   # nothing readable (standby/unsupported) -> skip
          temp=$(printf '%s' "$j" | jq -r '.temperature.current // empty' 2>/dev/null || true)
          model=$(printf '%s' "$j" | jq -r '(.model_name // .scsi_model_name // "unknown")' 2>/dev/null || true)
          rota=$(lsblk -dno ROTA "$dev" 2>/dev/null | tr -d ' ' || true)
          tran=$(lsblk -dno TRAN "$dev" 2>/dev/null | tr -d ' ' || true)
          # Emit temp only when the drive actually reported one (some bridges give
          # health/attrs but no temperature.current — don't emit an empty value).
          [ -n "$temp" ] && printf 'gromit_drive_temp_celsius{device="%s",model="%s",bus="%s",rotational="%s"} %s\n' \
            "$name" "''${model:-unknown}" "''${tran:-unknown}" "''${rota:-0}" "$temp"

          # --- SMART health + failure attributes (same $j read) ---
          ok=$(printf '%s' "$j" | jq -r 'if .smart_status.passed==true then 1 elif .smart_status.passed==false then 0 else empty end' 2>/dev/null || true)
          [ -n "$ok" ] && printf 'gromit_drive_smart_ok{device="%s",model="%s"} %s\n' "$name" "''${model:-unknown}" "$ok"
          for pair in "5:reallocated_sectors" "197:pending_sectors" "198:offline_uncorrectable" "199:crc_errors"; do
            aid=''${pair%%:*}; mname=''${pair##*:}
            raw=$(printf '%s' "$j" | jq -r --argjson id "$aid" 'first(.ata_smart_attributes.table[]? | select(.id==$id) | .raw.value) // empty' 2>/dev/null || true)
            [ -n "$raw" ] && printf 'gromit_drive_%s{device="%s",model="%s"} %s\n' "$mname" "$name" "''${model:-unknown}" "$raw"
          done
        done
        # ⚠️ THE BUG THIS REPLACES. The block used to end on whatever the last
        # drive's last attribute test happened to be, and publishing was gated
        # on it:
        #
        #     } > "$tmp" && mv "$tmp" "$out"
        #
        # A compound block exits with the status of its LAST command. That was
        # `[ -n "$raw" ] && printf ...` for attribute 199 (crc_errors) of the
        # last device the glob iterates. USB bridges expose no ATA attribute
        # table, so for a USB drive $raw is empty, the test is false, the block
        # exits 1, and `&& mv` silently never runs.
        #
        # /dev/sd[a-z] ends at sdm — a USB drive. Only sda-sde (SATA) ever emit
        # crc_errors. So the publish had become permanently gated on a test that
        # could no longer be true, and drive_temps.prom froze at 2026-08-17
        # 05:23 while the unit kept reporting "Finished / Deactivated
        # successfully" every 5 minutes for TEN DAYS.
        #
        # Worse than no data: node_exporter kept serving the stale file, so
        # Grafana saw plausible, unchanging temperatures. A frozen sensor reads
        # as "steady and fine", so no threshold alert could ever fire.
        #
        # Two defences now. First, this `true` makes the block's exit status
        # deterministic instead of an accident of the last drive's firmware.
        true
      } > "$tmp"

      # Second, publish UNCONDITIONALLY and stamp the run. Never keep the last
      # good file on failure: a stale metric that looks live is exactly what
      # hid this for ten days. If a run collects nothing, the series must go
      # empty and the timestamp must stop advancing, so the failure is loud.
      # Count BEFORE reopening $tmp for append — reading and writing the same
      # file in one block is SC2094 and genuinely racy.
      reported=$(grep -c '^gromit_drive_temp_celsius' "$tmp" || true)
      {
        echo "# HELP gromit_drive_temps_last_run_seconds Unix time of the last completed drive-temps run. If this stops advancing the exporter is broken, even while temperatures still appear."
        echo "# TYPE gromit_drive_temps_last_run_seconds gauge"
        echo "gromit_drive_temps_last_run_seconds $(date +%s)"
        echo "# HELP gromit_drive_temps_devices_reported Drives that returned a temperature this run. 0 means the read failed, not that the drives are cool."
        echo "# TYPE gromit_drive_temps_devices_reported gauge"
        echo "gromit_drive_temps_devices_reported ''${reported:-0}"
      } >> "$tmp"

      chmod 0644 "$tmp"
      mv -f "$tmp" "$out"
      mv -f "$newstate" "$state"
    '';
  };
in
{
  systemd.tmpfiles.rules = [ "d ${textfileDir} 0755 root root - -" ];

  # Merge with monitoring.nix's node_exporter flags — point the textfile collector
  # at our dir (the collector is on by default; it just needs a directory).
  services.prometheus.exporters.node.extraFlags = [
    "--collector.textfile.directory=${textfileDir}"
  ];

  systemd.services.drive-temps = {
    description = "Export drive temperatures (smartctl) to node_exporter textfile";
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${driveTemps}/bin/drive-temps-export";
      # Backstop below the 5-minute timer interval, so a run that wedges anyway
      # fails LOUDLY as a failed unit instead of silently overlapping the next
      # one. The per-smartctl `timeout 20` should make this unreachable; it
      # exists because "should" is what let this exporter publish nothing for
      # ten days while reporting success.
      TimeoutStartSec = "4min";
    };
  };
  systemd.timers.drive-temps = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "2min";
      OnUnitActiveSec = "5min";
      Persistent = true;
    };
  };
}
