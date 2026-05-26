# Jellyfin media server.
#
# Jellyfin keeps listening on 0.0.0.0:8096 (default) so existing
# http://100.82.117.116:8096 direct access for Roku/etc. clients still
# works. nginx adds jellyfin.rosemaryacres.com as the friendly URL,
# Tailscale-only via Cloudflare DNS-01.
{ config, lib, pkgs, ... }:

let
  jellyfinHost = "jellyfin.rosemaryacres.com";
  jellyfinPort = 8096;
in
{
  services.jellyfin = {
    enable = true;
    group = "media";
  };

  services.nginx.virtualHosts."${jellyfinHost}" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;

    extraConfig = ''
      # Allow big uploads (image sync, large transcoded segment buffers).
      client_max_body_size 20M;
      add_header X-Content-Type-Options "nosniff" always;
    '';

    locations."/" = {
      proxyPass = "http://127.0.0.1:${toString jellyfinPort}";
      proxyWebsockets = true;  # required for Jellyfin's now-playing + websocket transport
      extraConfig = ''
        # Streaming: disable response buffering so playback starts immediately
        # and big segment ranges don't hog nginx memory.
        proxy_buffering off;
        proxy_request_buffering off;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Protocol $scheme;
        proxy_set_header X-Forwarded-Host $http_host;
      '';
    };

    # Dedicated websocket path Jellyfin uses for control + push events.
    locations."/socket" = {
      proxyPass = "http://127.0.0.1:${toString jellyfinPort}";
      proxyWebsockets = true;
      extraConfig = ''
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
      '';
    };
  };
}
