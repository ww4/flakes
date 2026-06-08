# comin — GitOps applier for gromit (STAGED / INERT — not imported).
#
# This is the approval gate: the agent pushes branches/PRs but applies nothing.
# comin (running as root on the box) pulls the flake repo and rebuilds ONLY when
# a commit reaches the tracked branch. Chris merging the PR to `main` is the
# human-in-the-loop approval. See ./README.md.
#
# PREREQ: add comin as a flake input first, e.g. in flake.nix:
#   inputs.comin.url = "github:nlewo/comin";
# and import comin.nixosModules.comin in the host config. Then verify the option
# schema below with `nixos-option services.comin` (it shifts between versions).
{ config, lib, pkgs, ... }:

{
  services.comin = {
    enable = true;

    remotes = [
      {
        name = "origin";
        # The flake repo. Use the URL comin can pull (Forgejo or GitHub).
        url = "https://github.com/ww4/flakes.git";   # <-- confirm/replace

        # If the repo is private, give comin a read-only token file:
        # auth.access_token_path = "/var/lib/comin/repo-token";

        branches = {
          # `main` → full `nixos-rebuild switch` (persists + boots). Protect this
          # branch on the remote so only a reviewed PR merge can advance it.
          main.name = "main";
          # `testing` → `nixos-rebuild test` (ephemeral, no bootloader change), so
          # the agent can self-validate a change before you merge to main.
          testing.name = "testing";
        };
      }
    ];

    # comin builds nixosConfigurations.<networking.hostName> = "gromit". Default.
  };

  # comin needs git + nix; both present. It runs as root to rebuild — that's the
  # ONE privileged actor, and it only ever applies commits YOU approved onto main.
}
