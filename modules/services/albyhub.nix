# Alby Hub — self-hosted Lightning node + Nostr Wallet Connect server.
# https://alby.rosemaryacres.com
#
# Uses LDK-node (embedded Rust LN library, no separate lnd/cld required).
# State lives at /var/lib/albyhub (moved from /home/chris/.local/share/albyhub
# in the 2026-08 audit privsep — the Lightning keys were sitting in chris's
# home under a unit running as chris with no sandboxing; a bug in the daemon
# had chris's SSH/GPG/wallets in reach). Backed up in tier-1; the old home
# path is kept on disk untouched as a fallback until the migration is
# confirmed good.
#
# LN P2P listens on 0.0.0.0:9735 by default — outbound channels work fine
# without that port forwarded from the WAN. To accept inbound channels
# you'd need a NAT rule on the router; not configured here.
{ config, lib, pkgs, ... }:

{
  users.users.albyhub = {
    isSystemUser = true;
    group = "albyhub";
    home = "/var/lib/albyhub";
  };
  users.groups.albyhub = { };

  # One-time state migration, ordered before the daemon. Copy — never move —
  # so the original stays as the rollback path; a marker file records that
  # the copy happened. Runs as root (it must read chris's home and chown to
  # albyhub); refuses to overwrite a non-empty destination.
  systemd.services.albyhub-state-migrate = {
    description = "one-time: Alby Hub state from chris's home to /var/lib";
    before = [ "albyhub.service" ];
    requiredBy = [ "albyhub.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      old=/home/chris/.local/share/albyhub
      new=/var/lib/albyhub
      ${pkgs.coreutils}/bin/mkdir -p "$new"
      if [ -n "$(${pkgs.coreutils}/bin/ls -A "$new")" ]; then
        ${pkgs.coreutils}/bin/chown -R albyhub:albyhub "$new"
        exit 0   # already migrated (or already living there)
      fi
      if [ -d "$old" ] && [ -n "$(${pkgs.coreutils}/bin/ls -A "$old")" ]; then
        echo "copying Alby Hub state from $old..."
        ${pkgs.coreutils}/bin/cp -a "$old/." "$new/"
        ${pkgs.coreutils}/bin/touch "$old/.migrated-to-var-lib-2026-08-17"
      fi
      ${pkgs.coreutils}/bin/chown -R albyhub:albyhub "$new"
    '';
  };

  systemd.services.albyhub = {
    description = "Alby Hub Lightning node + NWC server";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" "albyhub-state-migrate.service" ];
    wants = [ "network-online.target" ];
    requires = [ "albyhub-state-migrate.service" ];
    environment = {
      WORK_DIR = "/var/lib/albyhub";
      PORT = "8086";          # 8080 is taken
      # LDK_NETWORK omitted on purpose — Alby Hub defaults to "bitcoin"
      # and passes the literal env-var string to api.getalby.com's LSP
      # API. The LSPs are registered under "bitcoin", so setting
      # "mainnet" (even though LDK accepts it via the switch fallthrough)
      # makes GetLSPInfo call /internal/lsp/<lsp>/mainnet/v1/get_info
      # which 404s. Keep the default.
    };
    serviceConfig = {
      # Reference via Nix so the path tracks rebuilds. The binary is in
      # chris's home-manager package set rather than system PATH, so
      # /run/current-system/sw/bin/albyhub doesn't resolve.
      ExecStart = "${pkgs.albyhub}/bin/albyhub";
      User = "albyhub";
      Group = "albyhub";
      StateDirectory = "albyhub";
      Restart = "on-failure";
      RestartSec = "30s";
      # Basic fences a Lightning key store deserves: no privilege regain, a
      # private /tmp, read-only system, chris's home invisible.
      NoNewPrivileges = true;
      PrivateTmp = true;
      ProtectSystem = "strict";
      ProtectHome = true;
      # Memory ceiling — LDK + Tantivy index can grow with channel count.
      MemoryMax = "1G";
    };
  };

  services.nginx.virtualHosts."alby.rosemaryacres.com" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:8086";
      recommendedProxySettings = true;
      proxyWebsockets = true;   # NWC + UI use websockets
    };
  };
}
