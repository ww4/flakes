#!/usr/bin/env bash
# agent-restic-ro — READ-ONLY restic access for the claude agent (via sudo).
#
# Deployed root-owned 0555 by claude-harness.nix to
# /etc/claude-code/agent-restic-ro.sh; allowlisted in agent/sudo.nix.
#
# Purpose: let the agent VERIFY backups (e.g. "did last night's snapshot pick
# up /home/claude/.claude?") without granting `sudo restic *` — raw restic
# could forget/prune snapshots — and without exposing the repo password.
# This is the "tiny purpose wrapper" pattern sudo.nix recommends over
# broad sudo on a powerful binary.
#
#   sudo /etc/claude-code/agent-restic-ro.sh snapshots
#   sudo /etc/claude-code/agent-restic-ro.sh ls latest /home/claude/.claude
#
# Read-only subcommand allowlist; repo pinned to the LOCAL repo (same content
# as the B2 copy, no B2 API cost); -r/--repo/--password* overrides rejected.
set -euo pipefail

sub="${1:-snapshots}"; shift || true
case "$sub" in
  snapshots|ls|stats|find) ;;
  *) echo "agent-restic-ro: subcommand '$sub' not allowed (read-only: snapshots|ls|stats|find)" >&2; exit 1 ;;
esac
for a in "$@"; do
  case "$a" in
    -r|--repo|--repo=*|--repository-file|--repository-file=*|--password*)
      echo "agent-restic-ro: repo/password overrides not allowed" >&2; exit 1 ;;
  esac
done

export RESTIC_REPOSITORY=/mnt/backup/all/restic
# sops-nix default rendered path for sops.secrets."restic-password"
# (defined in modules/services/backup.nix).
export RESTIC_PASSWORD_FILE=/run/secrets/restic-password
exec /run/current-system/sw/bin/restic "$sub" "$@"
