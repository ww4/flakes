# Weekly homelab digest — headless Claude Code → ntfy.
#
# Runs `claude -p "/catch-up"` non-interactively as the `claude` user, on the
# Claude *subscription* (OAuth creds at ~/.claude/.credentials.json — NOT API
# token-billed; verified 2026-06-13: a run consumes plan rate-limit quota, no
# per-token charge). The /catch-up playbook (in ~/.claude/commands → agent-toolkit)
# summarizes the open-loops task board + open PRs + a health glance.
#
# IMPORTANT: WorkingDirectory is the docs-repo project dir so the agent's memory
# loads — a test run from the wrong dir produced inaccurate results (it didn't
# know the pool-member unit names). PATH mirrors the claude user's interactive
# environment so the tools /catch-up shells out to (git, curl, jq, systemctl,
# gromit-notify) resolve.
{ config, lib, pkgs, ... }:
{
  systemd.services.claude-weekly-digest = {
    description = "Weekly homelab digest (claude -p /catch-up -> ntfy)";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      User = "claude";
      WorkingDirectory = "/home/claude/nixos-homelab-improvements";
      TimeoutStartSec = "15min";
      Environment = [
        "HOME=/home/claude"
        "PATH=/etc/profiles/per-user/claude/bin:/run/current-system/sw/bin:/usr/bin:/bin"
      ];
    };
    script = ''
      set -uo pipefail
      digest="$(timeout 10m claude -p "/catch-up" 2>/dev/null)" \
        || digest="weekly digest run failed — check: journalctl -u claude-weekly-digest"
      [ -n "$digest" ] || digest="(empty digest — check journalctl -u claude-weekly-digest)"
      gromit-notify "Homelab weekly digest" "$digest" default "calendar"
    '';
  };

  systemd.timers.claude-weekly-digest = {
    description = "Weekly homelab digest";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "Mon 08:00";          # America/New_York; Monday-morning rhythm
      Persistent = true;                  # catch up if the box was off
      RandomizedDelaySec = "10m";
    };
  };
}
