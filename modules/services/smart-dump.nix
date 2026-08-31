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
    # jq parses smartctl's -j output for the device-type probe; findutils for
    # the stale-dump sweep. Both were implicit before and must not be.
    runtimeInputs = with pkgs; [ smartmontools util-linux coreutils gnugrep gnused jq findutils ];
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
        # ⚠️ THE ACCEPTANCE TEST IS THE WHOLE TRICK, and the first version of
        # this got it wrong. It probed for a MODEL STRING ("Device Model" /
        # "Model Number"), which a SCSI-addressed bridge never emits — it says
        # Vendor:/Product: instead. So a bridge that answers health-only was
        # scored the same as one that answers nothing, every candidate "failed",
        # and the code fell back to auto-detect having learned nothing.
        #
        # drive-temps.nix already had this right and is the model here: accept a
        # candidate when it returns REAL DATA — an ATA attribute table, or a
        # health verdict — not when it returns a recognisable name.
        #
        # Prefer a candidate that yields the ATA ATTRIBUTE TABLE. Only if none
        # does, settle for one that yields a health verdict. A bridge answering
        # as a SCSI enclosure must never outrank one reaching the ATA drive,
        # which is why the plain/auto read is not automatically preferred.
        #
        # ⚠️ BOUNDED (timeout 20). A drive that has stopped answering makes
        # smartctl block for minutes — D1 on 2026-08-27 aged commands out at
        # 361 s — and this loop tries several candidates per drive.
        probe_json() {
          if [ -n "$1" ]; then timeout 20 smartctl -d "$1" -j -H -A -i "$dev" 2>/dev/null || true
          else                 timeout 20 smartctl        -j -H -A -i "$dev" 2>/dev/null || true; fi
        }
        # ⚠️ Keep the WINNING candidate's JSON in `bestjs`, not whatever the loop
        # last looked at. When a health-only candidate matches we deliberately
        # keep searching for a full one, so by the end `js` holds some later,
        # failed probe — classifying from that would report on the wrong read.
        dtype=""; quality=none; bestjs=""
        for cand in "" "sat,auto" sat "sat,16" "sat,12" usbjmicron "usbjmicron,x" usbsunplus usbprolific usbcypress scsi; do
          js=$(probe_json "$cand")
          [ -n "$js" ] || continue
          if printf '%s' "$js" | jq -e '((.ata_smart_attributes.table // []) | length) > 0' >/dev/null 2>&1; then
            dtype="$cand"; quality=full; bestjs="$js"; break
          fi
          if [ "$quality" = none ] \
             && printf '%s' "$js" | jq -e '.smart_status.passed != null' >/dev/null 2>&1; then
            dtype="$cand"; quality=health; bestjs="$js"   # keep looking for a full one
          fi
        done
        if [ -n "$dtype" ]; then dargs=(-d "$dtype"); else dargs=(); fi

        # Say which of the three outcomes this was — never print "auto" for a
        # total failure, which is what the previous version did and which made a
        # measured drive and an unmeasurable one look identical on screen.
        case "$quality" in
          full)   shown="''${dtype:-auto}" ;;
          health) shown="''${dtype:-auto}(H)" ;;
          none)   shown="NONE" ;;
        esac

        {
          echo "### smart-dump $(date '+%F %I:%M:%S %p %Z')"
          echo "### device-at-dump-time: $dev  (letters are NOT stable identities)"
          echo "### serial: $ser  model: $mod  bus: $tran"
          echo "### device-type: ''${dtype:-auto}   data-quality: $quality"
          [ "$quality" = health ] && echo "### ⚠️ HEALTH VERDICT ONLY — this bridge does not pass the ATA attribute table."
          [ "$quality" = none ]   && echo "### ⚠️ NOTHING READABLE — no device-type candidate returned data."
          echo
          # -x is the everything view: attributes, error log, self-test log and
          # the SCT / device-statistics pages that -a omits.
          timeout 60 smartctl "''${dargs[@]}" -x "$dev" 2>&1 || true
          echo
          echo "### --- self-test log (explicit, in case -x truncated it) ---"
          timeout 30 smartctl "''${dargs[@]}" -l selftest "$dev" 2>&1 || true
        } > "$f"
        chmod 644 "$f"

        # ⚠️ Classify from the PROBE's data-quality, not by grepping the -x text.
        # "The bridge refused" and "the drive answered and is healthy" are
        # different outcomes, and so is "the bridge gave a verdict but no
        # attributes" — that third case is the dangerous one, because a bare
        # PASSED is threshold-based and stays PASSED until very late. Fusion D1
        # reported PASSED in 626 of 626 samples, the last of them 8 minutes
        # before it stopped answering entirely.
        case "$quality" in
          none)   res="NOT MEASURED — no candidate returned data" ;;
          health) if printf '%s' "$bestjs" | jq -e '.smart_status.passed == true' >/dev/null 2>&1
                  then res="health-only PASSED — NO ATTRIBUTES (weak signal)"
                  else res="*** health-only FAILED ***"; fi ;;
          full)   if   grep -qi "self-assessment test result: PASSED" "$f"; then res="PASSED"
                  elif grep -qi "self-assessment test result: FAILED" "$f"; then res="*** FAILED ***"
                  else res="attributes present, no verdict line — read the file"; fi ;;
        esac
        printf "  %-20s %-28s %-5s %-9s %s\n" "$ser" "''${mod:0:28}" "''${tran:-?}" "$shown" "$res"
      done

      echo
      echo "  Done. Readable at $OUT/<serial>.txt"
      echo "  DEV-TYPE column: a bare type means the ATA attribute table was read;"
      echo "                   '(H)' means health verdict ONLY, no attributes;"
      echo "                   'NONE' means nothing was readable at all."
      echo "  ⚠️ 'health-only' and 'NOT MEASURED' are NOT clean bills of health."
      echo "     A bare PASSED is threshold-based and stays PASSED until very"
      echo "     late — fusion D1 reported it 8 minutes before it stopped"
      echo "     answering. Only a full attribute table predicts failure."
    '';
  };
in
{
  # smartmontools itself, so `smartctl` is on the PATH for a human at the
  # keyboard. It was NOT installed system-wide before — it existed only inside
  # the closures of this wrapper and drive-temps.nix, so on 2026-08-30 Chris ran
  #
  #   sudo smartctl -t long /dev/disk/by-id/ata-WDC_WD40EZRX-...
  #   sudo: smartctl: command not found
  #
  # and the only way to start D6's post-shuck surface test was a bare /nix/store
  # path. `smart-dump` deliberately cannot do this: a self-test CHANGES DEVICE
  # STATE and runs for hours, so it is excluded from the agent's read-only
  # wrapper by design. That makes an interactive smartctl the right answer for a
  # human, not a wider sudo grant for the agent.
  #
  # This adds no agent capability whatsoever — the agent's sudo entry names
  # `smart-dump` explicitly, not smartctl, so the agent still cannot run
  # `-t`, `-s`, `--set` or `-X`.
  environment.systemPackages = [ smart-dump pkgs.smartmontools ];

  # Keep the dump directory out of systemd-tmpfiles' default /var/tmp sweep so a
  # dump taken during a long investigation is still there a month later.
  systemd.tmpfiles.rules = [ "d ${outDir} 0755 root root -" ];
}
