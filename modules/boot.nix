# Bootloader and power behaviour.
{ config, lib, pkgs, ... }:

{
  # Bootloader.
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = true;
  boot.loader.efi.efiSysMountPoint = "/boot/efi";

  # Disable the boot-menu kernel-cmdline editor: with console access it
  # otherwise lets anyone append e.g. `init=/bin/sh` for an unauthenticated
  # root shell. The box has no disk encryption, so console access is already
  # powerful — but this closes the trivial, no-tooling path. (Tier 3 hardening.)
  # Takes effect on the next `switch` (bootloader install), i.e. when this merges.
  boot.loader.systemd-boot.editor = false;

  # Headless virtual display. gromit runs with no monitor attached, so every
  # i915 display connector probes "disconnected" → no CRTC/output exists → KDE
  # Plasma has no screen to place a desktop on and renders nothing, so
  # MeshCentral's remote desktop captures only a black framebuffer. Force the
  # HDMI-A-1 connector on at 1920x1080 ("e" = force-enabled even with nothing
  # plugged in) so a CRTC/output exists; Plasma then draws a desktop that
  # XGetImage (the MeshAgent KVM) can capture. Confirmed at runtime by writing
  # /sys/class/drm/card1-HDMI-A-1/status=on. See modules/services/meshagent.
  boot.kernelParams = [ "video=HDMI-A-1:1920x1080e" ];

  # This box is a server — never let it sleep. Disable the sleep/suspend
  # systemd targets, GNOME's auto-suspend, and block suspend/hibernate at
  # the polkit level so nothing can trigger it.
  systemd.targets.sleep.enable = false;
  systemd.targets.suspend.enable = false;
  systemd.targets.hibernate.enable = false;
  systemd.targets.hybrid-sleep.enable = false;

  services.displayManager.gdm.autoSuspend = false;
  security.polkit.extraConfig = ''
    polkit.addRule(function(action, subject) {
        if (action.id == "org.freedesktop.login1.suspend" ||
            action.id == "org.freedesktop.login1.suspend-multiple-sessions" ||
            action.id == "org.freedesktop.login1.hibernate" ||
            action.id == "org.freedesktop.login1.hibernate-multiple-sessions")
        {
            return polkit.Result.NO;
        }
    });
  '';
}
