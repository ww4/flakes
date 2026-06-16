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
cwd="$(printf '%s' "$input"   | jq -r '.cwd // empty')"

# The agent's own sandbox. Files it creates live here; Chris's files do not
# (and are OS-protected — the agent is a non-root user that can't write them).
WORKSPACE="/home/claude"

deny() {
  jq -n --arg r "$1" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
  exit 0
}

# Only guard Bash here; other tools are governed by settings.json.
[ "$tool" = "Bash" ] || { printf '{}'; exit 0; }

# --- Catastrophic deletes — blocked everywhere, always ---
case "$cmd" in
  *"rm -rf /"|*"rm -fr /"|*"rm -rf / "*|*"rm -fr / "*|*"rm -rf /*"*|*"rm -rf ~"*|*"rm -fr ~"*|*'rm -rf $HOME'*)
    deny "Catastrophic delete pattern — never." ;;
esac

# --- Deletes (rm / find -delete): allowed only INSIDE the agent's workspace ---
# Chris asked that the agent be free to delete files it created (its scratch dir
# is $WORKSPACE) while files Chris created stay protected. Those live outside
# $WORKSPACE and are OS-protected anyway; this fences the agent in as defense in
# depth. Anything reaching outside, or run as root, still routes to a PR / Chris.
case "$cmd" in
  *" rm "*|"rm "*|*"find "*" -delete"*)
    case "$cmd" in *"sudo "*) deny "Deleting as root is Chris-only — route via a flake PR." ;; esac
    case "$cmd" in
      *"/home/chris"*|*"/mnt"*|*"/etc"*|*"/var"*|*"/nix"*|*"/boot"*|*"/usr"*|*"/root"*|*"/srv"*|*"/opt"*|*"/sys"*|*"/proc"*|*"/dev"*|*"..")
        deny "That delete reaches outside your $WORKSPACE workspace — make the change via a flake PR, or ask Chris." ;;
    esac
    case "$cwd" in
      "$WORKSPACE"|"$WORKSPACE"/*|/tmp|/tmp/*) : ;;   # confined to the sandbox -> OK (defer to allow)
      *) deny "Delete from a non-workspace directory (cwd=$cwd) — cd into $WORKSPACE, or ask Chris." ;;
    esac
    ;;
  *" shred "*|*"truncate "*) deny "In-place destroy (shred/truncate) — make the change via a flake PR, or ask Chris." ;;
esac

# --- Other irreversible / out-of-band-privilege ops (unchanged) ---
case "$cmd" in
  *mkfs*|*" dd "*|*"of=/dev/"*|*" wipefs"*|*" fdisk"*|*" parted"*) deny "Disk/format op — must be run by Chris." ;;
  *"nixos-rebuild switch"*|*"nixos-rebuild boot"*) deny "Applying config is comin's job after a PR merge to main — don't switch directly." ;;
  *"git push"*)
    # Match the PUSH REFSPEC, not the word "main" anywhere on the line. The old
    # rule was a whole-line substring match, so a chained `git switch main`, a
    # push to a non-main branch (`main:testing`), an `echo`/`curl` mentioning
    # main, or a heredoc body all tripped it. Scope to the push invocation: take
    # everything after the last "git push" up to the next shell separator.
    seg="${cmd##*git push}"
    seg="${seg%%[;&|]*}"
    seg="${seg%%$'\n'*}"
    # Deny only a push that actually writes to main (`:main` dst, or a bare
    # `main`/`origin main` target). Reading FROM main (`main:testing`) is fine.
    case "$seg" in
      *":main"|*":main "*|*" main"|*" main "*) deny "Direct push to main — open a PR; Chris merges." ;;
    esac
    # Force-push is fine to testing / feature branches (needed for testing resets)
    # but never to main. main is also branch-protected server-side (defense in
    # depth). NOTE: a refspec-less `git push -f` while checked out on main isn't
    # caught here by string alone — server branch protection covers that case.
    case "$seg" in
      *"--force"*|*" -f "*|*" -f")
        case "$seg" in
          *":main"|*":main "*|*" main"|*" main "*) deny "Force-push to main is never allowed." ;;
        esac ;;
    esac
    ;;
  *"userdel"*|*"passwd "*|*"chpasswd"*|*"/etc/shadow"*) deny "Account/credential change — Chris only." ;;
  *">/dev/sd"*|*"> /dev/sd"*|*"> /dev/nvme"*) deny "Raw block-device write — Chris only." ;;
esac

# Everything else: defer to the settings allow/ask/deny lists and (if needed) the
# normal prompt. We intentionally do not auto-"allow" from here.
printf '{}'
exit 0
