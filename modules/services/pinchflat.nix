# PinchFlat — YouTube archiver.
{ config, lib, pkgs, ... }:

{
  services.pinchflat = {
    enable = true;
    selfhosted = true;
    mediaDir = "/mnt/fusion/pinchflat";
  };

  # Not great, but needed (per maintainer): run as a fixed system user
  # rather than a DynamicUser.
  users.users.pinchflat = {
    isSystemUser = true;
    group = "pinchflat";
  };
  systemd.services.pinchflat.serviceConfig.User = "pinchflat";
  systemd.services.pinchflat.serviceConfig.DynamicUser = lib.mkForce false;
}
