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
# Hardening knobs now ENABLED in managed-settings.json (doc-verified):
#   - allowManagedPermissionRulesOnly: only this file's allow/ask/deny apply;
#     user/project permission rules are ignored (agent can't self-grant).
#   - allowManagedHooksOnly: only this file's hook runs; user/project hooks are
#     suppressed (agent can't inject an auto-allow PreToolUse hook).
#   - permissions.disableBypassPermissionsMode = "disable": blocks
#     --dangerously-skip-permissions / bypassPermissions mode at startup.
# Not enabled (would need scoping first): allowManagedMcpServersOnly (no managed
# MCP allowlist defined → would block all MCP), sandbox.* read/network locks, and
# forceRemoteSettingsRefresh (that one is for REMOTE server-managed settings; we
# ship a local /etc file, so it would fail closed on every startup).
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
