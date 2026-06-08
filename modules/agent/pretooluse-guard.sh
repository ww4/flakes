#!/usr/bin/env bash
# PreToolUse guard for the `claude` agent. Deployed root-owned (0555) by
# claude-harness.nix to /etc/claude-code/pretooluse-guard.sh and referenced from
# the managed settings hook — so the agent cannot edit or remove its own backstop.
#
# Programmatic backstop ABOVE the settings allow/deny lists: hard-deny destructive
# commands no matter how they're phrased, so a single typo or clever rephrase
# can't slip a `rm -rf` past. (The PocketOS "agent deleted a volume" failure mode.)
#
# Contract: reads the tool call as JSON on stdin; emits a JSON decision.
#   permissionDecision: "deny"  -> blocked outright
#                       "ask"   -> fall through to the normal permission prompt
#                       "allow" -> auto-approved (we DON'T use this; let settings decide)
# Requires `jq` on PATH (add to the claude user's packages).

set -euo pipefail

input="$(cat)"
tool="$(printf '%s' "$input"  | jq -r '.tool_name // empty')"
cmd="$(printf '%s' "$input"   | jq -r '.tool_input.command // empty')"

deny() {
  jq -n --arg r "$1" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
  exit 0
}

# Only guard Bash here; other tools are governed by settings.json.
[ "$tool" = "Bash" ] || { printf '{}'; exit 0; }

# Hard-deny patterns — irreversible / out-of-band-privilege. Route real changes
# through a flake edit + PR (comin applies on merge), never an ad-hoc command.
case "$cmd" in
  *"rm -rf"*|*"rm -fr"*|*" rm "*"-rf"*) deny "Destructive delete — make the change via a flake PR, or ask Chris to run it." ;;
  *"find "*" -delete"*|*" shred "*|*"truncate "*) deny "Bulk/in-place destroy — make the change via a flake PR, or ask Chris to run it." ;;
  *mkfs*|*" dd "*|*"of=/dev/"*|*" wipefs"*|*" fdisk"*|*" parted"*) deny "Disk/format op — must be run by Chris." ;;
  *"nixos-rebuild switch"*|*"nixos-rebuild boot"*) deny "Applying config is comin's job after a PR merge to main — don't switch directly." ;;
  *"git push"*"--force"*|*"git push"*" main"*|*"git push"*":main"*) deny "Never force-push or push to main directly — open a PR; Chris merges." ;;
  *"userdel"*|*"passwd "*|*"chpasswd"*|*"/etc/shadow"*) deny "Account/credential change — Chris only." ;;
  *">/dev/sd"*|*"> /dev/sd"*|*"> /dev/nvme"*) deny "Raw block-device write — Chris only." ;;
esac

# Everything else: defer to the settings allow/ask/deny lists and (if needed) the
# normal prompt. We intentionally do not auto-"allow" from here.
printf '{}'
exit 0
