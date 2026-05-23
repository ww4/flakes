# User-level packages for chris. Moved here from environment.systemPackages
# during HM phase 3 so the system module only carries things that need to be
# on root's PATH (storage / network / admin tooling).
#
# To add a package, drop it in the list and `nixos-rebuild switch` —
# HM routes home.packages into users.users.chris.packages via the
# useUserPackages flag set in modules/home-manager.nix.
{ pkgs, ... }:

{
  home.packages = with pkgs; [
    # GUI applications
    google-chrome
    vscode               # Phase 4 will replace this with programs.vscode + pinned extensions
    logseq
    element-desktop
    libreoffice-fresh
    gimp                 # gimp-with-plugins has been flaky; plain gimp instead (Jan 2025)
    vlc
    feishin
    qbittorrent
    sparrow              # Bitcoin wallet — store wallet data lives in ~/.sparrow, not managed by HM
    albyhub              # Lightning hub — state in ~/.local/share/albyhub, not managed by HM

    # Terminal multiplexers + file management
    byobu
    tmux
    lf
    ncdu

    # Media / fun
    yt-dlp
    fastfetch            # neofetch went unmaintained; fastfetch is the drop-in (Jan 2025)
    bsdgames             # Colossal Cave Adventure and friends
    frotz                # Infocom / Zork interpreter
  ];
}
