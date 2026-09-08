# BLUEIRIS — CLI for customer Blue Iris NVRs over the JSON API.
#
# Built 2026-09-08 for Craigmyle Tractor. On 2026-09-08 the network said all 25
# cameras were serving RTSP while Chris was seeing cameras down, and answering
# "which does the NVR think are offline, and why" needed a two-hour drive. It is
# one API call, and the answer turned out to be a phantom camera slot with no
# device behind it.
#
# REACHABILITY. The NVR joined the tailnet as `craigmyle-blueiris`
# (100.68.224.97) carrying tag:cust-craigmyle, so gromit reaches it directly. It
# is a TAGGED node, which is the security property: a tagged device has no user
# identity, matches no `src` grant, and therefore cannot initiate anything back
# toward the homelab. See the tailnet policy notes.
#
# ⚠️ THE CAMERAS THEMSELVES ARE NOT REACHABLE from gromit — only the NVR is.
# Without 4via6 subnet routing there is no path to 192.168.1.0/24, so anything
# needing to talk to a camera directly (the RTSP-vs-NVR cross-check) still needs
# marcus on site. Snapshots are the exception: they proxy through the NVR.
#
# ⚠️ AUTH IS LAN-DEPENDENT and this bites. From the customer LAN Blue Iris
# reports "auth-exempt": true and lets clients straight in; over Tailscale the
# source is 100.x, which it does not consider LAN, so it reports false and
# demands the MD5 challenge. A working test from marcus proves nothing about
# the remote path.
#
# MULTI-SITE BY DESIGN: one env file per customer in ~/.config/blueiris/, and
# every command takes --site. Adding the next NVR is a tagged Tailscale node
# plus one 0600 env file — no code change.
{ config, lib, pkgs, ... }:

let
  # Shared with modules/services/homelab-mcp.nix, which shells out to this same
  # binary for its camera tools. One derivation, one tested auth path.
  blueiris = pkgs.callPackage ../../pkgs/blueiris { };
in
{
  environment.systemPackages = [ blueiris ];

  # Credentials deliberately live OUTSIDE the nix store and outside sops:
  # ~/.config/blueiris/<site>.env, 0600, claude-owned, delivered through
  # ~/secrets-inbox/. These are CUSTOMER credentials on customer equipment —
  # they belong on the one host that needs them, not committed anywhere and not
  # replicated to marcus or wallace. (secrets-handling-preference: "if a secret
  # only needs to live on one host, it goes there and nowhere else".)
  systemd.tmpfiles.rules = [
    "d /home/claude/.config/blueiris 0700 claude users -"
    # The mute list. StateDirectory= below also creates this, but only when the
    # timer first fires — and homelab-mcp.nix carries this path in
    # ReadWritePaths, where a MISSING directory makes the unit fail to start.
    # Without this line, enabling the camera tools would take the MCP down until
    # the first camera poll happened to run.
    "d /var/lib/blueiris 0750 claude users -"
  ];

  # --- camera-down alerting -------------------------------------------------
  # Polls the NVR and reports STATE CHANGES only. Every rule netwatch arrived at
  # the hard way applies here too, because a dead camera is a network event:
  #
  #   * NOTHING here pierces quiet hours. Chris's standing rule (2026-08-19)
  #     covers "all classes of network traffic" — a camera down is not a fire.
  #     Findings raised 22:00-07:00 are HELD and delivered after 07:00.
  #   * state change only: a camera down for a week is not news every 10 min.
  #   * an empty camlist is an ERROR, never an all-clear.
  #
  # 10 minutes is deliberate. Faster buys nothing — Blue Iris itself takes time
  # to declare a camera offline — and every extra poll is load on a customer's
  # NVR that is also recording 25 streams.
  systemd.services.blueiris-watch = {
    description = "Blue Iris camera-down watch (Craigmyle)";
    after = [ "network-online.target" "tailscaled.service" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      User = "claude";
      StateDirectory = "blueiris";
      Environment = [ "HOME=/home/claude" ];
      # Exit 0 even on failure: a customer NVR being briefly unreachable is not
      # a gromit fault and must not trip SystemdUnitFailed. The check reports
      # its own unreachability through ntfy instead, state-change gated.
      ExecStart = "${lib.getExe blueiris} watch";
      SuccessExitStatus = "0 1";
      TimeoutStartSec = "5min";
    };
  };

  systemd.timers.blueiris-watch = {
    description = "Blue Iris camera-down watch";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "*:0/10";
      Persistent = true;
      RandomizedDelaySec = "60s";
    };
  };
}
