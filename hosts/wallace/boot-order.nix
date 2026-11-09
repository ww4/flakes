# Re-assert NixOS first in the EFI boot order, at every boot.
#
# THE PROBLEM. Wallace dual-boots. Windows rewrites the EFI BootOrder to put
# `{bootmgr}` first EVERY time it runs — proven directly on 2026-08-16: the order
# was set with NixOS first, read back showing NixOS first, and came up in Windows
# with the order already reverted. So `efibootmgr -o` is a fix with a shelf life,
# not a setting.
#
# WHY IT WENT UNNOTICED FOR THREE WEEKS. NixOS running is NOT evidence the boot
# order is right. After the last Windows boot on 2026-08-16 23:10 the order was
# Windows-first, but NixOS was started via a one-shot `BootNext` and then ran
# until 2026-08-24 — eight days with a Windows-first order underneath and nothing
# to reveal it. The next cold start (2026-09-03 01:05) went straight to Windows
# and stayed there until 2026-09-06.
#
# THE FIX. Assert the order on every NixOS boot. Windows can still revert it, but
# the revert now survives only until the next NixOS boot instead of indefinitely,
# and the drift can no longer hide behind a running system.
#
# This does NOT make the machine self-recovering from Windows — nothing running
# under NixOS can, because NixOS is not running. Escaping a Windows-first order
# still needs the one-shot from Windows:
#   bcdedit /set "{fwbootmgr}" bootsequence "{e70d433a-9442-11f1-9d2f-806e6f6e6963}"
# What this removes is the SILENT part: after this, a cold boot lands on NixOS
# unless Windows has run since, which is a bounded and visible window.
{ config, lib, pkgs, ... }:

{
  systemd.services.efi-boot-order = {
    description = "Assert NixOS first in the EFI boot order (Windows reverts it)";
    wantedBy = [ "multi-user.target" ];
    after = [ "local-fs.target" ];

    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };

    # Never fail the boot over this. A cosmetic ordering preference must not be
    # able to leave the machine in a degraded state — it logs loudly and exits 0.
    script = ''
      set -u
      efibootmgr=${pkgs.efibootmgr}/bin/efibootmgr

      out=$("$efibootmgr" 2>/dev/null || true)
      if [ -z "$out" ]; then
        echo "efi-boot-order: efibootmgr returned nothing (no EFI vars?) — doing nothing"
        exit 0
      fi

      # The entry is matched by DESCRIPTION, not by a hardcoded number: entry
      # numbers are not stable across firmware changes, and acting on a stale
      # number would reorder the wrong thing.
      nixos=$(printf '%s\n' "$out" \
        | ${pkgs.gnugrep}/bin/grep -iE '^Boot[0-9A-Fa-f]{4}\*?[[:space:]]+NixOS-boot' \
        | ${pkgs.gnused}/bin/sed -E 's/^Boot([0-9A-Fa-f]{4}).*/\1/' | head -n1)

      if [ -z "$nixos" ]; then
        echo "efi-boot-order: no 'NixOS-boot' entry found — NOT guessing. Entries were:"
        printf '%s\n' "$out"
        exit 0
      fi

      current=$(printf '%s\n' "$out" \
        | ${pkgs.gnugrep}/bin/grep -E '^BootOrder:' | ${pkgs.gnused}/bin/sed -E 's/^BootOrder:[[:space:]]*//')

      if [ -z "$current" ]; then
        echo "efi-boot-order: could not read BootOrder — doing nothing"
        exit 0
      fi

      case "$current" in
        "$nixos"|"$nixos",*)
          echo "efi-boot-order: already first (BootOrder=$current, NixOS-boot=$nixos)"
          exit 0
          ;;
      esac

      # Preserve every other entry and its relative order — only move NixOS to
      # the front. Windows must stay in the list; dropping it would strand the
      # other half of a dual-boot machine.
      rest=$(printf '%s' "$current" | ${pkgs.gnused}/bin/sed -E "s/(^|,)$nixos(,|$)/\1/" \
        | ${pkgs.gnused}/bin/sed -E 's/^,//; s/,$//; s/,,/,/g')
      new="$nixos''${rest:+,$rest}"

      echo "efi-boot-order: BootOrder=$current -> $new (Windows had re-asserted itself)"
      "$efibootmgr" -o "$new" >/dev/null 2>&1 \
        || echo "efi-boot-order: efibootmgr -o failed — leaving the order alone"
    '';
  };
}
