# The agent's skills directory, assembled from the shared agent-modules library
# plus this site's own layer.
#
# WHY SHARED. The `step-by-step` method came out of a review of 45 agent-vm
# sessions that found ~150 failures in handing Chris steps to run himself. Six
# of its rules already existed as separate per-incident memories on that side
# and the failures kept recurring, which is the argument for one copy: when
# /retro on either side sharpens the method it goes to agent-modules and both
# agents get it. Site facts stay in SITE.md.
#
# ⚠️ This REPLACES a hand-made ~/.claude/skills with a read-only Nix symlink, so
# anything that lived there has to live here instead. Carried in: `herdr` and
# `plain-prose` (its scripts/ and references/ included).
#
# NOT carried in: `plain-prose-workspace`. That was 19 files of run OUTPUT —
# essay drafts, selftest, test-1, test-2 — sitting in the skills namespace and
# being offered to the agent as a skill every session. It belongs outside the
# tree; see the migration note in the PR. Nothing had written to the skills tree
# since October, so there was no live state to lose.
{ config, pkgs, agent-modules, ... }:

{
  # L+ replaces whatever is there, so a leftover hand-made directory cannot
  # shadow the managed one. The target is in the Nix store and therefore
  # read-only — a skill that needs to WRITE must write elsewhere.
  systemd.tmpfiles.rules = [
    "L+ /home/claude/.claude/skills - - - - ${agent-modules.lib.mkSkills pkgs ./skills}"
  ];
}
