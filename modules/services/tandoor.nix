# Tandoor Recipes — fronted by nginx at https://recipes.rosemaryacres.com
# (Tailscale-only A record on Cloudflare; TLS via Let's Encrypt DNS-01).
{ config, lib, pkgs, ... }:

{
  services.tandoor-recipes = {
    enable = true;
    address = "127.0.0.1";              # nginx fronts; no direct external access
    extraConfig = {
      ALLOWED_HOSTS = "recipes.rosemaryacres.com,localhost";
      CSRF_TRUSTED_ORIGINS = "https://recipes.rosemaryacres.com";
    };
  };

  # Public-facing reverse proxy. ACME via the existing Cloudflare DNS-01
  # defaults in nextcloud.nix.
  services.nginx.virtualHosts."recipes.rosemaryacres.com" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:8080";
      proxyWebsockets = true;
      extraConfig = ''
        client_max_body_size 512M;
        proxy_buffering off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
      '';
    };
  };
}
