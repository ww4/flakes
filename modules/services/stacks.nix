# stacks — the physical book catalog.
#
# What it is for: at a library book sale, scan a barcode and know within a
# second whether this house already owns the book — in ANY edition — and
# whether the 2025 flood destroyed a copy that was never replaced. Also the
# shelf inventory, the loans, and the wishlist.
#
# The one design point worth knowing before touching this: a holding imported
# from the pre-flood Libib export is `unverified`, never `present`. The export
# says what was owned in 2023, not what survived the water, so the scanner
# reports CAUTION rather than SKIP for those. Only a physical scan promotes a
# book to `present`. Getting that backwards sends someone home without a book
# they no longer own, which is the failure the whole system exists to prevent.
#
# Reachability follows the house posture: nginx + ACME on the rosemaryacres
# domain, gated to Tailscale/LAN by modules/services/nginx-access.nix. Nothing
# here is published to the WAN.
#
# TLS is not cosmetic. The browser's BarcodeDetector API is unavailable outside
# a secure context, so over plain HTTP the camera scanner silently does not
# exist — the page falls back to typing with no visible reason why.
{ config, lib, pkgs, ... }:

let
  package = pkgs.callPackage ../../pkgs/stacks { };

  port = 8099;
  domain = "books.rosemaryacres.com";

  user = "stacks";
  dbName = "stacks";

  # Cover art fetched from Open Library, kept forever. Open Library rate-limits
  # cover requests BY IDENTIFIER to 100 per IP per five minutes, so re-fetching
  # on demand is not an option; ~32 MB for this library.
  stateDir = "/var/lib/stacks";
in
{
  # --- database ---------------------------------------------------------
  services.postgresql = {
    enable = true;
    ensureDatabases = [ dbName ];
    ensureUsers = [{
      name = user;
      ensureDBOwnership = true;
    }];
  };

  users.users.${user} = {
    isSystemUser = true;
    group = user;
    home = stateDir;
  };
  users.groups.${user} = { };

  # --- service ----------------------------------------------------------
  systemd.services.stacks = {
    description = "stacks — physical book catalog";
    wantedBy = [ "multi-user.target" ];
    after = [ "network.target" "postgresql.service" ];
    requires = [ "postgresql.service" ];

    environment = {
      # Peer auth over the unix socket: the service runs as `stacks` and owns
      # the database, so no password exists to leak or rotate.
      DATABASE_URL = "postgresql+psycopg:///${dbName}";

      # Open Library asks clients to identify themselves and to cache rather
      # than hammer. Both are courtesy requirements of a free non-profit and
      # this service honours them.
      STACKS_OL_USER_AGENT =
        "stacks/0.1 (personal book catalog; chris.saenz@broadlinc.com)";
      STACKS_COVER_CACHE_DIR = "${stateDir}/covers";

      PYTHONUNBUFFERED = "1";
    };

    # Migrations run before the app, every start. Alembic rather than
    # create_all because create_all cannot alter an existing Postgres enum:
    # adding a value silently does nothing and only surfaces later as an
    # insert error.
    preStart = "${lib.getExe package} initdb";

    serviceConfig = {
      ExecStart = "${lib.getExe package} serve --host 127.0.0.1 --port ${toString port}";
      User = user;
      Group = user;
      Restart = "on-failure";
      RestartSec = "10s";

      StateDirectory = "stacks";
      StateDirectoryMode = "0750";
      WorkingDirectory = stateDir;

      # --- hardening ----------------------------------------------------
      NoNewPrivileges = true;
      PrivateTmp = true;
      PrivateDevices = true;
      ProtectSystem = "strict";
      ProtectHome = true;
      ProtectKernelTunables = true;
      ProtectKernelModules = true;
      ProtectControlGroups = true;
      ProtectClock = true;
      ProtectHostname = true;
      ProtectProc = "invisible";
      RestrictNamespaces = true;
      RestrictRealtime = true;
      RestrictSUIDSGID = true;
      LockPersonality = true;
      MemoryDenyWriteExecute = true;
      SystemCallArchitectures = "native";
      SystemCallFilter = [ "@system-service" "~@privileged" "~@resources" ];
      ReadWritePaths = [ stateDir ];
    };
  };

  # --- publication ------------------------------------------------------
  services.nginx.virtualHosts.${domain} = {
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

        # The offline catalog is ~7.5 MB of JSON that compresses to ~1.5 MB,
        # and the whole premise of handing the phone everything is that
        # everything compresses. The app sets its own gzip; this only raises
        # the ceiling so a large body is not truncated or buffered to disk.
        client_max_body_size 16m;
        proxy_read_timeout 120s;
      '';
    };
  };

  # Cover art is the bulk of the state and is re-fetchable from Open Library,
  # but the catalog itself is not: it is the only record of what the flood
  # destroyed. Postgres is already captured by the nightly pg_dump in
  # modules/services/backup.nix.
}
