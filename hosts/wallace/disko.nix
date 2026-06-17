# Disko layout for WALLACE — targets ONLY the Samsung 850 EVO (sdb) by stable
# by-id. Windows (Samsung 980 PRO nvme0n1) is NOT referenced here and is left
# 100% untouched. NixOS gets its own EFI partition on this disk, so the firmware
# boot menu lists both NixOS and Windows.
{
  disko.devices.disk.main = {
    type = "disk";
    device = "/dev/disk/by-id/ata-Samsung_SSD_850_EVO_500GB_S3PTNF0JA11795T";
    content = {
      type = "gpt";
      partitions = {
        ESP = {
          size = "1G";
          type = "EF00";
          content = {
            type = "filesystem";
            format = "vfat";
            mountpoint = "/boot";
            mountOptions = [ "umask=0077" ];
          };
        };
        root = {
          size = "100%";
          content = {
            type = "filesystem";
            format = "ext4";
            mountpoint = "/";
          };
        };
      };
    };
  };
}
