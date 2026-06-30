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
      tmp="$out.$$"
      # Per-device cumulative-I/O snapshot — used to tell whether a backup drive
      # is actively spinning before we risk waking it with a SMART read.
      state="${textfileDir}/.drive-iostat"
      newstate="$state.$$"
      : > "$newstate"

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
        for dev in /dev/sd[a-z]; do
          [ -b "$dev" ] || continue
          name=$(basename "$dev")
          # Total sectors transferred (read+written). SMART reads don't bump
          # these, so it's a clean activity signal (the same one hd-idle uses).
          io=$(awk -v d="$name" '$3==d {print $6+$10; f=1} END{if(!f) print 0}' /proc/diskstats)
          printf '%s %s\n' "$name" "$io" >> "$newstate"
          # Backup-pool drives: skip the SMART read unless they've done I/O since
          # the last run — otherwise a read would wake a sleeping drive.
          case " $backup_devs " in
            *" $name "*)
              prev=$(awk -v d="$name" '$1==d{print $2}' "$state" 2>/dev/null || true)
              if [ -z "''${prev:-}" ] || [ "$io" = "''${prev:-}" ]; then
                continue
              fi
              ;;
          esac
          # USB bridges usually need -d sat,auto; direct SATA accepts it too.
          j=$(smartctl -n standby -j -A -i -d sat,auto "$dev" 2>/dev/null) || true
          if [ -z "''${j:-}" ] || [ -z "$(printf '%s' "$j" | jq -r '.temperature.current // empty' 2>/dev/null)" ]; then
            j=$(smartctl -n standby -j -A -i "$dev" 2>/dev/null) || true
          fi
          temp=$(printf '%s' "''${j:-}" | jq -r '.temperature.current // empty' 2>/dev/null || true)
          [ -n "$temp" ] || continue   # no temp = standby/unsupported -> skip (don't wake)
          model=$(printf '%s' "$j" | jq -r '(.model_name // .scsi_model_name // "unknown")' 2>/dev/null || true)
          rota=$(lsblk -dno ROTA "$dev" 2>/dev/null | tr -d ' ' || true)
          tran=$(lsblk -dno TRAN "$dev" 2>/dev/null | tr -d ' ' || true)
          printf 'gromit_drive_temp_celsius{device="%s",model="%s",bus="%s",rotational="%s"} %s\n' \
            "$name" "''${model:-unknown}" "''${tran:-unknown}" "''${rota:-0}" "$temp"
        done
      } > "$tmp" && mv "$tmp" "$out"
      mv "$newstate" "$state"
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
