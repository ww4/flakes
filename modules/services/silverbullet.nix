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
{ config, lib, pkgs, nixpkgs-silverbullet, ... }:

let
  domain = "rosemaryacres.com";
  spaceDir = "/var/lib/silverbullet";

  # silverbullet from nixos-unstable: the flake's nixos-26.05 pin is frozen at
  # 2.6.1 (April 2026), and 2.7–2.9 carry the mobile fixes that matter here —
  # sync-snapshot persistence, offline handling that stops endless re-upload
  # loops, the IndexedDB reindex deadlock that leaves a space permanently
  # un-indexed, and the typing slowdown on link-heavy pages in large spaces.
  # Plain package import (no module eval), so unstable's darwin eval problem
  # (see flake.nix) is not in play.
  pkgsSilverbullet = import nixpkgs-silverbullet {
    inherit (pkgs.stdenv.hostPlatform) system;
  };

  # Adds one `preventDefault` on the action buttons' pointerdown so tapping an
  # arrow doesn't blur the editor (which closes the phone keyboard). The
  # minified identifiers change every release (2.6.1: `a.callback()`, 2.9.0:
  # `r.callback()`), so match them by regex, not by literal string. Refuses to
  # patch — and copies the original through untouched — if the handler doesn't
  # match exactly once.
  patchClientJs = pkgs.writeText "patch-client-js.py" ''
    import re
    import sys

    PAT = re.compile(
        r"onClick:([A-Za-z_$][\w$]*)=>\{"
        r"\1\.preventDefault\(\),\1\.stopPropagation\(\),"
        r"[A-Za-z_$][\w$]*\.callback\(\)\}")

    src_path, out_path = sys.argv[1], sys.argv[2]
    src = open(src_path).read()
    matches = list(PAT.finditer(src))
    if len(matches) == 1:
        m = matches[0]
        ev = m.group(1)
        patched = (src[:m.start()]
                   + "onPointerDown:%s=>%s.preventDefault()," % (ev, ev)
                   + m.group(0)
                   + src[m.end():])
        open(out_path, "w").write(patched)
        print("client.js patched: arrow taps no longer steal editor focus")
    else:
        open(out_path, "w").write(src)
        sys.stderr.write(
            "WARNING: SilverBullet's action-button handler matched %d times, "
            "expected 1. Serving the ORIGINAL client.js. The arrows still work; "
            "the phone keyboard will flicker on each tap.\n" % len(matches))
  '';
in
{
  services.silverbullet = {
    enable = true;
    package = pkgsSilverbullet.silverbullet;
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

  # SilverBullet sets each file's mtime to the client-supplied timestamp after
  # a sync write (that timestamp is what the sync protocol compares). chtimes
  # needs OWNERSHIP, not write permission, so on agent-created files (owned by
  # claude) it fails — the journal shows retry storms of "Failed to set the
  # mtime for …/Inbox.md: operation not permitted" every time the phone syncs,
  # and the space's git history has conflicted-copy artifacts for exactly the
  # phone-capture pages (Inbox ×2, Grocery List). CAP_FOWNER lets the service
  # set mtimes on files it can already write but doesn't own. The ACL story
  # (below) is unchanged — this only stops sync timestamps from lying.
  systemd.services.silverbullet.serviceConfig.AmbientCapabilities = [ "CAP_FOWNER" ];

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

  # ---- the two-writer permission repair (must run as ROOT) ----
  #
  # SilverBullet writes every page it creates with mode 0640, ignoring the
  # service UMask. With POSIX ACLs the file's mask is taken from the create
  # mode's GROUP bits, so a page created in the web UI gets `mask::r--` and is
  # read-only to the agent — the exact files Chris creates on his phone.
  #
  # The autosave job's `setfacl` heal CANNOT fix this: only a file's owner (or
  # root) may change its ACL, and those files are owned by `silverbullet`.
  # Verified 2026-07-09: `setfacl` as claude → "Operation not permitted".
  #
  # So repair from root, on a short timer. chmod restores the group bits (which
  # restores the mask); setfacl re-asserts it for anything odd.
  systemd.services.silverbullet-perms = {
    description = "Repair SilverBullet space permissions for the agent";
    path = [ pkgs.acl pkgs.coreutils ];
    serviceConfig = {
      Type = "oneshot";
      User = "root";
    };
    script = ''
      set -eu
      [ -d ${spaceDir} ] || exit 0
      # group-writable so the ACL mask lands on rwX for both writers
      chmod -R g+rwX ${spaceDir}
      setfacl -R -m m::rwX,u:claude:rwX,g:silverbullet:rwX ${spaceDir} || true
    '';
  };
  systemd.timers.silverbullet-perms = {
    description = "Repair SilverBullet space permissions every 2 min";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "2min";
      OnUnitActiveSec = "2min";
      AccuracySec = "30s";
    };
  };

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
      # Heal ACL masks on files this user OWNS (agent-created, umask 022).
      # Files owned by the silverbullet user are repaired by the root-run
      # silverbullet-perms timer instead — setfacl here would fail on them.
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

  # ---- keep the phone keyboard open when tapping the move-item arrows ----
  #
  # SilverBullet's action buttons (client/components/top_bar.tsx) handle onClick
  # but never preventDefault the pointerdown, so tapping one moves focus off the
  # editor and the mobile keyboard slams shut. Working around it in Space Lua
  # (refocus via editor.moveCursor) works but makes the keyboard flicker closed
  # and open on every tap — Chris flagged it.
  #
  # One `preventDefault` on pointerdown fixes it outright (verified by patching
  # the bundle in a headless browser: zero focus events, item still moves). So
  # serve a patched copy of client.js from nginx, regenerated whenever the
  # silverbullet package changes.
  #
  # FAILS SAFE: if the needle isn't found exactly once (i.e. upstream changed
  # the code), it writes the ORIGINAL bundle unmodified and logs a warning —
  # never a half-patched, broken client.
  systemd.services.silverbullet-client-patch = {
    description = "Serve a client.js patched to not steal editor focus";
    after = [ "silverbullet.service" ];
    requires = [ "silverbullet.service" ];
    wantedBy = [ "multi-user.target" ];
    restartTriggers = [ config.services.silverbullet.package ];
    path = [ pkgs.curl pkgs.python3 ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      StateDirectory = "silverbullet-client";
      StateDirectoryMode = "0755";
    };
    script = ''
      set -eu
      src=$(mktemp)
      out=/var/lib/silverbullet-client/client.js

      # wait for silverbullet to answer (it has only just started)
      for _ in $(seq 1 30); do
        curl -fsS -o "$src" http://127.0.0.1:3336/.client/client.js && break
        sleep 1
      done

      python3 ${patchClientJs} "$src" "$out"
      chmod 0644 "$out"
      rm -f "$src"
    '';
  };

  # DNS: notes.rosemaryacres.com -> 100.82.117.116 (proxy off), created by the
  # agent via the Cloudflare token, same as the other vhosts. Inherits the
  # global Tailscale/LAN source-gate (nginx-access.nix). No Authelia on this
  # vhost — Chris wants notetaking frictionless (2026-07-09).
  services.nginx.virtualHosts."notes.${domain}" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    # exact match wins over the "/" proxy: serve our patched bundle instead
    locations."= /.client/client.js" = {
      alias = "/var/lib/silverbullet-client/client.js";
      extraConfig = ''
        default_type application/javascript;
        add_header Cache-Control "no-cache";
      '';
    };
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
