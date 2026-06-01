# Aurral — "Jellyseerr for music": a MusicBrainz-backed discovery/request UI
# that hands one-click artist/album requests to Lidarr. The audio counterpart
# to Jellyseerr (which only does movies/TV).
#
# Single container (ghcr.io/lklynet/aurral); serves UI + API on container port
# 3001. Mapped to host 3007 (3001 is Grafana). On arr-net so it reaches
# Lidarr at lidarr:8686. Reachable at https://music.rosemaryacres.com.
#
# Secrets — NOT in git. Create before first rebuild (root 0600):
#   /var/lib/aurral/secrets.env
#     LIDARR_API_KEY=<Lidarr → Settings → General → API Key>
{ config, lib, pkgs, ... }:
let
  arrNet = "arr-net";
  hostPort = 3007;   # container listens on 3001; 3001 is taken by Grafana
in
{
  systemd.tmpfiles.rules = [
    "d /var/lib/aurral             0750 chris users - -"
    "f /var/lib/aurral/secrets.env 0600 root  root  - -"
  ];

  virtualisation.oci-containers.containers.aurral = {
    image = "ghcr.io/lklynet/aurral:latest";
    ports = [ "127.0.0.1:${toString hostPort}:3001" ];
    environment = {
      LIDARR_URL = "http://lidarr:8686";
      CONTACT_EMAIL = "admin@rosemaryacres.com";  # MusicBrainz API User-Agent contact
    };
    environmentFiles = [ "/var/lib/aurral/secrets.env" ];  # LIDARR_API_KEY
    volumes = [ "/var/lib/aurral:/app/data:rw" ];
    dependsOn = [ "lidarr" ];
    extraOptions = [ "--network=${arrNet}" ];
  };

  systemd.services.docker-aurral = {
    after = [ "docker-network-arr.service" "docker-lidarr.service" ];
    requires = [ "docker-network-arr.service" ];
  };

  services.nginx.virtualHosts."music.rosemaryacres.com" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:${toString hostPort}";
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
