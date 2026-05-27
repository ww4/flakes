# Alby Hub — self-hosted Lightning node + Nostr Wallet Connect server.
# https://alby.rosemaryacres.com
#
# Uses LDK-node (embedded Rust LN library, no separate lnd/cld required).
# State lives at /home/chris/.local/share/albyhub. Backed up in tier-1.
#
# LN P2P listens on 0.0.0.0:9735 by default — outbound channels work fine
# without that port forwarded from the WAN. To accept inbound channels
# you'd need a NAT rule on the router; not configured here.
{ config, lib, pkgs, ... }:

{
  systemd.services.albyhub = {
    description = "Alby Hub Lightning node + NWC server";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    environment = {
      WORK_DIR = "/home/chris/.local/share/albyhub";
      PORT = "8086";          # 8080 is taken
      LDK_NETWORK = "mainnet";
    };
    serviceConfig = {
      ExecStart = "/run/current-system/sw/bin/albyhub";
      User = "chris";
      Group = "users";
      Restart = "on-failure";
      RestartSec = "30s";
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
