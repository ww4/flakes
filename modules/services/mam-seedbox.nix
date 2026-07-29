# mam-seedbox — keep MyAnonaMouse's "dynamic seedbox" IP registration in sync
# with our rotating AirVPN exit IP.
#
# WHY: qBittorrent exits on the AirVPN IP, but Chris browses MAM from home — two
# different IPs. MAM's rule for that case is "authorize your client via the
# Dynamic Seedbox API." We deliberately do NOT pin an AirVPN server (rotating
# servers is a privacy benefit — a static exit IP builds a stronger long-term
# audit trail; Chris 2026-07-28), so the exit IP changes on reconnect and must
# be re-registered when it does.
#
# HOW (traffic-polite by design — [[polite-polling-ethos]]):
#   * IP-change detection reads gluetun's OWN journal ("Public IP address is X"),
#     which it logs on every (re)connect — ZERO external requests to detect.
#   * MAM's API is hit ONLY when the IP actually changed, and at most once/hour
#     (MAM's documented rate limit). Steady state = no external calls at all.
#   * The call runs INSIDE gluetun's netns (nsenter), so MAM sees the AirVPN IP
#     as the request source — no extra container/image, uses host curl.
#
# MAM API: GET https://t.myanonamouse.net/json/dynamicSeedbox.php with a
# `mam_id` session cookie (from MAM → Preferences → Security → a session created
# with "Dynamic Seedbox: Yes"). MAM registers the request's source IP and may
# hand back a refreshed mam_id (Set-Cookie) which we persist.
#
# INERT until enabled: `services.mamSeedbox.enable = true` + the sops secret.
# Blocked on Chris creating a MAM account + dynamic session and dropping the
# cookie via /sops-add (secrets/mam-id.yaml, key `mam-id`). Response parsing is
# verified end-to-end at activation (first run with a real cookie) — see the
# PR's "after enable" notes.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.mamSeedbox;
in
{
  options.services.mamSeedbox = {
    enable = lib.mkEnableOption "MyAnonaMouse dynamic-seedbox IP updater (AirVPN exit → MAM)";
    interval = lib.mkOption {
      type = lib.types.str;
      default = "10min";
      description = "How often to check gluetun's current exit IP (local journal read; MAM is only called on a change, ≤1×/hour).";
    };
    gluetunContainer = lib.mkOption {
      type = lib.types.str;
      default = "gluetun";
      description = "Name of the gluetun container whose netns the MAM call runs in.";
    };
  };

  config = lib.mkIf cfg.enable {
    # The MAM dynamic-seedbox session cookie. Root-only; read by the updater.
    sops.secrets."mam-id" = {
      sopsFile = ../../secrets/mam-id.yaml;
      key = "mam-id";
    };

    systemd.services.mam-seedbox-update = {
      description = "Register the current AirVPN exit IP with MyAnonaMouse (dynamic seedbox)";
      after = [ "docker-${cfg.gluetunContainer}.service" ];
      # Also run right after gluetun (re)starts — a container restart is the most
      # common IP change; the timer catches in-place VPN reconnects.
      wantedBy = [ "docker-${cfg.gluetunContainer}.service" ];
      serviceConfig = {
        Type = "oneshot";
        StateDirectory = "mam-seedbox";
        # cookie seed is root:0400; the whole thing runs as root (needs docker
        # inspect + nsenter).
        LoadCredential = [ "mam-id:${config.sops.secrets."mam-id".path}" ];
      };
      path = with pkgs; [ docker util-linux curl jq gnugrep gnused coreutils systemd ];
      script = ''
        set -euo pipefail
        STATE=/var/lib/mam-seedbox
        NTFY="http://127.0.0.1:8090/gromit-alerts"

        # --- current AirVPN exit IP, from gluetun's own journal (no network) ---
        cur="$(journalctl -u docker-${cfg.gluetunContainer}.service --no-pager 2>/dev/null \
                | grep -oE 'Public IP address is [0-9]+(\.[0-9]+){3}' | tail -1 \
                | grep -oE '[0-9]+(\.[0-9]+){3}' || true)"
        if [ -z "$cur" ]; then
          echo "no gluetun public-IP line in journal yet; nothing to do"; exit 0
        fi

        last="$(cat "$STATE/last_ip" 2>/dev/null || echo "")"
        if [ "$cur" = "$last" ]; then
          echo "exit IP unchanged ($cur); no MAM call"; exit 0
        fi

        # --- respect MAM's once-per-hour limit ---
        now="$(date +%s)"
        lastcall="$(cat "$STATE/last_call_epoch" 2>/dev/null || echo 0)"
        if [ "$((now - lastcall))" -lt 3900 ]; then
          echo "IP changed to $cur but within MAM's 1h window; will retry next tick"; exit 0
        fi

        # --- cookie: persisted (refreshed) one wins over the seeded credential ---
        if [ -f "$STATE/cookie" ]; then
          cookie="$(cat "$STATE/cookie")"
        else
          cookie="$(cat "$CREDENTIALS_DIRECTORY/mam-id")"
        fi

        pid="$(docker inspect -f '{{.State.Pid}}' ${cfg.gluetunContainer} 2>/dev/null || true)"
        if [ -z "$pid" ] || [ "$pid" = "0" ]; then
          echo "gluetun not running; skip"; exit 0
        fi

        # --- the one external call: from INSIDE gluetun's netns so MAM sees the AirVPN IP ---
        hdr="$(mktemp)"
        body="$(nsenter -t "$pid" -n -- curl -sS --max-time 20 -D "$hdr" \
                  -b "mam_id=$cookie" \
                  https://t.myanonamouse.net/json/dynamicSeedbox.php 2>/dev/null || true)"

        ok="$(printf '%s' "$body" | jq -r '.Success // .success // empty' 2>/dev/null || true)"
        msg="$(printf '%s' "$body" | jq -r '.msg // .message // empty' 2>/dev/null || true)"

        # MAM returns Success:true on update, and also a benign "No change" msg.
        if [ "$ok" = "true" ] || printf '%s' "$msg" | grep -qiE 'completed|no change|success'; then
          echo "$cur" > "$STATE/last_ip"
          echo "$now" > "$STATE/last_call_epoch"
          # Persist a refreshed mam_id if MAM rotated it.
          newck="$(grep -oiE 'mam_id=[^;]+' "$hdr" 2>/dev/null | tail -1 | sed 's/^mam_id=//' || true)"
          [ -n "$newck" ] && printf '%s' "$newck" > "$STATE/cookie"
          echo "MAM seedbox IP updated to $cur (msg: ''${msg:-ok})"
        else
          # Don't advance last_ip → retry next window. Notify once per changed IP.
          if [ "$(cat "$STATE/failed_ip" 2>/dev/null || echo)" != "$cur" ]; then
            echo "$cur" > "$STATE/failed_ip"
            curl -s --max-time 10 -H "Title: MAM seedbox update failed" -H "Tags: warning" \
              -d "Couldn't register AirVPN IP $cur with MyAnonaMouse (resp: ''${body:0:200}). Client may be on an unauthorized IP until fixed." "$NTFY" >/dev/null || true
          fi
          echo "MAM update failed for $cur"; exit 0
        fi
        rm -f "$hdr"
      '';
    };

    systemd.timers.mam-seedbox-update = {
      description = "Periodic check to re-register the AirVPN exit IP with MAM on change";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "3min";
        OnUnitActiveSec = cfg.interval;
        Persistent = true;
      };
    };
  };
}
