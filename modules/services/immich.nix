# Immich photo management — WIP, not yet enabled.
{ config, lib, pkgs, ... }:

{
  services.immich = {
    enable = false;  # WIP
    port = 2283;
  # mediaLocation = "/mnt/fusion/immich";
  # environment.IMMICH_MACHINE_LEARNING_URL = "http://localhost:3003";
    host = "0.0.0.0";
  };
}
