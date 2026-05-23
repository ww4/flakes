# Homepage dashboard — self-hosted launchpage at https://rosemaryacres.com
# (and www.rosemaryacres.com, which redirects to apex). Containerised via
# virtualisation.oci-containers; all config is fully declarative — every
# YAML file is bind-mounted read-only from the Nix store.
#
# Live-data widgets (Jellyfin now-playing, Nextcloud quota, Immich library
# stats, etc.) read API keys from env vars via Homepage's {{HOMEPAGE_VAR_*}}
# template syntax. Drop the keys into /var/lib/homepage/secrets.env
# (root:root, 0600) in dotenv format, then `sudo systemctl restart
# docker-homepage`:
#
#   HOMEPAGE_VAR_JELLYFIN_KEY=...
#   HOMEPAGE_VAR_IMMICH_KEY=...
#   HOMEPAGE_VAR_NEXTCLOUD_USER=chris
#   HOMEPAGE_VAR_NEXTCLOUD_PASS=...      # an app-password from Nextcloud
#   HOMEPAGE_VAR_AUDIOBOOKSHELF_KEY=...
#   HOMEPAGE_VAR_TANDOOR_KEY=...
#
# How to mint each key:
#   - Jellyfin       → Dashboard → Advanced → API Keys → +
#   - Immich         → Account Settings → API Keys → New API Key
#   - Nextcloud      → Personal → Security → Devices & sessions → "Create
#                      new app password" (use that, not the login password)
#   - Audiobookshelf → Settings → Users → click user → API Token
#   - Tandoor        → User dropdown → API Token → Create Token
#
# DNS prerequisite on Cloudflare:
#   A   rosemaryacres.com       100.82.117.116    (Tailscale IP, proxy off)
#   A   www.rosemaryacres.com   100.82.117.116    (Tailscale IP, proxy off)
{ config, lib, pkgs, ... }:
let
  hostname = "rosemaryacres.com";
  port = 3000;

  settingsYaml = pkgs.writeText "homepage-settings.yaml" ''
    title: Gromit
    theme: dark
    color: gray
    headerStyle: clean
    layout:
      Media:
        style: row
        columns: 3
      Cloud:
        style: row
        columns: 2
      Infrastructure:
        style: row
        columns: 2
  '';

  servicesYaml = pkgs.writeText "homepage-services.yaml" ''
    - Media:
        - Jellyfin:
            description: Movies, TV, music
            href: http://100.82.117.116:8096
            icon: jellyfin.png
            widget:
              type: jellyfin
              url: http://100.82.117.116:8096
              key: {{HOMEPAGE_VAR_JELLYFIN_KEY}}
              enableNowPlaying: true
              enableBlocks: true
        - Audiobookshelf:
            description: Audiobooks & podcasts
            href: https://abs.rosemaryacres.com
            icon: audiobookshelf.png
            widget:
              type: audiobookshelf
              url: https://abs.rosemaryacres.com
              key: {{HOMEPAGE_VAR_AUDIOBOOKSHELF_KEY}}
        - Immich:
            description: Photos & videos
            href: https://photos.rosemaryacres.com
            icon: immich.png
            widget:
              # Immich binds 127.0.0.1 only, so the container can't reach
              # it on the Tailscale IP — go through nginx instead.
              # version: 2 picks the new /api/server/{statistics,version}
              # endpoints (Immich 1.85+ dropped /api/server-info/*).
              type: immich
              url: https://photos.rosemaryacres.com
              key: {{HOMEPAGE_VAR_IMMICH_KEY}}
              version: 2

    - Cloud:
        - Nextcloud:
            description: Files, calendar, notes
            href: https://cloud.rosemaryacres.com
            icon: nextcloud.png
            widget:
              type: nextcloud
              url: https://cloud.rosemaryacres.com
              username: {{HOMEPAGE_VAR_NEXTCLOUD_USER}}
              password: {{HOMEPAGE_VAR_NEXTCLOUD_PASS}}
        - Tandoor:
            description: Recipes
            href: https://recipes.rosemaryacres.com
            icon: tandoor.png
            widget:
              type: tandoor
              url: https://recipes.rosemaryacres.com
              key: {{HOMEPAGE_VAR_TANDOOR_KEY}}

    - Infrastructure:
        - PinchFlat:
            description: YouTube archiver
            href: http://100.82.117.116:8945
            icon: pinchflat.png
        - ntfy:
            description: Push notifications (gromit-alerts)
            href: http://100.82.117.116:8090
            icon: ntfy.png
  '';

  bookmarksYaml = pkgs.writeText "homepage-bookmarks.yaml" ''
    - Code:
        - GitHub:
            - abbr: GH
              href: https://github.com/ww4
        - flakes repo:
            - abbr: FL
              href: https://github.com/ww4/flakes
    - Admin:
        - Cloudflare:
            - abbr: CF
              href: https://dash.cloudflare.com
        - Tailscale:
            - abbr: TS
              href: https://login.tailscale.com/admin/machines
  '';

  widgetsYaml = pkgs.writeText "homepage-widgets.yaml" ''
    - resources:
        label: gromit
        cpu: true
        memory: true
        disk:
          - /
          - /mnt/fusion
          - /mnt/backup/all
    - search:
        provider: duckduckgo
        target: _blank
    - datetime:
        text_size: xl
        format:
          dateStyle: long
          timeStyle: short
          hour12: true
  '';

  # Required by Homepage but unused — empty avoids "file not found" warnings.
  dockerYaml     = pkgs.writeText "homepage-docker.yaml" "";
  kubernetesYaml = pkgs.writeText "homepage-kubernetes.yaml" "";

in {
  virtualisation.oci-containers = {
    backend = "docker";
    containers.homepage = {
      image = "ghcr.io/gethomepage/homepage:latest";
      ports = [ "127.0.0.1:${toString port}:3000" ];
      volumes = [
        "${settingsYaml}:/app/config/settings.yaml:ro"
        "${servicesYaml}:/app/config/services.yaml:ro"
        "${bookmarksYaml}:/app/config/bookmarks.yaml:ro"
        "${widgetsYaml}:/app/config/widgets.yaml:ro"
        "${dockerYaml}:/app/config/docker.yaml:ro"
        "${kubernetesYaml}:/app/config/kubernetes.yaml:ro"
        # Mounted so the resources widget can statfs() these — host paths
        # don't exist inside the container otherwise.
        "/mnt/fusion:/mnt/fusion:ro"
        "/mnt/backup/all:/mnt/backup/all:ro"
      ];
      environmentFiles = [ "/var/lib/homepage/secrets.env" ];
      environment = {
        HOMEPAGE_ALLOWED_HOSTS = "${hostname},www.${hostname}";
      };
    };
  };

  # secrets.env must exist before container start, even if empty.
  systemd.tmpfiles.rules = [
    "d /var/lib/homepage 0755 root root - -"
    "f /var/lib/homepage/secrets.env 0600 root root - -"
  ];

  # Apex + www on Cloudflare DNS-01; www → apex 301.
  services.nginx.virtualHosts."${hostname}" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:${toString port}";
      proxyWebsockets = true;
    };
  };

  services.nginx.virtualHosts."www.${hostname}" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    globalRedirect = hostname;
  };
}
