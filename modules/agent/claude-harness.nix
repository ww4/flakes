# Tamper-proof Claude Code harness policy for the `claude` agent.
#
# Deploys the agent's permission policy and its PreToolUse guard into
# /etc/claude-code/ ROOT-OWNED, world-readable, agent-unwritable. This is Claude
# Code's "managed settings" tier — the highest precedence; the claude user cannot
# override it from ~/.claude/settings.json, and (unlike the old ~/.claude copy)
# cannot edit its own backstop script either.
#
# Why this exists / what it changes vs. the old ~/.claude/settings.json:
#   - The policy and guard were claude-OWNED → the agent could in principle weaken
#     or delete them. Now they live in the store, linked read-only into /etc.
#   - The old policy leaned on an `ask` list, which is only meaningful when a human
#     is at the keyboard. Headless/autonomous runs can't answer a prompt, so every
#     rule is now an explicit allow or deny (managed-settings.json). Unmatched
#     calls still prompt when interactive and fail closed when not.
#
# Precedence reminder: deny -> ask -> allow, first match wins. A managed `deny`
# cannot be overridden by any user/project allow.
#
# Follow-ups deliberately NOT included here (verify against current Claude Code
# docs, then add in a later PR): `allowManagedPermissionRulesOnly` (ignore any
# user/project rules entirely) and `disableBypassPermissionsMode` (block
# --dangerously-skip-permissions). Both are managed-only hardening knobs.
{ config, lib, pkgs, ... }:

{
  environment.etc."claude-code/managed-settings.json" = {
    source = ./managed-settings.json;
    mode = "0444";                       # root-owned, world-readable, no writers
  };

  environment.etc."claude-code/pretooluse-guard.sh" = {
    source = ./pretooluse-guard.sh;
    mode = "0555";                       # force a copy with the exec bit (a bare
  };                                     # symlink would inherit the source's 0644)
}
