{ self, config, lib, pkgs, ... }:{
  # Cloudflare DNS-01 token for ACME, migrated to sops 2026-06-16 from the old
  # plaintext /var/cloudflare-dns-api (which was 0644 = world-readable — closing
  # that hole is the point). owner=claude/0400 because this token is DUAL-USE:
  # ACME (systemd reads environmentFile as root before dropping to the acme user,
  # so root can read it regardless of owner) AND the claude agent's own DNS
  # automation, which reads the file directly (see memory cloudflare-api-access).
  sops.secrets."cloudflare-dns-api" = {
    sopsFile = ../../secrets/cloudflare-dns-api.yaml;
    key = "cloudflare-dns-api";
    owner = "claude";
    mode = "0400";
  };
  # Nextcloud admin password — sops 2026-06-16 (was 0644 plaintext). Only read at
  # first install (Nextcloud is long since set up), but kept wired to satisfy the
  # module's required adminpassFile and to retire the plaintext.
  sops.secrets."nextcloud-admin-pass" = {
    sopsFile = ../../secrets/nextcloud-admin-pass.yaml;
    key = "nextcloud-admin-pass";
    owner = "nextcloud";
    mode = "0400";
  };

  security.acme = {
    acceptTerms = true;
    defaults = {
      email = "chris@saenzmail.com";
      dnsProvider = "cloudflare";
      # CLOUDFLARE_DNS_API_TOKEN=[value], now decrypted to /run/secrets/ by sops.
      # https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#EnvironmentFile=
      environmentFile = config.sops.secrets."cloudflare-dns-api".path;
    };
  };
 services = {
    nginx = {
      enable = true;
      virtualHosts = {
        "cloud.rosemaryacres.com" = {
  #       ## Force HTTP redirect to HTTPS
          forceSSL = true;
          enableACME = true;
          # Use DNS Challenege.
          acmeRoot = null;
        };
      };
   };
    nextcloud = {
      enable = true;
      hostName = "cloud.rosemaryacres.com";
      package = pkgs.nextcloud32;
      database.createLocally = true;
      configureRedis = true;
      maxUploadSize = "16G";
      https = true;
      autoUpdateApps.enable = true;
      extraAppsEnable = true;
      extraApps = with config.services.nextcloud.package.packages.apps; {
        # List of apps we want to install and are already packaged in
        # https://github.com/NixOS/nixpkgs/blob/master/pkgs/servers/nextcloud/packages/nextcloud-apps.json
        inherit calendar contacts notes onlyoffice tasks cookbook qownnotesapi;
      };
  #  datadir = "/mnt/fusion/nextcloud"; # Temporarily disabled to track permissions issues 
      settings = {
        overwriteProtocol = "https";
        default_phone_region = "US";
        maintenance_window_start = 2; # start at 2AM
      };
      config = {
        # Nextcloud PostegreSQL database configuration, recommended over using SQLite
        dbtype = "pgsql";
        adminpassFile = config.sops.secrets."nextcloud-admin-pass".path;
        adminuser = "admin";
      };
      # Suggested by Nextcloud's health check.
      phpOptions."opcache.interned_strings_buffer" = "16";
  };
  # Nightly database backups.
  # Dump named databases individually (pg_dump per DB) rather than pg_dumpall.
  # pg_dumpall aborts entirely if any one database fails, which silently broke
  # all backups for 16 months when the orphaned `immich` DB became undumpable.
  postgresqlBackup = {
    enable = true;
    startAt = "*-*-* 01:15:00";
    databases = [ "nextcloud" "immich" ];
  };
 };
  systemd.services.nextcloud-setup.serviceConfig = {
    RequiresMountsFor = [ "/var/lib/nextcloud" ];
  };
}