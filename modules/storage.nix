# Physical drive mounts — the hardware half of storage. The mergerfs pools
# these feed are assembled by the library's mergerfs-pools module from the
# homelab.pools values in ./homelab-values.nix.
{ config, lib, pkgs, ... }:

{
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

      "/mnt/scratch" = {     # sdc WD Green — was /mnt/decom/D4; tier-3 scratch
        device = "/dev/disk/by-label/scratch";
        fsType = "xfs";
        options = [ "nofail" ];
      };

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
