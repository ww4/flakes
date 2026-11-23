# Scoped `claude` agent access on WALLACE.
#
# Deliberately NOT an import of ../../modules/agent/claude-user.nix: that module
# puts the agent in the `media` group, and `users.groups.media` is declared in
# modules/users.nix, which wallace does not import. Importing it wholesale fails
# to build. It also installs the claude-code package, which wallace has no use
# for — the agent RUNS on gromit and only reaches wallace over SSH.
#
# Why this exists: on 2026-09-06 wallace had been sitting in Windows since
# 2026-09-03 with no way for the agent to correct it. The EFI boot order can only
# be made durable FROM LINUX (Windows rewrites it on every boot), and the agent
# had no login on wallace's NixOS side — the old `id_dadpc_tmp` recon key was
# deliberately revoked on 2026-08-17 and is on the standing revoke list. So the
# one machine that could fix the problem was the one nobody could reach.
{ config, lib, pkgs, ... }:

let
  sw = "/run/current-system/sw/bin";
  nopw = [ "NOPASSWD" ];
in
{
  users.users.claude = {
    isNormalUser = true;
    description = "Claude Code agent (scoped, non-root) — remote ops from gromit";
    home = "/home/claude";
    shell = pkgs.bashInteractive;

    # Read-only diagnostics without sudo. Deliberately NOT `wheel`, NOT `docker`
    # — both are root-equivalent. This key grants an unprivileged shell plus the
    # journal, and nothing else beyond the one sudo rule below.
    extraGroups = [ "systemd-journal" ];

    # Generated on gromit 2026-09-06 for this purpose alone; the private half
    # lives in the agent's ~/.ssh on gromit and is not in this repo. A public key
    # is safe in the world-readable nix store.
    openssh.authorizedKeys.keys = [
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH+GmYPPfCNxXgVdxgaqutwpkWVom4iFN5tr5033MXJp claude-gromit->wallace-20260906"
    ];
  };

  # Privileged capabilities, following the house rules in modules/agent/sudo.nix:
  # exact commands, no wildcards on dangerous verbs, purpose wrappers over raw
  # tools, and nothing that applies configuration (comin owns that).
  #
  # PARITY WITH GROMIT (Chris, 2026-10-30): "I don't want to treat Wallace any
  # differently than gromit. You should have equivalent permissions on both at
  # all times." Until now this file granted exactly one command, so every
  # routine question on wallace — is that unit failed, why did that container
  # restart, what does the firewall look like — had to go through Chris. The
  # set below is gromit's list, minus the rules whose services do not exist
  # here, plus the podman equivalents of its docker rules.
  #
  # ⚠️ Equivalent does NOT mean broader. Deliberately still absent, exactly as
  # on gromit: mount/umount, rsync, rm, dd, mkfs, nixos-rebuild, user/secret
  # changes, and `podman inspect`/`exec` (inspect prints container env, which
  # carries secrets; exec is arbitrary execution as root).
  security.sudo.extraRules = [
    {
      users = [ "claude" ];
      commands = [
        # --- EFI boot order (the original rule; wallace-specific) ----------
        #   `efibootmgr`       bare — read the current entries and order
        #   `efibootmgr -o *`  REORDER only
        # Absent: `-b`/`-B` (delete) and `-c` (create). Reordering is fully
        # reversible and cannot orphan the Windows entry; deletion is not, and
        # a mistake there is a machine that boots nothing.
        { command = "${pkgs.efibootmgr}/bin/efibootmgr";      options = [ "NOPASSWD" ]; }
        { command = "${pkgs.efibootmgr}/bin/efibootmgr -o *"; options = [ "NOPASSWD" ]; }

        # --- Read-only inspection of service-owned state ------------------
        { command = "${sw}/systemctl status *";    options = nopw; }
        { command = "${sw}/systemctl is-active *"; options = nopw; }
        { command = "${sw}/systemctl is-failed *"; options = nopw; }

        # --- Routine, reversible ops ---------------------------------------
        { command = "${sw}/systemctl reset-failed *"; options = nopw; }

        # Containerised services as a class — wallace's analogue of gromit's
        # `systemctl restart docker-*`. Restart-only (no stop/start: a restart
        # cannot leave something down), prefix-fixed so it cannot reach a
        # non-container unit, and every one is supervised and reversible.
        # Covers podman-immich-ml, podman-kokoro, podman-open-webui.
        { command = "${sw}/systemctl restart podman-*"; options = nopw; }

        # --- Session + reboot control (added 2026-11-22) -------------------
        # WHY. On 2026-11-22 three separate dead ends in one evening were all
        # the same missing verb on this list:
        #   1. wallace sat in multi-user.target for 25 days with a display
        #      manager that had never started (comin switched the default
        #      target, but `switch-to-configuration switch` does not isolate),
        #      so the attached monitor showed nothing and the only fix was to
        #      walk Chris through `systemctl start` by hand.
        #   2. With no display server the RX 580 entered BACO runtime suspend,
        #      and a chip that is off cannot do hot-plug detect — so plugging a
        #      monitor in read as "no cable" on both sides. A modeset fixes it;
        #      starting the display manager is a modeset.
        #   3. The box then came up in Windows, and the only route back was the
        #      Windows-side SSH, because nothing here could reboot it.
        #
        # `start`/`restart` on the EXACT display-manager unit. No `stop`, for
        # the same reason the podman rule above is restart-only: a restart
        # cannot leave something down, and `reboot` below is the backstop.
        { command = "${sw}/systemctl start display-manager.service";   options = nopw; }
        { command = "${sw}/systemctl restart display-manager.service"; options = nopw; }

        # Exact targets, never `isolate *`. graphical.target REQUIRES
        # multi-user.target, so isolating up from multi-user starts the display
        # stack without stopping anything already running; multi-user.target is
        # the direct undo.
        { command = "${sw}/systemctl isolate graphical.target";  options = nopw; }
        { command = "${sw}/systemctl isolate multi-user.target"; options = nopw; }

        # --- Logitech Unifying pairing (added 2026-11-22) ------------------
        # The logitech-udev-rules grant access with TAG+="uaccess" only (the
        # MODE/GROUP="plugdev" line is commented out upstream). uaccess gives
        # an ACL to the user with an ACTIVE LOCAL SEAT session — chris at the
        # console — and nothing at all to an SSH session, so without this the
        # agent cannot reach the receiver and every pairing is a Chris job.
        #
        # `pair` is bounded and reversible: it claims a free slot on the
        # receiver (a Unifying receiver holds six) and does nothing until a
        # device actually announces itself. `show` is read-only.
        #
        # ⚠️ `unpair` is deliberately ABSENT. It is the one verb here that can
        # take away a working keyboard, and on a box whose console is the
        # point, that is not a mistake worth being able to make remotely.
        { command = "${sw}/solaar pair";   options = nopw; }
        { command = "${sw}/solaar show";   options = nopw; }
        { command = "${sw}/solaar show *"; options = nopw; }

        # ⚠️ `reboot`, but deliberately NOT `poweroff`, `halt` or `suspend`.
        # The asymmetry is the whole point: a reboot is self-recovering, since
        # boot-order.nix re-asserts NixOS first on every NixOS boot, so the
        # machine comes back reachable on its own. A poweroff is NOT — wallace
        # has no out-of-band power control, so an agent-initiated poweroff
        # could only be undone by Chris walking to the machine.
        { command = "${sw}/systemctl reboot"; options = nopw; }

        # One-shot BootNext, for deliberately booting the other OS once.
        # `-n` is consumed by the firmware on the next boot and reverts itself,
        # whereas the already-granted `-o` changes the order DURABLY — so for
        # "boot Windows this once", `-n` is the correct and safer verb. `-N`
        # clears a BootNext that is no longer wanted.
        { command = "${pkgs.efibootmgr}/bin/efibootmgr -n *"; options = nopw; }
        { command = "${pkgs.efibootmgr}/bin/efibootmgr -N";   options = nopw; }

        # --- Read-only container + firewall diagnostics --------------------
        # (sudoers gotcha, learned on gromit: "cmd *" does NOT match a bare
        # "cmd" — both must be listed.)
        { command = "${sw}/podman ps";       options = nopw; }
        { command = "${sw}/podman ps *";     options = nopw; }
        { command = "${sw}/podman logs *";   options = nopw; }
        { command = "${sw}/iptables-save";   options = nopw; }

        # NOT YET: smart-dump. The wrapper the agent is allowed to run on
        # gromit lives in homelab-modules and expects that library's
        # `homelab.*` option tree, which wallace does not import — adding it
        # pulls the whole option namespace in and fails to evaluate
        # (`attribute 'homelab' missing`). Worth doing separately: wallace is
        # taking the bitcoind chain on a WD30EZRX with 34,888 power-on hours
        # and 317,995 load cycles, past the WD Green design rating, so drive
        # health needs to be visible here. Until then SMART on wallace is
        # Chris-only.
      ];
    }
  ];

  # Audit: keep a record of what the agent ran as root — same as gromit.
  security.sudo.extraConfig = ''
    Defaults:claude log_output, logfile="/var/log/sudo-claude.log", !syslog
  '';
}
