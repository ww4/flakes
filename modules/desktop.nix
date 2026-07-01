# Desktop environment: KDE Plasma on X11, printing, sound, Steam.
{ config, lib, pkgs, ... }:

{
  # X11 + KDE Plasma 6, via SDDM. Switched from GNOME/Wayland because
  # MeshCentral's remote desktop is X11/XTEST-only (no Wayland screen capture),
  # and GNOME 50 has no X11 session. SDDM on X11 gives an X11 greeter so
  # MeshCentral can drive the console AND the login screen (true remote GUI
  # login). See modules/services/meshagent + the meshcentral-project notes.
  # Recovery if a login breaks: boot the previous generation from the GRUB menu.
  services.xserver.enable = true;
  services.displayManager.sddm.enable = true;
  services.displayManager.sddm.wayland.enable = false;   # X11 greeter (MeshCentral-capturable)
  services.desktopManager.plasma6.enable = true;
  services.displayManager.defaultSession = "plasmax11";  # X11 session (MeshCentral needs X11, not Wayland)

  # Keymap.
  services.xserver.xkb = {
    layout = "us";
    variant = "";
    options = "caps:super";
  };

  # Printing — CUPS + drivers, with mDNS discovery via Avahi.
  services.printing.enable = true;
  services.printing.drivers = with pkgs; [
    gutenprint
    brlaser
  ];
  services.avahi = {
    enable = true;
    nssmdns4 = true;
    openFirewall = true;
  };

  # Sound via PipeWire.
  security.rtkit.enable = true;
  services = {
    pulseaudio.enable = false;
    pipewire = {
      enable = true;
      alsa.enable = true;
      alsa.support32Bit = true;
      pulse.enable = true;
    };
  };

  # Steam. Security review 2026-06-04: the openFirewall flags opened the Steam
  # ports on ALL interfaces (LAN + public IPv6). Disabled them and re-opened the
  # same port set scoped to the LAN + Tailscale only.
  programs.steam = {
    enable = true;
    remotePlay.openFirewall = false;       # Steam Remote Play (scoped below)
    dedicatedServer.openFirewall = false;  # Source Dedicated Server (scoped below)
  };
  networking.firewall.interfaces =
    let
      steamTCP = [ 27015 27036 27040 ];
      steamUDP = [ 27015 27036 ];
      steamUDPRanges = [ { from = 27031; to = 27036; } ];  # Remote Play streaming
      steam = {
        allowedTCPPorts = steamTCP;
        allowedUDPPorts = steamUDP;
        allowedUDPPortRanges = steamUDPRanges;
      };
    in
    {
      enp3s0 = steam;      # LAN
      tailscale0 = steam;  # Tailscale
    };
}
