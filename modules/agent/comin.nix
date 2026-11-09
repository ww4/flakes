# comin — GitOps applier. Shared by both hosts (gromit + wallace); each builds
# nixosConfigurations.<its-hostname> from the same repo.
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
        # Pull from Forgejo — the SOURCE OF TRUTH — not the GitHub mirror.
        # Repointed 2026-09-07 after the push-mirror's GitHub token expired and
        # deploys silently stopped for 2 days (#241–#244 never reached GitHub;
        # comin's last deploy was Sep 5). The mirror is cosmetic now: if it
        # breaks again, nothing stops deploying. Public repo → comin pulls
        # anonymously, no token needed. (If flakes ever goes private, comin
        # needs a read token HERE first — see the 2026-09-07 exposure audit.)
        url = "https://git.rosemaryacres.com/ww4/flakes.git";

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
      {
        # BREAK-GLASS remote (2026-09-08 forge-redundancy work): the GitHub
        # push-mirror of the same repo. Normally a fast-forward twin of
        # Forgejo main (sync_on_commit, watched by mirror-drift-watch), so
        # comin's commit-selection sees the same history from both remotes —
        # its algorithm refuses force-pushed mains, and a push-mirror is
        # always fast-forward, so this can never fight the origin.
        #
        # What it buys: if Forgejo (or gromit's forge stack) is DOWN, deploys
        # don't stall — and in a disaster Chris can push directly to GitHub
        # main to drive the fleet without a working Forgejo. That closes the
        # "all changes deploy from the box where the forge lives" loop.
        name = "github-mirror";
        url = "https://github.com/ww4/flakes.git";
        branches = {
          main.name = "main";
        };
      }
    ];

    # comin builds nixosConfigurations.<networking.hostName> = "gromit". Default.
  };

  # comin runs as root to rebuild — the ONE privileged actor, and it only ever
  # applies commits Chris approved onto main.
}
