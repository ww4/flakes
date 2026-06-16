# Paperless-ngx — OCR-indexed document archive.
# https://paperless.rosemaryacres.com
#
# Consume folder: /var/lib/paperless/consume  — drop a PDF/jpg here and
# paperless OCRs, dates, tags, and files it.
# Storage: /var/lib/paperless/originals + /var/lib/paperless/archive
# DB: SQLite by default; switch to postgres if you ever cross ~50k docs.
#
# Initial admin: password read from /var/lib/paperless/admin-password
# (root 0600). Generate with `openssl rand -base64 24 > .../admin-password`.
{ config, lib, pkgs, ... }:

{
  # Paperless admin password via sops (migrated 2026-06-16). owner=paperless so
  # the paperless service user can read it directly (the module reads passwordFile
  # to set the Django superuser password).
  sops.secrets."paperless-admin" = {
    sopsFile = ../../secrets/paperless-admin.yaml;
    key = "paperless-admin";
    owner = "paperless";
  };

  # OIDC SSO via Authelia (Phase 2). The secret-bearing PAPERLESS_SOCIALACCOUNT_
  # PROVIDERS JSON (it embeds the client secret) goes in an environmentFile, NOT
  # in `settings` — settings render into the world-readable systemd unit. This
  # file is read by systemd (root) AND sourced by the paperless-manage wrapper as
  # the paperless user → owner=paperless. Matching client hash in authelia.nix.
  sops.secrets."paperless-oidc-env" = {
    sopsFile = ../../secrets/paperless-oidc-env.yaml;
    key = "paperless-oidc-env";
    owner = "paperless";
  };

  services.paperless = {
    environmentFile = config.sops.secrets."paperless-oidc-env".path;
    enable = true;
    address = "127.0.0.1";
    port = 28981;
    passwordFile = config.sops.secrets."paperless-admin".path;
    consumptionDir = "/var/lib/paperless/consume";
    consumptionDirIsPublic = false;
    mediaDir = "/var/lib/paperless/media";       # holds originals + archives
    dataDir = "/var/lib/paperless/data";
    settings = {
      # Where attached/scanned originals go after consumption — kept
      # explicitly under /var/lib/paperless so tier-1 backup catches it.
      PAPERLESS_URL = "https://paperless.rosemaryacres.com";
      PAPERLESS_OCR_LANGUAGE = "eng";
      PAPERLESS_OCR_MODE = "skip";               # only OCR files that need it
      PAPERLESS_TIME_ZONE = "America/New_York";
      PAPERLESS_CONSUMER_POLLING = 60;           # seconds; inotify is unreliable on NFS/mergerfs
      PAPERLESS_FILENAME_FORMAT = "{created_year}/{correspondent}/{title}";

      # --- OIDC SSO via Authelia (Phase 2) — non-secret settings. ---
      # The provider JSON (with the client secret) is in the environmentFile
      # above. Regular admin login stays enabled (not disabled) as a fallback.
      PAPERLESS_APPS = "allauth.socialaccount.providers.openid_connect";
      PAPERLESS_SOCIAL_AUTO_SIGNUP = true;             # skip the intermediate signup form
      PAPERLESS_SOCIALACCOUNT_ALLOW_SIGNUPS = true;    # allow first-time SSO users
    };
  };

  services.nginx.virtualHosts."paperless.rosemaryacres.com" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:28981";
      recommendedProxySettings = true;
      extraConfig = ''
        client_max_body_size 200M;     # for big scans
      '';
    };
  };
}
