# Network management tools configuration
# Enables iwd, nmcli, nmtui, and other network utilities

{ config, pkgs, ... }:

{
  # Use iwd as the WiFi backend instead of wpa_supplicant
  networking.networkmanager.wifi.backend = "iwd";
  
  # Enable iwd service
  networking.wireless.iwd = {
    enable = true;
    settings = {
      General = {
        # NetworkManager is the connection manager (wifi.backend = "iwd"
        # above) and owns DHCP. iwd's own network configuration must stay
        # OFF under NM — two DHCP clients race on WiFi transitions
        # (2026-08 audit M7; standard NM/iwd guidance).
        EnableNetworkConfiguration = false;
      };
      Network = {
        # Enable IPv6
        EnableIPv6 = true;
        # Randomize MAC for privacy (optional, can be disabled if causing issues)
        AddressRandomization = "once";
        # Use systemd-resolved for DNS
        NameResolvingService = "systemd";
      };
      Settings = {
        # Auto-connect to known networks
        AutoConnect = true;
      };
    };
  };

  # Ensure wpa_supplicant is disabled to avoid conflicts with iwd
  networking.wireless.enable = false;
  
  # Add network management and troubleshooting tools
  environment.systemPackages = with pkgs; [
    # NetworkManager tools (nmcli and nmtui are included with networkmanager)
    networkmanagerapplet  # nm-applet for system tray (if needed)
    
    # Wireless tools
    iwd                   # Intel Wireless Daemon (iwctl command)
    wirelesstools         # iwconfig, iwlist, etc.
    
    # Network troubleshooting and configuration
    ethtool              # Ethernet tool for advanced config
    dig                  # DNS lookup tool
    traceroute           # Network path tracing
    mtr                  # Combines ping and traceroute
    iperf3               # Network performance testing
    tcpdump              # Packet capture (command line)
    nmap                 # Network discovery and security scanning
    nettools             # Classic tools like ifconfig, route
    iproute2             # Modern ip command tools
    bind                 # Includes host, nslookup commands
  ];
}