# Silo — self-hosted media server (evaluation only), at https://silo.rosemaryacres.com
#
# WHY THIS EXISTS, AND WHY IT IS OFF BY DEFAULT
# ---------------------------------------------
# Silo (https://siloserver.org, AGPL-3.0-or-later) speaks the Jellyfin protocol,
# so existing Jellyfin clients can be pointed at it unchanged. This module exists
# to *evaluate* that claim on real hardware without touching the running Jellyfin.
# It is `enable = false` by default: importing it changes nothing about the built
# system until someone sets `services.silo.enable = true`.
#
# The 2026-10-06 evaluation said DO NOT migrate (see the `silo-server-watch`
# agent memory for the full baseline). Short version: the upstream repo was 4.5
# months old, had ZERO tagged releases, and ~90% of it is one contributor. This
# module is the cheap way to re-check that in ~April 2027 with evidence instead
# of a second round of guessing.
#
# UPSTREAM IS THE SOURCE OF TRUTH. Everything below mirrors the shipped
# docker-compose.yml (env var names, volume layout, the /host/proc mounts,
# read-only media). When re-testing, re-read that file first — this module is a
# translation of it, and a translation goes stale.
#   https://github.com/Silo-Server/silo-server/blob/main/docker-compose.yml
#
# ⚠️ PORT COLLISIONS ON GROMIT — the whole reason the defaults here differ from
# upstream's. Upstream publishes PORT=8090, JF_PORT=8096, postgres=5432. On
# gromit (checked 2026-10-06, `ss -lntp`): 8096 is Jellyfin, 8090 is ntfy, 5432
# is the system Postgres 14, and 8080 is already taken on loopback. THREE of
# upstream's four default host ports collide. The defaults below are chosen to
# avoid that; the database and cache are not published to the host at all.
#
# ⚠️ Re-pinning: `modules/services/silo-repin.sh` resolves current digests AND
# the upstream commit each image was built from. Do not hand-edit a digest —
# the provenance comments are only trustworthy if the script wrote them.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.silo;
  siloNet = "silo-net";
  ociBackend = config.virtualisation.oci-containers.backend;
  # The package matching the configured backend, so this module works under
  # either without a second edit.
  ociBin = if ociBackend == "podman" then pkgs.podman else pkgs.docker;

  # Shared by every container so a restart of one does not race the others.
  common = [ "--network=${siloNet}" ];
in
{
  options.services.silo = {
    enable = lib.mkEnableOption "the Silo media server (evaluation; see module header)";

    # ── Image pins ────────────────────────────────────────────────────────
    # Digest-pinned, resolved 2026-10-06 by silo-repin.sh. Upstream publishes
    # ~100 commit-SHA tags plus `latest`; there are no semver releases, so a
    # digest is the only stable handle. The provenance comment on each is what
    # makes a later re-test meaningful.
    image = lib.mkOption {
      type = lib.types.str;
      # upstream commit 47a29980e2765c1a946e09fade6322208d5a8388 ("build-1131"),
      # image built 2026-10-06T13:50:34Z. Multi-arch (amd64 + arm64).
      default = "ghcr.io/silo-server/silo-server:latest@sha256:727bbbcb4cf5be068893c2d3a7059906f17722a35ad70f19941db54a7b862f7d";
      description = "Silo server image, digest-pinned. Bump with silo-repin.sh.";
    };

    postgresImage = lib.mkOption {
      type = lib.types.str;
      # pgvector/pgvector:pg18 — upstream's own choice. Silo needs pgvector, and
      # the system Postgres on gromit is 14, so this stays containerised.
      default = "pgvector/pgvector:pg18@sha256:2358fcba361ed2233a5ed81b5fe4ca779ccb304120ce531a3bf51c0ed7e2bc11";
      description = "Postgres+pgvector image, digest-pinned.";
    };

    redisImage = lib.mkOption {
      type = lib.types.str;
      default = "redis:alpine@sha256:3811787313eba226a2ef38658c6ccb91cd5e110edc89c37767de373120a0e5a0";
      description = "Redis image, digest-pinned.";
    };

    # ── Ports (host side) ─────────────────────────────────────────────────
    # All bound to loopback; nginx fronts them. Nothing here may reuse 8080,
    # 8090, 8096 or 5432 on gromit — see the header.
    bindAddress = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Host address the published ports bind to. Loopback + nginx is the house pattern.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8099;
      description = "Host port for Silo's own web UI / API (container listens on 8080).";
    };

    jellyfinCompatPort = lib.mkOption {
      type = lib.types.port;
      default = 8097;
      description = ''
        Host port for the Jellyfin-protocol compatibility listener (container
        listens on 8096). ⚠️ NOT 8096 — that is the real Jellyfin on gromit.
        This is the port existing Jellyfin clients get pointed at to evaluate Silo.
      '';
    };

    audiobookshelfCompatPort = lib.mkOption {
      type = lib.types.nullOr lib.types.port;
      default = null;
      description = ''
        Host port for the Audiobookshelf-compatible listener (container 13378).
        null = do not publish it. gromit's own Audiobookshelf is on 8000, so
        13378 is free if it is ever wanted.
      '';
    };

    # ── Storage ───────────────────────────────────────────────────────────
    dataDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/silo";
      description = "Silo state: database, artwork, plugins, compat data.";
    };

    transcodeDir = lib.mkOption {
      type = lib.types.path;
      default = "/mnt/scratch/silo-transcode";
      description = ''
        Scratch for in-flight transcodes. Deliberately NOT under dataDir: this
        is high-churn write traffic and does not belong on the root SSD or in
        any backup set.
      '';
    };

    mediaRoots = lib.mkOption {
      type = lib.types.attrsOf lib.types.path;
      default = { "/mnt/media" = "/mnt/fusion"; };
      example = { "/mnt/media" = "/mnt/fusion"; "/mnt/media2" = "/mnt/primary/D1"; };
      description = ''
        Media visible to Silo, as { containerPath = hostPath; }. ALWAYS mounted
        read-only — an evaluation must not be able to write to the library, and
        upstream's own compose mounts :ro too.
      '';
    };

    # ── Resources ─────────────────────────────────────────────────────────
    # ⚠️ A systemd MemoryMax on docker-*.service constrains NOTHING — dockerd
    # puts container processes in their own /system.slice/docker-<id>.scope.
    # The limit has to be the docker --memory flag. (Learned the hard way on
    # mempool-api, which reached 3.6 GB RSS unbounded.)
    memoryLimit = lib.mkOption {
      type = lib.types.str;
      default = "4g";
      description = "Hard memory cap for the Silo server container (docker --memory).";
    };

    postgresMemoryLimit = lib.mkOption {
      type = lib.types.str;
      default = "2g";
      description = "Hard memory cap for the Postgres container.";
    };

    postgresShmSize = lib.mkOption {
      type = lib.types.str;
      default = "1g";
      description = ''
        Shared-memory size for Postgres. ⚠️ Upstream's compose defaults this to
        8gb, which is sized for a petabyte-library deployment, not an
        evaluation. Raise it if Postgres actually complains.
      '';
    };

    # ── Secrets ───────────────────────────────────────────────────────────
    environmentFile = lib.mkOption {
      type = lib.types.path;
      default = "/run/secrets/silo-env";
      description = ''
        Env file holding SECRET_KEY and POSTGRES_PASSWORD, kept out of the
        world-readable Nix store.

        ⚠️ SECRET_KEY IS NOT REGENERABLE. Silo uses it for at-rest encryption of
        stored credentials and refuses to start without it; losing it makes
        those secrets unrecoverable. Back it up SEPARATELY from database dumps.
        This is NOT like the Authelia hash — never "just generate a new one".

        Generate once:  openssl rand -base64 48
      '';
    };

    hostName = lib.mkOption {
      type = lib.types.str;
      default = "silo.rosemaryacres.com";
      description = "Vhost for Silo's own web UI.";
    };

    jellyfinCompatHostName = lib.mkOption {
      type = lib.types.str;
      default = "silo-jf.rosemaryacres.com";
      description = "Vhost for the Jellyfin-compat listener, so TLS clients (Infuse etc.) can reach it.";
    };
  };

  config = lib.mkIf cfg.enable {
    # Fail at EVAL time rather than at 3 a.m. in a container restart loop.
    assertions = [
      {
        assertion = cfg.jellyfinCompatPort != 8096;
        message = ''
          services.silo.jellyfinCompatPort is 8096, which is the real Jellyfin
          on this host. Pick another port — the point of the compat listener is
          to run ALONGSIDE Jellyfin during the evaluation, not to replace it.
        '';
      }
      {
        assertion = cfg.port != cfg.jellyfinCompatPort;
        message = "services.silo.port and .jellyfinCompatPort must differ.";
      }
      {
        assertion = cfg.mediaRoots != { };
        message = "services.silo.mediaRoots is empty — Silo would start with no library at all.";
      }
    ];

    # State dirs. The transcode dir is intentionally created here too, so a
    # missing /mnt/scratch surfaces as a tmpfiles failure rather than docker
    # silently creating a root-owned directory on the wrong filesystem.
    systemd.tmpfiles.rules = [
      "d ${cfg.dataDir}            0750 root root -"
      "d ${cfg.dataDir}/postgres   0750 root root -"
      "d ${cfg.dataDir}/redis      0750 root root -"
      "d ${cfg.dataDir}/plugins    0750 root root -"
      "d ${cfg.dataDir}/artwork    0750 root root -"
      "d ${cfg.dataDir}/compat     0750 root root -"
      "d ${cfg.transcodeDir}       0750 root root -"
    ];

    virtualisation.oci-containers.containers = {
      silo-db = {
        image = cfg.postgresImage;
        environment = {
          POSTGRES_USER = "silo";
          POSTGRES_DB = "silo";
        };
        # POSTGRES_PASSWORD comes from the env file, not the store.
        environmentFiles = [ cfg.environmentFile ];
        # Upstream mounts the whole /var/lib/postgresql, not .../data — keep it
        # identical or the container initialises an empty cluster next to the
        # real one.
        volumes = [ "${cfg.dataDir}/postgres:/var/lib/postgresql" ];
        # Deliberately NOT published to the host: 5432 is the system Postgres 14.
        # Silo reaches this over ${siloNet} by container name.
        cmd = [ "postgres" "-c" "listen_addresses=*" ];
        extraOptions = common ++ [
          "--memory=${cfg.postgresMemoryLimit}"
          "--shm-size=${cfg.postgresShmSize}"
          "--health-cmd=pg_isready -U silo"
          "--health-interval=5s"
          "--health-timeout=3s"
          "--health-retries=5"
        ];
      };

      silo-redis = {
        image = cfg.redisImage;
        volumes = [ "${cfg.dataDir}/redis:/data" ];
        # Also not published — 6379 stays free on the host.
        extraOptions = common ++ [
          "--memory=512m"
          "--health-cmd=redis-cli ping"
          "--health-interval=5s"
          "--health-timeout=3s"
          "--health-retries=5"
        ];
      };

      silo = {
        image = cfg.image;
        dependsOn = [ "silo-db" "silo-redis" ];
        environment = {
          MODE = "integrated";
          # Container-side listeners are FIXED at upstream's values; the host
          # side is what we remap. Changing these would diverge from upstream.
          PORT = "8080";
          JF_PORT = "8096";
          DATABASE_URL = "postgres://silo@silo-db:5432/silo?sslmode=disable";
          REDIS_URL = "redis://silo-redis:6379";
          SILO_PLUGIN_CACHE_DIR = "/var/lib/silo/plugins";
          POSTGRES_TUNE = "auto";
        };
        # SECRET_KEY + the password half of DATABASE_URL live here.
        environmentFiles = [ cfg.environmentFile ];

        ports =
          [
            "${cfg.bindAddress}:${toString cfg.port}:8080"
            "${cfg.bindAddress}:${toString cfg.jellyfinCompatPort}:8096"
          ]
          ++ lib.optional (cfg.audiobookshelfCompatPort != null)
            "${cfg.bindAddress}:${toString cfg.audiobookshelfCompatPort}:13378";

        volumes =
          # Media, always read-only.
          (lib.mapAttrsToList (ctr: host: "${host}:${ctr}:ro") cfg.mediaRoots)
          ++ [
            "${cfg.dataDir}/plugins:/var/lib/silo/plugins"
            "${cfg.dataDir}/artwork:/var/lib/silo/artwork"
            "${cfg.dataDir}/compat:/var/lib/silo/compat"
            "${cfg.transcodeDir}:/tmp/silo-transcode"
            # Upstream passes these so resource detection reads the container's
            # share rather than the whole machine. Load-bearing only when the
            # docker host is itself an LXC; harmless on bare metal, where they
            # are the same files Silo would read anyway. Kept for parity with
            # upstream — POSTGRES_TUNE=auto reads them.
            "/proc/meminfo:/host/proc/meminfo:ro"
            "/proc/stat:/host/proc/stat:ro"
            "/proc/loadavg:/host/proc/loadavg:ro"
          ];

        extraOptions = common ++ [ "--memory=${cfg.memoryLimit}" ];
      };
    };

    # ⚠️ ONE systemd.services attrset. Declaring `systemd.services.silo-network`
    # separately and then `systemd.services = ...` is "attribute already
    # defined" at eval — Nix does not merge two bindings of the same path
    # inside one attrset literal. (Caught by the module eval, not by reading.)
    systemd.services =
      # Containers must not start before the bridge exists. The unit prefix
      # follows the configured backend (docker-* vs podman-*) rather than being
      # hardcoded: an ordering edge naming a unit that does not exist is NOT an
      # error, so a hardcoded "docker-" would fail silently under podman.
      lib.genAttrs
        (map (n: "${ociBackend}-${n}") [ "silo-db" "silo-redis" "silo" ])
        (_: {
          after = [ "silo-network.service" ];
          requires = [ "silo-network.service" ];
        })
      // {
        silo-network = {
          description = "Create the ${siloNet} container network for Silo";
          after = [ "${ociBackend}.service" ];
          requires = [ "${ociBackend}.service" ];
          wantedBy = [ "multi-user.target" ];
          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
          };
          # `network create` is not idempotent, so inspect first. A failed
          # create must fail the unit — otherwise the containers start against
          # a missing network and crash-loop with a confusing error.
          script = ''
            ${ociBin}/bin/${ociBackend} network inspect ${siloNet} >/dev/null 2>&1 \
              || ${ociBin}/bin/${ociBackend} network create ${siloNet}
          '';
        };
      };

    services.nginx.virtualHosts.${cfg.hostName} =
      import ../lib/proxy-vhost.nix {
        port = cfg.port;
        extraConfig = ''
          # Media streaming: don't buffer, and don't time out a long seek.
          proxy_buffering off;
          proxy_request_buffering off;
          proxy_read_timeout 600s;
          proxy_send_timeout 600s;
          client_max_body_size 20M;
        '';
      };

    services.nginx.virtualHosts.${cfg.jellyfinCompatHostName} =
      import ../lib/proxy-vhost.nix {
        port = cfg.jellyfinCompatPort;
        extraConfig = ''
          proxy_buffering off;
          proxy_request_buffering off;
          proxy_read_timeout 600s;
          proxy_send_timeout 600s;
          client_max_body_size 20M;
        '';
      };
  };
}
