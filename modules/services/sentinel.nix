# gromit-sentinel — Phase 1: a lightweight watchdog that detects trouble and
# notifies (NO autonomous action yet — that's Phase 2/3).
#
# Tier 1 (this module): a cheap check runs every 2 min (systemd timer). It reads
# its checks from a config file, debounces + dedups, and on a confirmed NEW
# incident gathers a bit of evidence and posts to ntfy. The hand-off point where
# Phase 2 will call `claude -p` is marked in the script.
#
# Edit `sentinelConfig` below to change what it watches — it renders to
# /etc/sentinel/config.json, which the watcher reads each run (no code change
# needed to add/remove checks, just edit the attrset + deploy).
#
# Quick test without waiting for a real failure: trigger the built-in `selftest`
# marker check —  `touch /run/sentinel/fire-test`  (as the claude user, or
# `sudo touch ...`). Within ~2 min you get a "🔍 Sentinel: selftest" ntfy, the
# marker auto-clears, and ~2 min later a "✅ resolved" — exercising the whole
# detect → debounce → notify → resolve pipeline. Nothing actually wrong.
{ config, lib, pkgs, ... }:

let
  # Helper for the backup-health check: fires (exit 0 + a message) if any key
  # backup unit's last run did not succeed.
  backupCheck = pkgs.writeShellApplication {
    name = "sentinel-check-backups";
    runtimeInputs = [ pkgs.systemd ];
    text = ''
      units=(restic-backups-critical-local restic-backups-critical-b2 bub-mirror-sync media-mirror-sync gyb-backup postgresqlBackup-immich postgresqlBackup-nextcloud)
      bad=""
      for u in "''${units[@]}"; do
        r="$(systemctl show -p Result --value "$u" 2>/dev/null || echo unknown)"
        [ "$r" = "success" ] || bad="$bad $u($r)"
      done
      if [ -n "$bad" ]; then
        echo "backup unit(s) not OK:$bad"
        exit 0    # fire
      fi
      exit 1        # all good -> no incident
    '';
  };

  # Container-level silent failures. Units/metrics can't see these — proven by
  # TWO multi-week incidents: jellyseerr logged [error] ~24x/hour for 4 weeks
  # behind an active unit (docker-bridge firewall gap, #73), and qbittorrent
  # crash-looped for 17 days behind an "Up" container with EMPTY docker logs
  # (stale QLockFile, #93). Two detection modes:
  #   A. error-spam  — >= ERR_MAX error-ish lines in a container's journald
  #      stream (log-driver=journald) within the last hour. Generic, all
  #      running containers.
  #   B. dead-backend — a KNOWN loopback-published HTTP web UI stops answering
  #      HTTP while its container is Up. curl treats ANY HTTP status as alive;
  #      refused/timeout/empty/reset = dead. This catches the qbit mode, where
  #      docker-proxy ACCEPTS the TCP connect and the inside resets it — a raw
  #      TCP probe would false-negative. Static list on purpose (a mempool/
  #      fulcrum port speaks non-HTTP and would false-positive); extend the
  #      list when a new loopback web UI is added.
  # Runs as the sentinel user (claude): docker ps via the #76 scoped sudo,
  # journald via the systemd-journal group.
  containerCheck = pkgs.writeShellApplication {
    name = "sentinel-check-containers";
    runtimeInputs = [ pkgs.curl pkgs.gnugrep pkgs.coreutils ];
    text = ''
      ERR_MAX=20
      found=0

      # A: error-spam across every running container's journal stream.
      while IFS= read -r name; do
        [ -n "$name" ] || continue
        n=$(journalctl CONTAINER_NAME="$name" --since -1h --no-pager 2>/dev/null \
              | grep -cE '\[(error|ERROR|fatal|FATAL)\]|ERROR|FATAL|panic:' || true)
        if [ "''${n:-0}" -ge "$ERR_MAX" ]; then
          echo "$name: $n error-lines in the last hour"
          found=1
        fi
      done < <(/run/wrappers/bin/sudo -n docker ps --format '{{.Names}}' 2>/dev/null || true)

      # B: known loopback HTTP backends, probed only while their container is Up.
      running="$(/run/wrappers/bin/sudo -n docker ps --format '{{.Names}}' 2>/dev/null || true)"
      for entry in qbittorrent:8085 prowlarr:9696 sonarr:8989 radarr:7878 jellyseerr:5055 flaresolverr:8191; do
        cname="''${entry%%:*}"; port="''${entry##*:}"
        grep -qx "$cname" <<<"$running" || continue
        if ! curl -s -o /dev/null --max-time 4 "http://127.0.0.1:$port/"; then
          echo "$cname: web backend :$port not answering HTTP while container is Up"
          found=1
        fi
      done

      [ "$found" -eq 1 ]   # exit 0 => findings => sentinel fires
    '';
  };

  # SEEDING health — new 2026-08-10, because the cost of silent seeding downtime
  # changed. Four private trackers are now live, and DarkPeers' rules (3.4-3.6)
  # turn *disconnection* into account damage on an automated schedule staff cannot
  # override: 24h -> pre-warning, 3 days -> Warning, 3 Warnings -> 14-day download
  # ban, repeated -> ban. Seeding downtime used to cost nothing; it now costs
  # standing on every private tracker at once.
  #
  # This host has THREE documented ways to stop seeding SILENTLY, none of which any
  # existing check sees — every one of them leaves the container "Up" and the unit
  # "active":
  #   - qBit <-> gluetun VPN-restart wedge: every tracker returns EPERM, DHT 0
  #     peers, status "firewalled", while gluetun still reports (healthy)
  #   - qBittorrent stale QLockFile: crash-loop with EMPTY docker logs — ran 17 days
  #   - container DNS wedge: EAI_AGAIN forever, neighbouring containers fine
  # Under the old rules those were an annoyance. Under DarkPeers' they are 3 days
  # from a download ban.
  #
  # Four probes, cheapest first. Thresholds chosen to catch a SYSTEMIC failure and
  # ignore ordinary churn:
  seedingCheck = pkgs.writeShellApplication {
    name = "sentinel-check-seeding";
    runtimeInputs = [ pkgs.curl pkgs.jq pkgs.coreutils ];
    text = ''
      Q=http://127.0.0.1:8085

      # 1. Is the API answering at all? Catches the crash-loop / stale-lock mode,
      #    where the container is Up and docker logs are empty.
      info=$(curl -sS --max-time 10 "$Q/api/v2/transfer/info" 2>/dev/null || true)
      if [ -z "$info" ] || ! printf '%s' "$info" | jq -e . >/dev/null 2>&1; then
        echo "qBittorrent API not answering on $Q (container may be Up but wedged)"
        exit 0
      fi

      # 2. connection_status. "firewalled" is the exact signature of the
      #    gluetun VPN-restart wedge; "disconnected" is a dead tunnel.
      cs=$(printf '%s' "$info" | jq -r '.connection_status // "unknown"')
      if [ "$cs" != "connected" ]; then
        echo "qBittorrent connection_status=$cs (expected connected) — inbound likely dead, HnR risk on the private trackers"
        exit 0
      fi

      t=$(curl -sS --max-time 20 "$Q/api/v2/torrents/info" 2>/dev/null || true)
      printf '%s' "$t" | jq -e . >/dev/null 2>&1 || { echo "qBittorrent torrents API returned nothing usable"; exit 0; }
      total=$(printf '%s' "$t" | jq 'length')
      [ "$total" -eq 0 ] && exit 1   # nothing loaded: not a seeding fault

      # 3. Loaded but nothing seeding at all -> systemic, not per-torrent.
      seeding=$(printf '%s' "$t" | jq '[.[]|select(.state|test("UP$|uploading"))]|length')
      if [ "$seeding" -eq 0 ]; then
        echo "qBittorrent has $total torrents loaded but NONE seeding"
        exit 0
      fi

      # 4. Torrents with no WORKING tracker. qBit blanks .tracker when every
      #    announce fails, which is what EPERM does to all of them at once.
      #    50% because individual public trackers die routinely; a systemic
      #    failure takes them all. Healthy baseline measured 0/143.
      notrk=$(printf '%s' "$t" | jq '[.[]|select((.tracker//"")=="")]|length')
      if [ "$total" -ge 10 ] && [ "$(( notrk * 100 / total ))" -ge 50 ]; then
        echo "$notrk of $total torrents have NO working tracker — announces failing (EPERM/DNS wedge signature)"
        exit 0
      fi

      exit 1
    '';
  };

  # API-CONTENT checks — the gap that let mempool.space break for 17 days.
  #
  # Every liveness signal we had was green the whole time: containers "Up",
  # units "active", and the FRONTEND happily serving HTTP 200. The only broken
  # thing was /api/v1/blocks (500). The lesson generalises: for an app split
  # into a web frontend and a backend API, probing the frontend proves almost
  # nothing — it serves its bundle happily while everything behind it is dead.
  #
  # THREE probes, in order of how early they catch a real fault:
  #
  #   1. DIVERGENCE (primary) — mempool's tip vs the REAL chain tip, read from
  #      Fulcrum's Electrum port. This is the honest signal. It caught nothing
  #      before 2026-08-10 because the check only measured wall-clock age, and
  #      age is the wrong quantity: a tip 149 min old passed a 180 min limit
  #      while mempool sat 14 BLOCKS behind, plainly broken. Blocks are ~10 min
  #      apart and mempool normally trails by 0-1, so >=3 behind is unambiguous
  #      and cannot flap on ordinary timing.
  #      Fulcrum is the oracle rather than bitcoind because it needs no auth
  #      (bitcoind's cookie is 0600 and unreadable by the sentinel user) and it
  #      tracks bitcoind's tip exactly.
  #
  #   2. REACHABILITY — non-200 or unparsable => backend broken outright.
  #
  #   3. STALENESS (backstop) — catches the case divergence CANNOT see: the
  #      whole stack frozen together, where mempool and Fulcrum agree because
  #      neither is advancing. 6h, deliberately loose; divergence is the tripwire.
  apiCheck = pkgs.writeShellApplication {
    name = "sentinel-check-apis";
    runtimeInputs = [ pkgs.curl pkgs.jq pkgs.coreutils pkgs.gnused pkgs.libressl ];
    text = ''
      found=0

      # --- the real chain tip, via Fulcrum's Electrum protocol (no auth) ---
      chain=$(printf '{"jsonrpc":"2.0","id":1,"method":"blockchain.headers.subscribe","params":[]}\n' \
        | timeout 15 nc 127.0.0.1 50001 2>/dev/null | head -1 \
        | jq -r '.result.height // empty' 2>/dev/null || true)

      # name | url | jq expr yielding a unix timestamp | max age (s) | jq expr yielding a height
      probes=(
        "mempool|http://127.0.0.1:8081/api/v1/blocks|.[0].timestamp|21600|.[0].height"
      )
      for p in "''${probes[@]}"; do
        IFS='|' read -r name url expr maxage hexpr <<< "$p"
        body=$(curl -sS --max-time 15 -w $'\n%{http_code}' "$url" 2>/dev/null || true)
        code=$(printf '%s' "$body" | tail -n1)
        json=$(printf '%s' "$body" | sed '$d')
        if [ "$code" != "200" ]; then
          echo "$name: $url -> HTTP ''${code:-no-response}"
          found=1; continue
        fi

        # 1. divergence vs the real chain tip
        h=$(printf '%s' "$json" | jq -r "$hexpr" 2>/dev/null || true)
        if [ -n "$chain" ] && [ -n "$h" ] && [ "$h" != "null" ]; then
          lag=$(( chain - h ))
          if [ "$lag" -ge 3 ]; then
            echo "$name: BEHIND CHAIN by $lag blocks (mempool=$h, chain=$chain) — indexer stalled"
            found=1; continue
          fi
        fi

        # 2. usable data at all
        ts=$(printf '%s' "$json" | jq -r "$expr" 2>/dev/null || true)
        case "$ts" in
          ""|null|*[!0-9]*)
            echo "$name: $url returned 200 but no usable data at '$expr' (backend up, data broken)"
            found=1; continue ;;
        esac

        # 3. staleness backstop (whole stack frozen together)
        age=$(( $(date +%s) - ts ))
        if [ "$age" -gt "$maxage" ]; then
          echo "$name: data STALE — newest entry $((age/60)) min old (limit $((maxage/60)) min)"
          found=1
        fi
      done
      [ "$found" -eq 1 ] && exit 0   # fire
      exit 1                          # all good
    '';
  };

  sentinelConfig = {
    enabled = true;
    pollSec = 120;          # informational; the systemd timer drives the cadence
    debounce = 2;           # a condition must persist this many checks before it escalates
    cooldownSec = 7200;     # don't re-escalate the same incident within 2 h
    maxPerHour = 6;         # global rate limits (cost + anti-storm guard)
    maxPerDay = 30;
    ntfyServer = "http://127.0.0.1:8090";
    ntfyTopic = "gromit-alerts";

    # Phase 2: checks flagged `agent = true` get a read-only `claude -p`
    # diagnosis (per /etc/sentinel/playbook.md) after the detection notice.
    # agentEnabled is the master kill-switch for that layer (false => Phase-1
    # notify-only behaviour for every check).
    agentEnabled = true;
    agentTimeout = 300;     # seconds; claude -p is killed past this

    # Phase 3: checks flagged `act = true` (and only when agent = true) MAY take
    # one bounded corrective action (whitelisted restart or a Chris-gated fix
    # PR). actEnabled is the master kill-switch for ACTING (false => diagnose +
    # notify only, even for act-flagged checks). Acting is further bounded by a
    # daily cap and a per-incident action cooldown (don't re-act on a recurrence).
    actEnabled = true;
    maxActionsPerDay = 5;
    actionCooldownSec = 86400;   # after acting on a check, won't act again on it for 24 h (recurrence => escalate)

    checks = [
      # Any systemd unit in the failed state (excluding known-noisy ones).
      # notifyDetection = false: Grafana's SystemdUnitFailed alert already pings
      # detection, so we skip the duplicate notice and only send the diagnosis/action.
      { id = "failed-units"; type = "failed-units"; severity = "warning"; exclude = [ ]; agent = true; act = true; notifyDetection = false; }

      # comin couldn't build/eval/deploy/fetch — gromit silently stuck on the old gen.
      # (Grafana's CominDeployFailed already pings detection — skip the duplicate.)
      { id = "comin-deploy"; type = "comin"; severity = "warning"; agent = true; act = true; notifyDetection = false; }

      # Disk/pool space — the root fs + both mergerfs pools (node_exporter sees
      # them). Fires when any is >90% used. Diagnose (act off — escalate; the
      # agent shouldn't auto-delete to free space).
      { id = "disk-space"; type = "metric"; severity = "warning"; agent = true; act = false;
        expr = "100 - (node_filesystem_avail_bytes{mountpoint=~\"/|/mnt/fusion|/mnt/backup/all\"} * 100 / node_filesystem_size_bytes{mountpoint=~\"/|/mnt/fusion|/mnt/backup/all\"})";
        op = ">"; threshold = 90; }

      # Backup health — fires if any key backup unit's last run did not succeed.
      # (NOTE: catches FAILED runs; staleness/never-ran is a future refinement.)
      { id = "backup-health"; type = "command"; severity = "warning"; agent = true; act = false;
        cmd = "${backupCheck}/bin/sentinel-check-backups"; }

      # Kernel OOM-kills in the last 10 min (cert renewal failures are already
      # covered by failed-units, so no separate cert check).
      { id = "oom"; type = "command"; severity = "warning"; agent = true; act = false;
        cmd = "journalctl -k --since -10min --no-pager | grep -iE 'out of memory|oom-kill|killed process'"; }

      # Drive FAILURE — the real thing (Chris 2026-08-10: only wants to know if a
      # drive is actually failing, not just transiently slow). Fed by the SMART
      # health attributes drive-temps.nix now exports. Two signals:
      #   (a) SMART overall-health self-assessment = FAILED (definitive), and
      #   (b) reallocated / pending / offline-uncorrectable sectors appearing
      #       (>0) — developing failure, the early warning. CRC (attr 199) is a
      #       cable/link signal, tracked but not alerted (it's not drive death).
      # Labels by device so the ntfy names the failing drive.
      { id = "drive-smart-failed"; type = "metric"; severity = "critical"; agent = true; act = false;
        expr = "gromit_drive_smart_ok"; op = "<"; threshold = 1; }
      { id = "drive-bad-sectors"; type = "metric"; severity = "warning"; agent = true; act = false;
        expr = "gromit_drive_reallocated_sectors + gromit_drive_pending_sectors + gromit_drive_offline_uncorrectable";
        op = ">"; threshold = 0; }

      # Container-level silent failures (error-spam + dead HTTP backends) —
      # see containerCheck above for the two proven incident modes. Diagnose
      # only (act = false); 60s timeout covers the per-container journal scans.
      { id = "container-errors"; type = "command"; severity = "warning"; agent = true; act = false;
        cmd = "${containerCheck}/bin/sentinel-check-containers"; timeout = 60; }

      # API-content health (see apiCheck above). Catches the mode where the
      # frontend serves 200 while the backend API is dead or frozen — the
      # 17-day mempool cookie outage. Diagnose only (act = false): the fix is
      # usually a credential re-stage, which the agent can now do via its
      # Seeding health (see seedingCheck above). New 2026-08-10: with four private
      # trackers live, silent seeding downtime now costs account standing —
      # DarkPeers issues a Warning after 3 days disconnected, 3 Warnings = 14-day
      # download ban, fully automated. act = false deliberately for now: the known
      # fix is `systemctl restart docker-qbittorrent` (which IS in the agent's
      # sudo scope), but I want to see this fire correctly on a real incident
      # before letting it restart the client unattended.
      { id = "seeding-health"; type = "command"; severity = "warning"; agent = true; act = false;
        cmd = "${seedingCheck}/bin/sentinel-check-seeding"; timeout = 45; }

      # scoped sudo, but it should say what it found before touching anything.
      { id = "api-content"; type = "command"; severity = "warning"; agent = true; act = false;
        cmd = "${apiCheck}/bin/sentinel-check-apis"; timeout = 60; }

      # Built-in self-test (notify path only — no agent): fires when the marker
      # exists, then clears it. The "trigger on demand" hook for the pipeline.
      { id = "selftest"; type = "marker"; path = "/run/sentinel/fire-test";
        minConsecutive = 1; clearAfter = true; severity = "test"; agent = false;
        message = "Synthetic sentinel self-test — pipeline is working, nothing is wrong."; }

      # Agent diagnosis-path self-test (agent on, act OFF): verifies claude -p
      # diagnoses AND that it does NOT act when acting isn't permitted.
      # `touch /run/sentinel/fire-agenttest`.
      { id = "agenttest"; type = "marker"; path = "/run/sentinel/fire-agenttest";
        minConsecutive = 1; clearAfter = true; severity = "test"; agent = true; act = false;
        message = "Synthetic AGENT-path test — exercise claude -p DIAGNOSIS only; do not act; nothing is wrong."; }

      # Action-path drill (agent on, act ON): exercises the Phase-3 act path with
      # a safe, reversible, Chris-gated action. `touch /run/sentinel/fire-acttest`.
      { id = "acttest"; type = "marker"; path = "/run/sentinel/fire-acttest";
        minConsecutive = 1; clearAfter = true; severity = "test"; agent = true; act = true;
        message = "DRILL — Phase-3 action-path test. Perform EXACTLY ONE safe action: in the ww4/flakes repo (~/flakes locally) open a Chris-gated PR — branch, append one timestamped line to SENTINEL-DRILLS.md at the repo root (create it if missing), push, open the PR via the ww4-bot Forgejo API, request `chris` as reviewer, and do NOT merge it — titled '[sentinel drill] action-path test'. Report the PR number. Nothing is actually wrong; take no other action."; }
    ];
  };
  # ─────────────────────────────────────────────────────────────────

  watcher = pkgs.writers.writePython3Bin "gromit-sentinel" {
    flakeIgnore = [ "E501" "W503" "W504" ];
  } ''
    import html
    import json
    import os
    import subprocess
    import sys
    import time
    import urllib.parse
    import urllib.request

    CONFIG = os.environ.get("SENTINEL_CONFIG", "/etc/sentinel/config.json")
    STATE = os.environ.get("SENTINEL_STATE", "/var/lib/sentinel/state.json")
    INCIDENT_DIR = os.environ.get("SENTINEL_INCIDENTS", "/var/lib/sentinel/incidents")


    def load_json(path, default):
        try:
            with open(path) as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return default


    def save_json(path, data):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)


    def http_get(url, timeout=5):
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode()


    def sh(cmd, timeout=15):
        try:
            return subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return None


    def _cert_days_left(domain):
        # Read the cert nginx actually serves for `domain` over TLS. Uses only a
        # localhost handshake — no access to the root-only /var/lib/acme files.
        # Returns days-remaining as a float, or None if it can't be determined.
        r = sh("echo | ${pkgs.openssl}/bin/openssl s_client -connect 127.0.0.1:443 "
               "-servername %s 2>/dev/null | ${pkgs.openssl}/bin/openssl x509 "
               "-enddate -noout" % domain, timeout=10)
        if not r or not r.stdout or "notAfter=" not in r.stdout:
            return None
        s = r.stdout.split("notAfter=", 1)[1].strip()
        try:                                            # e.g. "Aug 22 13:23:51 2026 GMT"
            exp = time.mktime(time.strptime(s, "%b %d %H:%M:%S %Y %Z"))
        except ValueError:
            return None
        return (exp - time.time()) / 86400.0


    def _acme_false_alarm(unit):
        # A NixOS `acme-order-renew-<domain>.service` exits 11 to mean "cert is
        # still valid, nothing renewed" — but systemd records that as failed, so
        # it shows up in `systemctl --failed` (bit us 07-24 on grafana +
        # qbittorrent). Suppress it ONLY when it's genuinely benign: exit == 11
        # AND the served cert still has comfortable runway. Any other exit code
        # (a real renewal failure), or a cert actually nearing expiry, is NOT
        # suppressed — so a true problem still alerts, with days of headroom.
        prefix, suffix = "acme-order-renew-", ".service"
        if not (unit.startswith(prefix) and unit.endswith(suffix)):
            return False
        r = sh(["systemctl", "show", unit, "-p", "ExecMainStatus", "--value"])
        if not (r and r.stdout.strip() == "11"):
            return False
        domain = unit[len(prefix):-len(suffix)]
        days = _cert_days_left(domain)
        return days is not None and days > 20


    def check_failed_units(c):
        exclude = set(c.get("exclude", []))
        r = sh(["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"])
        lines = r.stdout.splitlines() if (r and r.stdout) else []
        units = [ln.split()[0] for ln in lines if ln.split()]
        units = [u for u in units if u not in exclude and not _acme_false_alarm(u)]
        return (len(units) > 0, "failed: " + ", ".join(units))


    def check_comin(c):
        try:
            text = http_get("http://127.0.0.1:4243/metrics")
        except Exception:
            return (False, "")
        bad = []
        for line in text.splitlines():
            if line.startswith("comin_last_") and "_failed" in line:
                try:
                    val = float(line.rsplit(None, 1)[1])
                except (ValueError, IndexError):
                    continue
                if val >= 1:
                    bad.append(line.split("{")[0].split()[0])
        return (len(bad) > 0, "comin: " + ", ".join(sorted(set(bad))))


    def check_metric(c):
        op = c.get("op", ">")
        thr = float(c.get("threshold", 0))
        url = "http://localhost:9090/api/v1/query?query=" + urllib.parse.quote(c["expr"])
        try:
            data = json.loads(http_get(url))
        except Exception:
            return (False, "")
        hits = []
        for s in data.get("data", {}).get("result", []):
            try:
                v = float(s["value"][1])
            except (KeyError, IndexError, ValueError):
                continue
            ok = {">": v > thr, "<": v < thr, ">=": v >= thr, "<=": v <= thr, "==": v == thr}.get(op, False)
            if ok:
                m = s.get("metric", {})
                tag = m.get("mountpoint") or m.get("device") or m.get("instance") or m.get("__name__") or ""
                hits.append(("%s=%g" % (tag, v)).strip("="))
        return (len(hits) > 0, "%s: %s" % (c["id"], ", ".join(hits)))


    def check_command(c):
        r = sh(c["cmd"], timeout=int(c.get("timeout", 15)))
        if r is None:
            return (False, "")
        fired = (r.returncode == int(c.get("fireOnExit", 0)))
        out = (r.stdout or r.stderr or "").strip().splitlines()
        return (fired, (out[0][:200] if out else c.get("message", c["id"])) if fired else "")


    def check_marker(c):
        if os.path.exists(c["path"]):
            if c.get("clearAfter", True):
                try:
                    os.remove(c["path"])
                except OSError:
                    pass
            return (True, c.get("message", "synthetic test trigger"))
        return (False, "")


    DISPATCH = {
        "failed-units": check_failed_units,
        "comin": check_comin,
        "metric": check_metric,
        "command": check_command,
        "marker": check_marker,
    }


    def gather_evidence(c, detail):
        t = c["type"]
        lines = ["[%s] %s" % (c["id"], detail), ""]
        if t == "failed-units":
            for u in detail.replace("failed: ", "").split(", "):
                if not u:
                    continue
                s = sh(["systemctl", "status", u, "--no-pager", "-l", "-n", "20"])
                lines += ["### systemctl status %s" % u, (s.stdout if s else "")[:2000], ""]
        elif t == "comin":
            s = sh(["journalctl", "-u", "comin", "-n", "40", "--no-pager"])
            lines += ["### comin log tail", (s.stdout if s else "")[-2000:]]
        elif t == "command":
            r = sh(c["cmd"])
            lines += ["### command output", ((r.stdout or "") if r else "")[:2000]]
        elif t == "metric":
            lines += ["Prometheus: %s  %s %s" % (c.get("expr"), c.get("op", ">"), c.get("threshold"))]
        elif t == "marker":
            lines += ["Synthetic test incident. The detect -> debounce -> handler -> ntfy pipeline is working; nothing is actually wrong."]
        return "\n".join(lines)


    def ntfy(cfg, title, body, priority, tags):
        server = cfg.get("ntfyServer", "http://127.0.0.1:8090")
        topic = cfg.get("ntfyTopic", "gromit-alerts")
        # HTTP headers are latin-1, so the Title must be ASCII — emoji belong in
        # the Tags field (ntfy renders tag names as icons). Strip any stray
        # non-ASCII defensively so a notification can never fail to send.
        safe_title = title.encode("ascii", "ignore").decode().strip() or "Sentinel"
        req = urllib.request.Request(
            "%s/%s" % (server, topic), data=body.encode(),
            headers={"Title": safe_title, "Priority": str(priority), "Tags": tags})
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print("ntfy post failed: %r" % e, file=sys.stderr)


    PRI = {"test": 2, "info": 2, "warning": 3, "critical": 4}
    TAG = {"test": "test_tube", "info": "information_source", "warning": "warning", "critical": "rotating_light"}


    def run_agent(cid, c, detail, path, cfg, act_permitted, recently_acted):
        # Phase 3: hand the incident to a headless `claude -p`. It diagnoses and,
        # only when act_permitted, may take ONE bounded action (whitelisted
        # restart or a Chris-gated PR) per the playbook. Returns the reply text,
        # or "" on timeout/failure (caller falls back to a plain notice).
        timeout = int(c.get("agentTimeout", cfg.get("agentTimeout", 300)))
        if act_permitted:
            permit = "ACTING IS PERMITTED for this incident (you may take ONE bounded action per the playbook, or escalate)."
        elif recently_acted:
            permit = "ACTING IS NOT PERMITTED: you acted on this within the last day and it has recurred — do NOT act again; diagnose and escalate to Chris."
        else:
            permit = "ACTING IS NOT PERMITTED for this incident — diagnose only and recommend any fix for Chris."
        prompt = (
            "You are gromit-sentinel's incident handler. Read /etc/sentinel/playbook.md "
            "and follow it EXACTLY.\n%s\n"
            "Incident:\n  check: %s (type %s, severity %s)\n  detail: %s\n  evidence file: %s\n"
            "Read the evidence file first, investigate read-only, then act-or-escalate per "
            "the playbook and reply in the required format (first line must be the ACTION: line)."
            % (permit, cid, c.get("type"), c.get("severity", "warning"), detail, path)
        )
        r = sh(["claude", "-p", prompt], timeout=timeout)
        if r is None or r.returncode != 0:
            return ""
        return (r.stdout or "").strip()


    PAGE_STYLE = ("body{max-width:60rem;margin:2rem auto;padding:0 1rem;font:15px/1.55 system-ui,-apple-system,sans-serif;color:#e6e6e6;background:#181818}"
                  "h1{font-size:1.5rem}.t{color:#888;font-size:.85em}"
                  ".inc{border:1px solid #444;border-radius:8px;padding:.6em .9em;margin:.9em 0;background:#1f1f1f}"
                  ".b{display:inline-block;font-size:.72em;font-weight:600;padding:.1em .5em;border-radius:4px;margin-left:.5em;vertical-align:middle}"
                  ".acted{background:#3a2f00;color:#ffcf5a}.diag{background:#0d2a3a;color:#6cb6ff}.det{background:#333;color:#ccc}"
                  "pre{white-space:pre-wrap;background:#2c2c2c;padding:.6em;border-radius:6px;font-size:.85em;margin:.5em 0 0}a{color:#6cb6ff}")


    SPACE_LOG_DIR = "/var/lib/silverbullet/System/Sentinel"


    def append_space_log(cid, ts, sev, detail, report):
        # Mirror the incident onto a monthly markdown page in the SilverBullet
        # space (System/Sentinel/YYYY-MM) — searchable/linkable durable record;
        # the HTML log below stays the no-login quick view. Newest last.
        # Decided with Chris 2026-07-09 (space = source of truth for reports).
        try:
            os.makedirs(SPACE_LOG_DIR, exist_ok=True)
            month = time.strftime("%Y-%m", time.localtime(ts))
            page = os.path.join(SPACE_LOG_DIR, "%s.md" % month)
            new = not os.path.exists(page)
            first = (report.splitlines() or [""])[0].strip().upper() if report else ""
            if first.startswith("ACTION:") and "NONE" not in first:
                verb = "ACTED"
            elif report:
                verb = "diagnosed"
            else:
                verb = "detected"
            with open(page, "a") as f:
                if new:
                    f.write("# Sentinel incidents — %s\n\nAppended by the watcher, newest last. No-login view: https://digest.rosemaryacres.com/sentinel/\n" % month)
                f.write("\n## %s `%s` — %s (%s)\n%s\n" % (
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)), cid, verb, sev, detail))
                if report:
                    f.write("```\n%s\n```\n" % report)
        except OSError:
            pass


    def render_page():
        # Rebuild the browsable incident log served at digest.rosemaryacres.com/sentinel.
        web = "/var/lib/sentinel/web"
        try:
            os.makedirs(web, exist_ok=True)
            names = [n for n in os.listdir(INCIDENT_DIR) if n.endswith(".txt")]
        except OSError:
            return

        def ts_of(n):
            try:
                return int(n.rsplit("-", 1)[1][:-4])
            except (IndexError, ValueError):
                return 0
        names.sort(key=ts_of, reverse=True)

        cards = []
        for n in names[:50]:
            cid = n.rsplit("-", 1)[0]
            ts = ts_of(n)
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"
            try:
                with open(os.path.join(INCIDENT_DIR, n)) as f:
                    content = f.read()
            except OSError:
                continue
            detail = (content.splitlines() or [""])[0]
            report = content.split("=== agent report ===", 1)[1].strip() if "=== agent report ===" in content else ""
            first = (report.splitlines() or [""])[0].strip().upper() if report else ""
            if first.startswith("ACTION:") and "NONE" not in first:
                badge = '<span class="b acted">ACTED</span>'
            elif report:
                badge = '<span class="b diag">diagnosed</span>'
            else:
                badge = '<span class="b det">detected</span>'
            cards.append(
                '<div class="inc"><div class="t">%s</div><strong>%s</strong>%s<div>%s</div><pre>%s</pre></div>'
                % (when, html.escape(cid), badge, html.escape(detail), html.escape(report or "(detection only — no agent run)"))
            )

        page = (
            '<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>gromit-sentinel log</title><style>%s</style></head><body>'
            '<h1>gromit-sentinel &mdash; incident log</h1>'
            '<p class="t">What the watchdog detected, diagnosed, and did. Newest first; last 50.</p>'
            '%s<hr><p class="t">Generated %s.</p></body></html>'
            % (PAGE_STYLE, "".join(cards) or "<p>No incidents recorded yet.</p>", time.strftime("%Y-%m-%d %H:%M %Z"))
        )
        tmp = os.path.join(web, "index.html.tmp")
        try:
            with open(tmp, "w") as f:
                f.write(page)
            os.replace(tmp, os.path.join(web, "index.html"))
            os.chmod(os.path.join(web, "index.html"), 0o644)
        except OSError:
            pass


    def main():
        cfg = load_json(CONFIG, {})
        if not cfg.get("enabled", True):
            return
        now = time.time()
        debounce = int(cfg.get("debounce", 2))
        cooldown = int(cfg.get("cooldownSec", 7200))
        max_hour = int(cfg.get("maxPerHour", 6))
        max_day = int(cfg.get("maxPerDay", 30))
        max_actions = int(cfg.get("maxActionsPerDay", 5))
        act_cooldown = int(cfg.get("actionCooldownSec", 86400))

        state = load_json(STATE, {"checks": {}, "escalations": [], "actions": []})
        cstate = state.setdefault("checks", {})
        state["escalations"] = [t for t in state.get("escalations", []) if now - t < 86400]
        state["actions"] = [t for t in state.get("actions", []) if now - t < 86400]
        esc_hour = sum(1 for t in state["escalations"] if now - t < 3600)
        esc_day = len(state["escalations"])
        acts_today = len(state["actions"])

        os.makedirs(INCIDENT_DIR, exist_ok=True)

        for c in cfg.get("checks", []):
            if not c.get("enabled", True):
                continue
            fn = DISPATCH.get(c.get("type"))
            if fn is None:
                continue
            cid = c["id"]
            st = cstate.setdefault(cid, {"consecutive": 0, "active": False, "last_escalated": 0})
            try:
                fired, detail = fn(c)
            except Exception as e:
                print("check %s errored: %r" % (cid, e), file=sys.stderr)
                continue

            if not fired:
                if st.get("active"):
                    st["active"] = False
                    ntfy(cfg, "Sentinel resolved: %s" % cid, "%s cleared." % cid, 2, "white_check_mark")
                st["consecutive"] = 0
                continue

            st["consecutive"] = st.get("consecutive", 0) + 1
            if st.get("active"):
                continue
            if st["consecutive"] < int(c.get("minConsecutive", debounce)):
                continue
            if now - st.get("last_escalated", 0) < int(c.get("cooldownSec", cooldown)):
                continue
            if esc_hour >= max_hour or esc_day >= max_day:
                print("rate-limited, skipping %s" % cid, file=sys.stderr)
                continue

            # ── ESCALATE ──
            evidence = gather_evidence(c, detail)
            ts = int(now)
            path = os.path.join(INCIDENT_DIR, "%s-%d.txt" % (cid, ts))
            try:
                with open(path, "w") as f:
                    f.write(evidence)
            except OSError:
                path = "(could not write evidence file)"
            sev = c.get("severity", "warning")
            # 1) Immediate detection notice — don't make Chris wait on the agent.
            #    Skipped when notifyDetection=false (e.g. checks Grafana already
            #    alerts on) so we don't double-ping; the diagnosis/action still sends.
            if c.get("notifyDetection", True):
                ntfy(cfg, "Sentinel: %s" % cid,
                     "%s\n\nEvidence: %s" % (detail, path),
                     PRI.get(sev, 3), TAG.get(sev, "warning"))
            # 2) Hand off to `claude -p`. Phase 3: an act-flagged check MAY take
            #    one bounded action when permitted; otherwise it diagnoses only.
            report = ""
            if cfg.get("agentEnabled", True) and c.get("agent", False):
                recently_acted = (now - st.get("last_action", 0)) < act_cooldown
                act_permitted = (cfg.get("actEnabled", True) and c.get("act", False)
                                 and acts_today < max_actions and not recently_acted)
                diag = run_agent(cid, c, detail, path, cfg, act_permitted, recently_acted)
                report = diag
                if diag:
                    try:
                        with open(path, "a") as f:
                            f.write("\n\n=== agent report ===\n" + diag)
                    except OSError:
                        pass
                    first = (diag.splitlines() or [""])[0].strip().upper()
                    acted = first.startswith("ACTION:") and "NONE" not in first
                    if acted:
                        st["last_action"] = now
                        state["actions"].append(now)
                        acts_today += 1
                    ntfy(cfg, "Sentinel: %s (%s)" % (cid, "acted" if acted else "diagnosed"),
                         diag[:1200], PRI.get(sev, 3), "wrench" if acted else "robot")
                else:
                    ntfy(cfg, "Sentinel: %s (agent unavailable)" % cid,
                         "claude -p produced no output or timed out; evidence at %s" % path, 3, "warning")
            append_space_log(cid, ts, c.get("severity", "warning"), detail, report)
            st["active"] = True
            st["last_escalated"] = now
            state["escalations"].append(now)
            esc_hour += 1
            esc_day += 1

        save_json(STATE, state)
        render_page()

        # Evidence-txt retention: the durable record now lives on the space
        # pages, so prune raw incident files after 30 days (the weekly digest
        # only reads the last 7).
        try:
            for n in os.listdir(INCIDENT_DIR):
                if n.endswith(".txt"):
                    p = os.path.join(INCIDENT_DIR, n)
                    if now - os.path.getmtime(p) > 30 * 86400:
                        os.remove(p)
        except OSError:
            pass


    if __name__ == "__main__":
        main()
  '';
in
{
  environment.etc."sentinel/config.json".text = builtins.toJSON sentinelConfig;

  # Serve the incident log at digest.rosemaryacres.com/sentinel — merges into the
  # digest vhost (defined in modules/agent/digest.nix); inherits its TLS + source-gate.
  services.nginx.virtualHosts."digest.rosemaryacres.com".locations = {
    "= /sentinel".extraConfig = "return 301 /sentinel/;";
    "/sentinel/" = {
      alias = "/var/lib/sentinel/web/";
      extraConfig = "index index.html;";
    };
  };

  # Prebaked instructions handed to `claude -p` on an agent-flagged incident.
  # Phase 3 = DIAGNOSE, then ACT within strict bounds (or escalate).
  environment.etc."sentinel/playbook.md".text = ''
    # gromit-sentinel incident playbook — Phase 3: DIAGNOSE, then ACT (bounded)

    You are the autonomous incident handler for Gromit (a NixOS homelab). The
    sentinel detected a problem and handed it to you. Diagnose it. Then — ONLY if
    the prompt says "ACTING IS PERMITTED" and a safe, bounded fix clearly applies
    — take ONE corrective action. Otherwise diagnose and escalate to Chris.

    ## The ONLY actions you may take (you have no other powers; the OS enforces it)
    1. Restart a WHITELISTED service via scoped sudo — ONLY one of:
         sudo systemctl restart vaultwarden
         sudo systemctl restart media-mirror-sync   (or: start media-mirror-sync)
         sudo systemctl reset-failed <unit>          (clear a failed state)
       No other unit is restartable — sudo will DENY anything else; do not try.
    2. Open a fix PR with the ww4-bot Forgejo API (token at
       ~/.config/ww4-bot/forgejo-token.env): branch -> push -> open PR -> request
       `chris` as reviewer. Title it with a `[sentinel]` prefix; in the body give
       the incident, your diagnosis, and the fix. NEVER merge a flakes PR — Chris
       gates every one.

    ## HARD RULES — do not break these (they are also enforced by guards)
    - NEVER merge a flakes PR, push to `main`, restart a non-whitelisted unit, run
      nixos-rebuild, `rm`, or edit files outside a PR branch.
    - ACT AT MOST ONCE. If the prompt says acting is NOT permitted — because you
      recently acted on this and it recurred, a daily cap is hit, or it's a
      diagnose-only check — then DO NOT act: diagnose and escalate.
    - If the fix is risky, non-trivial, or you are not confident, DIAGNOSE ONLY
      and recommend the action for Chris — do not perform it. When in doubt, escalate.
    - If the incident detail contains a DRILL instruction, do EXACTLY that and
      nothing else.
    - You are on a timeout; be efficient.

    ## Context
    Your homelab memory auto-loads (open-loops, gromit-access, comin-deploy-
    validation, …) — use it. The prompt gives the incident + an evidence-file
    path; READ THE EVIDENCE FIRST, then investigate read-only as needed.

    ## Your reply (sent verbatim as a phone notification — be terse)
    The FIRST LINE MUST be EXACTLY one of:
      ACTION: none
      ACTION: restarted <unit>
      ACTION: reset-failed <unit>
      ACTION: opened PR <number-or-url>
    Then AT MOST ~6 short lines, no markdown headers, no preamble:
      TL;DR — what is wrong (one line)
      Cause — your root-cause read
      Did / Recommend — what you did, or what Chris should do
      Confidence — high / medium / low
  '';

  systemd.services.gromit-sentinel = {
    description = "gromit-sentinel watchdog (Phase 3: detect + claude diagnose/act + notify)";
    # Runs as the claude user with the same headless-claude env as the weekly
    # digest, so an agent-flagged incident can invoke `claude -p` (subscription
    # OAuth, memory auto-loads from the working dir). systemd-journal group gives
    # read access for evidence gathering.
    serviceConfig = {
      Type = "oneshot";
      User = "claude";
      # Writes incident pages into the SilverBullet space — files must be born
      # group-writable or the ACL mask locks the web UI out (silverbullet.nix).
      UMask = "0002";
      SupplementaryGroups = [ "systemd-journal" ];
      StateDirectory = "sentinel";
      RuntimeDirectory = "sentinel";
      RuntimeDirectoryMode = "0775";   # so `sudo touch /run/sentinel/fire-test` works for testing
      RuntimeDirectoryPreserve = true; # keep /run/sentinel across the oneshot runs
      WorkingDirectory = "/home/claude/nixos-homelab-improvements";
      # Raw Environment= (not the NixOS `environment` option, which would collide
      # with the `path`-derived default PATH). Everything the watcher shells out
      # to (systemctl, journalctl, claude) must be on this PATH. Mirrors digest.nix.
      Environment = [
        "HOME=/home/claude"
        # /run/wrappers/bin MUST come first: it holds the setuid `sudo`. Without
        # it, `sudo` resolves to the plain (non-setuid) copy in
        # /run/current-system/sw/bin and every scoped `sudo systemctl
        # reset-failed …` action dies with "sudo must be owned by uid 0 and have
        # the setuid bit set" — which silently blocked sentinel's auto-resets
        # (seen 07-22 and 07-24 2026).
        "PATH=/run/wrappers/bin:/etc/profiles/per-user/claude/bin:/run/current-system/sw/bin:/usr/bin:/bin"
        "CLAUDE_AUTONOMOUS=1"   # the reflection Stop-hook no-ops in headless runs
      ];
      ExecStart = "${watcher}/bin/gromit-sentinel";
    };
  };

  systemd.timers.gromit-sentinel = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "3min";
      OnUnitActiveSec = "${toString sentinelConfig.pollSec}s";
      Persistent = true;
    };
  };

  # Watchdog for the watchdog: an INDEPENDENT timer (runs as root, not coupled to
  # the sentinel) that alerts if the sentinel stops running — its state.json is
  # rewritten every run, so a stale mtime means the watcher is dead. Posts to ntfy
  # directly (no dependency on the sentinel being alive).
  systemd.services.sentinel-watchdog = {
    description = "Alert if gromit-sentinel has stopped running";
    path = [ pkgs.curl pkgs.coreutils ];
    serviceConfig.Type = "oneshot";
    script = ''
      f=/var/lib/sentinel/state.json
      [ -e "$f" ] || exit 0           # sentinel hasn't run yet — nothing to check
      age=$(( $(date +%s) - $(stat -c %Y "$f" 2>/dev/null || echo 0) ))
      if [ "$age" -gt 900 ]; then
        curl -s --max-time 10 \
          -H "Title: Sentinel STALLED" -H "Priority: 4" -H "Tags: warning" \
          -d "gromit-sentinel has not run for $((age / 60)) min (state.json stale). Check: systemctl status gromit-sentinel.timer gromit-sentinel.service" \
          http://127.0.0.1:8090/gromit-alerts >/dev/null || true
      fi
    '';
  };
  systemd.timers.sentinel-watchdog = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "15min";
      OnUnitActiveSec = "10min";
      Persistent = true;
    };
  };
}
