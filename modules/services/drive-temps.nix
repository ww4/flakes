# Drive temperature monitoring for gromit's many spinning disks (SATA + USB pool).
#
# node_exporter's hwmon collector already covers the CPU + NVMe, but the SATA and
# USB HDDs have no hwmon temp. This adds a tiny smartctl-based exporter that writes
# per-drive temps to node_exporter's textfile collector every few minutes.
#
# Key choice: `smartctl -n standby` so a spun-down drive is NOT woken just to read
# its temp (the backup pool sleeps between weekly syncs; an idle drive makes ~no
# heat, so there's nothing to monitor anyway). Drives whose USB bridge can't report
# power state may still get read — acceptable.
{ config, lib, pkgs, ... }:
let
  textfileDir = "/var/lib/node-exporter-textfile";
  driveTemps = pkgs.writeShellApplication {
    name = "drive-temps-export";
    runtimeInputs = with pkgs; [ smartmontools jq gawk coreutils util-linux ];
    text = ''
      out="${textfileDir}/drive_temps.prom"
      tmp="$out.$$"
      {
        echo "# HELP gromit_drive_temp_celsius Drive temperature C (smartctl; standby drives omitted)."
        echo "# TYPE gromit_drive_temp_celsius gauge"
        for dev in /dev/sd[a-z]; do
          [ -b "$dev" ] || continue
          name=$(basename "$dev")
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
