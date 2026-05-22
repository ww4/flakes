# Gromit — top-level NixOS configuration.
#
# This file is just the module manifest: each concern lives in its own file
# under ./modules/ (base system) and ./modules/services/ (per-service). To
# try something out, add or comment a single import below and `nixos-rebuild
# test`; roll back with git or the boot menu.
{ ... }:

{
  imports = [
    # Hardware scan (generated — do not edit).
    ./hardware-configuration.nix

    # Base system.
    ./modules/boot.nix
    ./modules/storage.nix
    ./modules/networking.nix
    ./modules/desktop.nix
    ./modules/users.nix
    ./modules/system.nix
    ./modules/packages.nix
    ./modules/virtualisation.nix

    # Services.
    ./modules/services/jellyfin.nix
    ./modules/services/audiobookshelf.nix
    ./modules/services/tandoor.nix
    ./modules/services/pinchflat.nix
    ./modules/services/bitcoind.nix
    ./modules/services/immich.nix
    ./modules/services/vscode-server.nix
    ./modules/services/nextcloud.nix
    ./modules/services/backup.nix
    ./modules/services/notifications.nix
    ./modules/services/media-mirror.nix
  ];

  # The NixOS release the system was first installed from. Leave it pinned —
  # see `man configuration.nix`.
  system.stateVersion = "22.11";
}
