# Lidarr — music manager ("Sonarr/Radarr for music"). Same container pattern
# as arr.nix: on arr-net, sharing the /data tree (/mnt/fusion/arr) with
# qBittorrent so imports hardlink; Prowlarr syncs indexers; qBittorrent (via
# Gluetun) is the download client.
#
# Wiring after first rebuild (done via API in the deploy, else in the UI):
#   - Root folder:     /data/media/music
#   - Download client: qBittorrent  (http://gluetun:8085, category "music")
#   - Prowlarr → Settings → Apps → add Lidarr (pushes audio indexers)
#
# Existing /mnt/fusion/Music (jellyfin-owned) is left untouched; add it as a
# second root folder later if you want Lidarr to manage that library too
# (needs a group/permission tweak — it's jellyfin:media, this runs as 1000:100).
#
# Reach at https://lidarr.rosemaryacres.com — add the Cloudflare A record
# (lidarr → 100.82.117.116) like the other *arr subdomains.
{ config, lib, pkgs, ... }:
let
  PUID = "1000"; PGID = "100"; TZ = "America/New_York";
  arrNet = "arr-net";
  port = 8686;
in
{
  systemd.tmpfiles.rules = [
    "d /var/lib/lidarr             0750 chris users - -"
    "d /mnt/fusion/arr/media/music 0775 chris users - -"
  ];

  virtualisation.oci-containers.containers.lidarr = {
    image = "ghcr.io/linuxserver/lidarr:latest";
    ports = [ "127.0.0.1:${toString port}:8686" ];
    environment = { inherit PUID PGID TZ; };
    volumes = [
      "/var/lib/lidarr:/config:rw"
      "/mnt/fusion/arr:/data:rw"
    ];
    extraOptions = [ "--network=${arrNet}" ];
  };

  systemd.services.docker-lidarr = {
    after = [ "docker-network-arr.service" ];
    requires = [ "docker-network-arr.service" ];
  };

  services.nginx.virtualHosts."lidarr.rosemaryacres.com" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:${toString port}";
      proxyWebsockets = true;
      extraConfig = ''
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
      '';
    };
  };
}
