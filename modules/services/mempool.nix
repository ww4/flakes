# mempool.space — self-hosted block explorer at https://mempool.rosemaryacres.com
#
# Stack: mariadb + backend (Node.js) + frontend (Nginx). The official
# project ships Docker compose; this module deploys those three images via
# NixOS's oci-containers framework so everything stays declarative.
#
# Prereqs (set up in bitcoind.nix + fulcrum.nix):
#   - bitcoind with txindex=1, RPC on 127.0.0.1:8332, ZMQ on :28332/:28333
#   - Fulcrum on 127.0.0.1:50001
#
# mempool will start serving as soon as Fulcrum is reachable; backend
# catches up on historical blocks for ~30-60 min after that.
{ config, lib, pkgs, ... }:

let
  mempoolNet = "mempool-net";
in
{
  virtualisation.oci-containers.containers = {
    mempool-db = {
      image = "mariadb:11";
      environment = {
        MARIADB_DATABASE = "mempool";
        MARIADB_USER     = "mempool";
      };
      # MARIADB_PASSWORD / MARIADB_ROOT_PASSWORD come from a generated env file
      # (mempool-db-secrets, below) so no secrets land in the world-readable
      # Nix store. The DB is on an internal docker network, but kept out of the
      # store on principle (per the security review).
      environmentFiles = [ "/var/lib/mempool/db.env" ];
      volumes = [ "/var/lib/mempool/mysql:/var/lib/mysql" ];
      extraOptions = [ "--network=${mempoolNet}" ];
    };

    mempool-api = {
      image = "mempool/backend:latest";
      dependsOn = [ "mempool-db" ];
      environment = {
        MEMPOOL_BACKEND = "electrum";
        ELECTRUM_HOST   = "172.17.0.1";              # docker0 host gateway
        ELECTRUM_PORT   = "50001";
        ELECTRUM_TLS_ENABLED = "false";
        CORE_RPC_HOST   = "172.17.0.1";
        CORE_RPC_PORT   = "8332";
        CORE_RPC_USERNAME = "__cookie__";
        # cookie value is filled in at runtime by the systemd ExecStartPre
        # below — it changes every bitcoind restart, so we read it fresh.
        CORE_RPC_PASSWORD_FILE = "/run/mempool-cookie";
        DATABASE_ENABLED = "true";
        MYSQL_HOST     = "mempool-db";
        MYSQL_DATABASE = "mempool";
        MYSQL_USER     = "mempool";
        STATISTICS_ENABLED = "true";
      };
      # MYSQL_PASSWORD comes from the same generated env file as mempool-db.
      environmentFiles = [ "/var/lib/mempool/db.env" ];
      volumes = [
        "/var/lib/mempool/cache:/backend/cache"
        "/run/mempool-cookie:/run/mempool-cookie:ro"
      ];
      extraOptions = [ "--network=${mempoolNet}" ];
    };

    mempool-web = {
      image = "mempool/frontend:latest";
      dependsOn = [ "mempool-api" ];
      environment = {
        FRONTEND_HTTP_PORT = "8080";
        BACKEND_MAINNET_HTTP_HOST = "mempool-api";
      };
      ports = [ "127.0.0.1:8081:8080" ];     # nginx fronts this (8090 is ntfy)
      extraOptions = [ "--network=${mempoolNet}" ];
    };
  };

  # Cookie-shim: every bitcoind restart rewrites the cookie. Copy it into
  # a fixed path the mempool-api container can read.
  systemd.services.mempool-cookie-sync = {
    description = "Stage bitcoind cookie for mempool-api";
    wantedBy = [ "multi-user.target" ];
    after = [ "bitcoind-bitcoin.service" ];
    requires = [ "bitcoind-bitcoin.service" ];
    # Re-stage the cookie whenever bitcoind restarts (it rotates the cookie).
    partOf = [ "bitcoind-bitcoin.service" ];
    path = [ pkgs.coreutils ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      set -e
      until [ -f /mnt/fusion/bitcoind/.cookie ] ; do sleep 2 ; done
      # Cookie format: __cookie__:<password>. Strip prefix.
      cut -d: -f2 /mnt/fusion/bitcoind/.cookie > /run/mempool-cookie
      chmod 0644 /run/mempool-cookie
    '';
  };

  # Generate the MariaDB credentials once into a root-only env file the two
  # containers read — keeps secrets out of the Nix store. Runs before the db
  # (which initialises with them) and the api (which connects with them).
  systemd.services.mempool-db-secrets = {
    description = "Generate mempool MariaDB credentials (out of the Nix store)";
    wantedBy = [ "multi-user.target" ];
    before = [ "docker-mempool-db.service" "docker-mempool-api.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      set -e
      envf=/var/lib/mempool/db.env
      [ -s "$envf" ] && exit 0
      gen() { ${pkgs.coreutils}/bin/tr -dc 'A-Za-z0-9' < /dev/urandom | ${pkgs.coreutils}/bin/head -c 32; }
      pw=$(gen); rootpw=$(gen)
      umask 077
      ${pkgs.coreutils}/bin/printf 'MARIADB_PASSWORD=%s\nMARIADB_ROOT_PASSWORD=%s\nMYSQL_PASSWORD=%s\n' \
        "$pw" "$rootpw" "$pw" > "$envf"
    '';
  };

  # The containers share a user-defined docker network (for inter-container DNS
  # by name). oci-containers doesn't create it, so make it here, idempotently.
  systemd.services.init-mempool-net = {
    description = "Create the mempool-net docker network";
    after = [ "docker.service" ];
    requires = [ "docker.service" ];
    before = [ "docker-mempool-db.service" "docker-mempool-api.service" "docker-mempool-web.service" ];
    wantedBy = [ "multi-user.target" ];
    path = [ pkgs.docker ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      docker network inspect ${mempoolNet} >/dev/null 2>&1 || docker network create ${mempoolNet}
    '';
  };

  # Ordering: containers must wait for their prerequisite oneshots (the docker
  # network, the env file, the staged cookie) so docker doesn't fail to start
  # or bind-mount a missing path.
  systemd.services.docker-mempool-db = {
    after = [ "init-mempool-net.service" "mempool-db-secrets.service" ];
    requires = [ "init-mempool-net.service" "mempool-db-secrets.service" ];
  };
  systemd.services.docker-mempool-api = {
    after = [ "init-mempool-net.service" "mempool-db-secrets.service" "mempool-cookie-sync.service" ];
    requires = [ "init-mempool-net.service" "mempool-db-secrets.service" "mempool-cookie-sync.service" ];
    # Restart with bitcoind so it re-reads the freshly-staged cookie.
    partOf = [ "bitcoind-bitcoin.service" ];
  };
  systemd.services.docker-mempool-web = {
    after = [ "init-mempool-net.service" ];
    requires = [ "init-mempool-net.service" ];
  };

  systemd.tmpfiles.rules = [
    "d /var/lib/mempool         0755 root root - -"
    "d /var/lib/mempool/mysql   0755 root root - -"
    "d /var/lib/mempool/cache   0755 root root - -"
  ];

  services.nginx.virtualHosts."mempool.rosemaryacres.com" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:8081";    # frontend
      recommendedProxySettings = true;
      proxyWebsockets = true;
    };
  };
}
