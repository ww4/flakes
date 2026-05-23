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

  # Reachable only over the Tailscale interface — not the LAN, not the internet.
  networking.firewall.interfaces."tailscale0".allowedTCPPorts = [ 8090 ];

  environment.systemPackages = [ gromit-notify ];

  # Generic failure alerter: any unit can fire this via
  #   onFailure = [ "notify-failure@%N.service" ];
  systemd.services."notify-failure@" = {
    description = "Send an ntfy alert that %i failed";
    serviceConfig = {
      Type = "oneshot";
      ExecStart = ''${gromit-notify}/bin/gromit-notify "Gromit: %i failed" "The systemd unit %i failed. Check: journalctl -u %i" urgent rotating_light'';
    };
  };
}
