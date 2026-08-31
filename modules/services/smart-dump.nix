# smart-dump — dump the FULL SMART table for every drive to files the `claude`
# agent can read.
#
# ⚠️ WHY THIS EXISTS. Until now the agent could see exactly five SMART values —
# whatever `drive-temps.nix` happened to export (overall health, reallocated,
# pending, offline-uncorrectable, CRC), up to 15 minutes stale. Everything that
# actually predicts a failing spindle was invisible: power-on hours, spin-retry
# count, command timeouts, **Load_Cycle_Count** (the classic WD Green wear-out
# mode — several of gromit's drives are Greens), the SMART error log, and the
# self-test log. So every SMART question had to be routed through Chris pasting
# `smartctl` output into a chat window by hand.
#
# That mattered on 2026-08-30, when D6 was shucked out of its WD My Book after a
# two-week fault hunt. The platter turned out to be clean — 0 reallocated, 0
# pending, 0 offline-uncorrectable, 0 CRC — but the WD USB bridge had blocked ATA
# passthrough for the drive's ENTIRE service life, so none of that had ever been
# readable. Confirming the verdict still needed a manual copy-paste.
#
# ⚠️ WHY A WRAPPER AND NOT `sudo smartctl *`. smartctl is NOT a read-only tool:
#   -t <test>     starts a self-test (changes device state, runs for hours)
#   -s on|off     enables/disables SMART on the drive — PERSISTENT
#   --set=...     writes drive configuration (APM, DSN, write-cache, SCT)
#   -C / -X       abort/alter running tests
# A bare `smartctl *` sudo entry would hand the agent all of that. This wrapper
# takes NO arguments that reach smartctl: it enumerates block devices itself and
# invokes only `-x` and `-l selftest`, both pure reads. Same closed-vocabulary
# pattern as netdiag-priv and agent-restic-ro.sh.
#
# Files are named by SERIAL, never by /dev/sdX. Kernel letters drift across
# re-enumerations — on 2026-08-28 a device-letter read misattributed a healthy
# backup drive's 66 errors to the failing pool member. An evidence file named by
# letter is stale the moment the bus renumbers.
{ config, lib, pkgs, ... }:

let
  outDir = "/var/tmp/agent-smart";

  smart-dump = pkgs.writeShellApplication {
    name = "smart-dump";
    runtimeInputs = with pkgs; [ smartmontools util-linux coreutils gnugrep gnused ];
    text = ''
      OUT=${outDir}

      if [ "$(id -u)" != 0 ]; then
        echo "smart-dump: must run as root (SMART needs raw device access)." >&2
        echo "  try: sudo smart-dump" >&2
        exit 1
      fi

      mkdir -p "$OUT"
      chmod 755 "$OUT"
      # Clear stale dumps so a reader can never mistake last week's table for
      # today's. Anchored to *.txt inside our own directory.
      find "$OUT" -maxdepth 1 -type f -name '*.txt' -delete 2>/dev/null || true

      echo "  output: $OUT"
      echo
      printf "  %-20s %-28s %-5s %-9s %s\n" SERIAL MODEL BUS DEV-TYPE RESULT

      for dev in /dev/sd[a-z] /dev/nvme[0-9]n[0-9]; do
        [ -b "$dev" ] || continue
        ser=$(lsblk -dno SERIAL "$dev" 2>/dev/null | tr -d ' ')
        mod=$(lsblk -dno MODEL  "$dev" 2>/dev/null | sed 's/ *$//')
        tran=$(lsblk -dno TRAN  "$dev" 2>/dev/null | tr -d ' ')
        [ -n "$ser" ] || ser="unknown-$(basename "$dev")"
        f="$OUT/$ser.txt"

        # ── Pick a device type ────────────────────────────────────────────
        # smartctl auto-detects most bridges, but not all. The Seagate
        # Expansion Desk on gromit fails auto-detection outright:
        #   "Read Device Identity failed: scsi error unsupported field in
        #    scsi command ... look at the various --device=TYPE variants"
        # and then EXITS, so the whole dump is lost for that drive. Auto-detect
        # first (it is right for every other drive here, including all four WD
        # Elements), and only walk the candidate list when it fails.
        #
        # ⚠️ This loop must NEVER treat "the bridge refused" as "the drive is
        # fine". If every candidate fails, we say NOT MEASURED — an unmeasured
        # drive reading as healthy is precisely how D6 ran for its entire
        # service life with nobody able to see it.
        dtype=""
        probe() { smartctl "$@" -i "$dev" 2>&1 | grep -qiE "Device Model|Model Number|Model Family"; }
        if probe; then
          dtype=""            # auto-detect works
        else
          for cand in sat "sat,12" usbjmicron usbsunplus usbcypress scsi; do
            if probe -d "$cand"; then dtype="$cand"; break; fi
          done
        fi
        if [ -n "$dtype" ]; then dargs=(-d "$dtype"); else dargs=(); fi

        {
          echo "### smart-dump $(date '+%F %I:%M:%S %p %Z')"
          echo "### device-at-dump-time: $dev  (letters are NOT stable identities)"
          echo "### serial: $ser  model: $mod  bus: $tran"
          echo "### device-type: ''${dtype:-auto}"
          echo
          # -x is the everything view: attributes, error log, self-test log and
          # the SCT / device-statistics pages that -a omits.
          smartctl "''${dargs[@]}" -x "$dev" 2>&1 || true
          echo
          echo "### --- self-test log (explicit, in case -x truncated it) ---"
          smartctl "''${dargs[@]}" -l selftest "$dev" 2>&1 || true
        } > "$f"
        chmod 644 "$f"

        # ⚠️ Classify carefully. "The bridge refused to pass SMART through" is a
        # DIFFERENT outcome from "the drive answered and reported healthy", and
        # conflating them is how a USB drive reads as fine for three months
        # while nothing is actually being measured.
        if grep -qiE "Unknown USB bridge|Read Device Identity failed|please specify device type" "$f"; then
          res="BRIDGE BLOCKS SMART — NOT MEASURED"
        elif grep -qi "self-assessment test result: PASSED" "$f"; then
          res="PASSED"
        elif grep -qi "self-assessment test result: FAILED" "$f"; then
          res="*** FAILED ***"
        else
          res="no verdict line — read the file"
        fi
        printf "  %-20s %-28s %-5s %-9s %s\n" "$ser" "''${mod:0:28}" "''${tran:-?}" "''${dtype:-auto}" "$res"
      done

      echo
      echo "  Done. Readable at $OUT/<serial>.txt"
      echo "  NOTE: 'BRIDGE BLOCKS SMART' means NOT MEASURED. It is not a clean"
      echo "        bill of health — WD USB bridges deny ATA passthrough, so"
      echo "        those drives have no failure data at all until shucked."
    '';
  };
in
{
  environment.systemPackages = [ smart-dump ];

  # Keep the dump directory out of systemd-tmpfiles' default /var/tmp sweep so a
  # dump taken during a long investigation is still there a month later.
  systemd.tmpfiles.rules = [ "d ${outDir} 0755 root root -" ];
}
