# Networking: hostname, NetworkManager, Tailscale, firewall, SSH.
{ config, lib, pkgs, ... }:

{
  networking.hostName = "gromit";

  networking.networkmanager.enable = true;

  # Disable Network Manager Wait (issue on 11/3/23).
  systemd.services.NetworkManager-wait-online.enable = lib.mkForce false;
  systemd.services.systemd-networkd-wait-online.enable = lib.mkForce false;

  # Tailscale overlay network.
  services.tailscale.enable = true;
  networking.firewall.checkReversePath = "loose";

  # Firewall.
  networking.firewall = {
    enable = true;
    allowedTCPPorts = [ 80 443 631 3000 8096 2283 9090 ];
    allowedUDPPortRanges = [
      { from = 2000; to = 4007; }
      { from = 8000; to = 8300; }
    ];
  };

  # Remote access.
  services.openssh.enable = true;
}
