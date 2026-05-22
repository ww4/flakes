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

  # Steam.
  programs.steam = {
    enable = true;
    remotePlay.openFirewall = true;       # Steam Remote Play
    dedicatedServer.openFirewall = true;  # Source Dedicated Server
  };
}
