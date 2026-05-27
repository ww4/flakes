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
        MARIADB_DATABASE     = "mempool";
        MARIADB_USER         = "mempool";
        MARIADB_PASSWORD     = "mempool";              # local-only network
        MARIADB_ROOT_PASSWORD = "admin";
      };
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
        MYSQL_PASSWORD = "mempool";
        STATISTICS_ENABLED = "true";
      };
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
