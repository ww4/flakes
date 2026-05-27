# Vaultwarden — Rust re-implementation of the Bitwarden server.
#
# Tiny (~10 MB resident), single SQLite DB at /var/lib/bitwarden_rs.
# Reachable at https://vault.rosemaryacres.com (DNS only resolves to the
# Tailscale IP — same posture as the other rosemaryacres.com vhosts).
#
# Admin panel: /admin — requires ADMIN_TOKEN from /var/lib/vaultwarden/env.
# Generate that token once with `openssl rand -base64 48` and store in:
#   /var/lib/vaultwarden/env   (root 0600)
# with format:
#   ADMIN_TOKEN=<argon2-hashed-token>
# Use `vaultwarden hash` to compute the argon2 hash from a plaintext token
# before placing into env; that keeps the on-disk version one-way-hashed.
{ config, lib, pkgs, ... }:

{
  services.vaultwarden = {
    enable = true;
    dbBackend = "sqlite";
    environmentFile = "/var/lib/vaultwarden/env";   # holds ADMIN_TOKEN + SMTP creds
    config = {
      DOMAIN = "https://vault.rosemaryacres.com";
      ROCKET_ADDRESS = "127.0.0.1";
      ROCKET_PORT = 8222;
      # WebSockets for live-sync between clients (deprecated in upstream;
      # default-disabled but harmless to leave configured).
      WEBSOCKET_ENABLED = false;
      # Block public signups — invite-only via admin panel.
      SIGNUPS_ALLOWED = false;
      INVITATIONS_ALLOWED = true;
      # Send the favicon at the right place.
      SHOW_PASSWORD_HINT = false;
    };
  };

  services.nginx.virtualHosts."vault.rosemaryacres.com" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:8222";
      recommendedProxySettings = true;
      extraConfig = ''
        client_max_body_size 525M;       # for attachments
      '';
    };
  };
}
