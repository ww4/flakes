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
#
# Outbound email (SendGrid SMTP) is configured below. The non-secret SMTP_*
# settings live in `config`; the API key is the only secret and goes in the same
# env file as one line: SMTP_PASSWORD=<SendGrid Mail-Send API key>. See the note
# at the SMTP block for the SendGrid-verified-sender requirement and sequencing.
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

      # --- Outbound email via SendGrid (SMTP) ---
      # Enables: org/user invite emails, new-device login alerts, email 2FA,
      # emergency access, and password-hint delivery. Outbound only (no inbound
      # port), so it does NOT change the Tailscale-only posture.
      #
      # The API key is the ONLY secret and must NOT live here (this attrset is
      # rendered into the world-readable nix store). Put it in the existing
      # environmentFile /var/lib/vaultwarden/env (root 0600) as a single line:
      #   SMTP_PASSWORD=<SendGrid "Mail Send"-scoped API key>
      # Upstream rule: once SMTP_USERNAME is set, SMTP_PASSWORD is mandatory — so
      # add that line BEFORE this merges, or vaultwarden errors on the SMTP config.
      #
      # SMTP_FROM must be a SendGrid-verified sender (a verified single sender, or
      # an address under an authenticated domain), or SendGrid rejects the mail.
      SMTP_HOST = "smtp.sendgrid.net";
      SMTP_PORT = 587;
      SMTP_SECURITY = "starttls";        # 587 = STARTTLS (use force_tls + 465 for implicit TLS)
      SMTP_USERNAME = "apikey";          # SendGrid uses the literal string "apikey"
      SMTP_FROM = "vault@rosemaryacres.com";
      SMTP_FROM_NAME = "Rosemary Acres Vault";
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
