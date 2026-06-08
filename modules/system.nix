# System basics: locale, time, Nix settings, nixpkgs config.
{ config, lib, pkgs, ... }:

{
  # Time zone.
  time.timeZone = "America/New_York";

  # Internationalisation.
  i18n.defaultLocale = "en_US.UTF-8";
  i18n.extraLocaleSettings = {
    LC_ADDRESS = "en_US.UTF-8";
    LC_IDENTIFICATION = "en_US.UTF-8";
    LC_MEASUREMENT = "en_US.UTF-8";
    LC_MONETARY = "en_US.UTF-8";
    LC_NAME = "en_US.UTF-8";
    LC_NUMERIC = "en_US.UTF-8";
    LC_PAPER = "en_US.UTF-8";
    LC_TELEPHONE = "en_US.UTF-8";
    LC_TIME = "en_US.UTF-8";
  };

  # Allow unfree packages.
  nixpkgs.config.allowUnfree = true;

  # (Removed the electron-20.3.11 / electron-27.3.11 insecure-package pins on
  # 2026-06-08 — added 11/3/23, now stale: a full system build evaluates clean
  # without them, i.e. nothing in the closure pulls those electron versions.
  # If a future package needs an insecure electron, the build will fail loudly
  # and name it — re-pin the exact version here then.)

  # nix-ld: provide a real dynamic loader at /lib64/ld-linux-x86-64.so.2 so
  # generic (non-Nix) dynamically-linked binaries can run. NixOS otherwise
  # ships a stub loader that refuses them with a "cannot run dynamically linked
  # executable" error. Needed for binaries bundled inside VS Code extensions
  # (the auto-fix-vscode-server patcher only fixes VS Code's own server, not
  # extension payloads) — e.g. the Claude Code extension's native `claude`
  # binary (a Bun single-file exe needing only glibc). Added 2026-06-04.
  programs.nix-ld = {
    enable = true;
    libraries = with pkgs; [
      stdenv.cc.cc.lib   # libstdc++ / libgcc_s
      zlib
    ];
  };

  nix = {
    package = pkgs.nixVersions.stable;
    extraOptions = "experimental-features = nix-command flakes";
    optimise = {
      automatic = true;
      dates = [ "03:45" ];
    };
    gc = {
      automatic = true;
      dates = "weekly";
      options = "--delete-older-than 30d";
    };
  };
}
