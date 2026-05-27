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
    '';
  };

  systemd.services.bitcoind-bitcoin.unitConfig.RequiresMountsFor =
    "/mnt/fusion";
}
