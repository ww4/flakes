# Notification infrastructure — self-hosted ntfy for backup / system alerts.
#
# ntfy runs on Gromit and is reachable only over Tailscale (never LAN/internet).
# Subscribe the ntfy phone app to this server + topic:
#   server: http://100.82.117.116:8090
#   topic:  gromit-alerts
#
# Scripts and the shell send alerts with the `gromit-notify` helper:
#   gromit-notify "<title>" "<message>" [priority] [tags] [click-url]
# A click-url makes the whole notification tappable (see daily-reminders.nix).
{ config, lib, pkgs, ... }:

let
  gromit-notify = import ./notify-pkg.nix { inherit pkgs; };
in
{
  services.ntfy-sh = {
    enable = true;
    settings = {
      base-url = "http://100.82.117.116:8090";
      listen-http = ":8090";
      # ⚠️ SECURITY (2026-08-19). ntfy's default for this is `read-write`, which
      # means ANONYMOUS clients may both subscribe and publish. Verified live:
      # an unauthenticated GET of /gromit-alerts/json returned 200 with full
      # message bodies, and an unauthenticated POST was accepted.
      #
      # Port 8090 is firewalled off the LAN interface, but nginx proxies ntfy at
      # ntfy.rosemaryacres.com and its source gate allows the whole LAN — so any
      # device on the network could read every alert (sentinel diagnoses, unit
      # failures, device names and addresses) and inject fake ones.
      #
      # `write-only` = anonymous may PUBLISH but not SUBSCRIBE. Chosen over
      # `deny-all` deliberately: every local publisher (sentinel, media-mirror,
      # alertmanager-ntfy, netwatch, the daybook) posts anonymously to loopback
      # and keeps working untouched, while reading now requires an account.
      #
      # This also closes a hole in netwatch's notification action buttons: their
      # single-use nonce travels IN the message, so a reader of the topic could
      # press Accept on the alert about itself and silence its own detection.
      auth-default-access = "write-only";
    };
  };

  # The subscriber account, provisioned idempotently. Anonymous read is now
  # denied, so the phone (and the homepage widget) need credentials.
  #
  # The password is GENERATED here and written to a root-only file rather than
  # being asked for or echoed anywhere: it never passes through a chat, a commit
  # or the nix store. Read it once with:
  #     sudo cat /var/lib/ntfy-sh/subscriber-password.txt
  systemd.services.ntfy-provision = {
    description = "Provision the ntfy subscriber account (idempotent)";
    wantedBy = [ "multi-user.target" ];
    after = [ "ntfy-sh.service" ];
    path = [ pkgs.ntfy-sh pkgs.coreutils pkgs.gnugrep ];
    serviceConfig = { Type = "oneshot"; RemainAfterExit = true; };
    script = ''
      pwfile=/var/lib/ntfy-sh/subscriber-password.txt
      envfile=/var/lib/ntfy-sh/homepage-ntfy.env

      # ⚠️ NO --config FLAG EXISTS on `ntfy user`/`ntfy access`. Passing one
      # made the first version of this unit die immediately after writing the
      # password file — so the password existed, the ACCOUNT never did, and the
      # phone got "user chris not authorized" while notifications were already
      # locked down. These are server-side commands: they read the default
      # /etc/ntfy/server.yml and operate on its auth-file directly.

      # Reuse an existing password rather than minting a new one, so a re-run
      # never invalidates a password already typed into the phone.
      if [ -s "$pwfile" ]; then
        pw=$(cat "$pwfile")
      else
        pw=$(head -c 32 /dev/urandom | base64 | tr -dc "A-Za-z0-9" | head -c 24)
        umask 077
        printf "%s\n" "$pw" > "$pwfile"
      fi

      # ntfy creates user.db on first start; wait rather than race it.
      for _ in $(seq 1 30); do
        ntfy user list >/dev/null 2>&1 && break
        sleep 2
      done

      # Add, or if the account already exists, force its password to match the
      # file. That makes the file authoritative and the unit self-healing,
      # instead of depending on parsing `user list` output.
      if ! NTFY_PASSWORD="$pw" ntfy user add chris 2>/dev/null; then
        NTFY_PASSWORD="$pw" ntfy user change-pass chris
      fi
      ntfy access chris "gromit-alerts" rw

      printf "HOMEPAGE_VAR_NTFY_USER=chris\nHOMEPAGE_VAR_NTFY_PASS=%s\n" "$pw" > "$envfile"
      chmod 600 "$pwfile"
      chmod 640 "$envfile"
      echo "ntfy-provision: subscriber 'chris' ready. Password: sudo cat $pwfile"
    '';
  };

  # Guarantee the env file exists before the homepage container starts, so a
  # first boot cannot fail on a missing EnvironmentFile.
  systemd.tmpfiles.rules = [
    "f /var/lib/ntfy-sh/homepage-ntfy.env 0640 root root -"
  ];

  # Reachable only over the Tailscale interface and Docker bridges. The
  # extraCommands rule covers user-defined networks like arr-net (which
  # get auto-named bridges br-<id>) — the homepage container lives there.
  networking.firewall.interfaces."tailscale0".allowedTCPPorts = [ 8090 ];
  networking.firewall.interfaces."docker0".allowedTCPPorts   = [ 8090 ];
  networking.firewall.extraCommands = ''
    iptables -I nixos-fw 1 -i br-+ -p tcp --dport 8090 -j nixos-fw-accept
  '';

  # nginx vhost so anything reaching for ntfy can use the familiar
  # https://ntfy.rosemaryacres.com pattern with a real cert, matching
  # every other rosemaryacres.com service. The sops-managed Cloudflare
  # token used for the other ACME DNS-01 challenges supplies this one too.
  services.nginx.virtualHosts."ntfy.rosemaryacres.com" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:8090";
      recommendedProxySettings = true;
      extraConfig = ''
        proxy_buffering off;
        proxy_read_timeout 1h;     # ntfy event-stream connections are long-lived
      '';
    };
  };

  environment.systemPackages = [ gromit-notify ];

  # Failure alerting is now centralised in Grafana Alerting (see
  # monitoring.nix): node_exporter's systemd collector publishes
  # node_systemd_unit_state{state="failed"}, a Grafana rule fires on it, and
  # the notification policy routes to ntfy with a 22:00–07:00 mute timing so
  # overnight failures hold until morning. The old per-unit
  # `notify-failure@%N.service` template (which paged at `urgent` priority
  # around the clock) has been removed in favour of that single path.
  #
  # `gromit-notify` itself stays available for ad-hoc/manual alerts.
}
