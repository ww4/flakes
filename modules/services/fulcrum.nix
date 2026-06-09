# Fulcrum — fast Electrum protocol server. Indexes the bitcoind chain so the
# mempool.space backend (and Electrum clients like Sparrow) can query it.
#
# NOTE: this nixpkgs has NO `services.fulcrum` module — only the `fulcrum`
# package — so this is a hand-rolled systemd unit. Built on bitcoind running
# with txindex=1 (see bitcoind.nix). First-time index build takes ~6–12 h on
# top of an already-synced chain; subsequent startups are seconds. Final
# on-disk index ~100 GB under /var/lib/fulcrum (nvme root has ~329 GB free).
#
# Scope: tcp (50001) only, which is what the mempool backend uses. The TLS
# port for Sparrow-over-Tailscale (ssl 50002 + self-signed cert) is a later
# add-on — out of scope for the mempool bring-up.
{ config, lib, pkgs, ... }:

let
  dataDir = "/var/lib/fulcrum";
  # Fulcrum reads a simple `key = value` config file.
  fulcrumConf = pkgs.writeText "fulcrum.conf" ''
    datadir = ${dataDir}
    bitcoind = 127.0.0.1:8332
    rpccookie = ${dataDir}/.cookie
    # tcp binds 0.0.0.0 so the mempool backend container can reach it via the
    # docker host gateway (172.17.0.1:50001); firewalled to tailscale0 + docker
    # bridges only (below) — never the LAN or the internet.
    tcp = 0.0.0.0:50001
    # fast-sync trades RAM for speed during the initial index build.
    fast-sync = 4000
  '';
in
{
  users.users.fulcrum = {
    isSystemUser = true;
    group = "fulcrum";
    home = dataDir;
  };
  users.groups.fulcrum = { };

  systemd.services.fulcrum = {
    description = "Fulcrum Electrum server";
    wantedBy = [ "multi-user.target" ];
    after = [ "bitcoind-bitcoin.service" ];
    requires = [ "bitcoind-bitcoin.service" ];
    unitConfig.RequiresMountsFor = "/mnt/fusion";
    serviceConfig = {
      User = "fulcrum";
      Group = "fulcrum";
      StateDirectory = "fulcrum";
      StateDirectoryMode = "0750";
      # bitcoind writes its .cookie 0600 chris:users, which the fulcrum user
      # can't read — stage a fulcrum-owned copy as root before each start, so a
      # bitcoind restart's fresh cookie is always picked up.
      ExecStartPre = [
        ''+${pkgs.writeShellScript "fulcrum-cookie" ''
          set -e
          until [ -f /mnt/fusion/bitcoind/.cookie ]; do sleep 2; done
          ${pkgs.coreutils}/bin/install -o fulcrum -g fulcrum -m 0400 \
            /mnt/fusion/bitcoind/.cookie ${dataDir}/.cookie
        ''}''
      ];
      ExecStart = "${pkgs.fulcrum}/bin/Fulcrum ${fulcrumConf}";
      Restart = "on-failure";
      RestartSec = 30;
      # Indexing is I/O + CPU heavy for hours; be a polite background citizen.
      Nice = 10;
      IOSchedulingClass = "idle";
    };
  };

  # Tailscale + docker bridges only — never the LAN, never the internet.
  networking.firewall.interfaces."tailscale0".allowedTCPPorts = [ 50001 ];
  networking.firewall.extraCommands = ''
    iptables -I nixos-fw 1 -i br-+ -p tcp --dport 50001 -j nixos-fw-accept
  '';
}
