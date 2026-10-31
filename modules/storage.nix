# Physical drive mounts — the hardware half of storage. The mergerfs pools
# these feed are assembled by the library's mergerfs-pools module from the
# homelab.pools values in ./homelab-values.nix.
{ config, lib, pkgs, ... }:

{
  # Emergency headroom added by hand on 2026-10-23 (root disk / swap near-full
  # incident, sentinel incident swap-headroom-1792792408) via `swapon
  # /var/swapfile`. Persisted here so it survives a reboot instead of
  # silently dropping back to just the 8.8G nvme0n1p3 partition.
  swapDevices = [
    { device = "/var/swapfile"; size = 8192; }
  ];

  # Why the emergency file was needed at all: gromit over-commits swap and never
  # gets it back. Same finding in the sentinel's Sep 25, Oct 6 and Oct 23
  # incidents, and again on 2026-10-24 when swap hit 16/16 GiB with 84 KiB free
  # and the box started killing background work. It is not a leak — it is idle
  # tenants parking anon pages that are never faulted back in (Kokoro TTS held
  # 2.6 GiB on 14 MB RSS, idle since Sep 11; jellyfin 2.3 GiB; whisper 0.8 GiB).
  #
  # Two changes, both recommended by the Oct 6 incident and never applied:
  #
  # 1. swappiness 60 -> 10. The default is tuned for machines that want to trade
  #    RAM for page cache. This box has 30 GiB and a working set that fits; it
  #    should evict cache before it swaps anonymous pages out of a live service.
  # 2. zram. A compressed in-RAM swap device takes the first swap writes, so the
  #    idle-tenant pages that currently land on disk (and stay there) cost a few
  #    hundred MB of RAM instead of GiB of a full partition. NixOS gives zram a
  #    higher priority than disk swap by default, which is the order we want:
  #    zram absorbs the churn, the disk devices stay as the deep backstop.
  #
  # Deliberately NOT doing `swapoff -a` to reclaim — with 15 GiB committed and
  # 13 GiB available that is the one move that turns this into an outage.
  boot.kernel.sysctl."vm.swappiness" = 10;

  zramSwap = {
    enable = true;
    algorithm = "zstd";
    memoryPercent = 25;   # ~7.5 GiB of compressed swap on 30 GiB of RAM
  };

  fileSystems = {
  # Physical drives
      # Reclaimed from /mnt/decom (3× Hitachi HUA723030 + 1× WD Green) on
      # 2026-05-25. The three Hitachis joined fusion as D3-D5; the WD Green
      # (high load-cycle count, tier-3 trust) became /mnt/scratch — outside
      # fusion, for transient workloads like *arr downloads/incomplete or
      # build caches. by-label paths since these were freshly mkfs'd.

      "/mnt/primary/D3" = {  # sdb Hitachi HUA723030 — was /mnt/decom/D3
        device = "/dev/disk/by-label/primary-D3";
        fsType = "xfs";
        options = [ "nofail" ];
      };

      "/mnt/primary/D4" = {  # sdd Hitachi HUA723030 — was /mnt/decom/D1
        device = "/dev/disk/by-label/primary-D4";
        fsType = "xfs";
        options = [ "nofail" ];
      };

      "/mnt/primary/D5" = {  # sde Hitachi HUA723030 — was /mnt/decom/D2
        device = "/dev/disk/by-label/primary-D5";
        fsType = "xfs";
        options = [ "nofail" ];
      };

      "/mnt/primary/D6" = {  # sdk 3.6 TB WD My Book — was NTFS Backup drive (2026-05-25)
        device = "/dev/disk/by-label/primary-D6";
        fsType = "xfs";
        options = [ "nofail" ];
      };

      # /mnt/scratch REMOVED 2026-10: the WD Green (WD-WMC4N1297328, sharpie
      # "D4", formerly /mnt/decom/D4) was physically moved to wallace carrying
      # bitcoind's chain. Its other tenants were relocated first in flakes
      # #443 — qBittorrent's incomplete dir to a bind on sde1, and silo's
      # dormant transcodeDir default. gromit now has no tier-3 scratch tier;
      # if one is wanted again, SATA0 is free (and currently masked at POST
      # precisely because nothing is attached to it).

      "/mnt/backup/D1" = { # was mergerD6
        device = "/dev/disk/by-uuid/c9e23776-811b-4dec-9fc5-3e55454a21f0";
        fsType = "ext4";
        options = [
          "nofail"
        ];
      };

      "/mnt/backup/D2" = { # was mergerD7
        device = "/dev/disk/by-uuid/5411719d-176a-4e59-8ec2-c4efef9d5410";
        fsType = "ext4";
        options = [
          "nofail"
        ];
      };

      "/mnt/backup/D3" = { # was mergerD8
        device = "/dev/disk/by-uuid/4b986746-3de1-49de-8c02-850adcb9024e";
        fsType = "ext4";
        options = [
          "nofail"
        ];
      };

      "/mnt/backup/D4" = { # was mergerD9
        device = "/dev/disk/by-uuid/11841ae6-181e-4345-ab89-bea16dcdcc59";
        fsType = "ext4";
        options = [
          "nofail"
        ];
      };

       "/mnt/primary/D1" = { #was mergerD10 / Superbook
        device = "/dev/disk/by-uuid/8976631f-34b3-4192-b979-4012a66f233f";
        fsType = "xfs";
        options = [
          "nofail"
        ];
      };

       "/mnt/primary/D2" = { # was mergerD11
        device = "/dev/disk/by-uuid/14559789-ecec-41e6-aef1-c4c445c07193";
        fsType = "xfs";
        options = [
          "nofail"
        ];
      };

       "/home/chris" = {
        device = "/dev/disk/by-uuid/56c90b01-5f1e-4058-a2c4-c3db4df4deef";
        fsType = "ext4";
        options = [ "nofail" ];   # don't block boot on different hardware
      };
  };
}
