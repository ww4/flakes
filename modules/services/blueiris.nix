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
  blueiris = pkgs.writers.writePython3Bin "blueiris" {
    flakeIgnore = [ "E501" "E203" "W503" "W504" ];
  } (builtins.readFile ./blueiris.py);
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
  ];
}
