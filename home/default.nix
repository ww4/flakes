# Home-Manager config for chris@gromit.
#
# Scaffold only — phases 2+ (shell, git, packages, vscode, element, ssh) get
# added as separate files imported below. Keeping this file slim makes the
# composition obvious.
{ ... }:

{
  imports = [
    # Phase 2+ modules will be listed here, e.g.:
    # ./shell.nix
    # ./git.nix
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

  # Let HM manage itself (so `home-manager` CLI works for inspection).
  programs.home-manager.enable = true;
}
