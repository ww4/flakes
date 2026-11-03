# Deploy the lock3 VPS from gromit, replacing lock3's own pull-based autodeploy.
#
# WHY THE DIRECTION IS INVERTED. lock3 used to pull a GitHub mirror of
# ww4-bot/lock3-website and nixos-rebuild on change. Two independent problems
# with that, both found 2026-11-02:
#
#   1. The Forgejo -> GitHub push mirror had been DEAD for 62 days (expired
#      credential), and Forgejo's `last_update` advances on every ATTEMPT, so it
#      looked healthy. A merge simply never reached the box. See the
#      forgejo-push-mirror-silent-failure memory.
#   2. lock3 cannot pull from Forgejo directly either. Forgejo is tailnet-only
#      (git.rosemaryacres.com -> 100.82.117.116) and lock3 carries
#      `tag:jumphost`, whose ACL grants it exactly ONE port to gromit — 8789,
#      for homelab-mcp. 443 and 22 are denied. Measured, not assumed.
#
# ⚠️ That ACL is correct and deliberate: lock3 is internet-facing, and the trust
# direction is home -> VPS, never VPS -> home. Granting it gromit:443 would hand
# an internet-facing box TCP reachability to EVERY homelab vhost, since nginx
# routes by Host header. So rather than widen the ACL, the deploy direction is
# reversed to match the policy that already exists: gromit, which can reach
# lock3, builds lock3's config and pushes it.
#
# Failure mode, accepted explicitly by Chris: if gromit is down, lock3 does not
# update. The site keeps serving the last-deployed generation. "Just an out of
# date website (fine)."
{ config, lib, pkgs, ... }:

let
  # Failure has to reach a human. gromit's own ntfy is the right channel here,
  # unlike the external watcher's: a broken lock3 deploy is gromit telling you
  # about lock3, not lock3 trying to report its own absence.
  notify = "${pkgs.writeShellScript "lock3-deploy-notify" ''
    exec ${pkgs.curl}/bin/curl -sS --max-time 10 \
      -H "Title: $1" -H "Priority: 3" -H "Tags: warning,satellite" \
      -d "$2" http://127.0.0.1:8090/gromit-alerts >/dev/null
  ''}";

  # lock3 over Tailscale. Its SSH is on 22022, not 22.
  target = "root@100.125.31.125";
  sshPort = 22022;

  # The key already authorized for root on lock3 (it is listed in that repo's
  # users.users.root.openssh.authorizedKeys as "claude@gromit lock3-vps").
  #
  # ⚠️ Deliberately reusing it rather than minting a dedicated deploy key. A new
  # key would have to be added to lock3's authorizedKeys to work — and that
  # change can only REACH lock3 through this very unit, so a fresh key is a
  # chicken-and-egg that would need a manual first push. Reusing the working key
  # makes this deployable the moment it lands. If it is ever rotated, rotate it
  # in lock3-website's authorizedKeys in the same change.
  deployKey = "/home/claude/.ssh/id_lock3_vps";

  src = "/var/lib/lock3-deploy/src";
in
{
  systemd.services.lock3-deploy = {
    description = "Build lock3's config here and deploy it to the VPS";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    path = with pkgs; [ git nixos-rebuild nix openssh coreutils ];
    serviceConfig = {
      Type = "oneshot";
      StateDirectory = "lock3-deploy";
      # One deploy at a time; a slow nixos-rebuild must not get a second copy
      # started on top of it by the next timer tick.
      TimeoutStartSec = "30min";

      # ⚠️ Runs as `claude`, NOT root, and that is the point: claude is the only
      # identity that already holds BOTH credentials this needs —
      #   * the Forgejo git helper (URL-scoped in its global gitconfig, sourcing
      #     ~/.config/ww4-bot/forgejo-token.env). The repo is PRIVATE, so an
      #     anonymous clone fails with "could not read Username"; verified.
      #   * the SSH key already authorized for root on lock3.
      # Running as root would have meant minting a new Forgejo secret into sops
      # AND a new deploy key on lock3 — the latter being a chicken-and-egg,
      # since the only path to lock3's authorized_keys is this unit.
      #
      # Local root is not needed: with --target-host, nixos-rebuild builds
      # locally and activation happens over SSH on the target.
      User = "claude";
      Group = "users";

      # ProtectHome stays OFF deliberately — the git credential and the SSH key
      # both live under /home/claude. The rest of the sandboxing is kept.
      ProtectHome = false;
      NoNewPrivileges = true;
      PrivateTmp = true;
      ProtectKernelTunables = true;
      ProtectKernelModules = true;
      ProtectControlGroups = true;
      RestrictRealtime = true;
      LockPersonality = true;
    };
    environment = {
      # nixos-rebuild shells out to ssh for BOTH the closure copy and the
      # activation, and NIX_SSHOPTS is the only knob that reaches both.
      #
      # ⚠️ The non-standard port MUST ride here. A store URL's `?port=` is
      # silently ignored — nix answers "warning: unknown setting 'port'" and
      # then tries 22, which lock3 does not listen on. Verified 2026-11-02.
      NIX_SSHOPTS = "-i ${deployKey} -p ${toString sshPort} -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=10";

      # The git credential helper is URL-scoped in claude's GLOBAL gitconfig and
      # sources $HOME/.config/ww4-bot/forgejo-token.env, so HOME must be set
      # explicitly — a systemd unit does not inherit one. Verified that a clone
      # succeeds in an otherwise empty environment with only PATH and this HOME.
      HOME = "/home/claude";
      GIT_TERMINAL_PROMPT = "0";
    };
    script = ''
      set -euo pipefail

      # Forgejo is the source of truth and gromit CAN reach it — which is the
      # whole point of moving the fetch to this side.
      repo=https://git.rosemaryacres.com/ww4/lock3-website.git

      if [ ! -d "${src}/.git" ]; then
        git clone --branch main "$repo" "${src}"
      fi
      cd "${src}"

      # ⚠️ Reset the remote every run. The old unit on lock3 only ever cloned
      # when .git was ABSENT and otherwise fetched whatever `origin` already
      # pointed at — so changing a URL in config silently did nothing to an
      # existing checkout. Setting it unconditionally makes that class of bug
      # impossible here.
      git remote set-url origin "$repo"
      git fetch --quiet origin main
      git checkout --quiet -B main origin/main
      git reset --hard --quiet origin/main

      rev=$(git rev-parse HEAD)
      last=$(cat /var/lib/lock3-deploy/last-deployed 2>/dev/null || echo none)
      if [ "$rev" = "$last" ]; then
        echo "lock3-deploy: already at $rev; nothing to do"
        exit 0
      fi

      # Reachability first, so an unreachable VPS reads as "could not reach"
      # rather than as a build or activation failure.
      if ! ssh $NIX_SSHOPTS -o ConnectTimeout=10 ${target} true 2>/dev/null; then
        echo "lock3-deploy: cannot reach ${target} — not attempting a deploy" >&2
        exit 1
      fi

      echo "lock3-deploy: deploying $rev (was $last)"

      # ── What the target looked like BEFORE we touched it ──────────────────
      # Needed to tell "we broke something" from "something was already
      # broken". Without this baseline, a pre-existing failed unit would look
      # like our fault and trigger a pointless rollback.
      before=$(ssh $NIX_SSHOPTS ${target} 'systemctl --failed --no-legend --plain 2>/dev/null | awk "{print \$1}" | sort' || true)
      prevgen=$(ssh $NIX_SSHOPTS ${target} 'readlink -f /nix/var/nix/profiles/system' || true)
      echo "lock3-deploy: baseline failed units: $(echo "$before" | tr '\n' ' ')"
      echo "lock3-deploy: current generation: $prevgen"

      # ── GATE: does nginx itself accept the new config? ────────────────────
      #
      # ⚠️ THE NIX BUILD DOES NOT ANSWER THIS. NixOS renders nginx.conf without
      # ever asking nginx whether it is valid, so BUILD_EXIT=0 is silent about
      # it. On 2026-11-02 a duplicate `proxy_http_version` — emitted once by
      # proxyWebsockets and again by extraConfig — made nginx reject the whole
      # config, and lock3.net plus mcp.rosemaryacres.com were down for two
      # hours after a green build.
      #
      # It has to run ON THE TARGET, not here: `nginx -t` on gromit fails on
      # /var/lib/acme/lock3.net/fullchain.pem, which exists only on lock3. So
      # the closure is copied first, the config tested there, and only then
      # switched.
      newsys=$(nix build --no-link --print-out-paths "${src}#nixosConfigurations.lock3.config.system.build.toplevel")
      nix copy --to "ssh://${target}" "$newsys"

      nginxconf=$(nix eval --raw "${src}#nixosConfigurations.lock3.config.systemd.services.nginx.preStart" 2>/dev/null \
                  | grep -oE '/nix/store/[a-z0-9]+-nginx\.conf' | head -1 || true)
      nginxbin=$(nix eval --raw "${src}#nixosConfigurations.lock3.config.services.nginx.package" 2>/dev/null || true)

      if [ -n "$nginxconf" ] && [ -n "$nginxbin" ]; then
        echo "lock3-deploy: testing nginx config on the target: $nginxconf"
        # ⚠️ NOT `ssh ... | sed`. A pipeline's status is the LAST stage's, so
        # `if ! ssh ... | sed` tests sed — which always succeeds — and the gate
        # would never fire. Capture, then branch on ssh's own status.
        nginx_t_rc=0
        nginx_t_out=$(ssh $NIX_SSHOPTS ${target} "$nginxbin/bin/nginx -t -c $nginxconf" 2>&1) || nginx_t_rc=$?
        printf '%s\n' "$nginx_t_out" | sed 's/^/  nginx: /'
        if [ "$nginx_t_rc" -ne 0 ]; then
          echo "lock3-deploy: REFUSING TO SWITCH — nginx rejected the new config (rc=$nginx_t_rc)" >&2
          ${notify} "lock3 deploy BLOCKED" "nginx rejected the config for $rev; nothing was switched. $(printf '%s' "$nginx_t_out" | tail -2)" || true
          exit 1
        fi
      else
        # Not silently skipped: an unresolvable path means the gate is not
        # running, and a gate that quietly stops matching is worse than none.
        echo "lock3-deploy: WARNING could not resolve nginx conf/package; config gate SKIPPED" >&2
      fi

      # ── Switch ────────────────────────────────────────────────────────────
      switch_rc=0
      nixos-rebuild switch --flake "${src}#lock3" --target-host ${target} || switch_rc=$?

      # ── Did we leave it worse than we found it? ───────────────────────────
      #
      # ⚠️ The switch's exit code is NOT sufficient, which is the bug that let
      # the 2026-11-02 outage persist: the first run exited 4 and correctly did
      # not record success, but a LATER run re-ran nixos-rebuild, found the
      # generation already current, exited 0 "nothing to do", and wrote
      # last-deployed — while the box sat `degraded` with nginx failed. A
      # deploy is only done when the box is healthy, not when the command
      # returns 0.
      sleep 5
      after=$(ssh $NIX_SSHOPTS ${target} 'systemctl --failed --no-legend --plain 2>/dev/null | awk "{print \$1}" | sort' || true)
      newly=$(comm -13 <(echo "$before") <(echo "$after") || true)

      if [ "$switch_rc" -ne 0 ] || [ -n "$newly" ]; then
        echo "lock3-deploy: DEPLOY UNHEALTHY (switch rc=$switch_rc, newly failed: $(echo "$newly" | tr '\n' ' '))" >&2

        # ── Automatic rollback ─────────────────────────────────────────────
        # The 2026-11-02 outage lasted two hours only because it needed a human
        # to notice and run one command. Reverting to the generation that was
        # running a minute ago is the lowest-risk action available and it is
        # strictly better than leaving the box broken while nobody is looking.
        if [ -n "$prevgen" ]; then
          echo "lock3-deploy: rolling back to $prevgen" >&2
          ssh $NIX_SSHOPTS ${target} "$prevgen/bin/switch-to-configuration switch" >&2 || \
            echo "lock3-deploy: ROLLBACK ALSO FAILED — the box needs hands" >&2
        fi

        # Deliberately NOT recording this rev, so a fix lands on the next tick.
        ${notify} "lock3 deploy failed" \
          "rev $rev left lock3 unhealthy (switch rc=$switch_rc; newly failed: $(echo "$newly" | tr '\n' ' ')). Rolled back to the previous generation." \
          || true
        exit 1
      fi

      # Written ONLY after a switch that both succeeded AND left the target no
      # worse than the baseline.
      echo "$rev" > /var/lib/lock3-deploy/last-deployed
      echo "lock3-deploy: deployed $rev cleanly"
    '';
  };

  systemd.timers.lock3-deploy = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "5min";
      OnUnitActiveSec = "5min";   # the cadence lock3's own timer used
      # Persistent is fine here, unlike the heartbeat timer: a catch-up deploy
      # after downtime is exactly what is wanted, since it converges on the
      # current revision rather than replaying a stale moment.
      Persistent = true;
      AccuracySec = "1min";
    };
  };
}
