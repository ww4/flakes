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
  ntfyPort = 8090;
  ntfyTopic = "gromit-alerts";

  # Thin wrapper around curl -> the local ntfy instance.
  gromit-notify = pkgs.writeShellApplication {
    name = "gromit-notify";
    runtimeInputs = [ pkgs.curl ];
    text = ''
      # Usage: gromit-notify <title> <message> [priority] [tags]
      #   priority: min | low | default | high | urgent
      #   tags:     comma-separated ntfy tags/emoji (e.g. warning,floppy_disk)
      title=''${1:?usage: gromit-notify <title> <message> [priority] [tags]}
      message=''${2:?usage: gromit-notify <title> <message> [priority] [tags]}
      priority=''${3:-default}
      tags=''${4:-}

      args=( -fsS --max-time 15
             -H "Title: $title"
             -H "Priority: $priority" )
      if [ -n "$tags" ]; then
        args+=( -H "Tags: $tags" )
      fi
      curl "''${args[@]}" -d "$message" \
        "http://localhost:${toString ntfyPort}/${ntfyTopic}" > /dev/null
    '';
  };
in
{
  services.ntfy-sh = {
    enable = true;
    settings = {
      base-url = "http://100.82.117.116:${toString ntfyPort}";
      listen-http = ":${toString ntfyPort}";
    };
  };

  # Reachable only over the Tailscale interface — not the LAN, not the internet.
  networking.firewall.interfaces."tailscale0".allowedTCPPorts = [ ntfyPort ];

  environment.systemPackages = [ gromit-notify ];
}
