# Bitcoin Core full node.
#
# Hosting Fulcrum + mempool.space requires the full (non-pruned) chain
# with txindex=1. Migrated to /mnt/fusion/bitcoind (8+ TB free; nvme root
# is only 449 GB which is too small for ~700 GB chain). On first switch,
# the new datadir is empty so bitcoind starts a fresh IBD — expect ~1-3
# days to fully sync depending on network + disk I/O. Existing Test Wallet
# is preserved by a one-time copy before flipping prune off.
{ config, lib, pkgs, ... }:

{
  services.bitcoind.bitcoin = {
    enable = true;
    user = "chris";
    group = "users";
    dataDir = "/mnt/fusion/bitcoind";   # moved off nvme root
    prune = 0;                          # full chain
    dbCache = 8000;                     # 8 GB chainstate cache — drops to ~450 MB after IBD
    extraConfig = ''
      server=1
      txindex=1                          # required by Fulcrum + mempool
      # ZMQ pubsub: mempool.space subscribes to these for live block/tx
      # notifications instead of polling.
      zmqpubrawblock=tcp://127.0.0.1:28332
      zmqpubrawtx=tcp://127.0.0.1:28333
      zmqpubhashblock=tcp://127.0.0.1:28334
      # RPC reachability for the mempool backend container, which connects via
      # the docker host gateway (172.17.0.1:8332). Bind all interfaces but gate
      # with rpcallowip to loopback + docker bridge subnets only. Port 8332 is
      # NOT in the firewall allowlist (allowedTCPPorts=[80 443]), so the LAN and
      # internet can't reach it regardless — rpcallowip is the second layer.
      # 0.0.0.0 (not 172.17.0.1) avoids a boot-ordering dependency on docker0
      # existing before bitcoind starts.
      rpcbind=0.0.0.0
      rpcallowip=127.0.0.1
      rpcallowip=172.16.0.0/12
    '';
  };

  systemd.services.bitcoind-bitcoin.unitConfig.RequiresMountsFor =
    "/mnt/fusion";

  # The mempool backend container (on a docker bridge, e.g. mempool-net) reaches
  # bitcoind RPC via the host gateway 172.17.0.1:8332. rpcbind=0.0.0.0 + the
  # rpcallowip above are necessary but NOT sufficient: nixos-fw default-drops the
  # container's packets at the INPUT layer before rpcallowip is ever consulted,
  # so the connection silently times out (ETIMEDOUT in mempool-api). Fulcrum's
  # 50001 already has this accept; 8332 was missing it (latent until Fulcrum
  # finished indexing). Accept 8332 from docker bridges ONLY — never tailscale0
  # or the LAN; cookie auth + rpcallowip remain the gate for who can actually use
  # it. Mirrors modules/services/fulcrum.nix.
  networking.firewall.extraCommands = ''
    iptables -I nixos-fw 1 -i br-+ -p tcp --dport 8332 -j nixos-fw-accept
  '';
}
