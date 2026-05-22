# Filesystems and the MergerFS pools.
{ config, lib, pkgs, ... }:

{
  fileSystems = {
  # Physical drives
      "/mnt/decom/D1" = {  # was mergerD1
        device = "/dev/disk/by-uuid/4731e760-fe51-48ef-8e35-5b764b84c249";
        fsType = "ext4";
        options = [
          "nofail"
        ];
      };

      "/mnt/decom/D2" = {  # was mergerD2
        device = "/dev/disk/by-uuid/d49dac75-4d28-4c56-b2d2-606b3271b9b5";
        fsType = "ext4";
        options = [
          "nofail"
        ];
      };

      "/mnt/decom/D3" = {  # was mergerD3
        device = "/dev/disk/by-uuid/a500d06c-9878-4db3-9873-d0ee7f07dfc0";
        fsType = "ext4";
        options = [
          "nofail"
        ];
      };

      "/mnt/decom/D4" = { # was mergerD4
        device = "/dev/disk/by-uuid/f11eeedc-60aa-4567-948a-c19ff0ccf337";
        fsType = "ext4";
        options = [
          "nofail"
        ];
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
      };

  # mergerfs: using 3 buckets, rsynced, then remove #3 for archival storage.

    "/mnt/fusion" = {   # Primary Bucket - 16.4 TB (contains 8TB + 10TB drive)
      device = "/mnt/primary/D*";
      fsType = "fuse.mergerfs";
      options = [
        "defaults"
        "allow_other"
        "use_ino"
        "cache.files=off"
        "moveonenospc=true"
        "dropcacheonclose=true"
        "category.create=mfs"
        "func.getattr=newest"
        "fsname=mergerfs"
      ];
    };

     "/mnt/backup/all" = {   # Backup Bucket - 22 TB (contains 4x 6TB drives)
      device = "/mnt/backup/D*";
      fsType = "fuse.mergerfs";
      options = [
        "defaults"
        "allow_other"
        "use_ino"
        "cache.files=off"
        "moveonenospc=true"
        "dropcacheonclose=true"
        "category.create=mfs"
      ];
    };

     "/mnt/decom/all" = {   # Decom Bucket - 10.8 TB of soon-to-be decommisioned drives. Remove this group once done.
      device = "/mnt/decom/D*";
      fsType = "fuse.mergerfs";
      options = [
        "defaults"
        "allow_other"
        "use_ino"
        "cache.files=off"
        "moveonenospc=true"
        "dropcacheonclose=true"
        "category.create=mfs"
      ];
    };
  };

  # Needed for MergerFS (allow_other).
  programs.fuse.userAllowOther = true;
}
