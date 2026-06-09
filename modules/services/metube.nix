# MeTube — web GUI for yt-dlp, for one-off video downloads at
# https://metube.rosemaryacres.com (Tailscale-only via the nginx source-gate).
#
# Downloads land in /mnt/fusion/youtube/metube, written group `media` and
# world-readable (UMASK 022) so Jellyfin (runs as group media) can read them.
# PinchFlat keeps doing recurring channel archiving at /mnt/fusion/pinchflat;
# a single Jellyfin "YouTube" library can include both folders.
{ config, lib, pkgs, ... }:

let
  downloadDir = "/mnt/fusion/youtube/metube";
  mediaGid = config.users.groups.media.gid;
in
{
  # Dedicated user; primary group `media` so output is readable by Jellyfin.
  users.users.metube = {
    isSystemUser = true;
    group = "media";
  };

  virtualisation.oci-containers.containers.metube = {
    image = "ghcr.io/alexta69/metube:latest";
    environment = {
      DOWNLOAD_DIR = "/downloads";
      STATE_DIR    = "/downloads/.metube";
      TEMP_DIR     = "/downloads/.tmp";
      UID   = toString config.users.users.metube.uid;
      GID   = toString mediaGid;
      UMASK = "022";                       # world-readable files for Jellyfin
      # Flat, Jellyfin-friendly naming; id keeps titles from colliding.
      OUTPUT_TEMPLATE = "%(title)s [%(id)s].%(ext)s";
    };
    volumes = [ "${downloadDir}:/downloads" ];
    ports = [ "127.0.0.1:8092:8081" ];     # nginx fronts this
  };

  systemd.tmpfiles.rules = [
    "d /mnt/fusion/youtube        0755 root   root  - -"
    "d ${downloadDir}            0775 metube media - -"
  ];

  services.nginx.virtualHosts."metube.rosemaryacres.com" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:8092";
      recommendedProxySettings = true;
      proxyWebsockets = true;                # MeTube uses websockets for progress
      extraConfig = ''
        client_max_body_size 100M;
      '';
    };
  };
}
