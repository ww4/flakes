# Fulcrum — fast Electrum protocol server. Indexes the bitcoind chain so
# Sparrow (and any other Electrum-protocol client) can query addresses
# privately against your own node instead of leaking history to public
# servers.
#
# Built on top of bitcoind running with txindex=1 (see bitcoind.nix).
# First-time index build takes ~6-12 h on top of an already-synced chain;
# subsequent startups are seconds. Final on-disk index ~100 GB.
#
# Sparrow setup: Preferences → Server → Electrum, host=gromit.local
# (or the Tailscale IP), port 50002, SSL.
{ config, lib, pkgs, ... }:

{
  services.fulcrum = {
    enable = true;
    # The cookie file bitcoind writes is the cheapest auth method — Fulcrum
    # reads it directly so we don't need to hard-code RPC creds.
    settings = {
      datadir = "/var/lib/fulcrum";

      # bitcoind RPC connection
      bitcoind = "127.0.0.1:8332";
      rpccookie = "/mnt/fusion/bitcoind/.cookie";

      # Listen sockets — bind localhost; nginx fronts the SSL/WSS variants
      # for external clients via tls.rosemaryacres.com if you want them.
      tcp = "127.0.0.1:50001";
      ssl = "127.0.0.1:50002";
      # SSL cert for the Electrum TLS port — Fulcrum's own self-signed is
      # fine for personal use because Sparrow asks once and pins.
      cert  = "/var/lib/fulcrum/fulcrum.crt";
      key   = "/var/lib/fulcrum/fulcrum.key";

      # WebSocket variants for in-browser clients (bitcoin block explorers,
      # mempool.space if you ever want it via WSS).
      ws  = "127.0.0.1:50003";
      wss = "127.0.0.1:50004";

      # Resource hints. fast-sync trades RAM for speed during initial index.
      fast-sync = 4000;          # MB of RAM for initial index build
    };
  };

  # Self-signed cert on first start (the NixOS module doesn't bundle one).
  systemd.services.fulcrum.serviceConfig.ExecStartPre = lib.mkAfter [
    "+${pkgs.writeShellScript "fulcrum-selfsigned" ''
      set -e
      cd /var/lib/fulcrum
      [ -f fulcrum.crt ] && [ -f fulcrum.key ] && exit 0
      ${pkgs.openssl}/bin/openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout fulcrum.key -out fulcrum.crt -days 3650 \
        -subj "/CN=fulcrum.gromit"
      chown fulcrum:fulcrum fulcrum.crt fulcrum.key
      chmod 0600 fulcrum.key
    ''}"
  ];

  systemd.services.fulcrum.unitConfig.RequiresMountsFor = "/mnt/fusion";
  systemd.services.fulcrum.unitConfig.After = [ "bitcoind-bitcoin.service" ];

  # Tailscale + docker bridges only — never the LAN, never the internet.
  networking.firewall.interfaces."tailscale0".allowedTCPPorts = [ 50001 50002 50003 50004 ];
  networking.firewall.extraCommands = ''
    iptables -I nixos-fw 1 -i br-+ -p tcp --dport 50001 -j nixos-fw-accept
  '';
}
