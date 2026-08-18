# qbit-seed-guard — keep qBittorrent actually seeding, and watch the private
# trackers' hit-and-run rules.
#
# ⚠️ THE INCIDENT THIS EXISTS FOR (2026-08-17/18).
# The D6 zombie mount ([[xfs-zombie-mount]]) took /mnt/primary/D6 out of the
# fusion pool at 05:27. At 14:04:51 a comin deploy restarted docker-qbittorrent;
# it came back at 14:05:37 and file_stat()'d every torrent. D6 was still absent,
# so 102 torrents were rejected with "fast resume rejected ... mismatching file
# size" and dropped to `missingFiles`. D6 recovered at 14:17:08 — TWELVE MINUTES
# later — and qBittorrent never looked again.
#
# Two properties made that expensive:
#   1. qBittorrent NEVER retries a rejected fast-resume. `missingFiles` is
#      terminal until something issues a force-recheck. The data was completely
#      intact the whole time; only qBit's state was stale.
#   2. A `missingFiles` torrent stops announcing, so private trackers count it
#      as *disconnected* and start their hit-and-run clocks. Nothing on this box
#      watched that, so the first notification was an email from DarkPeers 19 h
#      later, with 4.7 h left before a 24 h rule tripped.
#
# So this module does two jobs, both derived from one API fetch:
#   RECOVER  — force-recheck `missingFiles` torrents whose files are verifiably
#              present, then reannounce them.
#   WATCH    — evaluate every torrent against its tracker's H&R rules and
#              publish metrics, so "stopped seeding" pages in ~30 min rather
#              than arriving by email from the tracker.
#
# Safety model:
#   - Recovery is gated on the pool being healthy (pool_member_healthy from
#     pool-autoremount). Rechecking during an outage would fail, waste hours of
#     disk I/O, and burn the per-torrent attempt budget for nothing.
#   - Per torrent, the content path must EXIST on the host before a recheck is
#     issued. A recheck cannot invent data; if the file is genuinely gone this
#     module must not spin on it.
#   - Attempt cap: `maxAttempts` rechecks per torrent per rolling 24 h. Data
#     that is actually lost gets left alone for the alert instead of being
#     rechecked forever.
#   - `maxPerRun` bounds how many rechecks are started at once, so a 100-torrent
#     backlog does not saturate the USB pool in one pass.
#   - If the qBit API is unreachable the run exits WITHOUT publishing metrics,
#     leaving the last good sample in place rather than reporting a false zero.
#   - Read-only otherwise: it issues recheck/reannounce, never delete, never
#     pause, never move data.
#   - Maintenance: `touch /run/qbit-seed-guard.hold` to pause it.
{ config, lib, pkgs, ... }:

let
  gromit-notify = import ./notify-pkg.nix { inherit pkgs; };

  textfileDir = "/var/lib/node-exporter-textfile";
  qbitUrl = "http://127.0.0.1:8085";

  # qBittorrent runs in a container; its /data is the host's /mnt/fusion/arr.
  # Both categories live under it (`/data/downloads` and `/data/manual`),
  # verified 2026-08-18.
  containerRoot = "/data";
  hostRoot = "/mnt/fusion/arr";

  maxAttempts = 3;    # rechecks per torrent per rolling 24 h
  maxPerRun = 8;      # rechecks started per run, to bound pool I/O

  # ── Hit-and-run rules, per tracker ──────────────────────────────────────
  # Supplied by Chris 2026-08-18. These are the ACTUAL rules of each site, not
  # guesses — keep them in sync with the tracker's own rules page, and see the
  # `tracker-hnr-rules` memory for the prose version and any nuance that does
  # not reduce to numbers.
  #
  #   seedSeconds   required seeding time
  #   ratioAlt      ratio that satisfies the rule instead (0 = no ratio route)
  #   withinDays    deadline to BANK the requirement, from download completion
  #                 (0 = none). NOT the same thing as a cure window — see below.
  #   minProgress   obligation only attaches once this much is downloaded
  #                 (0 = every torrent on the tracker counts)
  #   graceSeconds  how long a torrent may go without announcing before the
  #                 tracker records an H&R (0 = no published grace). Drives
  #                 qbit_hnr_grace_used_ratio, so each tracker gets an alert
  #                 proportional to its own tolerance instead of one shared
  #                 fixed delay.
  #   cureDays      DOCUMENTATION ONLY, and deliberately separate from
  #                 withinDays: the window to remove an H&R the tracker has
  #                 ALREADY recorded. That is tracker-side state — qBittorrent
  #                 cannot see it — so it is never computed here, only quoted
  #                 in the alert runbook. Conflating it with withinDays would
  #                 silently mis-measure both.
  trackers = [
    {
      id = "digitalcore";
      label = "DigitalCore";
      match = "digitalcore\\.club";
      seedSeconds = 5 * 24 * 3600;   # 5 days
      ratioAlt = 1.0;                # ...or 1:1
      withinDays = 0;
      minProgress = 0.10;            # "started to download 10% or more"
      graceSeconds = 3600;           # ⚠️ ONE HOUR without an announce = H&R
      cureDays = 10;                 # then it is permanent: points / upload credit / donation
      # ⚠️ The tightest tracker here by two orders of magnitude — DarkPeers
      # allows 24 h, DigitalCore allows 1 h. The dangerous case is a storage
      # outage: qbit-seed-guard deliberately DEFERS recovery while a pool
      # member is unhealthy (a recheck would only fail), and that is exactly
      # when this hour burns. Hence the grace-ratio alert, which fires at 25%
      # of the grace = 15 min here, leaving 45 min to act.
      # Account level, not visible from qBittorrent: 5 uncured H&R => warning,
      # continued accumulation => download ban.
    }
    {
      id = "darkpeers";
      label = "DarkPeers";
      match = "darkpeers\\.org";
      seedSeconds = 6 * 24 * 3600;   # 6 days
      ratioAlt = 1.0;                # ...or 1:1
      withinDays = 0;
      minProgress = 0;
      graceSeconds = 24 * 3600;      # must not be disconnected >24 h
      cureDays = 0;
    }
    {
      id = "retrotoon";
      label = "RetroToon";
      match = "retrotoon\\.world";
      seedSeconds = 72 * 3600;       # 72 h — "strictest and most important rule"
      ratioAlt = 0;                  # no ratio shortcut
      withinDays = 10;               # must be banked within 10 days of download
      minProgress = 0;
      graceSeconds = 0;              # no published announce grace
      cureDays = 0;
    }
    {
      id = "torrentleech";
      label = "TorrentLeech";
      match = "torrentleech\\.org|tleechreload\\.org";
      seedSeconds = 10 * 24 * 3600;  # 10 days for Chris's user class
      ratioAlt = 0;
      withinDays = 0;
      minProgress = 0;
      graceSeconds = 0;              # H&R raised immediately, but CLEARS on resume
      cureDays = 0;
      # Nuance not expressible as a number: TL raises an H&R as soon as a
      # torrent stops and clears it as soon as it resumes; a warning only lands
      # at 50+ concurrent H&R for 5+ consecutive days. So TL forgives a blip
      # but not a long silence — which is what not_seeding_seconds measures.
    }
  ];

  hnrJq = ./qbit-hnr.jq;

  qbit-seed-guard = pkgs.writeShellApplication {
    name = "qbit-seed-guard";
    runtimeInputs = [ pkgs.curl pkgs.jq pkgs.coreutils pkgs.gnugrep pkgs.gawk gromit-notify ];
    text = ''
      QBIT="${qbitUrl}"
      STATE="''${STATE_DIRECTORY:-/var/lib/qbit-seed-guard}"
      ATTEMPTS="$STATE/attempts"
      POOL_METRIC="${textfileDir}/pool-autoremount.prom"
      now=$(date +%s)
      window=$(( 24 * 3600 ))

      mkdir -p "$ATTEMPTS"

      if [ -e /run/qbit-seed-guard.hold ]; then
        echo "maintenance hold present (/run/qbit-seed-guard.hold) — skipping"
        exit 0
      fi

      # ── fetch ────────────────────────────────────────────────────────────
      # A failed fetch must NOT publish metrics: reporting 0 missing files
      # because the API was down is precisely the false all-clear this module
      # is meant to eliminate.
      torrents=$(curl -s --max-time 30 "$QBIT/api/v2/torrents/info" || true)
      if [ -z "$torrents" ] || ! jq -e 'type=="array"' <<<"$torrents" >/dev/null 2>&1; then
        echo "qBit API unreachable or returned a non-array — skipping run (metrics left untouched)"
        exit 0
      fi

      report=$(jq -c --argjson now "$now" \
                    --argjson rules '${builtins.toJSON trackers}' \
                    -f ${hnrJq} <<<"$torrents")

      n_missing=$(jq -r '.totals.missing_files' <<<"$report")
      n_total=$(jq -r '.totals.torrents' <<<"$report")
      echo "torrents=$n_total missingFiles=$n_missing"

      # ── how long has each at-risk torrent been off the air? ───────────────
      # A count of not-seeding torrents cannot answer "how much of DigitalCore's
      # one-hour grace is gone?". Stamp each hash the first run it appears at
      # risk, clear it when it recovers, and the difference is the real elapsed
      # time — accurate to the 5 min timer regardless of restarts.
      NS="$STATE/notseeding"
      mkdir -p "$NS"
      risk_now=$(mktemp)
      jq -r '.per_tracker[] | .at_risk[]' <<<"$report" | sort -u > "$risk_now"

      while read -r h; do
        [ -n "$h" ] || continue
        [ -f "$NS/$h" ] || echo "$now" > "$NS/$h"
      done < "$risk_now"

      # Forget torrents that recovered, so the next outage times from zero.
      for f in "$NS"/*; do
        [ -e "$f" ] || continue
        b=$(basename "$f")
        grep -qx "$b" "$risk_now" || rm -f "$f"
      done

      # Longest-running outage per tracker, and how much of that tracker's
      # published grace it has consumed.
      grace_tmp=$(mktemp)
      while IFS=$'\t' read -r tid grace; do
        [ -n "$tid" ] || continue
        worst=0
        while read -r h; do
          [ -n "$h" ] || continue
          [ -f "$NS/$h" ] || continue
          since=$(cat "$NS/$h" 2>/dev/null || echo "$now")
          d=$(( now - since ))
          [ "$d" -gt "$worst" ] && worst=$d
        done < <(jq -r --arg t "$tid" '.per_tracker[] | select(.id==$t) | .at_risk[]' <<<"$report")
        # -1 => this tracker publishes no announce grace, so no ratio applies
        # and the alert must not treat it as 0% consumed (which would look fine).
        if [ "$grace" -gt 0 ]; then
          ratio=$(awk -v w="$worst" -v g="$grace" 'BEGIN{printf "%.4f", w/g}')
        else
          ratio=-1
        fi
        printf '%s %s %s\n' "$tid" "$worst" "$ratio" >> "$grace_tmp"
        if [ "$worst" -gt 0 ]; then
          echo "  $tid: worst not-seeding streak ''${worst}s (grace=''${grace}s, used=$ratio)"
        fi
      done < <(jq -r '.per_tracker[] | "\(.id)\t\(.grace_seconds)"' <<<"$report")

      # ── announce health for private-tracker torrents ──────────────────────
      # /torrents/info does not carry per-tracker status, so ask per torrent —
      # but only for torrents on a monitored tracker, which bounds the calls.
      not_working_file=$(mktemp)
      reannounce_list=""
      while read -r h; do
        [ -n "$h" ] || continue
        st=$(curl -s --max-time 10 "$QBIT/api/v2/torrents/trackers?hash=$h" 2>/dev/null \
             | jq -r '[.[] | select(.url|startswith("http")) | .status] | max // 0' 2>/dev/null || echo 0)
        if [ "$st" != "2" ]; then
          echo "$h" >> "$not_working_file"
          reannounce_list="$reannounce_list|$h"
        fi
      done < <(jq -r '.private[]' <<<"$report")
      n_not_working=$(wc -l < "$not_working_file" | tr -d ' ')

      # Nudge anything not announcing. Cheap, idempotent, and on TorrentLeech a
      # successful announce is what actually clears an H&R flag.
      if [ -n "$reannounce_list" ]; then
        echo "reannouncing $n_not_working private-tracker torrent(s) with a non-working tracker"
        curl -s -o /dev/null --max-time 20 -X POST \
          --data "hashes=''${reannounce_list#|}" "$QBIT/api/v2/torrents/reannounce" || true
      fi

      # ── recovery ─────────────────────────────────────────────────────────
      started=0
      pool_ok=1
      if [ -r "$POOL_METRIC" ]; then
        if grep -qE '^pool_member_healthy\{[^}]*\} 0$' "$POOL_METRIC"; then
          pool_ok=0
        fi
      fi

      if [ "$n_missing" -gt 0 ] && [ "$pool_ok" -eq 0 ]; then
        echo "$n_missing torrent(s) in missingFiles but a pool member is UNHEALTHY — deferring recovery until the pool is whole"
      elif [ "$n_missing" -gt 0 ]; then
        while read -r line; do
          [ -n "$line" ] || continue
          [ "$started" -lt ${toString maxPerRun} ] || { echo "reached maxPerRun=${toString maxPerRun}; remaining torrents will be picked up next run"; break; }

          hash=$(jq -r '.hash' <<<"$line")
          name=$(jq -r '.name' <<<"$line")
          cpath=$(jq -r '.content_path' <<<"$line")

          # Container path -> host path.
          hostpath="${hostRoot}''${cpath#${containerRoot}}"
          if [ ! -e "$hostpath" ]; then
            echo "SKIP $name — content not present on host ($hostpath); a recheck cannot recover missing data"
            continue
          fi

          # Attempt cap (rolling 24 h), same shape as pool-autoremount's flap cap.
          log="$ATTEMPTS/$hash"
          recent=0
          if [ -f "$log" ]; then
            tmp=$(mktemp)
            while read -r ts; do
              [ -n "$ts" ] || continue
              if [ $(( now - ts )) -lt "$window" ]; then
                echo "$ts" >> "$tmp"
                recent=$(( recent + 1 ))
              fi
            done < "$log"
            mv "$tmp" "$log"
          fi
          if [ "$recent" -ge ${toString maxAttempts} ]; then
            echo "SKIP $name — already rechecked $recent times in 24 h; leaving it for the alert"
            continue
          fi

          echo "RECHECK $name (attempt $(( recent + 1 )) of ${toString maxAttempts})"
          if curl -s -o /dev/null --max-time 20 -X POST \
               --data "hashes=$hash" "$QBIT/api/v2/torrents/recheck"; then
            echo "$now" >> "$log"
            started=$(( started + 1 ))
          else
            echo "  recheck request FAILED for $name"
          fi
        done < <(jq -c '.missing[]' <<<"$report")

        if [ "$started" -gt 0 ]; then
          gromit-notify "qBittorrent: recovering stalled torrents" \
            "$started torrent(s) were in missingFiles with their data verifiably present, and have been force-rechecked. $n_missing were affected in total. This usually follows a storage-pool member dropping while qBit was restarted." \
            low "qbit,arrows_counterclockwise" || true
        fi
      fi

      # ── metrics ──────────────────────────────────────────────────────────
      # One sample per series (Prometheus rejects duplicates and one bad file
      # fails the whole textfile collector — learned the hard way in the
      # pool-autoremount change).
      mtmp=$(mktemp "${textfileDir}/.qbit-seed-guard.prom.XXXXXX") || exit 0
      {
        echo "# HELP qbit_torrents_missing_files Torrents qBittorrent has dropped to missingFiles."
        echo "# TYPE qbit_torrents_missing_files gauge"
        echo "qbit_torrents_missing_files $n_missing"
        echo "# HELP qbit_torrents_total Torrents known to qBittorrent."
        echo "# TYPE qbit_torrents_total gauge"
        echo "qbit_torrents_total $n_total"
        echo "# HELP qbit_hnr_unmet Torrents whose tracker H&R requirement is not yet satisfied."
        echo "# TYPE qbit_hnr_unmet gauge"
        echo "# HELP qbit_hnr_not_seeding Torrents with an UNMET H&R requirement that are not currently seeding."
        echo "# TYPE qbit_hnr_not_seeding gauge"
        echo "# HELP qbit_hnr_breached Torrents whose H&R deadline passed with the requirement unmet."
        echo "# TYPE qbit_hnr_breached gauge"
        echo "# HELP qbit_hnr_min_hours_to_deadline Hours until the soonest H&R deadline among unmet torrents (-1 = tracker has no deadline)."
        echo "# TYPE qbit_hnr_min_hours_to_deadline gauge"
        jq -r '.per_tracker[] |
          "qbit_hnr_unmet{tracker=\"\(.id)\"} \(.unmet)\n" +
          "qbit_hnr_not_seeding{tracker=\"\(.id)\"} \(.not_seeding)\n" +
          "qbit_hnr_breached{tracker=\"\(.id)\"} \(.breached)\n" +
          "qbit_hnr_min_hours_to_deadline{tracker=\"\(.id)\"} \(.min_hours_to_deadline)"' <<<"$report"
        echo "# HELP qbit_hnr_not_seeding_seconds Longest current not-seeding streak among torrents with an outstanding obligation."
        echo "# TYPE qbit_hnr_not_seeding_seconds gauge"
        echo "# HELP qbit_hnr_grace_used_ratio Fraction of the tracker's announce grace consumed by that streak (-1 = tracker publishes no grace)."
        echo "# TYPE qbit_hnr_grace_used_ratio gauge"
        while read -r tid worst ratio; do
          [ -n "$tid" ] || continue
          echo "qbit_hnr_not_seeding_seconds{tracker=\"$tid\"} $worst"
          echo "qbit_hnr_grace_used_ratio{tracker=\"$tid\"} $ratio"
        done < "$grace_tmp"
        echo "# HELP qbit_tracker_not_working Private-tracker torrents whose tracker is not in the working state."
        echo "# TYPE qbit_tracker_not_working gauge"
        echo "qbit_tracker_not_working $n_not_working"
        echo "# HELP qbit_seed_guard_rechecks_started Rechecks started by this run."
        echo "# TYPE qbit_seed_guard_rechecks_started gauge"
        echo "qbit_seed_guard_rechecks_started $started"
        echo "# HELP qbit_seed_guard_last_success_epoch Unix time of the last fully successful run."
        echo "# TYPE qbit_seed_guard_last_success_epoch gauge"
        echo "qbit_seed_guard_last_success_epoch $now"
      } > "$mtmp"
      chmod 0644 "$mtmp"
      mv -f "$mtmp" "${textfileDir}/qbit-seed-guard.prom" || rm -f "$mtmp"
      rm -f "$not_working_file" "$risk_now" "$grace_tmp"

      jq -r '.per_tracker[] | "  \(.label): total=\(.total) unmet=\(.unmet) not_seeding=\(.not_seeding) breached=\(.breached) min_h_to_deadline=\(.min_hours_to_deadline)"' <<<"$report"
    '';
  };
in
{
  environment.systemPackages = [ qbit-seed-guard ];

  systemd.services.qbit-seed-guard = {
    description = "Recover stalled qBittorrent torrents and watch tracker H&R rules";
    after = [ "docker-qbittorrent.service" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${qbit-seed-guard}/bin/qbit-seed-guard";
      StateDirectory = "qbit-seed-guard";
    };
  };

  systemd.timers.qbit-seed-guard = {
    description = "Periodic qBittorrent seeding + hit-and-run check";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "5min";
      OnUnitActiveSec = "5min";
      AccuracySec = "60s";
    };
  };
}
