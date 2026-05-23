# VS Code. Thin Phase 4 — just enable HM's vscode module so the declarative
# seam exists, but leave extensions and settings.json as user-mutable state.
#
# Why so light:
#   - You have ~50 unique extensions spanning Microsoft proprietary, AI
#     tools, vendor SDKs, etc. Pinning them all would mean either pulling
#     in nix-vscode-extensions (a whole extra flake input) or accepting
#     losses for the ones not in nixpkgs. Mutable mode skips that fight.
#   - settings.json updates every time you add an SSH target
#     (remote.SSH.remotePlatform). Declaring it would fight you.
#
# Future tightening if/when you want it: add specific extensions to the
# `extensions = [...]` list below; they'll be Nix-managed alongside the
# mutable rest. Set `userSettings = { ... }` to lock down a few key prefs.
{ pkgs, ... }:

{
  programs.vscode = {
    enable = true;

    # The default `package` is pkgs.vscode (proprietary build). Pin'd via
    # flake.lock the same way every other input is.

    # Preserve the 89-extension reality on disk; HM won't wipe
    # ~/.vscode/extensions on activation.
    mutableExtensionsDir = true;

    profiles.default = {
      # extensions = with pkgs.vscode-extensions; [
      #   jnoortheen.nix-ide        # example — uncomment when you want a pin
      # ];

      # userSettings = { };          # leave unset so settings.json stays yours
    };
  };
}
