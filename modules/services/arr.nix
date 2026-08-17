# *arr stack — Prowlarr + Sonarr + Radarr + Jellyseerr + qBittorrent (via Gluetun-AirVPN).
#
# All containers via virtualisation.oci-containers (same pattern as
# homepage.nix). Each web UI binds to 127.0.0.1 and is fronted by nginx
# with a Tailscale-only Cloudflare DNS-01 cert.
#
# Network topology — qBittorrent shares Gluetun's network namespace so all
# its traffic exits through AirVPN WireGuard (switched from Mullvad 2026-07-27
# after the Mullvad account expired; AirVPN adds static port forwarding for
# seeding). Gluetun is the only thing that publishes ports for qBittorrent's
# web UI (8080).
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
# Secrets — in sops (`secrets/gluetun-wg.yaml`, edit with `sops`). Keys
# (from the AirVPN Client Area: Config Generator WireGuard .conf + the
# Forwarded ports page):
#         WIREGUARD_PRIVATE_KEY=<[Interface] PrivateKey>
#         WIREGUARD_PRESHARED_KEY=<[Peer] PresharedKey — AirVPN uses one>
#         WIREGUARD_ADDRESSES=10.x.x.x/32  ([Interface] Address, IPv4)
#         SERVER_COUNTRIES=United States
#         FIREWALL_VPN_INPUT_PORTS=<the AirVPN forwarded port — gluetun opens
#         its tunnel-side firewall for inbound peers; qBittorrent's listen
#         port must be set to the SAME number (WebUI → Connection)>.
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

  # Force IPv4-only inside the *arr containers.
  #
  # `arr-net` is a plain Docker bridge: IPv4 subnet, no IPv6 subnet, no NAT66.
  # So a container has no route to a v6 address. Many indexers (and TMDB) are
  # Cloudflare-fronted and DUAL-STACK, and glibc's getaddrinfo prefers the AAAA
  # answer — so the container picks an address it cannot reach and the
  # connection dies as "Resource temporarily unavailable" / ERR_NAME_NOT_RESOLVED,
  # while A-only hosts work fine.
  #
  # Diagnosed 2026-07-29 after 5 of 9 Prowlarr indexers auto-disabled and Sonarr
  # logged "No available indexers" 95× in 24h. The correlation was exact:
  #
  #   api.knaben.org  A=2 AAAA=2  -> failed      api.ipify.org  A=3 AAAA=0  -> worked
  #   1337x.to        A=2 AAAA=2  -> failed      github.com     A=1 AAAA=0  -> worked
  #
  # Prowlarr's own error message says "ensure IPv6 is working or disabled".
  # Disabling it in the container makes getaddrinfo return the A record.
  #
  # The alternative — giving arr-net a real IPv6 subnet + NAT66 — is more moving
  # parts for no benefit: nothing here needs v6 reachability.
  ipv4Only = "--sysctl=net.ipv6.conf.all.disable_ipv6=1";

in
{
  # Gluetun WireGuard creds (WIREGUARD_PRIVATE_KEY / _ADDRESSES) via sops
  # (migrated 2026-06-16). Read by root (docker --env-file) before the gluetun
  # container starts → root:0400.
  sops.secrets."gluetun-wg" = {
    sopsFile = ../../secrets/gluetun-wg.yaml;
    key = "gluetun-wg";
  };

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
  ];

  # Images are pinned tag@digest (2026-08 audit): a bare :latest re-pulled on
  # every comin redeploy, which made ~15 containers a standing supply-chain
  # surface — and containers sit INSIDE the nginx source-gate (nginx-access
  # allows 172.16.0.0/12), so a compromised upstream image is a LAN-equivalent
  # attacker. The tag stays for readability; the digest is what deploys.
  # To bump one deliberately:
  #   nix run nixpkgs#skopeo -- inspect --format '{{.Digest}}' docker://<image>:<tag>
  # then update the digest here in a PR. Same pattern in mempool, homepage,
  # metube, decluttarr, unpackerr, lidarr, lazylibrarian, aurral.
  virtualisation.oci-containers.containers = {
    #--- Prowlarr (indexer hub) ---
    prowlarr = {
      image = "ghcr.io/linuxserver/prowlarr:latest@sha256:1295cff29d10b486c0d8324d1559a552140a5932bf8b3d87e398654414f63f92";
      ports = [ "127.0.0.1:${toString ports.prowlarr}:9696" ];
      environment = { inherit PUID PGID TZ; };
      volumes = [
        "/var/lib/prowlarr:/config:rw"
      ];
      extraOptions = [ "--network=${arrNet}" ipv4Only ];
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
      image = "ghcr.io/flaresolverr/flaresolverr:latest@sha256:139dfee1c6f89249c8d665d1333a42e8ec74ec0a86bc6bb1c8461e10d3a66a47";
      ports = [ "127.0.0.1:${toString ports.flaresolverr}:8191" ];
      environment = {
        inherit TZ;
        LOG_LEVEL = "info";
      };
      extraOptions = [ "--network=${arrNet}" ipv4Only ];
    };

    #--- Sonarr (TV) ---
    sonarr = {
      image = "ghcr.io/linuxserver/sonarr:latest@sha256:373159ba768e23a3a1c497d9f2b936addf8fd5b1fdce7dd6a14080ac928bfda0";
      ports = [ "127.0.0.1:${toString ports.sonarr}:8989" ];
      environment = { inherit PUID PGID TZ; };
      volumes = [
        "/var/lib/sonarr:/config:rw"
        dataVolume
        keepersTvVolume   # tier-2 promotion target
      ];
      extraOptions = [ "--network=${arrNet}" ipv4Only ];
    };

    #--- Radarr (movies) ---
    radarr = {
      image = "ghcr.io/linuxserver/radarr:latest@sha256:a45b5ab0f850f39edb4cc9c95bbd967b52ddc3d4574a4dfb45561177db6c88f4";
      ports = [ "127.0.0.1:${toString ports.radarr}:7878" ];
      environment = { inherit PUID PGID TZ; };
      volumes = [
        "/var/lib/radarr:/config:rw"
        dataVolume
        keepersMoviesVolume   # tier-2 promotion target
      ];
      extraOptions = [ "--network=${arrNet}" ipv4Only ];
    };

    #--- Jellyseerr (request UI) ---
    jellyseerr = {
      image = "fallenbagel/jellyseerr:latest@sha256:4538137bc5af902dece165f2bf73776d9cf4eafb6dd714670724af8f3eb77764";
      ports = [ "127.0.0.1:${toString ports.jellyseerr}:5055" ];
      environment = { inherit TZ; };
      volumes = [
        "/var/lib/jellyseerr:/app/config:rw"
      ];
      extraOptions = [ "--network=${arrNet}" ];
    };

    #--- Gluetun (AirVPN WireGuard) ---
    # Owns the network namespace that qBittorrent shares. Publishes
    # qBittorrent's port 8080 here because qBittorrent itself has no
    # ports field (its netns is borrowed).
    gluetun = {
      image = "qmcgaw/gluetun:latest@sha256:e3272b29a4bc177b389fbdcb54cf9716ccbfc30f04d8b7a35b0a5be9cdb58461";
      ports = [
        # qBittorrent's web UI. Both sides 8085 (matches WEBUI_PORT below)
        # because the default 8080 collides with Tandoor on this host.
        "127.0.0.1:${toString ports.qbittorrent}:${toString ports.qbittorrent}"
        # qBittorrent's torrent listen port is the AirVPN FORWARDED port
        # (FIREWALL_VPN_INPUT_PORTS in the sops env), bound on the
        # VPN-tunnel side, not the host. No host publishing needed.
      ];
      environment = {
        VPN_SERVICE_PROVIDER = "airvpn";
        VPN_TYPE             = "wireguard";
        # Everything account-specific lives in the sops gluetun-wg secret:
        # WIREGUARD_PRIVATE_KEY / _PRESHARED_KEY / _ADDRESSES,
        # SERVER_COUNTRIES, FIREWALL_VPN_INPUT_PORTS (see header comment).
      };
      environmentFiles = [ config.sops.secrets."gluetun-wg".path ];
      extraOptions = [
        "--cap-add=NET_ADMIN"
        "--device=/dev/net/tun"
        "--sysctl=net.ipv4.conf.all.rp_filter=2"
        "--network=${arrNet}"
        # Stable hostname for the shared netns. qBittorrent inherits it
        # (--network=container:gluetun forbids setting its own), and Qt's
        # QLockFile refuses to clear a stale profile lock written under a
        # DIFFERENT hostname — with the default hostname (= container ID,
        # new on every recreation) any hard-kill leaves an unclearable lock
        # and qbittorrent-nox crash-loops silently on every start. Bit us
        # 2026-07-10→27 (6,134 rotated crash logs) after the Jul 4 Mullvad
        # die-off hard-killed the stack.
        "--hostname=gluetun"
      ];
    };

    #--- qBittorrent (downloads via Gluetun's netns) ---
    qbittorrent = {
      image = "ghcr.io/linuxserver/qbittorrent:latest@sha256:212b86dff59e3962b4082b5ef20a577e76c8f8527d2ab505cfa887b4bcecb0b0";
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
        DOCKER_MODS = "ghcr.io/gabe565/linuxserver-mod-vuetorrent:latest@sha256:543f484b84489b651ccfed1ac8af62255652c00418143726c1a7d2331035abad";
      };
      volumes = [
        "/var/lib/qbittorrent:/config:rw"
        dataVolume
        "${scratchRoot}:/scratch/incomplete:rw"
      ];
      # Share Gluetun's network namespace — all traffic exits via AirVPN.
      # NOTE: no `ports` field here; port 8080 is published by gluetun.
      extraOptions = [
        "--network=container:gluetun"
      ];
    };
  };

  # The gabe565 vuetorrent mod drops files at /vuetorrent, but its
  # s6-init step that flips qBit's WebUI\AlternativeUIEnabled fails on the
  # linuxserver:latest base (s6 v3 vs v2 layout mismatch). In practice
  # qBit serves VueTorrent fine just from WebUI\RootFolder being set, even
  # if the AlternativeUIEnabled flag in the conf reads `false` — qBit
  # validates the path on startup and serves the alt UI from memory. This
  # post-start poke is belt-and-suspenders: API-set both keys after qBit
  # is up. Never fails the unit (|| true) so a stuck startup window doesn't
  # cause a restart loop.
  systemd.services.docker-qbittorrent.serviceConfig.ExecStartPost = [
    "+${pkgs.writeShellScript "qbit-enable-vuetorrent" ''
      for i in $(seq 1 60); do
        ${pkgs.curl}/bin/curl -fsS --max-time 2 \
          -o /dev/null http://127.0.0.1:8085/api/v2/app/version && break
        sleep 1
      done
      # Subnet whitelist (set elsewhere) lets us call without auth from host.
      # JSON body must be url-encoded under the json= param per qBit API docs.
      ${pkgs.curl}/bin/curl -fsS -X POST \
        --data-urlencode 'json={"alternative_webui_enabled":true,"alternative_webui_path":"/vuetorrent"}' \
        http://127.0.0.1:8085/api/v2/app/setPreferences || true
    ''}"
  ];

  #--- nginx vhosts (Cloudflare DNS-01 ACME, Tailscale-only) ---
  services.nginx.virtualHosts = {
    "prowlarr.rosemaryacres.com"    = vhost ports.prowlarr;
    "sonarr.rosemaryacres.com"      = vhost ports.sonarr;
    "radarr.rosemaryacres.com"      = vhost ports.radarr;
    "requests.rosemaryacres.com"    = vhost ports.jellyseerr;
    "qbittorrent.rosemaryacres.com" = vhost ports.qbittorrent;
  };
}
