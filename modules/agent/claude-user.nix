# Dedicated `claude` agent user.
#
# Runs the Claude Code agent under its own uid instead of `chris`, so it does NOT
# inherit chris's SSH keys, wallets, GPG, or GUI session. Standing access is
# READ-ONLY (journal); privileged actions go through comin (rebuilds) or the
# scoped allowlist in sudo.nix. See ./README.md.
{ config, lib, pkgs, ... }:

{
  users.users.claude = {
    isNormalUser = true;                 # needs a home + shell to run Claude Code
    description = "Claude Code agent (scoped, non-root)";
    home = "/home/claude";
    shell = pkgs.bashInteractive;

    # The agent's runtime + tooling. claude-code is the agent itself; jq is
    # required by the PreToolUse guard hook. (git/curl/openssh come from the
    # system profile.) Auth is a one-time `claude login` (OAuth) as this user.
    packages = with pkgs; [
      claude-code
      jq
    ];

    # Read-only diagnostics without sudo: journald. Deliberately NOT in `wheel`,
    # `docker` (root-equivalent), or chris's data groups.
    extraGroups = [ "systemd-journal" ];

    # The agent connects as claude@ with this key (moved here from root/chris on
    # activation). Public key — safe in the world-readable store.
    openssh.authorizedKeys.keys = [
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAII72tYB6OdaFY3kAOYk7A/AEa9hrbckKe6gCoeM1SRhB chris@openclaw-claude-20260515"
    ];
  };

  # The agent works on its OWN checkout of the flake under its home, pushes
  # branches, and opens PRs — it never edits chris's ~/code/flakes or touches main
  # directly. (Clone + a git-push deploy key are set up at activation, not here.)
  #
  # NOTE: root login for the agent is removed once this is active — the agent is
  # `claude@`, never `root@`. Keep PermitRootLogin=prohibit-password only if some
  # other automation still needs it; otherwise consider disabling root SSH too.
}
