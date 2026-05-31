# Notification infrastructure — self-hosted ntfy for backup / system alerts.
#
# ntfy runs on Gromit and is reachable only over Tailscale (never LAN/internet).
# Subscribe the ntfy phone app to this server + topic:
#   server: http://100.82.117.116:8090
#   topic:  gromit-alerts
#
# Scripts and the shell send alerts with the `gromit-notify` helper:
#   gromit-notify "<title>" "<message>" [priority] [tags]
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
    };
  };

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
  # every other rosemaryacres.com service. The /var/cloudflare-dns-api
  # already used for other ACME challenges supplies the DNS-01 token.
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
