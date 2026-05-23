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
    # Serve uploaded recipe images straight from disk; Tandoor's catch-all
    # otherwise sends anonymous /media/* requests to the login redirect.
    # Deny *.sqlite3 because MEDIA_ROOT shares the dir with db.sqlite3.
    locations."/media/" = {
      alias = "/var/lib/tandoor-recipes/";
      extraConfig = ''
        location ~ \.(sqlite|sqlite3|db)$ { deny all; }
        expires 7d;
        access_log off;
      '';
    };
    locations."/" = {
      proxyPass = "http://127.0.0.1:8080";
      proxyWebsockets = true;
      # Pass the real Host so Django's ALLOWED_HOSTS check sees
      # recipes.rosemaryacres.com instead of 127.0.0.1 (would 400).
      extraConfig = ''
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;
        client_max_body_size 512M;
        proxy_buffering off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
      '';
    };
  };
}
