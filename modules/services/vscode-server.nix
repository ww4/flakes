# VS Code remote server support. The option comes from the vscode-server
# flake input wired in flake.nix.
{ config, lib, pkgs, ... }:

{
  services.vscode-server.enable = true;
}
