# Vaultwarden — Rust re-implementation of the Bitwarden server.
#
# Tiny (~10 MB resident), single SQLite DB at /var/lib/bitwarden_rs.
# Reachable at https://keys.rosemaryacres.com (DNS only resolves to the
# Tailscale IP — same posture as the other rosemaryacres.com vhosts).
# (Renamed from vault.* — Chrome Safe Browsing kept flagging the "vault" name.)
#
# Admin panel: /admin — requires ADMIN_TOKEN. The ADMIN_TOKEN + SMTP creds now
# live in sops (`secrets/vaultwarden-env.yaml`, migrated 2026-06-16); edit with
# `sops`. They decrypt to /run/secrets at activation and are passed via the
# `environmentFile` below. (Token is a one-way `vaultwarden hash` argon2 value;
# Postmark uses its Server API token as BOTH SMTP_USERNAME and SMTP_PASSWORD.)
# See the note at the SMTP block for the verified-sender requirement and sequencing.
{ config, lib, pkgs, ... }:

{
  # ADMIN_TOKEN + SMTP creds via sops (migrated 2026-06-16). systemd reads the
  # environmentFile as root before dropping to the vaultwarden user → root:0400.
  sops.secrets."vaultwarden-env" = {
    sopsFile = ../../secrets/vaultwarden-env.yaml;
    key = "vaultwarden-env";
  };

  services.vaultwarden = {
    enable = true;
    dbBackend = "sqlite";
    environmentFile = config.sops.secrets."vaultwarden-env".path;   # ADMIN_TOKEN + SMTP creds
    config = {
      DOMAIN = "https://keys.rosemaryacres.com";
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

      # --- Outbound email via Postmark (SMTP) ---
      # Enables: org/user invite emails, new-device login alerts, email 2FA,
      # emergency access, and password-hint delivery. Outbound only (no inbound
      # port), so it does NOT change the Tailscale-only posture.
      #
      # Postmark authenticates with the server's API token used as BOTH the SMTP
      # username AND password — so BOTH are secrets and must NOT live here (this
      # attrset renders into the world-readable nix store). Put both lines in the
      # existing environmentFile /var/lib/vaultwarden/env (root 0600):
      #   SMTP_USERNAME=<Postmark Server API token>
      #   SMTP_PASSWORD=<same Postmark Server API token>
      # Upstream rule: once SMTP_USERNAME is set, SMTP_PASSWORD is mandatory — add
      # both BEFORE this merges, or vaultwarden errors on the SMTP config.
      #
      # SMTP_FROM must be a Postmark-verified sender: a confirmed Sender Signature
      # for that exact address, or an address under a domain with verified DKIM /
      # Return-Path. Host is the transactional stream (smtp.postmarkapp.com);
      # broadcasts use smtp-broadcasts.postmarkapp.com — not what we want here.
      SMTP_HOST = "smtp.postmarkapp.com";
      SMTP_PORT = 587;
      SMTP_SECURITY = "starttls";        # 587 = STARTTLS (use force_tls + 465 for implicit TLS)
      SMTP_FROM = "vault@rosemaryacres.com";
      SMTP_FROM_NAME = "Rosemary Acres Vault";
    };
  };

  services.nginx.virtualHosts."keys.rosemaryacres.com" = {
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
