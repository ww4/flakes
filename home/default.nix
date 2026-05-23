# Home-Manager config for chris@gromit.
#
# Scaffold only — phases 2+ (shell, git, packages, vscode, element, ssh) get
# added as separate files imported below. Keeping this file slim makes the
# composition obvious.
{ pkgs, ... }:

{
  imports = [
    ./shell.nix
    ./git.nix
    # Future phases:
    # ./packages.nix
    # ./vscode.nix
    # ./element.nix
    # ./ssh.nix
  ];

  home.username = "chris";
  home.homeDirectory = "/home/chris";

  # Pin to the NixOS version active when HM was first set up. Like
  # `system.stateVersion`, leave this alone going forward — bumping it can
  # trigger schema/default changes that aren't always backward compatible.
  home.stateVersion = "26.05";

  # `programs.home-manager.enable = true` only installs the man pages when
  # `useUserPackages = true`; the actual CLI has to be added explicitly so
  # `home-manager generations` etc. are available.
  programs.home-manager.enable = true;
  home.packages = [ pkgs.home-manager ];
}
