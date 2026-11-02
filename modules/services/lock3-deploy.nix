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
      nixos-rebuild switch --flake "${src}#lock3" --target-host ${target}

      # Written ONLY after a successful switch, so a failed deploy is retried on
      # the next tick instead of being recorded as done.
      echo "$rev" > /var/lib/lock3-deploy/last-deployed
      echo "lock3-deploy: deployed $rev"
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
