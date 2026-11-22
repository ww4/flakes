# MINISPLIT — local control of the Turbro mini split (Tuya OEM) from gromit.
#
#   minisplit status | on | off | temp 72 | mode heat|cool|dry|fan|auto
#   sudo minisplit-cloud status | on [MINUTES] | off
#
# The device ID + local key were fetched ONCE (2026-11-22) by a Smart Life QR
# login and live in ~claude/.config/minisplit/tuya-devices.json, 0600. Not in
# sops: the key is per-device and re-issued on every re-pair, so it is state,
# not configuration. Only `claude` can read it, so the CLI and the homelab-mcp
# tools (which run as claude) work; other users go through those.
#
# ── CLOUD ACCESS IS OFF BY DEFAULT ─────────────────────────────────────────
# Chris, 2026-11-22: keep the AIR.ai / Smart Life apps installed as a backup,
# "but we control the access". The unit is gated BY MAC in kindle-fwd (see
# kindle-segment.nix): its traffic jumps to the `minisplit-cloud` chain, which
# is EMPTY unless opened, and then hits a LOG+DROP. Every firewall reload or
# reboot rebuilds it empty, so the failure direction is "no cloud".
#
# `minisplit-cloud on` opens it for a bounded time (default 60 min) and
# schedules its own close; `off` closes it and kills the unit's open flows, which
# otherwise ride the ESTABLISHED rule at the top of kindle-fwd indefinitely.
# Local control from gromit doesn't depend on any of this.
{ config, lib, pkgs, ... }:

let
  site = import ../../pkgs/minisplit/site.nix;
  uplink = "enp3s0";
  nonPublic = [ "10.0.0.0/8" "172.16.0.0/12" "192.168.0.0/16" "100.64.0.0/10" "169.254.0.0/16" ];

  raw = pkgs.callPackage ../../pkgs/minisplit { };
  minisplit = pkgs.writeShellScriptBin "minisplit" ''
    export MINISPLIT_HOST="''${MINISPLIT_HOST:-${site.ip}}"
    export MINISPLIT_PROTOCOL="''${MINISPLIT_PROTOCOL:-${site.protocol}}"
    exec ${raw}/bin/minisplit "$@"
  '';

  cloud = pkgs.writeShellApplication {
    name = "minisplit-cloud";
    runtimeInputs = with pkgs; [ iptables conntrack-tools systemd coreutils gnugrep ];
    text = ''
      chain=minisplit-cloud
      timer=minisplit-cloud-close
      [ "$(id -u)" -eq 0 ] || { echo "minisplit-cloud: run with sudo" >&2; exit 1; }
      iptables -n -L "$chain" >/dev/null 2>&1 || {
        echo "minisplit-cloud: chain $chain missing (is the kindle segment firewall loaded?)" >&2; exit 1; }

      close() {
        systemctl stop "$timer.timer" "$timer.service" 2>/dev/null || true
        iptables -F "$chain"
        # Established flows would otherwise survive the flush (conntrack
        # ACCEPT sits first in kindle-fwd). Exit 1 = "no entries", fine.
        conntrack -D -s ${site.ip} >/dev/null 2>&1 || true
        conntrack -D -d ${site.ip} >/dev/null 2>&1 || true
      }

      case "''${1:-status}" in
        on)
          mins="''${2:-60}"
          [[ "$mins" =~ ^[0-9]+$ ]] && [ "$mins" -ge 1 ] && [ "$mins" -le 1440 ] || {
            echo "minisplit-cloud: minutes must be 1-1440" >&2; exit 1; }
          close
          ${lib.concatMapStringsSep "\n          " (d: "iptables -A \"$chain\" -d ${d} -j RETURN") nonPublic}
          iptables -A "$chain" -o ${uplink} -j ACCEPT
          systemd-run --quiet --unit="$timer" --on-active="''${mins}m" \
            --description="close mini split cloud access" /run/current-system/sw/bin/minisplit-cloud off
          echo "cloud access OPEN for $mins min (closes $(date -d "+$mins min" '+%-I:%M %p'))"
          ;;
        off)
          close
          echo "cloud access CLOSED"
          ;;
        status)
          if iptables -S "$chain" | grep -q -- '-j ACCEPT'; then
            left=$(systemctl list-timers "$timer.timer" --no-legend 2>/dev/null | awk '{print $1, $2, $3}')
            echo "cloud access: OPEN''${left:+ (closes $left)}"
          else
            echo "cloud access: CLOSED"
          fi
          iptables -L kindle-fwd -v -n | grep -i -E "minisplit|MAC ${site.mac}" || true
          ;;
        *)
          echo "usage: minisplit-cloud [status|on [MINUTES]|off]" >&2; exit 1 ;;
      esac
    '';
  };
in
{
  environment.systemPackages = [ minisplit cloud ];
}
