# Virtualisation: VirtualBox and Docker.
{ config, lib, pkgs, ... }:

{
  # VirtualBox.
  virtualisation.virtualbox.host.enable = true;
  users.extraGroups.vboxusers.members = [ "chris" ];

  # Docker.
  virtualisation.docker.enable = true;
}
