# System-wide packages — anything that must be on root's PATH or available
# before chris's user profile loads (storage tools, network, admin utilities).
#
# Personal apps + CLI tools live in ../home/packages.nix.
# To search: `nix search nixpkgs <name>`.
{ config, lib, pkgs, ... }:

{
  environment.systemPackages = with pkgs; [
    # Core admin utilities (useful as root, useful in emergencies)
    wget
    htop
    git                # required for nixos-rebuild against this flake

    # Network
    tailscale          # the daemon is enabled in modules/networking.nix; CLI lives here

    # Secrets (edit secrets/*.yaml from a checkout with `sops` — needs the
    # admin age key for existing files; creating new ones needs only .sops.yaml)
    sops
    age

    # Storage
    mergerfs
    mergerfs-tools
    xfsprogs
    ntfs3g
    gparted            # GUI partition editor; root-only by nature
  ];
}
