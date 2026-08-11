# qbit-vpn-watchdog — self-heal the gluetun-IP-change qBittorrent wedge.
#
# THE WEDGE ([[qbit-gluetun-vpn-restart-wedge]]): qBittorrent runs in gluetun's
# network namespace (--network=container:gluetun, no interface binding of its
# own). When gluetun's WireGuard tunnel drops and reconnects onto a NEW public
# IP — e.g. a DNS-timeout restart storm, as on 2026-08-10 18:06-18:08 UTC —
# qBit keeps trying to send over the now-dead tun0. Every send fails EPERM, so
# connection_status flips to "firewalled", DHT drops to 0, and it silently stops
# seeding while the container still shows "Up" and gluetun still shows healthy.
# It does NOT self-recover; the only fix is restarting qBit (NOT gluetun, which
# is fine — restarting gluetun just re-wedges qBit) so it rebinds to the live
# tun0. Until now that cost a sentinel page and a manual restart every time.
#
# THE FIX, deliberately narrow to avoid storm-amplification: don't restart qBit
# on every gluetun restart (a storm would restart qBit 15x and risk its own
# stale-QLockFile trouble). Instead poll once a minute and act only when the
# wedge is actually present AND gluetun has SETTLED:
#   - gluetun's current public IP = the newest "[ip getter] Public IP address is
#     X" line in its journald stream; the line's embedded RFC3339 timestamp tells
#     us how long that IP has held (the settle signal — no cross-poll state).
#   - qBit's bound IP + health = /api/v2/transfer/info (localhost auth bypass).
#   - Trigger = qBit bound to an IP != gluetun's current one AND connection_status
#     != "connected" AND that IP has held >= SETTLE seconds. Both-bad is the wedge;
#     the settle gate keeps us out of a storm; a cooldown bounds any failed retry.
# Fail-safe throughout: any unknown (no IP line, qBit API down, address not yet
# determined) => do nothing. Runs as root so it can restart the unit directly.
{ pkgs, ... }:
let
  gromit-notify = import ./notify-pkg.nix { inherit pkgs; };

  watchdog = pkgs.writeShellApplication {
    name = "qbit-vpn-watchdog";
    runtimeInputs = [ pkgs.curl pkgs.jq pkgs.gnugrep pkgs.gawk pkgs.coreutils pkgs.systemd gromit-notify ];
    text = ''
      SETTLE=120     # gluetun's IP must have held this long before we act
      COOLDOWN=300   # min seconds between our own restarts (bounds a failed retry)
      QBIT="http://127.0.0.1:8085"
      STATE="''${STATE_DIRECTORY:-/var/lib/qbit-vpn-watchdog}"
      LAST="$STATE/last-restart-epoch"
      now="$(date +%s)"

      # 1) gluetun's current public IP + when it was last (re)established. gluetun
      #    logs to journald (docker journald driver); its own line carries an
      #    RFC3339 timestamp as field 1.
      line="$(journalctl CONTAINER_NAME=gluetun --since -12h --no-pager 2>/dev/null \
                | grep 'Public IP address is' | tail -1 || true)"
      [ -n "$line" ] || { echo "no gluetun public-IP line in window; skip"; exit 0; }
      g_ip="$(grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' <<<"$line" | head -1)"
      g_ts="$(awk '{for(i=1;i<=NF;i++) if($i ~ /^20[0-9-]+T[0-9:]+Z?$/){print $i; exit}}' <<<"$line")"
      g_epoch="$(date -d "$g_ts" +%s 2>/dev/null || echo 0)"
      { [ -n "$g_ip" ] && [ "$g_epoch" -gt 0 ]; } || { echo "unparseable gluetun IP/ts; skip"; exit 0; }

      age=$(( now - g_epoch ))
      if [ "$age" -lt "$SETTLE" ]; then
        echo "gluetun IP $g_ip only ''${age}s old (<''${SETTLE}s settle) — gluetun may still be flapping; waiting"
        exit 0
      fi

      # 2) qBit's bound external IP + connection health.
      info="$(curl -s --max-time 5 "$QBIT/api/v2/transfer/info" 2>/dev/null || true)"
      [ -n "$info" ] || { echo "qBit API unreachable (likely mid-restart); skip"; exit 0; }
      q_ip="$(jq -r '.last_external_address_v4 // ""' <<<"$info" 2>/dev/null || echo "")"
      q_status="$(jq -r '.connection_status // ""' <<<"$info" 2>/dev/null || echo "")"
      # qBit hasn't determined its external address yet => it's still coming up; don't act.
      { [ -n "$q_ip" ] && [ "$q_ip" != "null" ]; } || { echo "qBit external addr not set yet; skip"; exit 0; }

      # 3) The wedge = bound to the WRONG IP *and* not connected. Either alone is
      #    benign (a transient status blip, or a harmless IP-string difference).
      if [ "$q_ip" = "$g_ip" ] || [ "$q_status" = "connected" ]; then
        echo "healthy: qBit ip=$q_ip gluetun=$g_ip status=$q_status — no wedge"
        exit 0
      fi

      last=0; [ -r "$LAST" ] && last="$(cat "$LAST" 2>/dev/null || echo 0)"
      if [ $(( now - last )) -lt "$COOLDOWN" ]; then
        echo "wedge ($q_ip != $g_ip, status=$q_status) but within ''${COOLDOWN}s cooldown; skip"
        exit 0
      fi

      echo "WEDGE: qBit bound to $q_ip but gluetun settled on $g_ip ''${age}s ago (status=$q_status) — restarting qBittorrent"
      echo "$now" > "$LAST"
      systemctl restart docker-qbittorrent.service
      gromit-notify "qBittorrent auto-healed (VPN IP change)" \
        "gluetun moved to $g_ip; qBit was stuck on $q_ip ($q_status) and stopped seeding. Restarted it to rebind — recovered without a page." \
        "low" "vpn,qbit" || true
    '';
  };
in
{
  systemd.services.qbit-vpn-watchdog = {
    description = "Self-heal the gluetun-IP-change qBittorrent netns wedge";
    after = [ "docker-gluetun.service" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${watchdog}/bin/qbit-vpn-watchdog";
      StateDirectory = "qbit-vpn-watchdog";
    };
  };
  systemd.timers.qbit-vpn-watchdog = {
    description = "Poll every 60s for the gluetun-IP-change qBittorrent wedge";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "3min";
      OnUnitActiveSec = "60s";
    };
  };
}
