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
  # ─────────────────────────── EDIT ME ───────────────────────────
  # The watcher config. `checks` is a list; each has an id, a type, and
  # type-specific fields. Common optional fields: enabled (default true),
  # severity (test|info|warning|critical), minConsecutive (debounce override),
  # cooldownSec (re-alert suppression override).
  sentinelConfig = {
    enabled = true;
    pollSec = 120;          # informational; the systemd timer drives the cadence
    debounce = 2;           # a condition must persist this many checks before it escalates
    cooldownSec = 7200;     # don't re-escalate the same incident within 2 h
    maxPerHour = 6;         # global rate limits (cost + anti-storm guard)
    maxPerDay = 30;
    ntfyServer = "http://127.0.0.1:8090";
    ntfyTopic = "gromit-alerts";

    checks = [
      # Any systemd unit in the failed state (excluding known-noisy ones).
      { id = "failed-units"; type = "failed-units"; severity = "warning"; exclude = [ ]; }

      # comin couldn't build/eval/deploy/fetch — gromit silently stuck on the old gen.
      { id = "comin-deploy"; type = "comin"; severity = "warning"; }

      # Example metric check (commented — enable + tune when wanted):
      # { id = "rootfs-full"; type = "metric"; severity = "warning";
      #   expr = "100 - (node_filesystem_avail_bytes{mountpoint=\"/\"} * 100 / node_filesystem_size_bytes{mountpoint=\"/\"})";
      #   op = ">"; threshold = 90; }

      # Built-in self-test: fires immediately when the marker file exists, then
      # clears it. This is the "trigger on demand" hook for testing the pipeline.
      { id = "selftest"; type = "marker"; path = "/run/sentinel/fire-test";
        minConsecutive = 1; clearAfter = true; severity = "test";
        message = "Synthetic sentinel self-test — pipeline is working, nothing is wrong."; }
    ];
  };
  # ─────────────────────────────────────────────────────────────────

  watcher = pkgs.writers.writePython3Bin "gromit-sentinel" {
    flakeIgnore = [ "E501" "W503" "W504" ];
  } ''
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


    def check_failed_units(c):
        exclude = set(c.get("exclude", []))
        r = sh(["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"])
        lines = r.stdout.splitlines() if (r and r.stdout) else []
        units = [ln.split()[0] for ln in lines if ln.split()]
        units = [u for u in units if u not in exclude]
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
                tag = m.get("device") or m.get("instance") or m.get("__name__") or ""
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
        req = urllib.request.Request(
            "%s/%s" % (server, topic), data=body.encode(),
            headers={"Title": title, "Priority": str(priority), "Tags": tags})
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print("ntfy post failed: %r" % e, file=sys.stderr)


    PRI = {"test": 2, "info": 2, "warning": 3, "critical": 4}
    TAG = {"test": "test_tube", "info": "information_source", "warning": "warning", "critical": "rotating_light"}


    def main():
        cfg = load_json(CONFIG, {})
        if not cfg.get("enabled", True):
            return
        now = time.time()
        debounce = int(cfg.get("debounce", 2))
        cooldown = int(cfg.get("cooldownSec", 7200))
        max_hour = int(cfg.get("maxPerHour", 6))
        max_day = int(cfg.get("maxPerDay", 30))

        state = load_json(STATE, {"checks": {}, "escalations": []})
        cstate = state.setdefault("checks", {})
        state["escalations"] = [t for t in state.get("escalations", []) if now - t < 86400]
        esc_hour = sum(1 for t in state["escalations"] if now - t < 3600)
        esc_day = len(state["escalations"])

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
                    ntfy(cfg, "✅ Sentinel resolved: %s" % cid, "%s cleared." % cid, 2, "white_check_mark")
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

            # ── Phase 2/3 HAND-OFF POINT ──
            # Today: gather evidence + notify. Later: pass the evidence bundle +
            # the prebaked playbook to `claude -p` here for diagnosis/action.
            evidence = gather_evidence(c, detail)
            ts = int(now)
            path = os.path.join(INCIDENT_DIR, "%s-%d.txt" % (cid, ts))
            try:
                with open(path, "w") as f:
                    f.write(evidence)
            except OSError:
                path = "(could not write evidence file)"
            sev = c.get("severity", "warning")
            body = "%s\n\nEvidence: %s\n\nPhase 1: detection only — no auto-action taken." % (detail, path)
            ntfy(cfg, "🔍 Sentinel: %s" % cid, body, PRI.get(sev, 3), TAG.get(sev, "warning"))
            st["active"] = True
            st["last_escalated"] = now
            state["escalations"].append(now)
            esc_hour += 1
            esc_day += 1

        save_json(STATE, state)


    if __name__ == "__main__":
        main()
  '';
in
{
  environment.etc."sentinel/config.json".text = builtins.toJSON sentinelConfig;

  systemd.services.gromit-sentinel = {
    description = "gromit-sentinel watchdog (Phase 1: detect + notify)";
    # Read access to the journal for evidence gathering; runs as the claude user
    # so Phase 2/3 can invoke `claude -p` with its creds/scope without a re-home.
    serviceConfig = {
      Type = "oneshot";
      User = "claude";
      SupplementaryGroups = [ "systemd-journal" ];
      StateDirectory = "sentinel";
      RuntimeDirectory = "sentinel";
      RuntimeDirectoryMode = "0775";   # so `sudo touch /run/sentinel/fire-test` works for testing
      RuntimeDirectoryPreserve = true; # keep /run/sentinel across the oneshot runs
      ExecStart = "${watcher}/bin/gromit-sentinel";
    };
    path = [ pkgs.systemd ];
  };

  systemd.timers.gromit-sentinel = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "3min";
      OnUnitActiveSec = "${toString sentinelConfig.pollSec}s";
      Persistent = true;
    };
  };
}
