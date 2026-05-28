# *arr stack — Prowlarr + Sonarr + Radarr + Jellyseerr + qBittorrent (via Gluetun-Mullvad).
#
# All containers via virtualisation.oci-containers (same pattern as
# homepage.nix). Each web UI binds to 127.0.0.1 and is fronted by nginx
# with a Tailscale-only Cloudflare DNS-01 cert.
#
# Network topology — qBittorrent shares Gluetun's network namespace so all
# its traffic exits through Mullvad WireGuard. Gluetun is the only thing
# that publishes ports for qBittorrent's web UI (8080).
#
# Storage layout (gated on the fusion remount picking up D3-D6):
#   /mnt/fusion/arr/
#   ├── media/{tv,movies}/                        # Sonarr/Radarr libraries
#   └── downloads/
#       ├── incomplete/  →  /mnt/scratch/...      # bind-mounted to spare fusion IO
#       └── complete/                             # hardlink target for *arr import
#
# Hardlinks work because complete/ and media/ are both inside the fusion
# mergerfs. incomplete/ lives on /mnt/scratch (separate FS, the WD Green
# tier-3 disk) — qBittorrent does a one-time copy when a torrent completes.
#
# Secrets — NOT in git. Drop these on the host (root 0600) before first
# rebuild after this lands:
#   /var/lib/gluetun/wg.env          Mullvad WireGuard config
#       Required keys:
#         WIREGUARD_PRIVATE_KEY=<from Mullvad account WireGuard config>
#         WIREGUARD_ADDRESSES=10.x.x.x/32
#         SERVER_CITIES=Atlanta            (or whichever Mullvad city)
#         (Gluetun derives VPN_SERVICE_PROVIDER=mullvad / VPN_TYPE=wireguard
#         from its image env defaults; can override here if needed)
#
# Each *arr generates its own API key on first run; wire them up in the
# UIs (Prowlarr → Settings → Apps adds Sonarr/Radarr; Jellyseerr → Settings
# → Services adds Sonarr/Radarr; download-client wiring → qBittorrent).
#
# Cloudflare A records (Tailscale IP) for these subdomains need to exist
# before ACME will issue certs. Use the existing cf-dns helper or add via
# Cloudflare UI:
#   sonarr.rosemaryacres.com    → 100.82.117.116
#   radarr.rosemaryacres.com    → 100.82.117.116
#   prowlarr.rosemaryacres.com  → 100.82.117.116
#   requests.rosemaryacres.com  → 100.82.117.116
#   qbittorrent.rosemaryacres.com → 100.82.117.116
{ config, lib, pkgs, ... }:

let
  # Match existing /mnt/fusion file ownership (chris:users) so the *arr
  # containers can read/write without permission gymnastics.
  PUID = "1000";
  PGID = "100";
  TZ   = "America/New_York";

  arrRoot     = "/mnt/fusion/arr";
  scratchRoot = "/mnt/scratch/qbittorrent-incomplete";

  # Standard set of mounts each *arr container needs — the unified /data
  # tree gives Sonarr/Radarr/qBittorrent matching paths for hardlinks.
  dataVolume = "${arrRoot}:/data:rw";

  # Tier-2 "keepers" mounts — promotion targets when you mark a release as
  # long-term-keeper. In Sonarr/Radarr → Settings → Media Management →
  # Root Folders, add /keepers/{movies,tv} alongside /data/media/{movies,tv}.
  # Promote in the UI: right-click → Edit → Root Folder dropdown. *arr
  # moves the file + updates its DB; media-mirror picks it up on next sync.
  keepersMoviesVolume = "/mnt/fusion/Movies:/keepers/movies:rw";
  keepersTvVolume     = "/mnt/fusion/TV Shows:/keepers/tv:rw";

  # Subdomain → backend port
  ports = {
    prowlarr     = 9696;
    sonarr       = 8989;
    radarr       = 7878;
    jellyseerr   = 5055;
    qbittorrent  = 8085;  # qBit default 8080 collides with Tandoor on this host
    flaresolverr = 8191;  # headless browser proxy for Cloudflare-protected indexers
  };

  # Helper to build a Tailscale-only nginx vhost for a 127.0.0.1 backend.
  vhost = port: {
    forceSSL  = true;
    enableACME = true;
    acmeRoot   = null;
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
  # User-defined Docker network gives the *arr containers DNS-based
  # service discovery (Prowlarr can reach `flaresolverr:8191`, Sonarr
  # can reach `prowlarr:9696`, etc.). The default Docker bridge doesn't
  # do DNS between containers, only by IP — and IPs can shuffle on
  # restart.
  arrNet = "arr-net";

in
{
  # Create the arr-net Docker network before any *arr container starts.
  systemd.services.docker-network-arr = {
    description = "Create the arr-net Docker bridge network";
    wantedBy = [ "multi-user.target" ];
    after = [ "docker.service" ];
    before = map (n: "docker-${n}.service") [
      "prowlarr" "sonarr" "radarr" "jellyseerr" "gluetun" "flaresolverr"
      "homepage"   # Homepage joins arr-net to resolve *arr widget hostnames
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      ${pkgs.docker}/bin/docker network inspect ${arrNet} >/dev/null 2>&1 || \
        ${pkgs.docker}/bin/docker network create --driver bridge ${arrNet}
    '';
  };

  # State + media + scratch dirs must exist before containers start.
  systemd.tmpfiles.rules = [
    "d ${arrRoot}                         0775 chris users - -"
    "d ${arrRoot}/media                   0775 chris users - -"
    "d ${arrRoot}/media/tv                0775 chris users - -"
    "d ${arrRoot}/media/movies            0775 chris users - -"
    "d ${arrRoot}/downloads               0775 chris users - -"
    "d ${arrRoot}/downloads/complete      0775 chris users - -"
    "d ${scratchRoot}                     0775 chris users - -"
    "d /var/lib/prowlarr                  0750 chris users - -"
    "d /var/lib/sonarr                    0750 chris users - -"
    "d /var/lib/radarr                    0750 chris users - -"
    "d /var/lib/jellyseerr                0750 chris users - -"
    "d /var/lib/qbittorrent               0750 chris users - -"
    "d /var/lib/gluetun                   0700 root  root  - -"
    "f /var/lib/gluetun/wg.env            0600 root  root  - -"
  ];

  virtualisation.oci-containers.containers = {
    #--- Prowlarr (indexer hub) ---
    prowlarr = {
      image = "ghcr.io/linuxserver/prowlarr:latest";
      ports = [ "127.0.0.1:${toString ports.prowlarr}:9696" ];
      environment = { inherit PUID PGID TZ; };
      volumes = [
        "/var/lib/prowlarr:/config:rw"
      ];
      extraOptions = [ "--network=${arrNet}" ];
    };

    #--- FlareSolverr (Cloudflare challenge solver for protected indexers) ---
    # Runs a headless Chromium; Prowlarr POSTs requests here when an indexer
    # is gated by Cloudflare's JS challenge (1337x, RuTracker at times, etc.)
    # FlareSolverr solves the challenge and returns the cookie+HTML to
    # Prowlarr. Configure in Prowlarr: Settings → Indexers → FlareSolverr
    # field → http://flaresolverr:8191/v1
    # Stays OUTSIDE Gluetun's netns — only does HTTP challenge solving, not
    # torrent traffic, so it doesn't need VPN routing.
    flaresolverr = {
      image = "ghcr.io/flaresolverr/flaresolverr:latest";
      ports = [ "127.0.0.1:${toString ports.flaresolverr}:8191" ];
      environment = {
        inherit TZ;
        LOG_LEVEL = "info";
      };
      extraOptions = [ "--network=${arrNet}" ];
    };

    #--- Sonarr (TV) ---
    sonarr = {
      image = "ghcr.io/linuxserver/sonarr:latest";
      ports = [ "127.0.0.1:${toString ports.sonarr}:8989" ];
      environment = { inherit PUID PGID TZ; };
      volumes = [
        "/var/lib/sonarr:/config:rw"
        dataVolume
        keepersTvVolume   # tier-2 promotion target
      ];
      extraOptions = [ "--network=${arrNet}" ];
    };

    #--- Radarr (movies) ---
    radarr = {
      image = "ghcr.io/linuxserver/radarr:latest";
      ports = [ "127.0.0.1:${toString ports.radarr}:7878" ];
      environment = { inherit PUID PGID TZ; };
      volumes = [
        "/var/lib/radarr:/config:rw"
        dataVolume
        keepersMoviesVolume   # tier-2 promotion target
      ];
      extraOptions = [ "--network=${arrNet}" ];
    };

    #--- Jellyseerr (request UI) ---
    jellyseerr = {
      image = "fallenbagel/jellyseerr:latest";
      ports = [ "127.0.0.1:${toString ports.jellyseerr}:5055" ];
      environment = { inherit TZ; };
      volumes = [
        "/var/lib/jellyseerr:/app/config:rw"
      ];
      extraOptions = [ "--network=${arrNet}" ];
    };

    #--- Gluetun (Mullvad WireGuard) ---
    # Owns the network namespace that qBittorrent shares. Publishes
    # qBittorrent's port 8080 here because qBittorrent itself has no
    # ports field (its netns is borrowed).
    gluetun = {
      image = "qmcgaw/gluetun:latest";
      ports = [
        # qBittorrent's web UI. Both sides 8085 (matches WEBUI_PORT below)
        # because the default 8080 collides with Tandoor on this host.
        "127.0.0.1:${toString ports.qbittorrent}:${toString ports.qbittorrent}"
        # 6881 TCP/UDP is qBittorrent's torrent listen port; bound on the
        # Mullvad-tunnel side, not the host. No host publishing needed.
      ];
      environment = {
        VPN_SERVICE_PROVIDER = "mullvad";
        VPN_TYPE             = "wireguard";
        # Specific city pinned via /var/lib/gluetun/wg.env (SERVER_CITIES=...)
        # along with WIREGUARD_PRIVATE_KEY and WIREGUARD_ADDRESSES.
      };
      environmentFiles = [ "/var/lib/gluetun/wg.env" ];
      extraOptions = [
        "--cap-add=NET_ADMIN"
        "--device=/dev/net/tun"
        "--sysctl=net.ipv4.conf.all.rp_filter=2"
        "--network=${arrNet}"
      ];
    };

    #--- qBittorrent (downloads via Gluetun's netns) ---
    qbittorrent = {
      image = "ghcr.io/linuxserver/qbittorrent:latest";
      dependsOn = [ "gluetun" ];
      environment = {
        inherit PUID PGID TZ;
        WEBUI_PORT = toString ports.qbittorrent;
        # VueTorrent replaces qBit's default WebUI with the nicer Vue.js
        # alternative. The mod downloads VueTorrent at container start and
        # sets WebUI\AlternativeUIEnabled + WebUI\RootFolder in qBit's
        # config automatically — no manual qBittorrent.conf edits needed.
        # Backend API unchanged, so Sonarr/Radarr download-client and the
        # Homepage widget keep working through the same endpoints.
        DOCKER_MODS = "ghcr.io/gabe565/linuxserver-mod-vuetorrent:latest";
      };
      volumes = [
        "/var/lib/qbittorrent:/config:rw"
        dataVolume
        "${scratchRoot}:/scratch/incomplete:rw"
      ];
      # Share Gluetun's network namespace — all traffic exits via Mullvad.
      # NOTE: no `ports` field here; port 8080 is published by gluetun.
      extraOptions = [
        "--network=container:gluetun"
      ];
    };
  };

  #--- nginx vhosts (Cloudflare DNS-01 ACME, Tailscale-only) ---
  services.nginx.virtualHosts = {
    "prowlarr.rosemaryacres.com"    = vhost ports.prowlarr;
    "sonarr.rosemaryacres.com"      = vhost ports.sonarr;
    "radarr.rosemaryacres.com"      = vhost ports.radarr;
    "requests.rosemaryacres.com"    = vhost ports.jellyseerr;
    "qbittorrent.rosemaryacres.com" = vhost ports.qbittorrent;
  };
}
