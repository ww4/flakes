# SilverBullet — markdown-native notes/tasks web app at
# https://notes.rosemaryacres.com (Tailscale source-gate + Authelia forward-auth;
# the auth bypass rules for its PWA assets live in authelia.nix).
#
# This space is the SOURCE OF TRUTH for the scheduling-assistant / daybook
# system (decided 2026-07-09, docs repo: docs/scheduling-assistant-research.md):
# plain .md files under /var/lib/silverbullet that BOTH Chris (via the web UI /
# PWA) and the claude agent (direct file access) read and write, with full
# mutual agency. Calendar events are separate (vdir, see pim.nix) and get
# rendered INTO the space by the daybook runs (modules/agent/daybook.nix).
#
# Mutual-write mechanics: the service runs as the `silverbullet` user; claude
# joins the `silverbullet` group and recursive default POSIX ACLs keep every
# file writable by both parties regardless of who created it (plain group perms
# break here because each side's umask would drop group-write on new files).
#
# History/undo for max-agency collaboration: an hourly autosave git commit of
# the space (local repo only — content stays on the box; restic backs up
# /var/lib/silverbullet via the backup module's criticalPaths if added there).
{ config, lib, pkgs, ... }:

let
  domain = "rosemaryacres.com";
  spaceDir = "/var/lib/silverbullet";
in
{
  services.silverbullet = {
    enable = true;
    listenAddress = "127.0.0.1";
    listenPort = 3336;
    spaceDir = spaceDir;
  };

  # Agent joins the service's group; ACLs below do the heavy lifting.
  users.users.claude.extraGroups = [ "silverbullet" ];

  # New files must be born group-writable or their ACL MASK caps the other
  # party at read-only (mode 644 -> mask r-- — found on the testing deploy:
  # web-UI saves to agent-created pages would fail and vice versa). UMask on
  # the service covers web-UI creations; daybook.nix does the same for agent
  # runs; the autosave heal below catches interactive agent sessions.
  systemd.services.silverbullet.serviceConfig.UMask = "0002";

  # systemd reapplies the StateDirectory mode on EVERY service start, and the
  # group bits double as the ACL mask — the default 0755 kept resetting the
  # space-root mask to r-x, locking the agent out of creating/deleting at the
  # top level (found on the testing deploy: git init in the space failed after
  # a restart). 0770 keeps the mask rwx across restarts.
  systemd.services.silverbullet.serviceConfig.StateDirectoryMode = "0770";

  # Recursive + default ACLs: everything in the space, present and future,
  # stays rw for the service user (via group) and for claude. Runs on every
  # activation, so files that somehow lost the ACL get healed.
  systemd.tmpfiles.rules = [
    # Ensure the dir exists at tmpfiles time (first deploy: the service's
    # StateDirectory would otherwise only create it at first start, after the
    # ACL pass had already run against a missing path). Mode 0770: the group
    # bits double as the ACL mask — 0750 silently capped claude's effective
    # rights at r-x (found on the testing deploy). The explicit m:rwX below
    # guards the mask the same way for pre-existing entries.
    "d ${spaceDir} 0770 silverbullet silverbullet - -"
    "A+ ${spaceDir} - - - - u:claude:rwX,g:silverbullet:rwX,m::rwX,d:u:claude:rwX,d:g:silverbullet:rwX,d:m::rwX"
  ];

  # The space git repo is operated by claude but the top dir is owned by the
  # silverbullet user (StateDirectory enforces that) — git's safe.directory
  # check calls that "dubious ownership" and refuses. System-wide exception so
  # the autosave/daybook units (which run without a HOME gitconfig) work too.
  programs.git = {
    enable = true;
    config.safe.directory = spaceDir;
  };

  # Hourly autosave commit — the undo log for two-writer collaboration.
  # .silverbullet.db* is SilverBullet's derived index, not content — ignored.
  systemd.services.silverbullet-autosave = {
    description = "Autosave git commit of the SilverBullet space";
    path = [ pkgs.git ];
    serviceConfig = {
      Type = "oneshot";
      User = "claude";
      Group = "silverbullet";
      UMask = "0002";
      WorkingDirectory = spaceDir;
    };
    script = ''
      set -eu
      # Heal ACL masks: files created by interactive agent sessions (umask 022)
      # are born mask r--, locking the web UI out of them until this pass.
      ${pkgs.acl}/bin/setfacl -R -m m::rwX . 2>/dev/null || true
      if [ ! -d .git ]; then
        git init -q
        git config user.name "space-autosave"
        git config user.email "autosave@gromit.local"
        printf '%s\n' '.silverbullet.db*' > .gitignore
      fi
      git add -A
      git diff --cached --quiet || git commit -q -m "autosave $(date '+%Y-%m-%d %H:%M')"
    '';
  };
  systemd.timers.silverbullet-autosave = {
    description = "Hourly SilverBullet space autosave";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "*:05";
      Persistent = true;
    };
  };

  # DNS: notes.rosemaryacres.com -> 100.82.117.116 (proxy off), created by the
  # agent via the Cloudflare token, same as the other vhosts. Inherits the
  # global Tailscale/LAN source-gate (nginx-access.nix); Authelia forward-auth
  # is merged onto this vhost in authelia.nix.
  services.nginx.virtualHosts."notes.${domain}" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:3336";
      recommendedProxySettings = true;
      proxyWebsockets = true;
      extraConfig = ''
        client_max_body_size 20M;      # attachment uploads
      '';
    };
  };
}
