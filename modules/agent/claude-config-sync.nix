# Auto-pull the synced global Claude config (ww4/claude-config) so this box stays
# current with edits made elsewhere (Forgejo web UI / a PR / the work box).
#
# The repo is cloned at /home/claude/claude-config and ~/.claude/CLAUDE.md is a
# symlink into it (set up out-of-band; see the claude-config-sync memory). This
# only PULLS — config changes are authored via the web UI / a PR, not pushed
# from here.
#
# --ff-only: if the working tree is dirty or has diverged, the pull is skipped
# rather than creating a merge — fail-safe. Wrapped so the unit always exits 0
# (a transient pull failure shouldn't trip the failed-unit alert); the git error
# is still logged to the journal.
{ config, lib, pkgs, ... }:
{
  systemd.services.claude-config-sync = {
    description = "Pull the synced global Claude config (ww4/claude-config)";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      User = "claude";
      Environment = [ "HOME=/home/claude" ];   # so git finds ~/.gitconfig (bot credential helper)
      WorkingDirectory = "/home/claude/claude-config";
      ExecStart = ''${pkgs.bash}/bin/bash -c '${pkgs.git}/bin/git -C /home/claude/claude-config pull --quiet --ff-only || echo "claude-config-sync: pull skipped/failed (see git output)"' '';
    };
  };

  systemd.timers.claude-config-sync = {
    description = "Hourly pull of the synced global Claude config";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "5m";
      OnUnitActiveSec = "1h";
      RandomizedDelaySec = "5m";
      Persistent = true;          # catch up after downtime
    };
  };
}
