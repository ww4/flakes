# Audiobookshelf audiobook / podcast server — fronted by nginx at
# https://abs.rosemaryacres.com (Tailscale-only A record on Cloudflare; TLS
# via Let's Encrypt DNS-01 using the shared defaults in nextcloud.nix).
{ config, lib, pkgs, ... }:

{
  services.audiobookshelf = {
    enable = true;
    group = "media";
    host = "127.0.0.1";        # nginx fronts; no direct external access
  };

  services.nginx.virtualHosts."abs.rosemaryacres.com" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:8000";
      proxyWebsockets = true;
      extraConfig = ''
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 0;
        proxy_buffering off;
      '';
    };
  };
}
