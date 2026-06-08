# Desktop environment: X11, GNOME, printing, sound, Steam.
{ config, lib, pkgs, ... }:

{
  # X11 + GNOME.
  services.xserver.enable = true;
  services.displayManager.gdm.enable = true;
  services.desktopManager.gnome.enable = true;

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
