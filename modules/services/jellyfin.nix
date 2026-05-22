# Jellyfin media server.
{ config, lib, pkgs, ... }:

{
  services.jellyfin = {
    enable = true;
    group = "media";
  };
}
