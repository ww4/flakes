# Paperless-ngx — OCR-indexed document archive.
# https://paperless.rosemaryacres.com
#
# Consume folder: /var/lib/paperless/consume  — drop a PDF/jpg here and
# paperless OCRs, dates, tags, and files it.
# Storage: /var/lib/paperless/originals + /var/lib/paperless/archive
# DB: SQLite by default; switch to postgres if you ever cross ~50k docs.
#
# Initial admin: password from sops (`secrets/paperless-admin.yaml`, migrated
# 2026-06-16); edit with `sops`. Wired via `passwordFile` below. OIDC SSO is also
# available (user_oidc via Authelia) — see the OIDC block.
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

  services.paperless = {
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
