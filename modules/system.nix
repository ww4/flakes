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

  # Allow insecure packages (added 11/3/23 for error updating).
  nixpkgs.config.permittedInsecurePackages = [
    "electron-20.3.11"
    "electron-27.3.11"
  ];

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
