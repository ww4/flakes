# Network management tools configuration
# Enables iwd, nmcli, nmtui, and other network utilities

{ config, pkgs, ... }:

{
  # Use iwd as the WiFi backend instead of wpa_supplicant
  networking.networkmanager.wifi.backend = "iwd";

  # ONE driver at the wheel. With the iwd backend, iwd ALSO autoconnects to
  # every known network on its own — and with several sibling SSIDs saved
  # (the Craigmyle site broadcasts four), iwd's pick and NM's pick differ,
  # each new attempt cancels the other mid-auth ("aborting authentication
  # ... by local choice" in the kernel log, "IWD is connecting to the wrong
  # AP" from NM), and the result is a livelock where NOTHING ever connects
  # (diagnosed live 2026-08-18; deleting all but one profile broke the loop).
  # This tells NM to strip the autoconnect flag from iwd's known networks so
  # NM alone initiates every connection.
  networking.networkmanager.settings.device."wifi.iwd.autoconnect" = false;

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
        # Use systemd-resolved for DNS
        NameResolvingService = "systemd";
      };
      # Two lines used to sit here doing nothing, and one actively misled a
      # diagnosis (2026-08-18):
      #  - [Network] AddressRandomization=once — that key belongs to
      #    [General]; in [Network] iwd ignores it, so the laptop always used
      #    its real MAC while the config claimed otherwise.
      #  - [Settings] AutoConnect=true — a PER-NETWORK setting, meaningless
      #    in main.conf. The real autoconnect control for the NM pairing is
      #    the NM-side wifi.iwd.autoconnect above.
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