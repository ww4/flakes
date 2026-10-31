# The Bitcoin stack on WALLACE: bitcoind's chain and Fulcrum's index.
#
# WHY IT IS HERE AND NOT ON GROMIT. gromit is an i5-4690K: four cores, load15
# p50 3.91 (saturated), CPU pressure `some avg10` 45%, and 32 GB of RAM that
# cannot be raised — LGA1150 caps there and all four DIMM slots are full. So
# offloading was the only lever left, and this stack was the best candidate
# because it is the one large thing with NO dependency on gromit's media pool:
# its data is the blockchain.
#
# HOW THE DATA GOT HERE. Not over the network. gromit's NIC turned out to be
# negotiating 100 Mb (11 MiB/s host-to-host), which made a ~960 GB copy a 27 h
# job, and wallace's largest free filesystem was 477 G — the chain would not
# have fitted anyway. Instead the whole `scratch` filesystem was **physically
# unplugged and moved**: 2026-10-28, SATA3 on gromit, serial WD-WMC4N1297328.
# Fulcrum's 191 GB index could not ride along (it lived on gromit's Mushkin
# SSD) so it was copied cold, verified byte-for-byte at 204,141,985,791 bytes
# / 12,405 files, and landed on the Intel NVMe here.
#
# ⚠️ FULCRUM MUST NOT RUN FROM THE WD GREEN. services/fulcrum.nix has the
# measurements: it is a RocksDB serving small random reads for every
# mempool.space page load and every Sparrow sync, and the moved disk is a
# 5400 rpm WDC WD30EZRX (nvme 122 read IOPS @ 48 KB vs sdd 39 IOPS @ 133 KB).
# The index therefore lives on nvme1n1p1 and only the chain is on the spinner.
# That file's comment rejecting wallace for Fulcrum was written when bitcoind
# was staying on gromit, which would have put the cookie and RPC a tailnet hop
# away on every block. Moving both together is what dissolves that objection.
{ config, lib, pkgs, ... }:

{
  imports = [
    ../../modules/services/bitcoind.nix
    ../../modules/services/fulcrum.nix
  ];

  # The moved disk. By LABEL, deliberately: it arrived on a different port and
  # a different device letter than it left on, and gromit's own storage.nix
  # mounts every pool member this way for the same reason.
  #
  # `nofail` keeps a missing disk from blocking boot; it is not a loosening,
  # because bitcoind.nix pairs it with RequiresMountsFor on the datadir. Boot
  # survives, the node stays down and says so. That pairing is what stops the
  # 2026-08-27 failure mode where a vanished branch let bitcoind start against
  # a datadir that was not there and LevelDB built a fresh database over it.
  fileSystems."/mnt/scratch" = {
    device = "/dev/disk/by-label/scratch";
    fsType = "xfs";
    options = [ "nofail" ];
  };

  # Fulcrum's index. The partition also holds `homebak/rick` — 150 GB of Rick's
  # old Linux home, left in place on purpose: it is verified present twice more
  # on gromit (fusion D6 and the backup pool, byte-identical at
  # 155,897,175,453), so it is a third copy rather than a last one, and at
  # 340/469 G used there is no space pressure to reclaim.
  fileSystems."/mnt/nvme" = {
    device = "/dev/disk/by-uuid/d5f67f8e-8719-4946-b912-a18d62719cb0";
    fsType = "ext4";
    options = [ "nofail" ];
  };

  # Point the bind in services/fulcrum.nix at this host's copy. Only the two
  # host-specific values are overridden; every guard in that module still
  # applies unchanged.
  fileSystems."/var/lib/fulcrum" = {
    device = "/mnt/nvme/fulcrum";
    depends = [ "/mnt/nvme" ];
  };

  # Fulcrum's Electrum port, reachable only over the tailnet — never the LAN,
  # never the internet. gromit's copy of this rule also punched a hole for the
  # docker bridges so mempool's containers could reach it; that is deliberately
  # NOT here, because mempool has not moved yet. It belongs in the podman port
  # (PR 2b) where the bridge is `podman0`, not `br-+`.
  networking.firewall.interfaces."tailscale0".allowedTCPPorts = [ 50001 ];
}
