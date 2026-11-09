# Per-unit CPU/memory for USER units, into node_exporter's textfile collector.
#
# WHY. On 2026-10-28 herdr's `herdr server` process was found at 91.7% of a
# core in a 60 s sample, and 30.5% averaged over its whole 21.9-day life
# (575,414 CPU-seconds). After a reboot the fresh instance sat at 1.6%, and
# 1.2 days later was still 1.56% — flat. So there are two readings 14 times
# apart and NO WAY TO TELL whether the old one climbed slowly or just reflected
# a busier fortnight, because nothing on this box records the curve:
#
#   - node_exporter has no per-process CPU collector
#   - its systemd collector exports `node_systemd_unit_state` only — state,
#     not resource use — and herdr is a USER unit, so it is not even listed
#   - `CPUUsageNSec` is right there in the cgroup the whole time, unsampled
#
# Diagnosing from two spot readings is guesswork. This samples the thing.
#
# WHAT IT EXPORTS, and why both levels. The cgroup covers herdr AND every pane
# under it — useful, but it moves with whatever the agents are doing, so it
# cannot answer a question about herdr itself. The process series is the
# multiplexer alone. On 2026-11-01 those read 27.8% and 2.2% respectively;
# conflating them is exactly how a quiet service looks like a busy one.
{ config, lib, pkgs, ... }:

let
  textfileDir = "/var/lib/node-exporter-textfile";

  # (metric label, cgroup path, exact process name for the binary itself)
  # `comm` is matched exactly — `pgrep -x` — because a pattern match here would
  # also catch this script's own shell and any pane running the same name.
  watched = [
    {
      name = "herdr";
      cgroup = "/sys/fs/cgroup/user.slice/user-1002.slice/user@1002.service/app.slice/herdr.service";
      comm = "herdr";
    }
  ];

  mkBlock = w: ''
    # ── ${w.name} ──────────────────────────────────────────────────────────
    cg="${w.cgroup}"
    if [ -r "$cg/cpu.stat" ]; then
      usec=$(${pkgs.gnugrep}/bin/grep -m1 '^usage_usec ' "$cg/cpu.stat" | ${pkgs.gawk}/bin/awk '{print $2}')
      # A counter, deliberately: the cgroup's usec resets when the unit
      # restarts, which is exactly what a Prometheus counter reset means. Had
      # this been a gauge, the 2026-10-30 reboot would have looked like the
      # problem fixing itself rather than the measurement starting over.
      [ -n "$usec" ] && printf 'user_unit_cpu_seconds_total{unit="%s"} %s\n' "${w.name}" \
        "$(${pkgs.gawk}/bin/awk -v u="$usec" 'BEGIN{printf "%.3f", u/1000000}')"
      [ -r "$cg/memory.current" ] && printf 'user_unit_memory_bytes{unit="%s"} %s\n' "${w.name}" "$(cat "$cg/memory.current")"
      [ -r "$cg/pids.current" ]   && printf 'user_unit_pids{unit="%s"} %s\n'          "${w.name}" "$(cat "$cg/pids.current")"
    fi

    # The binary alone. Skipped entirely when absent rather than emitted as 0 —
    # a zero here is indistinguishable from "idle" and would read as an
    # all-clear for a process that is not running at all.
    pid=$(${pkgs.procps}/bin/pgrep -x "${w.comm}" | ${pkgs.coreutils}/bin/head -1 || true)
    if [ -n "$pid" ] && [ -r "/proc/$pid/stat" ]; then
      # getconf lives in glibc.bin, not coreutils. Pointing at the wrong one
      # does not fail loudly — the command-not-found goes to stderr, `hz`
      # comes back EMPTY, and awk then divides by zero on every sample. Found
      # by running the generated script; the build was perfectly happy.
      ${pkgs.gawk}/bin/awk -v n="${w.name}" -v hz="$(${pkgs.glibc.bin}/bin/getconf CLK_TCK)" '{
        printf "user_unit_proc_cpu_seconds_total{unit=\"%s\"} %.2f\n", n, ($14+$15)/hz
        printf "user_unit_proc_threads{unit=\"%s\"} %d\n", n, $20
      }' "/proc/$pid/stat"
      rss=$(${pkgs.gawk}/bin/awk '/^VmRSS:/ {print $2*1024}' "/proc/$pid/status" 2>/dev/null)
      [ -n "$rss" ] && printf 'user_unit_proc_rss_bytes{unit="%s"} %s\n' "${w.name}" "$rss"
    fi
  '';
in
{
  systemd.services.user-unit-metrics = {
    description = "Export user-unit CPU/memory to the node_exporter textfile collector";
    serviceConfig = {
      Type = "oneshot";
      # Reads other users' cgroups and /proc — root, but it only ever reads and
      # writes one .prom file.
      User = "root";
    };
    script = ''
      set -u
      ${pkgs.coreutils}/bin/mkdir -p ${textfileDir}

      # Atomic publish. node_exporter reads this directory on every scrape, and
      # a half-written file makes the WHOLE textfile collector fail — not just
      # this metric (the lesson recorded in qbit-seed-guard.nix). Write to a
      # temp in the same filesystem, then rename.
      tmp=$(${pkgs.coreutils}/bin/mktemp "${textfileDir}/.user-unit-metrics.prom.XXXXXX") || exit 0
      # Clean up the temp if anything below dies. Without this a failing
      # sample leaves its mktemp behind every single minute — observed while
      # testing the getconf bug above. They are dot-files and do not end in
      # .prom so node_exporter ignores them, which is worse, not better: they
      # would pile up in a directory nobody looks at.
      trap '${pkgs.coreutils}/bin/rm -f "$tmp"' EXIT
      {
        echo "# HELP user_unit_cpu_seconds_total CPU consumed by a user unit's whole cgroup, including children."
        echo "# TYPE user_unit_cpu_seconds_total counter"
        echo "# HELP user_unit_memory_bytes Current memory of a user unit's cgroup."
        echo "# TYPE user_unit_memory_bytes gauge"
        echo "# HELP user_unit_pids Processes in a user unit's cgroup."
        echo "# TYPE user_unit_pids gauge"
        echo "# HELP user_unit_proc_cpu_seconds_total CPU consumed by the unit's main binary alone, excluding children."
        echo "# TYPE user_unit_proc_cpu_seconds_total counter"
        echo "# HELP user_unit_proc_rss_bytes RSS of the unit's main binary alone."
        echo "# TYPE user_unit_proc_rss_bytes gauge"
        echo "# HELP user_unit_proc_threads Threads in the unit's main binary."
        echo "# TYPE user_unit_proc_threads gauge"
        ${lib.concatMapStringsSep "\n" mkBlock watched}
      } > "$tmp"
      ${pkgs.coreutils}/bin/chmod 0644 "$tmp"
      ${pkgs.coreutils}/bin/mv -f "$tmp" "${textfileDir}/user-unit-metrics.prom"
    '';
  };

  systemd.timers.user-unit-metrics = {
    description = "Sample user-unit resource use every minute";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "2min";
      OnUnitActiveSec = "1min";
      # A missed sample is a gap in a graph, not an event to catch up on.
      Persistent = false;
    };
  };
}
