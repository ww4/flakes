# Pin claude-code to a NEWER version than nixos-26.05 ships.
#
# WHY THIS EXISTS. The agent, the newsdesk reader, the weekly digest and every
# scheduled `claude -p` task all run the claude-code from nixpkgs. On 2026-09-08
# that was 2.1.223, and `nixos-26.05` ALSO had exactly 2.1.223 — so a
# `nix flake update` was a no-op for this package and could not have helped.
#
# 26.05 last touched claude-code on 2026-08-06, bumping 2.1.219 -> 2.1.223 in a
# single burst and then nothing. That is the shape of a fast-moving package that
# was being auto-updated on the unstable branch and stopped getting backports
# once the release branch froze. Upstream was on 2.1.263 by 2026-09-06 — about
# forty releases ahead.
#
# The visible symptom was model availability: claude-code carries a CLIENT-SIDE
# model registry, and its own error says so —
#
#   "<name> is not a model this version of Claude Code recognizes, so
#    auto-compact will keep this session within 200k tokens ... to make it
#    recognized, map it in the modelOverrides setting or update Claude Code"
#
# So an unrecognised model does not merely fail to appear in the picker; the
# context window silently falls back to a 200k assumption, which matters most in
# exactly the long sessions where it is least noticeable.
#
# WHY AN OVERLAY AND NOT AN INPUT BUMP. The whole system is pinned to
# nixos-26.05 deliberately (see flake.nix): unstable rolled to 26.11 and dropped
# x86_64-darwin, and nixpkgs's own module eval touches darwin option types even
# for a linux-only flake. Moving the channel to chase one package would trade a
# stale CLI for a broken evaluation. This overlay changes ONE package and leaves
# that pin untouched.
#
# HOW IT WORKS. nixpkgs' claude-code takes its version and per-platform checksums
# from a `manifest` argument that defaults to its own vendored manifest.json:
#
#   manifest ? lib.importJSON ./manifest.json
#
# so overriding that single argument is enough — no forked package, no copied
# build logic to drift out of sync with upstream's.
#
# TO UPDATE: refetch the manifest for the desired version and drop it in place.
#
#   curl -sS https://downloads.claude.ai/claude-code-releases/<VERSION>/manifest.json \
#     > modules/agent/claude-code-manifest.json
#
# It is the vendor's own manifest, carries the sha256 for every platform, and is
# the same file nixpkgs vendors — so `fetchurl` verifies the binary against a
# checksum published by Anthropic rather than one we computed locally.
#
# WHICH VERSION TO TAKE. Upstream publishes two channel pointers, both plain
# text:
#
#   curl -sS https://downloads.claude.ai/claude-code-releases/stable   # 2.1.311 on 2026-11-13
#   curl -sS https://downloads.claude.ai/claude-code-releases/latest   # 2.1.323 on 2026-11-13
#
# ⭐ POLICY, set by Chris 2026-11-13: **TAKE `latest`, UNLESS KNOWN ISSUES EXIST.**
#
# This REVERSES the stable-not-latest rule this file carried until now. That rule
# reasoned that ~10 unattended `claude -p` units (daybook am/pm, weekly digest,
# queue consumer, inbox triage, podcast triage, newsdesk, wx, sentinel every
# 120 s) discover a regression hours later with nobody watching, so the few days
# `stable` lags were worth having.
#
# WHY IT CHANGED. That argument weighs a POSSIBLE regression against a CERTAIN
# one. Reading the actual 2.1.318 -> 2.1.323 diff on 11-13, the delta was almost
# entirely fixes to permission enforcement — the thing this box's whole safety
# model rests on:
#
#   2.1.321  deny rules and protected-file prompts not applying to the file a
#            `cp` or `mv` INTO A DIRECTORY writes  (a deny rule you can walk around)
#   2.1.321  Bash permission checks judging a file write from the wrong folder
#   2.1.321  a read-only Bash command running outside the sandbox with no prompt
#   2.1.321  MCP OAuth sending refresh tokens to another host on redirect
#   2.1.323  `rm` judged from the wrong folder, skipping the dangerous-removal prompt
#   2.1.323  `sandbox.excludedCommands` letting an unnamed command out of the sandbox
#
# Lagging on `stable` does not avoid risk here, it RETAINS known holes in the
# managed-settings deny list and the PreToolUse guard. For this package
# specifically, security fixes dominate the stability lag.
#
# ⚠️ "UNLESS KNOWN ISSUES EXIST" IS THE OPERATIVE HALF, and it is work, not a
# formality. Before taking a version, read the changelog from the pinned version
# forward and look for: changes to auto mode, permissions or sandboxing (this box
# runs auto mode in ~29 panes); anything touching `claude -p` startup or exit
# codes; model deprecations. If something looks like it could change unattended
# behaviour, say so and let Chris decide rather than bumping quietly.
#
#   curl -sS https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md
#
# Worked example from this bump: 2.1.319 changes auto mode when telemetry OR
# feature flags are off — calls the server cannot check are asked about or denied
# with no fallback to the built-in classifier. That would have been a real
# regression across every pane. CHECKED BEFORE BUMPING: on gromit
# CLAUDE_CODE_ENABLE_TELEMETRY, DISABLE_TELEMETRY and
# CLAUDE_CODE_DISABLE_FEATURE_FLAGS are all UNSET (only DISABLE_AUTOUPDATER=1 is
# set, which is this pin), so the change does not apply. `CLAUDE_CODE_AUTO_MODE_SERVER=0`
# is the escape hatch if it ever does.
#
# ⚠️ AND CHECK THE CHANGELOG AGAINST THE VERSION YOU TAKE. Capability gating is
# real and easy to get wrong in both directions: on 2026-11-04 a review found
# this box 48 releases behind and named features needing 2.1.314
# (`permissions.blockWritesOutsideWorkingDirectories`), 2.1.317
# (`onFailure: "block"` for SDK hooks) and 2.1.318 (`loginExpiresAt` in
# `/auth status` — the native replacement for the bespoke login-expiry detector in
# `claude-queue-consumer-start`, which shipped INVERTED and reported "resolved" as
# the token died). 2.1.323 carries all three. Read the version a feature landed in
# BEFORE designing against it; a feature absent from the pinned build fails
# silently, not loudly.
# See docs/native-capability-review-2026-11-04.md in nixos-homelab-improvements.
#
# ⚠️ REMOVE THIS FILE once nixos-26.05 (or whatever channel this box is on) ships
# a claude-code newer than the pinned manifest. Leaving it in place would PIN the
# package BACKWARDS — an overlay is not a floor, it is an assignment.
{ lib, ... }:

{
  nixpkgs.overlays = [
    (final: prev: {
      claude-code = prev.claude-code.override {
        manifest = lib.importJSON ./claude-code-manifest.json;
      };
    })
  ];
}
