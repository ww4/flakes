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

  services.nginx.virtualHosts."abs.rosemaryacres.com" =
    import ../lib/proxy-vhost.nix {
      port = 8000;
      extraConfig = ''
        client_max_body_size 0;
        proxy_buffering off;
      '';
    };
}
