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
      Media Mgmt:
        style: row
        columns: 3
      Cloud:
        style: row
        columns: 2
      AI:
        style: row
        columns: 2
      Bitcoin:
        style: row
        columns: 2
      Infrastructure:
        style: row
        columns: 2
      Monitoring:
        style: row
        columns: 2
      Riverwatch:
        style: row
        columns: 2
      Recent Alerts:
        style: row
        columns: 1
  '';

  servicesYaml = pkgs.writeText "homepage-services.yaml" ''
    - Media:
        - Jellyfin:
            description: Movies, TV, music
            href: https://jellyfin.rosemaryacres.com
            icon: jellyfin.png
            widget:
              type: jellyfin
              url: https://jellyfin.rosemaryacres.com
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

    # *arr stack — widgets pull live stats from each service via API.
    # Mint API keys in each UI:
    #   Sonarr/Radarr/Prowlarr   Settings → General → API Key
    #   Jellyseerr               Settings → "API Key" panel
    # qBittorrent uses username/password (no API key model).
    # Add to /var/lib/homepage/secrets.env:
    #   HOMEPAGE_VAR_PROWLARR_KEY=...
    #   HOMEPAGE_VAR_SONARR_KEY=...
    #   HOMEPAGE_VAR_RADARR_KEY=...
    #   HOMEPAGE_VAR_JELLYSEERR_KEY=...
    #   HOMEPAGE_VAR_LIDARR_KEY=...     # Lidarr → Settings → General → API Key
    #   HOMEPAGE_VAR_QBITTORRENT_USER=admin
    #   HOMEPAGE_VAR_QBITTORRENT_PASS=...
    # Then: sudo systemctl restart docker-homepage
    - Media Mgmt:
        - Prowlarr:
            description: Indexer hub
            href: https://prowlarr.rosemaryacres.com
            icon: prowlarr.png
            widget:
              type: prowlarr
              url: http://prowlarr:9696
              key: {{HOMEPAGE_VAR_PROWLARR_KEY}}
        - Sonarr:
            description: TV
            href: https://sonarr.rosemaryacres.com
            icon: sonarr.png
            widget:
              type: sonarr
              url: http://sonarr:8989
              key: {{HOMEPAGE_VAR_SONARR_KEY}}
        - Radarr:
            description: Movies
            href: https://radarr.rosemaryacres.com
            icon: radarr.png
            widget:
              type: radarr
              url: http://radarr:7878
              key: {{HOMEPAGE_VAR_RADARR_KEY}}
        - Jellyseerr:
            description: Requests
            href: https://requests.rosemaryacres.com
            icon: jellyseerr.png
            widget:
              type: jellyseerr
              url: http://jellyseerr:5055
              key: {{HOMEPAGE_VAR_JELLYSEERR_KEY}}
        - Music Requests:
            description: Request music (Aurral → Lidarr)
            href: https://music.rosemaryacres.com
            icon: mdi-music
            # Link-only: Aurral has no Homepage widget integration.
        - qBittorrent:
            description: Downloads (via Mullvad VPN)
            href: https://qbittorrent.rosemaryacres.com
            icon: qbittorrent.png
            widget:
              # qBit's WebUI lives in Gluetun's netns; both Homepage and
              # qBit are on arr-net, so this hostname resolves. Quote the
              # creds: passwords containing YAML-reserved leading chars
              # (*, &, !, %) would otherwise parse as anchors/aliases.
              type: qbittorrent
              url: http://gluetun:8085
              username: "{{HOMEPAGE_VAR_QBITTORRENT_USER}}"
              password: "{{HOMEPAGE_VAR_QBITTORRENT_PASS}}"
        - Lidarr:
            description: Music
            href: https://lidarr.rosemaryacres.com
            icon: lidarr.png
            widget:
              type: lidarr
              url: http://lidarr:8686
              key: {{HOMEPAGE_VAR_LIDARR_KEY}}
        - LazyLibrarian:
            description: Audiobooks & ebooks
            href: https://lazylibrarian.rosemaryacres.com
            icon: lazylibrarian.png
            # Link-only: Homepage has no LazyLibrarian widget (no API integration).
        - MeTube:
            description: One-off YouTube/video downloads (yt-dlp)
            href: https://metube.rosemaryacres.com
            icon: metube.png
            # Link-only: Homepage has no MeTube widget.

    - Productivity:
        - Vaultwarden:
            description: Password vault
            href: https://keys.rosemaryacres.com
            icon: vaultwarden.png
        - Paperless:
            description: OCR document archive
            href: https://paperless.rosemaryacres.com
            icon: paperless.png
        - Uptime Kuma:
            description: Status board
            href: https://uptime.rosemaryacres.com
            icon: uptime-kuma.png
            widget:
              type: uptimekuma
              url: https://uptime.rosemaryacres.com
              slug: default

    - AI:
        - Open WebUI:
            description: Local LLM chat + book RAG (Qwen 3B-GPU / 32B-CPU, on Wallace)
            href: https://chat.rosemaryacres.com
            icon: open-webui.png
            # Link-only: the Open WebUI widget needs an API key; not wired here.

    - Cloud:
        - Nextcloud:
            description: Files, calendar, notes
            href: https://cloud.rosemaryacres.com
            icon: nextcloud.png
            widget:
              type: nextcloud
              url: https://cloud.rosemaryacres.com
              username: "{{HOMEPAGE_VAR_NEXTCLOUD_USER}}"
              password: "{{HOMEPAGE_VAR_NEXTCLOUD_PASS}}"
        - Tandoor:
            description: Recipes
            href: https://recipes.rosemaryacres.com
            icon: tandoor-recipes.png
            widget:
              type: tandoor
              url: https://recipes.rosemaryacres.com
              key: {{HOMEPAGE_VAR_TANDOOR_KEY}}

    # Bitcoin / Lightning. mempool has a native Homepage widget (fee + block
    # stats, no key). Alby Hub has no widget, so it's an href tile. bitcoind and
    # Fulcrum have no web UI (RPC / Electrum TCP only), so they get no tile.
    - Bitcoin:
        - mempool:
            description: Blocks, fees & explorer
            href: https://mempool.rosemaryacres.com
            icon: mempool.png
            widget:
              type: mempool
              url: https://mempool.rosemaryacres.com
        - Alby Hub:
            description: Lightning node & Nostr Wallet Connect
            href: https://alby.rosemaryacres.com
            icon: alby.png

    - Infrastructure:
        - Forgejo:
            description: Self-hosted Git (mirrors of GitHub)
            href: https://git.rosemaryacres.com
            icon: forgejo.png
            widget:
              # Forgejo speaks the Gitea API, so the gitea widget works.
              type: gitea
              url: https://git.rosemaryacres.com
              key: {{HOMEPAGE_VAR_FORGEJO_KEY}}
              showOpen: true
              showCounters: true
              showRepoStats: false
        - PinchFlat:
            description: YouTube archiver
            href: http://100.82.117.116:8945
            icon: pinchflat.png

    - Monitoring:
        - Grafana:
            description: Dashboards & alerting
            href: https://grafana.rosemaryacres.com
            icon: grafana.png
        - Prometheus:
            description: Metrics & queries
            href: https://prometheus.rosemaryacres.com
            icon: prometheus.png
            widget:
              type: prometheus
              url: https://prometheus.rosemaryacres.com

    - Gromit (live):
        # Glances exposes a REST API at https://glances.rosemaryacres.com
        # (DNS resolves to the Tailscale IP, so this is not internet-
        # exposed). Each block below is a separate metric card with a
        # small sparkline. v4 = Glances API v4. Cards link to the full
        # Glances HTML UI on click.
        - CPU:
            href: https://glances.rosemaryacres.com
            icon: glances.png
            widget:
              type: glances
              url: https://glances.rosemaryacres.com
              metric: cpu
              version: 4
              chart: true
              pointsLimit: 30
        - Memory:
            href: https://glances.rosemaryacres.com
            icon: glances.png
            widget:
              type: glances
              url: https://glances.rosemaryacres.com
              metric: memory
              version: 4
              chart: true
              pointsLimit: 30
        - Disk I/O:
            href: https://glances.rosemaryacres.com
            icon: glances.png
            widget:
              type: glances
              url: https://glances.rosemaryacres.com
              metric: disk:nvme0n1
              version: 4
              chart: true
              pointsLimit: 30
        - Network (LAN):
            href: https://glances.rosemaryacres.com
            icon: glances.png
            widget:
              type: glances
              url: https://glances.rosemaryacres.com
              metric: network:enp3s0
              version: 4
              chart: true
              pointsLimit: 30
        - Load + uptime:
            href: https://glances.rosemaryacres.com
            icon: glances.png
            widget:
              type: glances
              url: https://glances.rosemaryacres.com
              metric: info
              version: 4
        - Temperatures:
            description: CPU / NVMe / ambient °C — warn & critical alerts armed
            href: https://grafana.rosemaryacres.com/d/gromit-temps
            icon: mdi-thermometer
            widget:
              # Same embed pattern as the Riverwatch graph below: panel 1 of
              # the gromit-temps Grafana dashboard via d-solo. Anonymous Viewer
              # + allow_embedding are set in monitoring.nix, so it loads without
              # auth (Grafana is Tailscale-only).
              type: iframe
              name: Temps
              src: https://grafana.rosemaryacres.com/d-solo/gromit-temps/_?orgId=1&panelId=1&theme=dark&from=now-24h&to=now&refresh=1m&kiosk
              classes: h-[360px] w-full
              referrerPolicy: same-origin
              allowScrolling: "no"

    - Riverwatch:
        - Kentucky River — observed + NWPS forecast:
            description: Stage at Lockport & Gratz with flood thresholds
            href: https://grafana.rosemaryacres.com/d/riverwatch
            icon: mdi-chart-line
            widget:
              # Embeds panel 1 (Stage time series + flood-threshold reference
              # lines + NWPS 5-day forecast) from the Riverwatch Grafana
              # dashboard. Anonymous Viewer is enabled in monitoring.nix so
              # this loads without auth — fine because Grafana is
              # Tailscale-only. allow_embedding=true is also set there so
              # Grafana doesn't send X-Frame-Options: deny.
              type: iframe
              name: River Graph
              # `to=now+5d` (URL-encoded) makes room for the NWPS forecast
              # overlay, which extends ~5 days into the future.
              src: https://grafana.rosemaryacres.com/d-solo/riverwatch/_?orgId=1&panelId=7&theme=dark&from=now-7d&to=now%2B5d&refresh=5m&kiosk
              classes: h-[640px] w-full
              referrerPolicy: same-origin
              allowScrolling: "no"
        - Lockport (Lock 2):
            description: Kentucky River
            href: https://water.noaa.gov/gauges/lptk2
            icon: mdi-waves
            widget:
              type: customapi
              url: https://api.water.noaa.gov/nwps/v1/gauges/LPTK2
              refreshInterval: 600000
              method: GET
              display: list
              mappings:
                - field:
                    status:
                      observed: primary
                  label: Stage (ft)
                - field:
                    status:
                      observed: secondary
                  label: Flow (kcfs)
                - field:
                    status:
                      forecast: primary
                  label: Forecast crest (ft)
                - field:
                    status:
                      observed: floodCategory
                  label: Status
    - Recent Alerts:
        - ntfy:
            description: Latest message on gromit-alerts
            href: https://ntfy.rosemaryacres.com
            icon: ntfy.png
            widget:
              type: ntfy
              url: https://ntfy.rosemaryacres.com
              topic: gromit-alerts
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
  # Homepage widget API keys via sops (migrated 2026-06-16). Read by root (docker
  # --env-file) before the container starts → root:0400.
  sops.secrets."homepage-env" = {
    sopsFile = ../../secrets/homepage-env.yaml;
    key = "homepage-env";
  };

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
      environmentFiles = [ config.sops.secrets."homepage-env".path ];
      environment = {
        HOMEPAGE_ALLOWED_HOSTS = "${hostname},www.${hostname}";
      };
      # Join arr-net (created by modules/services/arr.nix) so the *arr stack
      # widgets can resolve `prowlarr`, `sonarr`, `gluetun`, etc. by name.
      extraOptions = [ "--network=arr-net" ];
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
