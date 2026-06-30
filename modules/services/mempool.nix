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
  # NOTE: mempool's db.env + rpc.env are deliberately NOT in sops — they're
  # runtime-generated/self-managing (mempool-db-secrets generates+preserves the
  # MariaDB creds coupled to the DB volume; mempool-cookie-sync rewrites rpc.env
  # with the rotating bitcoind cookie on each restart). They're already kept out
  # of git + the Nix store, so sops would only add a fragile second source of
  # truth. Left as /var/lib/mempool/*.env on purpose.
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
        # bitcoind cookie auth = HTTP basic with user "__cookie__" and the cookie
        # password. CORE_RPC_PASSWORD is injected from rpc.env (written fresh by
        # mempool-cookie-sync on each bitcoind restart); this container is partOf
        # bitcoind, so it restarts and re-reads the rotated password.
        CORE_RPC_USERNAME = "__cookie__";
        DATABASE_ENABLED  = "true";
        # mempool backend uses DATABASE_* env names (NOT MYSQL_*); with MYSQL_*
        # it silently fell back to 127.0.0.1:3306 and crash-looped.
        DATABASE_HOST     = "mempool-db";
        DATABASE_PORT     = "3306";
        DATABASE_DATABASE = "mempool";
        DATABASE_USERNAME = "mempool";
        STATISTICS_ENABLED = "true";
      };
      # DATABASE_PASSWORD (db.env) + CORE_RPC_PASSWORD (rpc.env) come from
      # generated env files, kept out of the Nix store.
      environmentFiles = [ "/var/lib/mempool/db.env" "/var/lib/mempool/rpc.env" ];
      volumes = [
        "/var/lib/mempool/cache:/backend/cache"
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

  # Cookie-shim: every bitcoind restart rewrites the cookie. Write the cookie
  # password into an env file (CORE_RPC_PASSWORD=...) the mempool-api reads.
  systemd.services.mempool-cookie-sync = {
    description = "Write bitcoind cookie password into mempool rpc.env";
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
      # Cookie format: __cookie__:<password>. Strip the prefix; emit as env.
      pw=$(cut -d: -f2 /mnt/fusion/bitcoind/.cookie)
      umask 077
      printf 'CORE_RPC_PASSWORD=%s\n' "$pw" > /var/lib/mempool/rpc.env
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
    path = [ pkgs.coreutils pkgs.gnugrep ];
    script = ''
      set -e
      envf=/var/lib/mempool/db.env
      umask 077
      if [ -s "$envf" ]; then
        # Preserve existing creds (the DB volume was initialised with them);
        # just regenerate the file so DATABASE_PASSWORD mirrors MARIADB_PASSWORD.
        pw=$(grep '^MARIADB_PASSWORD=' "$envf" | cut -d= -f2-)
        rootpw=$(grep '^MARIADB_ROOT_PASSWORD=' "$envf" | cut -d= -f2-)
      else
        gen() { tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32; }
        pw=$(gen); rootpw=$(gen)
      fi
      # mempool-db reads MARIADB_*; mempool-api reads DATABASE_PASSWORD — same value.
      printf 'MARIADB_PASSWORD=%s\nMARIADB_ROOT_PASSWORD=%s\nDATABASE_PASSWORD=%s\n' \
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

  # Self-heal MariaDB's transaction-coordinator log. An unclean shutdown (e.g. a
  # USB pool drive dropping mid-write + reboot) can corrupt tc.log →
  # "Can't init tc log / Crash recovery failed" → mempool-db crash-loops to the
  # start-limit and api/web cascade-fail. tc.log is XA-coordinator scratch, not
  # data (single non-XA instance; InnoDB's own redo log handles real crash
  # recovery), so clearing a stale one before launch is safe and lets the DB
  # start clean. Runs before every db start (requires/before below).
  systemd.services.mempool-db-tclog-clean = {
    description = "Clear stale MariaDB tc.log before mempool-db starts (unclean-shutdown self-heal)";
    before = [ "docker-mempool-db.service" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${pkgs.coreutils}/bin/rm -f /var/lib/mempool/mysql/tc.log";
    };
  };

  # Ordering: containers must wait for their prerequisite oneshots (the docker
  # network, the env file, the staged cookie, the tc.log self-heal) so docker
  # doesn't fail to start or bind-mount a missing path.
  systemd.services.docker-mempool-db = {
    after = [ "init-mempool-net.service" "mempool-db-secrets.service" "mempool-db-tclog-clean.service" ];
    requires = [ "init-mempool-net.service" "mempool-db-secrets.service" "mempool-db-tclog-clean.service" ];
  };
  # (api/web also order after the tc.log self-heal — harmless, and the changed
  # unit definition makes a deploy restart them too, so merging this recovers the
  # whole stack from the current failed/inactive state, not just the db.)
  systemd.services.docker-mempool-api = {
    after = [ "init-mempool-net.service" "mempool-db-secrets.service" "mempool-cookie-sync.service" "mempool-db-tclog-clean.service" ];
    requires = [ "init-mempool-net.service" "mempool-db-secrets.service" "mempool-cookie-sync.service" ];
    # Restart with bitcoind so it re-reads the freshly-staged cookie.
    partOf = [ "bitcoind-bitcoin.service" ];
  };
  systemd.services.docker-mempool-web = {
    after = [ "init-mempool-net.service" "mempool-db-tclog-clean.service" ];
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
