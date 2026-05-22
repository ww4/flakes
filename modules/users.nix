# User accounts, groups, login behaviour.
{ config, lib, pkgs, ... }:

{
  # Primary user.
  users.users.chris = {
    isNormalUser = true;
    description = "chris";
    extraGroups = [ "networkmanager" "wheel" "media" "lp" ];
    packages = with pkgs; [
      firefox
    #  thunderbird
    ];
  };

  # Media group — shared by Jellyfin, Audiobookshelf, etc.
  users.groups.media = { };

  # Passwordless sudo for wheel (single-user homelab, Tailscale-only access).
  security.sudo.wheelNeedsPassword = false;

  # Automatic login.
  services.displayManager.autoLogin.enable = true;
  services.displayManager.autoLogin.user = "chris";

  # Workaround for GNOME autologin:
  # https://github.com/NixOS/nixpkgs/issues/103746#issuecomment-945091229
  systemd.services."getty@tty1".enable = false;
  systemd.services."autovt@tty1".enable = false;
}
