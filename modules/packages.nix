# System-wide packages. To search: nix search nixpkgs <name>
{ config, lib, pkgs, ... }:

{
  environment.systemPackages = with pkgs; [
  # GUI Applications
     google-chrome
     vscode
  #  teams   # deprecated, unmaintained by upstream
     logseq
  #   bitwarden  # having issues triggering a build from scratch, which fails. Not really needed...
     element-desktop
     libreoffice-fresh
     gimp       # gimp-with-plugins giving issues as of 1/8/25, switched to GIMP instead
     vlc
     feishin
     qbittorrent
     sparrow
     albyhub

  # Terminal Utilities
     byobu
     wget
     tmux
     htop
     git
     mergerfs
     tailscale
     lf
     yt-dlp
     xfsprogs
     ntfs3g
     ncdu
     gparted
     mergerfs-tools
     fastfetch  # neofetch removed upstream; fastfetch covers it
     bsdgames  # Colossal Cave Adventure and others
     frotz    # for infocom / zork
     # uudeview # for infocom / zork - broken as of 1/8/25
  ];
}
