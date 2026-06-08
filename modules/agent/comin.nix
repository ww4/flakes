# comin — GitOps applier for gromit.
#
# This is the approval gate's enforcement arm: the agent (ww4-bot) pushes
# branches/PRs but applies nothing. comin (running as root) polls the public
# flake repo and rebuilds ONLY when a commit reaches `main` — and `main` is
# branch-protected so only a reviewed PR merge advances it. Chris merging the
# PR IS the human-in-the-loop approval. See ./README.md.
{ config, lib, pkgs, ... }:

{
  services.comin = {
    enable = true;

    remotes = [
      {
        name = "origin";
        # Public repo → comin pulls anonymously, no token needed.
        url = "https://github.com/ww4/flakes.git";

        branches = {
          # `main` → full `nixos-rebuild switch` (persists + boots). main is
          # branch-protected, so only a reviewed PR merge can advance it.
          main.name = "main";
          # `testing` → ephemeral `nixos-rebuild test` (applied live, auto-reverts
          # on reboot, no bootloader change). NOT branch-protected: the agent
          # pushes here to iterate live WITHOUT a PR. Only `main` persists, and
          # only via a reviewed merge. This is the fast-iteration path.
          testing.name = "testing";
        };
      }
    ];

    # comin builds nixosConfigurations.<networking.hostName> = "gromit". Default.
  };

  # comin runs as root to rebuild — the ONE privileged actor, and it only ever
  # applies commits Chris approved onto main.
}
