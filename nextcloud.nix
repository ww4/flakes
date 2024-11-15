{ self, config, lib, pkgs, ... }:{
  security.acme = {
    acceptTerms = true;
    defaults = {
      email = "chris@saenzmail.com";
      dnsProvider = "cloudflare";
      # location of your CLOUDFLARE_DNS_API_TOKEN=[value]
      # https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#EnvironmentFile=
      environmentFile = "/var/cloudflare-dns-api";
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
      package = pkgs.nextcloud30;
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
        adminpassFile = "/var/nextcloud-admin-pass";
        adminuser = "admin";
      };
      # Suggested by Nextcloud's health check.
      phpOptions."opcache.interned_strings_buffer" = "16";
  };
  # Nightly database backups.
  postgresqlBackup = {
    enable = true;
    startAt = "*-*-* 01:15:00";
  };
 };
  systemd.services.nextcloud-setup.serviceConfig = {
    RequiresMountsFor = [ "/var/lib/nextcloud" ];
  };
}