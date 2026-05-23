# Remote desktop via xrdp — fresh GNOME session per connect, Tailscale-only.
{ config, lib, pkgs, ... }:

{
  services.xrdp = {
    enable = true;
    defaultWindowManager = "${pkgs.gnome-session}/bin/gnome-session";
    # We open the port per-interface below, not globally.
    openFirewall = false;
  };

  # RDP only over Tailscale; never over WAN or LAN.
  networking.firewall.interfaces.tailscale0.allowedTCPPorts = [ 3389 ];
}
