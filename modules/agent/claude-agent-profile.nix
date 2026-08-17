{ config, lib, pkgs, ... }:

# Harness profile for the scoped agent, using the shared `agent-modules` flake.
#
# Replaces claude-harness.nix, which shipped the guard, hooks and managed
# settings as files in THIS repo. The Broadlinc agent host needs the same
# harness, and two copies of a security backstop drift — a drifted fence still
# looks like protection. One definition now serves both.
#
# Everything below is chosen to reproduce gromit's CURRENT behaviour exactly,
# except for two deliberate fixes documented in the PR.

{
  services.claudeAgent = {
    enable = true;
    user = "claude";
    operator = "Chris";
    workspace = "/home/claude";
    protectedBranch = "main";

    # Same set the old guard hard-coded, in the same order.
    protectedPaths = [
      "/home/chris" "/mnt" "/etc" "/var" "/nix" "/boot"
      "/usr" "/root" "/srv" "/opt" "/sys" "/proc" "/dev"
    ];

    # The SilverBullet space is shared agent-writable ground (Chris, 2026-07-09:
    # full agency in the space, deletes included — the space's git autosave is
    # the undo log). Was the hard-coded `SPACE` variable in the old guard.
    writablePaths = [ "/var/lib/silverbullet" ];

    # ⚠️ The two-word forms ONLY. A bare "nixos-rebuild" would also deny
    # `nixos-rebuild build`, which is how /flake-pr validates every change
    # before opening it — the agent would lose its own ability to check its
    # work. `switch`/`boot` are the ones that APPLY, and applying is comin's
    # job after a merge.
    deniedApplyCommands = [ "nixos-rebuild switch" "nixos-rebuild boot" ];

    # media-mirror's deletion queue is cold storage; approving it is Chris's
    # call, never the agent's. Carried over verbatim from the old deny list.
    extraDeny = [
      "Bash(media-mirror approve:*)"
      "Bash(sudo media-mirror approve:*)"
    ];

    reflection.enable = true;   # Stop hook: capture + distill checkpoints
    clock.enable = true;        # UserPromptSubmit: real time on every prompt
  };

  # Read-only restic wrapper for backup verification — allowlisted in
  # ./sudo.nix, never raw `sudo restic`. Not part of the shared agent-modules
  # harness (it is gromit-specific: pinned to this host's local repo), so it
  # is installed here. The 2026-08 audit found the sudo grant dangling: the
  # module that used to install this file (claude-harness.nix) had been
  # replaced by this profile without carrying the entry over, so the granted
  # capability silently did not exist on the box.
  environment.etc."claude-code/agent-restic-ro.sh" = {
    source = ./agent-restic-ro.sh;
    mode = "0555";
  };
}
