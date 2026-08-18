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

  # Shared by the server and the cover-warming job, so the two cannot drift
  # into reading different databases or writing art to different directories —
  # which would be invisible until the browse page came up blank.
  commonEnv = {
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

  commonHardening = {
    User = user;
    Group = user;

    StateDirectory = "stacks";
    StateDirectoryMode = "0750";
    WorkingDirectory = stateDir;

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

    environment = commonEnv;

    # Migrations run before the app, every start. Alembic rather than
    # create_all because create_all cannot alter an existing Postgres enum:
    # adding a value silently does nothing and only surfaces later as an
    # insert error.
    preStart = "${lib.getExe package} initdb";

    serviceConfig = commonHardening // {
      ExecStart = "${lib.getExe package} serve --host 127.0.0.1 --port ${toString port}";
      Restart = "on-failure";
      RestartSec = "10s";
    };
  };

  # --- cover art --------------------------------------------------------
  # Browsing a shelf of grey placeholders is a different thing from browsing a
  # shelf of books, so the art matters more than it sounds. It is fetched
  # rather than shipped: a cover is ~12 KB and there are a couple of thousand
  # of them, and Open Library is the source of record anyway.
  #
  # A timer rather than a one-off because the catalog keeps growing — every
  # book added by scanning an unknown barcode at a sale arrives with no art,
  # and nobody should have to remember to go and get it.
  systemd.services.stacks-warm-covers = {
    description = "stacks — fetch missing cover art from Open Library";
    after = [ "network-online.target" "postgresql.service" ];
    wants = [ "network-online.target" ];

    environment = commonEnv;

    serviceConfig = commonHardening // {
      Type = "oneshot";
      ExecStart = "${lib.getExe package} warm-covers --limit 4000";

      # Covers already on disk are skipped, and books Open Library genuinely
      # has no art for are remembered in known-missing.txt — so a run after
      # the first is nearly free. The first one is not: art without a known
      # cover id has to go by ISBN, which their limit paces at one every
      # three seconds. Roughly two hours from empty, once.
      #
      # TimeoutStartSec, not RuntimeMaxSec. A oneshot spends its whole life in
      # "activating", so RuntimeMaxSec never applies to it — systemd said so
      # in the journal the moment this deployed ("RuntimeMaxSec= has no effect
      # in combination with Type=oneshot. Ignoring."), leaving the job with no
      # bound at all. The default would have been 90s, which would have killed
      # it mid-run; oneshot happens to default to infinity instead, so the
      # visible symptom was nothing rather than a broken job.
      TimeoutStartSec = "4h";

      # Nice to the box: this is background housekeeping and must never
      # compete with the server answering a scan at a book sale.
      Nice = 10;
      IOSchedulingClass = "idle";
    };
  };

  systemd.timers.stacks-warm-covers = {
    description = "stacks — keep cover art warm";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      # Daily is plenty: it exists to pick up newly added books.
      OnCalendar = "daily";

      # Also shortly after this unit is first installed, so a fresh deploy
      # fills itself in without anyone being told to go and start it.
      OnActiveSec = "10min";

      # Catch up a run missed while the box was off, and spread the start so
      # we are not knocking on Open Library's door at exactly midnight.
      Persistent = true;
      RandomizedDelaySec = "45min";
    };
  };

  # --- publication ------------------------------------------------------
  services.nginx.virtualHosts.${domain} =
    import ../lib/proxy-vhost.nix {
      inherit port;
      extraConfig = ''

        # The offline catalog is ~7.5 MB of JSON that compresses to ~1.5 MB,
        # and the whole premise of handing the phone everything is that
        # everything compresses. The app sets its own gzip; this only raises
        # the ceiling so a large body is not truncated or buffered to disk.
        client_max_body_size 16m;
        proxy_read_timeout 120s;
      '';
    };

  # Cover art is the bulk of the state and is re-fetchable from Open Library,
  # but the catalog itself is not: it is the only record of what the flood
  # destroyed. The database is dumped nightly via postgresqlBackup ("stacks"
  # in the databases list, nextcloud.nix) and the dump dir plus
  # /var/lib/stacks ride restic via criticalPaths in backup.nix. (Until
  # 2026-08-17 this comment claimed coverage that did not exist — the
  # 2026-08 code audit found the DB in no backup job at all.)
}
