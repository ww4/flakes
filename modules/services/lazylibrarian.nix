# LazyLibrarian — ebook/audiobook automation; the maintained successor to the
# retired Readarr. Same container pattern as arr.nix: on arr-net, sharing the
# /data tree (/mnt/fusion/arr) with qBittorrent for hardlink imports; Prowlarr
# syncs Torznab indexers; qBittorrent (via Gluetun) is the download client.
#
# NOTE: LazyLibrarian has no clean REST API for full configuration, so the
# download client, providers, and library are set in its web UI. Finalise that
# once a real content source is connected (e.g. RuTracker) — public trackers
# have almost no audiobooks, so there's nothing to test against until then.
#
# Library target: /data/media/audiobooks  → add this folder to Audiobookshelf's
# libraries so auto-downloaded audiobooks show up in ABS. (ebooks → /data/media/books)
#
# Reach at https://lazylibrarian.rosemaryacres.com — add the Cloudflare A
# record (lazylibrarian → 100.82.117.116).
{ config, lib, pkgs, ... }:
let
  PUID = "1000"; PGID = "100"; TZ = "America/New_York";
  arrNet = "arr-net";
  port = 5299;
in
{
  systemd.tmpfiles.rules = [
    "d /var/lib/lazylibrarian          0750 chris users - -"
    "d /mnt/fusion/arr/media/audiobooks 0775 chris users - -"
    "d /mnt/fusion/arr/media/books      0775 chris users - -"
  ];

  virtualisation.oci-containers.containers.lazylibrarian = {
    image = "ghcr.io/linuxserver/lazylibrarian:latest";
    ports = [ "127.0.0.1:${toString port}:5299" ];
    environment = {
      inherit PUID PGID TZ;
      # Calibre + ebook conversion tooling docker mod (handy for ebooks; harmless for audiobooks)
      DOCKER_MODS = "linuxserver/mods:universal-calibre";
    };
    volumes = [
      "/var/lib/lazylibrarian:/config:rw"
      "/mnt/fusion/arr:/data:rw"
    ];
    extraOptions = [ "--network=${arrNet}" ];
  };

  systemd.services.docker-lazylibrarian = {
    after = [ "docker-network-arr.service" ];
    requires = [ "docker-network-arr.service" ];
  };

  services.nginx.virtualHosts."lazylibrarian.rosemaryacres.com" = {
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
