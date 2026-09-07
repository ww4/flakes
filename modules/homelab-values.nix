# Personal values for the homelab-modules library (the PUBLIC repo), plus the
# sops declarations its modules expect. This file IS the private half of the
# split: implementations live in the library; everything gromit-specific they
# read lives here. When a module migrates to the library, its values and
# secret declarations land here.
{ ... }:

{
  # ── homelab.* option values ────────────────────────────────────────────────
  homelab.ntfy.url = "http://localhost:8090/gromit-alerts";
  homelab.quietHours = { start = 22; end = 7; };   # no non-critical pages overnight
  homelab.arrMissingSweep.user = "claude";         # owns the arr-api sops secret

  # ── values that lived in the moved base modules ────────────────────────────
  # (was modules/system.nix)
  time.timeZone = "America/New_York";

  # (was modules/boot.nix) Headless virtual display. gromit runs with no
  # monitor attached, so every i915 display connector probes "disconnected" →
  # no CRTC/output exists → KDE Plasma has no screen to place a desktop on and
  # renders nothing, so MeshCentral's remote desktop captures only a black
  # framebuffer. Force the HDMI-A-1 connector on at 1920x1080 ("e" =
  # force-enabled even with nothing plugged in) so a CRTC/output exists; Plasma
  # then draws a desktop that XGetImage (the MeshAgent KVM) can capture.
  # Confirmed at runtime via /sys/class/drm/card1-HDMI-A-1/status=on.
  boot.kernelParams = [ "video=HDMI-A-1:1920x1080e" ];

  # ── sops declarations for library modules ──────────────────────────────────
  # Library modules only ever reference config.sops.secrets.<name>.path; the
  # declarations (and the encrypted files) stay in this repo.
  sops.secrets."decluttarr-env" = {
    sopsFile = ../secrets/decluttarr-env.yaml;
    key = "decluttarr-env";
  };
  sops.secrets."meshagent-msh" = {
    sopsFile = ../secrets/meshagent-msh.yaml;
    key = "msh";
  };
}
