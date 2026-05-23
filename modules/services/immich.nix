# Immich — self-hosted photo & video management.
#
# Fronted by nginx at https://photos.rosemaryacres.com (Tailscale-only A
# record on Cloudflare; TLS via Let's Encrypt DNS-01 using the same defaults
# Nextcloud uses). Immich itself binds 127.0.0.1 only.
{ config, lib, pkgs, ... }:

{
  services.immich = {
    enable = true;
    host = "127.0.0.1";                # nginx fronts; no direct external access
    port = 2283;
    mediaLocation = "/mnt/fusion/immich";
  };

  # Trust the local nginx so Immich honors X-Forwarded-Proto and generates
  # https:// URLs in the UI / share links.
  systemd.services.immich-server.environment.IMMICH_TRUSTED_PROXIES = "127.0.0.1";

  # Public-facing reverse proxy. ACME via the existing Cloudflare DNS-01
  # defaults in nextcloud.nix.
  services.nginx.virtualHosts."photos.rosemaryacres.com" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:2283";
      proxyWebsockets = true;
      extraConfig = ''
        client_max_body_size 50000M;
        proxy_buffering off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
      '';
    };
  };
}
